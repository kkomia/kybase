"""账号引导与登录会话（v10）。

镜像同构：``app/services/auth.py`` → 本文件。

要钉住的核心承诺：
- setup 是一次性的，且要**认领无主老数据**（不然升级后管理员自己也看不到旧库）；
- 登录失败对外只有一句话（不区分"用户不存在"与"口令不对"）；
- 连续失败会限流；
- 会话滑动续期、改密吊销其他会话、禁用/删除账号即会话作废。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import ConflictError, InvalidRequestError, UnauthorizedError
from app.core.security import hash_token
from app.core.storage import build_stores, reset_stores
from app.models.enums import UserRole
from app.services.auth import URL_SIGNING_SECRET_SETTING, AuthService, ensure_url_signing_secret
from app.storage.base import ConversationRecord, KnowledgeBaseRecord, StoreBundle, UserRecord


@pytest.fixture
def auth(bundle) -> AuthService:  # type: ignore[no-untyped-def]
    return AuthService(bundle)


def _setup(auth: AuthService, **overrides):  # type: ignore[no-untyped-def]
    payload = {"username": "admin", "password": "correct horse battery"}
    payload.update(overrides)
    return auth.setup(**payload)


# --------------------------------------------------------------------- 首次初始化


def test_setup_creates_admin_and_returns_working_session(auth: AuthService) -> None:
    result = _setup(auth, username="Admin")

    assert result.user.role is UserRole.ADMIN
    # 登录名统一小写归一化：Admin 与 admin 是同一个人
    assert result.user.username == "admin"
    user, _session = auth.authenticate_session(result.token)
    assert user.id == result.user.id


def test_setup_is_one_shot(auth: AuthService) -> None:
    _setup(auth)

    with pytest.raises(ConflictError, match="已关闭"):
        _setup(auth, username="second")


def test_setup_claims_legacy_data(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """无主老数据（v10 之前）必须归首个管理员——不认领等于升级后数据"消失"。"""
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_old", name="老库", embedding_model_id="m", embedding_dim=768)
    )
    store.create_conversation(ConversationRecord(id="conv_old"))

    result = _setup(auth)

    assert store.get_knowledge_base("kb_old").owner_id == result.user.id  # type: ignore[union-attr]
    assert store.get_conversation("conv_old").owner_id == result.user.id  # type: ignore[union-attr]


def test_setup_generates_signing_secret(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """下载签名密钥独立落库，不再依赖控制台令牌兜底。"""
    _setup(auth)

    assert store.get_setting(URL_SIGNING_SECRET_SETTING)


def test_setup_rejects_short_password(auth: AuthService) -> None:
    with pytest.raises(InvalidRequestError, match="至少"):
        _setup(auth, password="short")
    # 没建成：入口必须还开着（校验失败不该把一次性入口消耗掉）
    assert not auth.has_accounts()


# --------------------------------------------------------------------- 登录


def test_login_success_and_username_is_case_insensitive(auth: AuthService) -> None:
    _setup(auth, username="Admin")

    result = auth.login(username=" ADMIN ", password="correct horse battery")

    assert result.user.username == "admin"


def test_login_failure_message_is_uniform(auth: AuthService) -> None:
    """ "用户不存在"与"口令不对"对外是同一句——区分开就是用户枚举预言机。"""
    _setup(auth)

    with pytest.raises(UnauthorizedError, match="用户名或密码不正确"):
        auth.login(username="admin", password="wrong-password")
    with pytest.raises(UnauthorizedError, match="用户名或密码不正确"):
        auth.login(username="ghost", password="wrong-password")


def test_roster_entry_cannot_log_in(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """纯名册条目（无 username/口令）不是账号。"""
    store.create_user(UserRecord(id="user_r", name="仅名册"))

    with pytest.raises(UnauthorizedError, match="用户名或密码不正确"):
        auth.login(username="仅名册", password="whatever-123")


def test_login_lockout_after_repeated_failures(auth: AuthService) -> None:
    _setup(auth)
    for _ in range(5):
        with pytest.raises(UnauthorizedError):
            auth.login(username="admin", password="wrong-password")

    # 锁定期间：即使口令对了也不放行
    with pytest.raises(UnauthorizedError, match="尝试次数过多"):
        auth.login(username="admin", password="correct horse battery")


def test_successful_login_clears_failure_count(auth: AuthService) -> None:
    _setup(auth)
    for _ in range(4):
        with pytest.raises(UnauthorizedError):
            auth.login(username="admin", password="wrong-password")
    auth.login(username="admin", password="correct horse battery")

    # 成功之后计数清零：再错 4 次不应触发锁定（阈值是 5）
    for _ in range(4):
        with pytest.raises(UnauthorizedError, match="用户名或密码不正确"):
            auth.login(username="admin", password="wrong-password")
    auth.login(username="admin", password="correct horse battery")


# --------------------------------------------------------------------- 会话


def test_session_sliding_renewal(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """每次用到都把过期时间推后：天天用的用户不该被硬性过期踢出去。"""
    result = _setup(auth)
    # 距上次活动超过一小时才会真的写库续期（避免每请求一次 UPDATE）；
    # 把 last_seen 改到两小时前，模拟"隔了一阵又回来用"的用户
    session_id = hash_token(result.token)
    with store._db.session() as conn:  # 测试需要直接操纵时间（仓储层不接受非法状态）
        conn.execute(
            "update sessions set last_seen_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(hours=2), session_id),
        )
    before = store.get_session(session_id)

    user, session = auth.authenticate_session(result.token)

    after = store.get_session(session.id)
    assert user.id == result.user.id
    assert after is not None and before is not None
    assert after.expires_at > before.expires_at  # type: ignore[operator]


def test_session_touch_is_skipped_within_an_hour(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """刚活动过的会话不重复写库：续期语义不变，只是省掉无效 UPDATE。"""
    result = _setup(auth)
    before = store.get_session(hash_token(result.token))

    auth.authenticate_session(result.token)

    after = store.get_session(hash_token(result.token))
    assert after is not None and before is not None
    # 创建于同一秒内的会话不应被 touch（last_seen 原地不动）
    assert after.last_seen_at == before.last_seen_at


def test_expired_session_is_rejected(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    result = _setup(auth)
    # 时间旅行：仓储接口不接受"造一条已过期会话"（那是非法状态），
    # 直接 UPDATE 库里的过期时间。测的是"过期被拒"，不是"怎么造出来的"
    session_id = hash_token(result.token)
    with store._db.session() as conn:  # 测试需要直接操纵时间（仓储层不接受非法状态）
        conn.execute(
            "update sessions set expires_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(seconds=1), session_id),
        )

    with pytest.raises(UnauthorizedError, match="重新登录"):
        auth.authenticate_session(result.token)


def test_logout_revokes_session(auth: AuthService) -> None:
    result = _setup(auth)
    _, session = auth.authenticate_session(result.token)

    auth.logout(session.id)

    with pytest.raises(UnauthorizedError):
        auth.authenticate_session(result.token)


def test_disabled_account_kills_existing_session(auth: AuthService, store) -> None:  # type: ignore[no-untyped-def]
    """禁用账号后，既有会话必须立刻失效——不能等它自然过期。"""
    result = _setup(auth)
    store.set_user_disabled(result.user.id, True)

    with pytest.raises(UnauthorizedError, match="账号不可用"):
        auth.authenticate_session(result.token)


# --------------------------------------------------------------------- 改密


def test_change_password_revokes_other_sessions(auth: AuthService) -> None:
    first = _setup(auth)
    second = auth.login(username="admin", password="correct horse battery")
    _, first_session = auth.authenticate_session(first.token)

    revoked = auth.change_password(
        first.user,
        old_password="correct horse battery",
        new_password="new-horse-battery",
        keep_session_id=first_session.id,
    )

    assert revoked == 1
    # 当前会话保住，其他会话作废
    auth.authenticate_session(first.token)
    with pytest.raises(UnauthorizedError):
        auth.authenticate_session(second.token)
    # 旧口令不能再登录，新口令可以
    with pytest.raises(UnauthorizedError):
        auth.login(username="admin", password="correct horse battery")
    auth.login(username="admin", password="new-horse-battery")


def test_change_password_requires_correct_old_password(auth: AuthService) -> None:
    result = _setup(auth)

    with pytest.raises(UnauthorizedError, match="原密码不正确"):
        auth.change_password(
            result.user,
            old_password="wrong-old-password",
            new_password="new-horse-battery",
            keep_session_id="whatever",
        )


# --------------------------------------------------- 下载签名密钥（本机档那次 401 修的）
#
# 这一组**自带本机库**（带 `local` marker）：文件里其他用例跑在 PG 上，而"谁在什么时候
# 生成这条密钥"这件事两档不同——服务器档在 `setup` 里生成，本机档在组合根生成。


@pytest.fixture
def local_bundle(tmp_path: Path) -> Iterator[StoreBundle]:
    stores = build_stores(
        Settings(_env_file=None, deployment="local", data_dir=tmp_path / "data")  # type: ignore[call-arg]
    )
    yield stores
    reset_stores()


@pytest.mark.local
def test_ensure_url_signing_secret_generates_once_and_keeps_it(
    local_bundle: StoreBundle,
) -> None:
    """生成一次、之后**一直是同一把**（幂等）。

    换一把的后果不是"库里多了一条"，而是"所有已经发出去的签名链接一起失效"——
    凭据会轮换，而签出去的链接不该跟着死（这条键独立存在的整个理由）。
    """
    assert local_bundle.meta.get_setting(URL_SIGNING_SECRET_SETTING) is None

    first = ensure_url_signing_secret(local_bundle.meta)

    assert first and len(first) >= 32, "token_urlsafe(32) 那一档的随机串"
    assert local_bundle.meta.get_setting(URL_SIGNING_SECRET_SETTING) == first
    assert ensure_url_signing_secret(local_bundle.meta) == first, "第二次不许换一把"
    assert ensure_url_signing_secret(local_bundle.meta) == first


@pytest.mark.local
def test_the_step_setup_calls_goes_through_the_same_function(
    local_bundle: StoreBundle,
) -> None:
    """``setup`` 里那一步（``_ensure_signing_secret``）与组合根走**同一个函数**。

    为什么不在这里跑整条 ``setup``：那是**服务器档**的流程，它从头就要读 KB 域
    （账号与认领都在那一侧），而本机档那一半没有数据源（``RemoteMetaStore`` 没挂
    reader 就抛）——那也正是"本机档永远跑不到 setup"的原因。所以这一条只钉那个接缝：
    `setup` 调的那一步与组合根调的是同一段代码（两处各写一遍 token_urlsafe 就是
    两个"这把钥匙怎么来"的答案）。整条 ``setup`` 的判据在文件上面那些 PG 用例里。
    """
    auth = AuthService(local_bundle)
    assert local_bundle.meta.get_setting(URL_SIGNING_SECRET_SETTING) is None

    auth._ensure_signing_secret()
    generated = local_bundle.meta.get_setting(URL_SIGNING_SECRET_SETTING)

    assert generated and len(generated) >= 32
    auth._ensure_signing_secret()
    assert local_bundle.meta.get_setting(URL_SIGNING_SECRET_SETTING) == generated, (
        "同一条路再走一遍也不许换一把"
    )
