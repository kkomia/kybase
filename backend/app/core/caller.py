"""请求身份契约：一次调用**是谁在调**（《架构设计 v0.2》§3.2）。

用法只有一处：`services/api_key.py` 的 `check_access` / `visible_kb_ids`
按它判准入；建库/上传/建钥匙的归属都从它取（`owner_id`）。

**为什么与实现分家**（2026-10-08 剥离阶段 0 的那一刀）：账号体系的实现（发钥匙、
校验、库范围判定）随 KB 走，而这份**契约**当时是两侧共用的——契约与实现同住
`services/api_key.py` 时，Agent 侧十几个模块 import 一个 KB 域模块，
在域间引用检查里全被算成越界（报告第 10.1 节）。搬到这里之后：

- 需要契约的模块从 `app.core.caller` 取；
- KB 侧调用点一行没动：`services/api_key.py` 仍从 `__all__` 再导出这几个名字；
- `resolve_caller`（要靠 `Services` 做分流）留在实现侧，它不属于契约。

**`Caller` 的三个派生属性（`permission` / `knowledge_base_ids` / `owner_id`）也在这份
契约里**，而不是散到调用点：它们已经有过几份副本，而"同一份数据在两个页面里看到的
不是同一份"这类不一致极难查（见 `owner_id` 的说明）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import ApiKeyPermission
from app.storage.base import ApiKeyRecord, UserRecord

__all__ = [
    "READ",
    "WRITE",
    "Caller",
]

READ = ApiKeyPermission.READONLY
"""语义别名：``check_access`` 的入参用它，读起来比枚举原名清楚。"""

WRITE = ApiKeyPermission.READWRITE


@dataclass(frozen=True, slots=True)
class Caller:
    """一次调用的主体。管理员会话与 API Key 在这里统一成一种形状，
    免得下游每个地方都要判"这是哪种凭据"。"""

    #: 管理员会话没有 API Key 记录；用它区分"管理员"与"受限调用方"
    is_admin: bool = False
    api_key: ApiKeyRecord | None = None
    #: 登录会话对应的账号（v10）。``None`` = API Key 通道
    user: UserRecord | None = None
    #: 当前会话 id（明文 token 的哈希）。退出登录、改密吊销都要定位到它
    session_id: str | None = None

    @property
    def permission(self) -> ApiKeyPermission | None:
        """``None`` 表示不受范围限制（管理员会话如此）。"""
        return None if self.is_admin else (self.api_key.permission if self.api_key else None)

    @property
    def knowledge_base_ids(self) -> tuple[str, ...]:
        if self.is_admin or self.api_key is None:
            return ()
        return tuple(self.api_key.knowledge_base_ids)

    @property
    def owner_id(self) -> str | None:
        """这一轮该按**谁**的归属去读写（知识库、文档、目录、API Key）。

        普通成员 → 自己的账号；管理员会话与 API Key 通道 → ``None``（共享桶）。

        口径写在这里、不写在各调用点：它已经有过三份副本（MCP 端点、工具执行器、
        建库/上传两条 REST 路径），而这三处必须**完全一致**——只要有一处判成了别人，
        表现就是"同一份数据在两个页面里看到的不是同一份"，那种不一致极难查。
        """
        if self.user is not None and not self.is_admin:
            return self.user.id
        return None
