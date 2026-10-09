"""API Key 服务：作用域、权限与"不泄露"的判定（M4 T4.4）。

镜像同构：``app/services/api_key.py`` → ``tests/unit/services/test_api_key.py``。

这份用例只钉**安全性质**，不测实现细节：

1. 明文不入库、响应不回声（凭据类数据的底线）；
2. 只读密钥不能写（权限档位真的生效）；
3. 受限密钥不能碰范围外的库（作用域真的生效）；
4. 无效凭据的报错**不区分原因**（不给攻击者布尔预言机）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import API_KEY_PREFIX, hash_token
from app.models.enums import ApiKeyPermission, SharePermission
from app.services.api_key import READ, WRITE, ApiKeyService
from app.storage.base import KnowledgeBaseRecord, ShareRecord, UserRecord


@pytest.fixture
def service(bundle) -> ApiKeyService:  # type: ignore[no-untyped-def]
    return ApiKeyService(bundle)


def _issue(service: ApiKeyService, **kwargs):  # type: ignore[no-untyped-def]
    defaults = {
        "name": "测试密钥",
        "permission": ApiKeyPermission.READONLY,
        "knowledge_base_ids": [],
    }
    defaults.update(kwargs)
    return service.create(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------- 不泄露


def test_plaintext_never_reaches_storage(service, bundle) -> None:
    """库里只应有摘要：明文一旦落盘，读一次数据库就等于拿到全部钥匙。"""
    issued = _issue(service)

    by_hash = bundle.meta.get_api_key_by_hash(hash_token(issued.token))
    assert by_hash is not None, "哈希查不到，说明存进去的不是 token 的摘要"

    stored = bundle.meta.list_api_keys()
    assert len(stored) == 1
    assert stored[0].key_hash != issued.token, "存了明文"
    assert stored[0].key_hash == hash_token(issued.token)

    # 前缀是刻意留的展示信息，但不能长到能拼回密钥
    assert stored[0].key_prefix
    assert stored[0].key_prefix != issued.token
    assert issued.token not in stored[0].key_hash


def test_token_has_identifiable_prefix(service) -> None:
    """带前缀是为了能被密钥扫描工具识别，也为了用户能分辨是哪把。"""
    issued = _issue(service)
    assert issued.token.startswith(API_KEY_PREFIX)
    assert len(issued.token) > len(API_KEY_PREFIX) + 20


def test_two_tokens_differ(service) -> None:
    """随机性最低要求：连着发两把不能一样。"""
    assert _issue(service).token != _issue(service).token


# --------------------------------------------------------------------- 认证


def test_authenticate_accepts_a_valid_token(service) -> None:
    issued = _issue(service)
    caller = service.authenticate(issued.token)
    assert caller.api_key is not None
    assert caller.api_key.id == issued.record.id
    assert not caller.is_admin


def test_authenticate_rejects_revoked_token(service) -> None:
    issued = _issue(service)
    service.revoke(issued.record.id)
    with pytest.raises(UnauthorizedError):
        service.authenticate(issued.token)


@pytest.mark.parametrize("bad", ["", "   ", "kylab_sk_不存在", "随便一串"])
def test_authenticate_rejects_garbage(service, bad: str) -> None:
    with pytest.raises(UnauthorizedError):
        service.authenticate(bad)


def test_failure_messages_do_not_reveal_which_part_was_wrong(service) -> None:
    """无效凭据的文案必须一致。

    若"格式不对"与"库里有这条但已撤销"给出不同提示，攻击者就能拿它当预言机，
    逐个确认哪些 key 曾经存在过。
    """
    _issue(service)
    messages = set()
    for bad in ["kylab_sk_格式不对", "kylab_sk_这条不存在"]:
        with pytest.raises(UnauthorizedError) as excinfo:
            service.authenticate(bad)
        messages.add(str(excinfo.value))

    assert len(messages) == 1, f"不同失败原因给出了不同文案：{messages}"


def test_authenticate_records_last_used(service, bundle) -> None:
    issued = _issue(service)
    assert bundle.meta.list_api_keys()[0].last_used_at is None
    service.authenticate(issued.token)
    assert bundle.meta.list_api_keys()[0].last_used_at is not None


# --------------------------------------------------------------------- 权限


def test_readonly_key_cannot_write(service) -> None:
    caller = service.authenticate(_issue(service, permission=ApiKeyPermission.READONLY).token)
    service.check_access(caller, need=READ)  # 读放行
    with pytest.raises(ForbiddenError):
        service.check_access(caller, need=WRITE)


def test_readwrite_key_can_write(service) -> None:
    caller = service.authenticate(_issue(service, permission=ApiKeyPermission.READWRITE).token)
    service.check_access(caller, need=WRITE)


# --------------------------------------------------------------------- 作用域


def test_scoped_key_can_reach_its_own_kb(service) -> None:
    issued = _issue(service, knowledge_base_ids=["kb_a", "kb_b"])
    caller = service.authenticate(issued.token)
    service.check_access(caller, kb_ids=["kb_a"])
    service.check_access(caller, kb_ids=["kb_a", "kb_b"])


def test_scoped_key_is_blocked_outside_its_scope(service) -> None:
    """越界访问必须被拦——这是"绑定知识库范围"的全部意义。"""
    caller = service.authenticate(_issue(service, knowledge_base_ids=["kb_a"]).token)

    with pytest.raises(ForbiddenError) as excinfo:
        service.check_access(caller, kb_ids=["kb_secret"])

    # 报错要点出是哪个库：用户配错范围得能自己看出来
    assert "kb_secret" in str(excinfo.value)


def test_partially_out_of_scope_is_rejected(service) -> None:
    """一次请求里混进一个越界库也要整体拒绝，不能"部分放行"。"""
    caller = service.authenticate(_issue(service, knowledge_base_ids=["kb_a"]).token)
    with pytest.raises(ForbiddenError):
        service.check_access(caller, kb_ids=["kb_a", "kb_b"])


def test_unscoped_key_reaches_everything(service) -> None:
    """空范围 = 不限范围（与"范围为空所以什么都看不了"是两回事）。"""
    caller = service.authenticate(_issue(service, knowledge_base_ids=[]).token)
    service.check_access(caller, kb_ids=["kb_任意"])


def test_visible_kb_ids_filters_console_and_scope(service) -> None:
    from app.services.api_key import Caller

    console = Caller(is_admin=True)
    assert service.visible_kb_ids(console) is None, "控制台不受限"

    scoped = service.authenticate(_issue(service, knowledge_base_ids=["kb_a"]).token)
    assert service.visible_kb_ids(scoped) == ["kb_a"]

    unscoped = service.authenticate(_issue(service).token)
    assert service.visible_kb_ids(unscoped) is None, "空范围 = 不限制"


def test_console_bypasses_scope_checks(service) -> None:
    from app.services.api_key import Caller

    console = Caller(is_admin=True)
    service.check_access(console, need=WRITE, kb_ids=["kb_任意"])


# --------------------------------------------------------------------- 成员会话（v10）


def _member(service: ApiKeyService, user_id: str = "user_m"):  # type: ignore[no-untyped-def]
    """构造一个普通成员的调用主体（登录会话通道）。"""
    from app.services.api_key import Caller
    from app.storage.base import UserRecord

    return Caller(user=UserRecord(id=user_id, name="成员", username="m"), session_id="s_m")


def test_member_sees_only_owned_kbs(service, store) -> None:  # type: ignore[no-untyped-def]
    """成员可见范围 = 自己拥有的库；**绝不能回 None**（那是不受限的意思）。

    这条与 check_access 的成员分支必须一致：一个放行一个拦，就是越权洞。
    """
    from app.storage.base import KnowledgeBaseRecord

    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_mine", name="我的", embedding_model_id="m",
                            embedding_dim=768, owner_id="user_m")
    )
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_yours", name="别人的", embedding_model_id="m",
                            embedding_dim=768, owner_id="user_o")
    )

    caller = _member(service)
    assert service.visible_kb_ids(caller) == ["kb_mine"]
    service.check_access(caller, kb_ids=["kb_mine"])
    with pytest.raises(ForbiddenError, match="kb_yours"):
        service.check_access(caller, kb_ids=["kb_yours"])
    # 不涉及具体库的调用（如列任务）不受阻
    service.check_access(caller, kb_ids=None)


def test_member_cannot_touch_admin_only_endpoints(service) -> None:  # type: ignore[no-untyped-def]
    """成员会话 is_admin=False：require_admin 那层（设置页/密钥管理）进不去。"""
    assert _member(service).is_admin is False


def test_readonly_share_cannot_write(service, store) -> None:  # type: ignore[no-untyped-def]
    """只读档分享在 need=WRITE 时必须被拒——这是'分享只读不破写'的唯一闸门。

    端点侧的上传 403 由集成测试钉住；这里钉判定本身：哪天某个写端点漏接
    ``need=WRITE``，这条与该端点的集成用例会一起红。
    """
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_shared", name="分享来的", embedding_model_id="m",
                            embedding_dim=768, owner_id="user_o")
    )
    store.create_user(UserRecord(id="user_m2", name="成员", username="m2"))
    store.put_share(
        ShareRecord(kb_id="kb_shared", user_id="user_m2", permission=SharePermission.READ)
    )

    caller = _member(service, "user_m2")
    # 读放行
    service.check_access(caller, need=READ, kb_ids=["kb_shared"])
    assert service.visible_kb_ids(caller) == ["kb_shared"]
    # 写拒绝，且文案点明是"只读分享"
    with pytest.raises(ForbiddenError, match="只读"):
        service.check_access(caller, need=WRITE, kb_ids=["kb_shared"])

    # 升档为 write 后放行
    store.put_share(
        ShareRecord(kb_id="kb_shared", user_id="user_m2", permission=SharePermission.WRITE)
    )
    service.check_access(caller, need=WRITE, kb_ids=["kb_shared"])
