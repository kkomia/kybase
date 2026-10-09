"""使用者名册（G6）。

镜像同构：``app/services/users.py`` → 本文件。

要钉住的核心是**名册条目的名字不是鉴权边界**：伪造一个名字不会获得任何权限，
只会让归属记错。v10 起名册升级为账号（username/password_hash），
但那是另一条路径（登录会话），与本模块的"按名字解析归属"互不干涉。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.services.users import MAX_NAME_CHARS, UserService


@pytest.fixture
def users(bundle) -> UserService:  # type: ignore[no-untyped-def]
    return UserService(bundle)


# --------------------------------------------------------------------- 名册


def test_create_and_list(users: UserService) -> None:
    created = users.create(name="小王", note="运营")

    assert created.id.startswith("user_")
    assert [item.id for item in users.list()] == [created.id]
    assert users.get(created.id).note == "运营"


def test_duplicate_name_is_rejected(users: UserService) -> None:
    """重名会让名册失去意义——它存在的全部理由就是"能分辨是谁"。"""
    users.create(name="小王")

    with pytest.raises(ConflictError):
        users.create(name="小王")


def test_blank_name_is_rejected(users: UserService) -> None:
    with pytest.raises(InvalidRequestError):
        users.create(name="   ")


def test_name_is_flattened_and_truncated(users: UserService) -> None:
    """换行会让归属在列表里撑破行；过长则截断——名字是给人看的标签。"""
    created = users.create(name="小\n王")
    assert created.name == "小 王"

    long_name = users.create(name="名" * 100)
    assert len(long_name.name) == MAX_NAME_CHARS


def test_get_missing_user_raises(users: UserService) -> None:
    with pytest.raises(NotFoundError):
        users.get("user_不存在")


def test_delete_keeps_the_documents(users: UserService, bundle, kb) -> None:  # type: ignore[no-untyped-def]
    """**删使用者不能删他的文档。**

    文档已经进了知识库、已经向量化、可能已被引用——顺手删掉是数据丢失，
    不是权限撤销。归属置空，界面显示"未记录"。
    """
    from app.storage.base import DataSourceKind, DocumentRecord, DocumentStage

    user = users.create(name="小王")
    bundle.meta.create_document(
        DocumentRecord(
            id="doc_1",
            knowledge_base_id=kb.id,
            name="a.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="h",
            stage=DocumentStage.UPLOADED,
            uploaded_by=user.id,
        )
    )
    assert users.count_documents(user.id) == 1

    users.delete(user.id)

    with pytest.raises(NotFoundError):
        users.get(user.id)
    kept = bundle.meta.get_document("doc_1")
    assert kept is not None, "文档被连带删掉了——那是数据丢失"
    assert kept.uploaded_by is None, "归属应当置空"


# --------------------------------------------------------------------- 解析身份


def test_resolve_known_operator(users: UserService) -> None:
    created = users.create(name="小王")

    assert users.resolve_operator("小王").id == created.id  # type: ignore[union-attr]


def test_resolve_missing_name_returns_none(users: UserService) -> None:
    """**不自动创建使用者。**

    拼错一个字就会静默多出一个使用者，名册很快就脏了。
    前端因此应当从名册里选，而不是让人手打。
    """
    assert users.resolve_operator("没这个人") is None
    assert users.list() == [], "不该因为解析失败而建出一个使用者"


def test_resolve_empty_name_returns_none(users: UserService) -> None:
    """名册是可选功能：没选身份就照旧工作，不算错误。"""
    assert users.resolve_operator(None) is None
    assert users.resolve_operator("") is None
    assert users.resolve_operator("   ") is None


def test_resolve_flattens_whitespace(users: UserService) -> None:
    """请求头里可能带多余空白，压平后才好比对。"""
    users.create(name="小王")
    assert users.resolve_operator("  小王  ") is not None


def test_name_has_no_effect_on_permissions(users: UserService) -> None:
    """**名册条目本身不是鉴权主体**：只有名字、没有账号字段的记录不能登录。

    v10 起了变化要钉清楚：``UserRecord`` 现在**有** role 字段（账号体系），
    但纯名册条目没有 username/password_hash——它回答"是谁做的"，
    而"能做什么"从登录会话来（``services/auth.py``），不是从这条记录的名字来。
    """
    user = users.create(name="小王")
    # 名册条目没有凭据：登录按 username 查，查不到它
    assert user.username is None
    assert user.password_hash is None
    # role 字段存在但默认 member 且无所附着——它只在账号登录后才有意义
    assert user.role.value == "member"

def test_resolve_accepts_an_id(users: UserService) -> None:
    """**界面发的是 id**（HTTP 头只能是 ASCII，而名字可能是中文）。

    实测发中文名字时 curl 与 httpx 都会抛
    ``UnicodeEncodeError: 'ascii' codec can't encode``。
    """
    created = users.create(name="小王")

    assert users.resolve_operator(created.id).id == created.id  # type: ignore[union-attr]


def test_resolve_falls_back_to_name(users: UserService) -> None:
    """手写 curl 时发名字更自然，所以两种都收。"""
    created = users.create(name="wang")

    assert users.resolve_operator("wang").id == created.id  # type: ignore[union-attr]


def test_resolve_missing_id_returns_none(users: UserService) -> None:
    assert users.resolve_operator("user_nobody") is None
