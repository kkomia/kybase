"""API Key 的发放、校验与作用域判定（《架构设计 v0.2》§3.2）。

架构给的约定只有两句：**绑定知识库范围**（可指定单个/多个库）**+ 只读 / 读写两种权限**。
本模块把这两句落成可执行的判断，其余（怎么放进 HTTP 请求、怎么回显）交给协议层。

**身份契约不在这里**（2026-10-08 剥离阶段 0 搬走）：`Caller` / `READ` / `WRITE` /
`LOCAL_CALLER` / `LOCAL_USER_ID` 住在 `app.core.caller`——它们是两侧共用的契约
（KB 侧判准入、Agent 侧拿它当类型并判 `WRITE`），与"发钥匙/校验"这套实现分开。
本模块从那里 import 进来并在 `__all__` 里再导出，**KB 侧调用点一行没动**。

三处刻意的设计：

1. **校验走"哈希查库"而不是"取全部再逐把比对"**。前者是 O(1) 索引查询，
   后者在密钥变多后既慢又会把全部摘要读进内存。

2. **作用域判定只有一个入口 ``check_access``**。权限与范围的组合判断散落到各个
   端点里，早晚会出现"某个端点忘了判"的洞——这类洞不会报错，只会静默放行。

3. **``使用时间`` 的更新是"尽力而为"**。它只用于界面展示，写失败不该让一次正常请求
   失败；所以这里吞掉异常并记日志，而不是让它冒到调用方。

**已知缺口（架构未要求，但要说清楚）**：API Key 目前**没有有效期**，
只能靠"撤销后重发"轮换。若要加过期，落点是 ``ApiKeyRecord`` 加一列 +
本模块 ``authenticate`` 里判一次——不需要动协议层。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.caller import LOCAL_CALLER, LOCAL_USER_ID, READ, WRITE, Caller
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import SESSION_TOKEN_PREFIX, display_prefix, generate_token, hash_token
from app.models.enums import ApiKeyPermission, SharePermission, UserRole
from app.storage.base import ApiKeyRecord, StoreBundle

if TYPE_CHECKING:  # 只为标注：core.services 会 import 本模块，顶层 import 就成了环
    from app.core.services import KbServices

__all__ = [
    "LOCAL_CALLER",
    "LOCAL_USER_ID",
    "READ",
    "WRITE",
    "ApiKeyService",
    "Caller",
    "IssuedApiKey",
    "resolve_caller",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """创建结果：``token`` 是**唯一一次**出现的明文。"""

    record: ApiKeyRecord
    token: str


def resolve_caller(services: KbServices, token: str) -> Caller:
    """把一串凭据换成调用主体（v0.12）。

    **REST 与 MCP 共用这一处**：两条协议层各写一份分流，迟早会漂
    （一处认会话令牌、另一处只认 API Key），而"同一串令牌在 A 入口是管理员、
    在 B 入口是匿名"正是最难发现的一类不一致。

    凭据按前缀分流——会话与 API Key 是两种东西，各走各的校验路径：

    - ``kylab_st_`` 开头 = 登录会话 → 身份来自会话里的账号，成员/管理员据此判定；
    - 其余 = API Key → 受限调用方，范围由密钥绑定决定。

    为什么要支持会话令牌：**归属**。API Key 通道没有账号，用它建的笔记
    ``user_id`` 只能是 NULL，而成员视角的列表按 ``user_id`` 过滤——
    于是"agent 替我把成果存进我的知识库"会存成一条用户自己看不见的笔记。
    """
    if token.startswith(SESSION_TOKEN_PREFIX):
        user, session = services.auth.authenticate_session(token)
        return Caller(
            is_admin=user.role is UserRole.ADMIN,
            user=user,
            session_id=session.id,
        )
    return services.api_keys.authenticate(token)


class ApiKeyService:
    """把 API Key 的存储细节收在一处。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 发放

    def create(
        self,
        *,
        name: str,
        permission: ApiKeyPermission,
        knowledge_base_ids: list[str] | None = None,
    ) -> IssuedApiKey:
        """发一把新钥匙。

        ``knowledge_base_ids`` 为空表示**该凭据可访问全部知识库**——
        与"绑定到空集合（什么都访问不了）"是两回事，所以这里显式区分：
        传 ``None``/空列表都按"不限范围"处理，并在返回的记录里保持空元组。
        """
        token = generate_token()
        record = ApiKeyRecord(
            id=f"key_{uuid.uuid4().hex[:12]}",
            name=name,
            key_hash=hash_token(token),
            permission=permission,
            knowledge_base_ids=tuple(knowledge_base_ids or ()),
            # 存一段前缀只为界面能分辨"哪把是哪把"；明文不入库
            key_prefix=display_prefix(token),
        )
        created = self._stores.meta.create_api_key(record)
        return IssuedApiKey(record=created, token=token)

    def list(self) -> list[ApiKeyRecord]:
        return self._stores.meta.list_api_keys()

    def revoke(self, key_id: str) -> None:
        self._stores.meta.delete_api_key(key_id)

    # ------------------------------------------------------------------ 校验

    def authenticate(self, token: str) -> Caller:
        """把明文凭据换成调用主体；无效即抛 401。

        错误文案**不区分**"前缀不对""长度不对""库里有没这条"——
        区分开等于给攻击者一个布尔预言机（"这个 key 存在但已撤销"是有用情报）。
        """
        if not token or not token.strip():
            raise UnauthorizedError("缺少 API Key：请在请求头带上 Authorization: Bearer <key>")

        record = self._stores.meta.get_api_key_by_hash(hash_token(token.strip()))
        if record is None:
            raise UnauthorizedError("API Key 无效或已被撤销")

        try:
            self._stores.meta.touch_api_key(record.id)
        except Exception:
            logger.warning("更新 API Key 使用时间失败（不影响本次调用）", exc_info=True)

        return Caller(api_key=record)

    def check_access(
        self,
        caller: Caller,
        *,
        need: ApiKeyPermission = READ,
        kb_ids: list[str] | None = None,
    ) -> None:
        """判定"这次调用能不能碰这些库"。不通过就抛 403。

        ``kb_ids`` 传 ``None`` 表示"不涉及具体知识库"（例如列全部任务），
        此时只判权限级别，不做范围收窄。

        越界时的报错**指出是哪个库**：用户配错了范围要能自己看出来，
        而库 ID 不是秘密（列表接口本来就能看到），提示它不额外泄露信息。
        """
        if caller.is_admin:
            return  # 管理员会话不受库范围限制

        if caller.user is not None:
            # 登录成员（v10）：范围 = 自己拥有的库 + 被分享的库。
            # 成员不是 API Key，没有 permission 档位——写操作要求"拥有"或"write 档分享"
            if kb_ids is None:
                return
            owned, shares = self._member_scope(caller.user.id)
            visible = owned | shares.keys()
            outside = [kb_id for kb_id in kb_ids if kb_id not in visible]
            if outside:
                raise ForbiddenError("你没有访问这些知识库的权限：" + "、".join(outside))
            if need is WRITE:
                readonly = [
                    kb_id
                    for kb_id in kb_ids
                    if kb_id not in owned and shares.get(kb_id) is SharePermission.READ
                ]
                if readonly:
                    raise ForbiddenError(
                        "这些知识库只以只读方式分享给你：" + "、".join(readonly)
                    )
            return

        permission = caller.permission
        if permission is None:
            # 构造上不该出现（Caller 只有管理员会话 / 成员会话 / API Key 三种形态）。
            # 真出现说明有人绕过了 authenticate，这时**拒绝**而不是放行——
            # 安全判定的默认值必须是"不通过"
            raise UnauthorizedError("调用主体缺少权限信息")

        if need is WRITE and permission is not ApiKeyPermission.READWRITE:
            raise ForbiddenError("该 API Key 只有只读权限，本次操作需要读写权限")

        scope = caller.knowledge_base_ids
        if not scope:
            return  # 空范围 = 不受限（见 create 的说明）

        if kb_ids is None:
            return

        outside = [kb_id for kb_id in kb_ids if kb_id not in scope]
        if outside:
            raise ForbiddenError(
                "该 API Key 未授权访问知识库：" + "、".join(outside) + "（请检查密钥的绑定范围）"
            )

    def visible_kb_ids(self, caller: Caller) -> list[str] | None:
        """列出调用方能看到的库；``None`` 表示不受限（调用方不必再过滤）。"""
        if caller.is_admin:
            return None
        if caller.user is not None:
            # 成员：自己的库 + 被分享的库。**不能回 None**——那是不受限的意思，
            # 与 check_access 的成员分支必须一致，两处矛盾就是越权洞
            owned, shares = self._member_scope(caller.user.id)
            return sorted(owned | shares.keys())
        scope = caller.knowledge_base_ids
        return list(scope) if scope else None

    def can_write(self, caller: Caller, kb_id: str) -> bool:
        """能否**写**这个库（不抛异常，供界面判定该不该显示上传/删除入口）。

        刻意直接复用 ``check_access``：写成第二套判定必然与它漂，
        而"看得见但写不动"的错判正是越权洞的形状。代价是每个库一次范围查询——
        个人规模（几十个库）可忽略，换来的是这条规则只有一处实现。
        """
        try:
            self.check_access(caller, need=WRITE, kb_ids=[kb_id])
        except (ForbiddenError, UnauthorizedError):
            return False
        return True

    def _member_scope(self, user_id: str) -> tuple[set[str], dict[str, SharePermission]]:
        """成员的可见范围：自己拥有的库 id 集合 + 被分享的库（kb_id → 档位）。"""
        owned = set(self._owned_kb_ids(user_id))
        shares = {
            share.kb_id: share.permission
            for share in self._stores.meta.list_shares_for_user(user_id)
        }
        return owned, shares

    def _owned_kb_ids(self, user_id: str) -> list[str]:
        return [
            kb.id for kb in self._stores.meta.list_knowledge_bases() if kb.owner_id == user_id
        ]
