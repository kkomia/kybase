"""知识库分享（v10）。

镜像同构：``app/services/share.py`` → 本文件。

要钉住的核心：**谁能管分享**（owner 或管理员，被分享者不行——write 档也不行，
否则权限不受控扩散）与**授给谁的边界**（名册条目/管理员/自己/被禁用的都拒绝）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ForbiddenError, InvalidRequestError, NotFoundError
from app.models.enums import SharePermission, UserRole
from app.services.share import ShareService
from app.storage.base import KnowledgeBaseRecord, UserRecord

OWNER = "user_owner"


@pytest.fixture
def shares(bundle, store):  # type: ignore[no-untyped-def]
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_1", name="研究库", embedding_model_id="m",
                            embedding_dim=768, owner_id=OWNER)
    )
    store.create_user(UserRecord(id=OWNER, name="主人", username="owner"))
    store.create_user(UserRecord(id="user_m", name="成员", username="member",
                                 password_hash="h", role=UserRole.MEMBER))
    return ShareService(bundle)


def _grant(shares: ShareService, **overrides):  # type: ignore[no-untyped-def]
    payload = {
        "kb_id": "kb_1",
        "username": "member",
        "permission": SharePermission.READ,
        "actor_id": OWNER,
        "is_admin": False,
    }
    payload.update(overrides)
    return shares.grant(**payload)


def test_owner_grants_and_regrant_updates_permission(shares: ShareService) -> None:
    view = _grant(shares)
    assert (view.user_id, view.username, view.permission) == (
        "user_m", "member", SharePermission.READ
    )

    upgraded = _grant(shares, permission=SharePermission.WRITE)
    assert upgraded.permission is SharePermission.WRITE
    assert len(shares.list_for_kb(kb_id="kb_1", actor_id=OWNER, is_admin=False)) == 1


def test_non_owner_cannot_manage_shares(shares: ShareService) -> None:
    """被分享者（write 档）也不能再往外授：权限扩散必须止于 owner。"""
    _grant(shares, permission=SharePermission.WRITE)

    with pytest.raises(ForbiddenError, match="拥有者或管理员"):
        shares.grant(
            kb_id="kb_1", username="owner", permission=SharePermission.READ,
            actor_id="user_m", is_admin=False
    )
    with pytest.raises(ForbiddenError):
        shares.list_for_kb(kb_id="kb_1", actor_id="user_m", is_admin=False)


def test_admin_can_manage_any_kb_shares(shares: ShareService) -> None:
    view = _grant(shares, actor_id=None, is_admin=True)
    assert view.user_id == "user_m"


def test_ownerless_kb_shares_are_admin_only(shares: ShareService, store) -> None:  # type: ignore[no-untyped-def]
    """无主库（API Key 通道建的）没有 owner：成员谁都不算主人，只能管理员管。"""
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_free", name="无主库", embedding_model_id="m",
                            embedding_dim=768)
    )
    with pytest.raises(ForbiddenError):
        shares.grant(
            kb_id="kb_free", username="member", permission=SharePermission.READ,
            actor_id=OWNER, is_admin=False
    )


def test_share_target_must_be_a_real_account(shares: ShareService, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError, match="没有登录名"):
        _grant(shares, username="ghost")
    # 纯名册条目（无 username）不能登录，分享给他没有意义
    store.create_user(UserRecord(id="user_r", name="仅名册"))
    with pytest.raises(NotFoundError, match="名册条目不能登录"):
        _grant(shares, username="仅名册")


def test_share_to_owner_or_admin_is_rejected(shares: ShareService, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError, match="本来就是他的"):
        _grant(shares, username="owner")
    store.create_user(
        UserRecord(id="user_admin2", name="管理员", username="admin2", role=UserRole.ADMIN)
    )
    with pytest.raises(InvalidRequestError, match="管理员本来就能看到"):
        _grant(shares, username="admin2")


def test_share_to_disabled_account_is_rejected(shares: ShareService, store) -> None:  # type: ignore[no-untyped-def]
    store.set_user_disabled("user_m", True)
    with pytest.raises(InvalidRequestError, match="已被禁用"):
        _grant(shares)


def test_revoke(shares: ShareService) -> None:
    _grant(shares)
    shares.revoke(kb_id="kb_1", user_id="user_m", actor_id=OWNER, is_admin=False)
    assert shares.list_for_kb(kb_id="kb_1", actor_id=OWNER, is_admin=False) == []
