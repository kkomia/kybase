"""知识库分享（v10：私有 + 可分享）。

owner 把自己的库授给另一个成员，读/写两档。三件事收在这里：

1. **谁能管分享**：只有库的 owner 或管理员。被分享者（哪怕是 write 档）**不能**
   再往外授——否则权限会不受控地扩散，owner 完全失去"谁能看我的库"的掌控。
2. **授给谁**：按 **username** 而不是 user id。分享是人对人的动作，界面让用户
   敲对方登录名比翻 id 列表自然；而且成员没有权限列全量用户（/users 是
   控制台级），username 输入是唯一不泄露名册全貌的方式。
3. **边界校验**：目标是纯名册条目（不能登录）、是管理员（本来就全可见）、
   是 owner 自己（无意义）、已被禁用——都拒绝并说清楚为什么，不静默落库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import ForbiddenError, InvalidRequestError, NotFoundError
from app.models.enums import SharePermission, UserRole
from app.storage.base import ShareRecord, StoreBundle

__all__ = ["ShareService", "ShareView"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShareView:
    """一条分享的展示形态：带上对方的登录名与显示名（界面要显示"分享给了谁"）。"""

    kb_id: str
    user_id: str
    username: str
    name: str
    permission: SharePermission
    created_at: datetime | None


class ShareService:
    """分享的授出、收回与查询。判定逻辑都在这里，端点只做转发。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 查询

    def list_for_kb(self, *, kb_id: str, actor_id: str | None, is_admin: bool) -> list[ShareView]:
        kb = self._require_kb(kb_id)
        self._require_owner_or_admin(
            kb_owner=kb.owner_id, actor_id=actor_id, is_admin=is_admin
        )
        return [self._view(record) for record in self._stores.meta.list_shares_for_kb(kb_id)]

    # ------------------------------------------------------------------ 授出与收回

    def grant(
        self,
        *,
        kb_id: str,
        username: str,
        permission: SharePermission,
        actor_id: str | None,
        is_admin: bool,
    ) -> ShareView:
        kb = self._require_kb(kb_id)
        self._require_owner_or_admin(kb_owner=kb.owner_id, actor_id=actor_id, is_admin=is_admin)

        target = self._stores.meta.find_user_by_username(username.strip().lower())
        if target is None or target.username is None:
            raise NotFoundError(f"没有登录名为「{username}」的账号（名册条目不能登录，无法分享）")
        if target.id == kb.owner_id:
            raise InvalidRequestError("这个库本来就是他的，无需分享")
        if target.role is UserRole.ADMIN:
            raise InvalidRequestError("管理员本来就能看到全部知识库，无需分享")
        if target.disabled:
            raise InvalidRequestError("该账号已被禁用，请先让管理员启用")

        record = self._stores.meta.put_share(
            ShareRecord(kb_id=kb_id, user_id=target.id, permission=permission)
        )
        logger.info("知识库 %s 已分享给 %s（%s）", kb_id, target.username, permission.value)
        return self._view(record)

    def revoke(self, *, kb_id: str, user_id: str, actor_id: str | None, is_admin: bool) -> None:
        kb = self._require_kb(kb_id)
        self._require_owner_or_admin(kb_owner=kb.owner_id, actor_id=actor_id, is_admin=is_admin)
        self._stores.meta.delete_share(kb_id, user_id)

    # ------------------------------------------------------------------ 内部

    def _require_kb(self, kb_id: str):  # type: ignore[no-untyped-def]
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")
        return kb

    @staticmethod
    def _require_owner_or_admin(
        *, kb_owner: str | None, actor_id: str | None, is_admin: bool
    ) -> None:
        if is_admin:
            return
        # 无主库（API Key 建的）只有管理员能管分享——成员谁都不算 owner
        if actor_id is None or kb_owner is None or kb_owner != actor_id:
            raise ForbiddenError("只有知识库的拥有者或管理员能管理分享")

    def _view(self, record: ShareRecord) -> ShareView:
        user = self._stores.meta.get_user(record.user_id)
        return ShareView(
            kb_id=record.kb_id,
            user_id=record.user_id,
            username=user.username if user and user.username else "",
            name=user.name if user else "（已删除）",
            permission=record.permission,
            created_at=record.created_at,
        )
