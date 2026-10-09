"""笔记服务的单元测试（用真实 SQLite 夹具，不碰网络）。

镜像同构：``app/services/notes.py`` → ``tests/unit/services/test_notes.py``。
"""

import pytest

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.services.notes import (
    MAX_TAGS,
    NOTE_FOLDER_NAME_MAX_CHARS,
    NotesService,
    derive_title,
    normalize_tags,
)


@pytest.fixture
def notes(bundle):  # type: ignore[no-untyped-def]
    return NotesService(bundle)


# ------------------------------------------------------------------ 纯函数


def test_derive_title_from_first_meaningful_line() -> None:
    assert derive_title("\n\n# 眼轴监测\n\n每三个月一次") == "眼轴监测"
    assert derive_title("**重点**\n后续") == "重点"
    assert derive_title("   ") == ""


def test_normalize_tags_dedupes_trims_and_limits() -> None:
    tags = normalize_tags([" 眼科 ", "", "眼科", "a" * 40, *[f"t{i}" for i in range(10)]])

    assert tags[0] == "眼科"
    assert len(tags) == MAX_TAGS
    assert len(set(tags)) == len(tags)  # 无重复
    assert all(len(tag) <= 24 for tag in tags)


# ------------------------------------------------------------------ CRUD


def test_create_fills_title_from_content_and_defaults(notes: NotesService) -> None:
    record = notes.create(
        user_id="u1", content_md="# 眼轴监测\n\n每三个月一次", tags=["眼科", "眼科"]
    )

    assert record.id.startswith("note_")
    assert record.title == "眼轴监测"
    assert record.tags == ["眼科"]
    assert record.source_kind == "manual"
    assert record.created_at is not None


def test_create_accepts_chat_source(notes: NotesService) -> None:
    record = notes.create(
        user_id="u1",
        title="问过的问题",
        content_md="回答正文",
        source_kind="chat",
        source_ref="conv_1",
    )

    assert record.source_kind == "chat"
    assert record.source_ref == "conv_1"


def test_unknown_source_kind_falls_back_to_manual(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="x", source_kind="drop")
    assert record.source_kind == "manual"


def test_get_for_owner_hides_other_peoples_notes(notes: NotesService) -> None:
    """越主即 404：403 会把别人的笔记 id 变成可探测的存在性 oracle。"""
    mine = notes.create(user_id="u1", title="我的")

    assert notes.get_for_owner(mine.id, "u1").id == mine.id
    with pytest.raises(NotFoundError):
        notes.get_for_owner(mine.id, "u2")


def test_get_for_owner_none_means_admin_see_all(notes: NotesService) -> None:
    """归属为 None（管理员/API Key 通道）不校验归属——否则"列表看得到、点进去 404"。"""
    mine = notes.create(user_id="u1", title="我的")

    assert notes.get_for_owner(mine.id, None).id == mine.id


def test_update_persists_and_replaces_tags(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="旧", content_md="旧正文", tags=["a"])
    before = record.updated_at

    updated = notes.update(
        record.id, user_id="u1", title="新", content_md="新正文", tags=["b", "c"], pinned=True
    )

    assert (updated.title, updated.content_md) == ("新", "新正文")
    assert updated.tags == ["b", "c"]
    assert updated.pinned is True
    assert updated.updated_at >= before  # type: ignore[operator]


def test_update_without_tags_keeps_existing(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="t", content_md="正文", tags=["保留"])

    updated = notes.update(record.id, user_id="u1", content_md="改了")

    assert updated.tags == ["保留"]
    assert updated.content_md == "改了"


def test_delete_removes_note_and_tags(notes: NotesService, bundle) -> None:  # type: ignore[no-untyped-def]
    record = notes.create(user_id="u1", title="t", tags=["x"])

    notes.delete(record.id, user_id="u1")

    with pytest.raises(NotFoundError):
        notes.get(record.id)
    assert bundle.meta.list_note_tags(user_id="u1") == []


def test_list_filters_sorts_and_counts(notes: NotesService) -> None:
    notes.create(user_id="u1", title="眼轴监测", content_md="每三个月一次")
    notes.create(user_id="u1", title="散瞳验光", content_md="用药后验光")
    pinned = notes.create(user_id="u1", title="置顶条目", content_md="无关内容", tags=["重点"])
    notes.update(pinned.id, user_id="u1", pinned=True)
    notes.create(user_id="u2", title="别人的", content_md="眼轴")

    items, total = notes.list(user_id="u1")

    assert total == 3
    assert items[0].id == pinned.id  # 置顶优先
    assert all(item.user_id == "u1" for item in items)

    searched, searched_total = notes.list(user_id="u1", query="眼轴")
    assert searched_total == 1 and searched[0].title == "眼轴监测"

    tagged, tagged_total = notes.list(user_id="u1", tag="重点")
    assert tagged_total == 1 and tagged[0].id == pinned.id

    # 不过滤归属（管理员通道）能看到所有人的
    _, all_total = notes.list(user_id=None)
    assert all_total == 4


def test_list_search_escapes_like_wildcards(notes: NotesService) -> None:
    """用户输入的 ``%`` 不该变成通配符——否则搜 "%" 会命中全部。"""
    notes.create(user_id="u1", title="百分之五十", content_md="占 50%")
    notes.create(user_id="u1", title="无关", content_md="什么都没有")

    items, total = notes.list(user_id="u1", query="50%")

    assert total == 1 and items[0].title == "百分之五十"


def test_tags_are_aggregated(notes: NotesService) -> None:
    notes.create(user_id="u1", title="a", tags=["眼科", "重点"])
    notes.create(user_id="u1", title="b", tags=["眼科"])

    tags = dict(notes.tags(user_id="u1"))

    assert tags == {"眼科": 2, "重点": 1}


# ------------------------------------------------------------------ 文件夹（v14）


def test_folders_nest_and_carry_counts(notes: NotesService) -> None:
    """两层文件夹 + 各自的条数；未归档与总数也在这份读数里。"""
    work = notes.create_folder(user_id="u1", name="工作")
    meetings = notes.create_folder(user_id="u1", name="会议记录", parent_id=work.id)
    notes.create(user_id="u1", title="周会", folder_id=work.id)
    notes.create(user_id="u1", title="复盘", folder_id=meetings.id)
    notes.create(user_id="u1", title="随手记")

    overview = notes.folder_overview(user_id="u1")

    # 顺序不断言：PG 的字符串排序跟着库的 collation 走（glibc 的 C/en_US 与 ICU
    # 对中文的先后不一样），拿它当断言只会得到一条"换台机器就红"的用例。
    # 排序本身由下面那条用 ASCII 名的用例钉住。
    assert {item.id for item in overview.folders} == {work.id, meetings.id}
    assert overview.folders[1 if overview.folders[0].id == work.id else 0].parent_id == work.id
    assert overview.counts == {work.id: 1, meetings.id: 1}
    assert overview.unfiled == 1
    assert overview.total == 3  # 总数 = 各项之和，未归档也在内


def test_folders_are_listed_by_name(notes: NotesService) -> None:
    """树上的先后是"按名字"，不是"按建的时候"——所以先建的后出现也算对。"""
    notes.create_folder(user_id="u1", name="banana")
    notes.create_folder(user_id="u1", name="Apple")

    names = [item.name for item in notes.folder_overview(user_id="u1").folders]

    assert names == ["Apple", "banana"]  # lower(name) 排序：大小写不敏感


def test_folder_counts_exclude_other_peoples_notes(notes: NotesService) -> None:
    mine = notes.create_folder(user_id="u1", name="我的")
    notes.create(user_id="u1", title="我的", folder_id=mine.id)
    notes.create(user_id="u2", title="别人的")

    overview = notes.folder_overview(user_id="u1")

    assert overview.counts == {mine.id: 1}
    assert overview.unfiled == 0 and overview.total == 1


def test_duplicate_folder_name_is_rejected_per_level(notes: NotesService) -> None:
    """**同级**不许重名；不同层各自有一个「会议」是正常的。"""
    root = notes.create_folder(user_id="u1", name="工作")
    notes.create_folder(user_id="u1", name="会议")
    child = notes.create_folder(user_id="u1", name="归档", parent_id=root.id)
    notes.create_folder(user_id="u1", name="会议", parent_id=child.id)  # 另一层，允许

    with pytest.raises(ConflictError):
        notes.create_folder(user_id="u1", name="会议")
    with pytest.raises(ConflictError):
        notes.create_folder(user_id="u1", name="会议", parent_id=child.id)


def test_folder_name_is_cleaned_and_bounded(notes: NotesService) -> None:
    created = notes.create_folder(user_id="u1", name="  项目  笔记  ")
    assert created.name == "项目 笔记"

    with pytest.raises(InvalidRequestError):
        notes.create_folder(user_id="u1", name="   ")
    with pytest.raises(InvalidRequestError):
        notes.create_folder(user_id="u1", name="x" * (NOTE_FOLDER_NAME_MAX_CHARS + 1))


def test_unknown_parent_folder_is_404(notes: NotesService) -> None:
    with pytest.raises(NotFoundError):
        notes.create_folder(user_id="u1", name="子", parent_id="fld_missing")


def test_folders_of_another_user_are_invisible(notes: NotesService) -> None:
    """越主即 404（与笔记同一口径）：否则别人的文件夹 id 会变成存在性 oracle。"""
    theirs = notes.create_folder(user_id="u2", name="别人的")

    with pytest.raises(NotFoundError):
        notes.get_folder_for_owner(theirs.id, "u1")
    assert notes.folder_overview(user_id="u1").folders == []
    with pytest.raises(NotFoundError):
        notes.create_folder(user_id="u1", name="子", parent_id=theirs.id)
    with pytest.raises(NotFoundError):
        notes.rename_folder(theirs.id, user_id="u1", name="改个名")


def test_rename_folder_rejects_sibling_duplicate(notes: NotesService) -> None:
    notes.create_folder(user_id="u1", name="A")
    second = notes.create_folder(user_id="u1", name="B")

    with pytest.raises(ConflictError):
        notes.rename_folder(second.id, user_id="u1", name="A")

    renamed = notes.rename_folder(second.id, user_id="u1", name="C")
    assert renamed.name == "C"
    assert notes.rename_folder(second.id, user_id="u1", name="C").name == "C"  # 原地重命名


def test_move_folder_into_itself_or_its_descendant_is_rejected(notes: NotesService) -> None:
    """环一旦写进库，树就再也长不出来（前端遍历会无限展开）。"""
    root = notes.create_folder(user_id="u1", name="工作")
    child = notes.create_folder(user_id="u1", name="会议", parent_id=root.id)
    grandchild = notes.create_folder(user_id="u1", name="周会", parent_id=child.id)

    with pytest.raises(InvalidRequestError, match="它自己"):
        notes.move_folder(root.id, user_id="u1", parent_id=root.id)
    with pytest.raises(InvalidRequestError, match="子文件夹"):
        notes.move_folder(root.id, user_id="u1", parent_id=child.id)
    with pytest.raises(InvalidRequestError, match="子文件夹"):
        notes.move_folder(root.id, user_id="u1", parent_id=grandchild.id)


def test_move_folder_between_parents_and_back_to_root(notes: NotesService) -> None:
    root = notes.create_folder(user_id="u1", name="工作")
    other = notes.create_folder(user_id="u1", name="生活")
    child = notes.create_folder(user_id="u1", name="会议", parent_id=root.id)

    moved = notes.move_folder(child.id, user_id="u1", parent_id=other.id)
    assert moved.parent_id == other.id

    back = notes.move_folder(child.id, user_id="u1", parent_id=None)
    assert back.parent_id is None


def test_move_folder_rejects_duplicate_name_at_the_destination(notes: NotesService) -> None:
    """换位置要按**新位置的兄弟**比一次：两个「会议」不能在同一个父下面碰头。"""
    target = notes.create_folder(user_id="u1", name="目标")
    notes.create_folder(user_id="u1", name="会议", parent_id=target.id)
    loose = notes.create_folder(user_id="u1", name="会议")

    with pytest.raises(ConflictError):
        notes.move_folder(loose.id, user_id="u1", parent_id=target.id)


def test_deleting_a_folder_keeps_its_notes_and_unfiles_them(notes: NotesService) -> None:
    """**这条是本轮的核心纪律**：删文件夹 → 里面的笔记回到未归档，一个都不许少。"""
    work = notes.create_folder(user_id="u1", name="工作")
    meetings = notes.create_folder(user_id="u1", name="会议", parent_id=work.id)
    inside = notes.create(user_id="u1", title="周会纪要", folder_id=meetings.id)

    notes.delete_folder(work.id, user_id="u1")

    assert notes.folder_overview(user_id="u1").folders == []  # 子文件夹跟着删（级联）
    survived = notes.get_for_owner(inside.id, "u1")
    assert survived.title == "周会纪要" and survived.folder_id is None
    assert notes.folder_overview(user_id="u1").unfiled == 1


def test_move_note_into_folder_and_back_to_unfiled(notes: NotesService) -> None:
    folder = notes.create_folder(user_id="u1", name="工作")
    record = notes.create(user_id="u1", title="条目")

    moved = notes.move_note(record.id, user_id="u1", folder_id=folder.id)
    assert moved.folder_id == folder.id
    # 移动不是编辑：不动 updated_at，否则列表会把它顶到最前面
    assert moved.updated_at == record.updated_at

    back = notes.move_note(record.id, user_id="u1", folder_id=None)
    assert back.folder_id is None


def test_move_note_rejects_unknown_or_foreign_folder(notes: NotesService) -> None:
    theirs = notes.create_folder(user_id="u2", name="别人的")
    record = notes.create(user_id="u1", title="条目")

    with pytest.raises(NotFoundError):
        notes.move_note(record.id, user_id="u1", folder_id="fld_missing")
    with pytest.raises(NotFoundError):
        notes.move_note(record.id, user_id="u1", folder_id=theirs.id)
    with pytest.raises(NotFoundError):  # 越主的笔记连移动都不行
        notes.move_note(record.id, user_id="u2", folder_id=theirs.id)


def test_create_note_inside_a_folder_and_reject_foreign_folder(notes: NotesService) -> None:
    folder = notes.create_folder(user_id="u1", name="工作")

    inside = notes.create(user_id="u1", title="周会", folder_id=folder.id)

    assert inside.folder_id == folder.id
    with pytest.raises(NotFoundError):
        notes.create(user_id="u1", title="越主", folder_id="fld_missing")


def test_list_filters_by_folder_and_unfiled(notes: NotesService) -> None:
    work = notes.create_folder(user_id="u1", name="工作")
    life = notes.create_folder(user_id="u1", name="生活")
    in_work = notes.create(user_id="u1", title="工作条目", folder_id=work.id)
    notes.create(user_id="u1", title="生活条目", folder_id=life.id)
    notes.create(user_id="u1", title="没归档的")

    items, total = notes.list(user_id="u1", folder_id=work.id)
    assert total == 1 and items[0].id == in_work.id

    unfiled_items, unfiled_total = notes.list(user_id="u1", unfiled=True)
    assert unfiled_total == 1 and unfiled_items[0].title == "没归档的"

    _, all_total = notes.list(user_id="u1")
    assert all_total == 3

    # 搜索与文件夹两个条件叠加：同一个轴上的两种取法 + 子串
    _searched, searched_total = notes.list(user_id="u1", folder_id=work.id, query="工作条目")
    assert searched_total == 1


def test_list_does_not_validate_the_folder(bundle) -> None:  # type: ignore[no-untyped-def]
    """不存在的文件夹 id 返回空列表而不是 404：别的标签页刚删掉它时，
    空列表比报错更像"这个地方现在是空的"。"""
    service = NotesService(bundle)
    service.create(user_id="u1", title="无关")

    items, total = service.list(user_id="u1", folder_id="fld_missing")

    assert items == [] and total == 0


def test_note_folder_foreign_keys_declare_the_documented_semantics(pg_stores) -> None:  # type: ignore[no-untyped-def]
    """外键的 ``ON DELETE`` 是**表定义上的语义**，所以直接问数据库（而不是问服务层）。

    ``SET NULL``：删文件夹 → 笔记回到未归档（``confdeltype='n'``）；
    ``CASCADE``：删文件夹 → 子文件夹跟着走（``'c'``）。
    这条用例钉的是"语义真的落在表上"——服务层哪天被改成手写两段删除，
    这两条仍然必须成立（它们是数据层的保证，不是某条代码路径的行为）。
    """
    with pg_stores.read() as conn:
        rows = conn.execute(
            "select conrelid::regclass::text as rel, "
            "       a.attname as col, c.confdeltype as del "
            "  from pg_constraint c "
            "  join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey) "
            " where c.contype = 'f' "
            "   and c.conrelid in ('notes'::regclass, 'note_folders'::regclass)"
        ).fetchall()

    rules = {(row["rel"], row["col"]): row["del"] for row in rows}
    assert rules[("notes", "folder_id")] == "n", "删文件夹要留下笔记（SET NULL）"
    assert rules[("note_folders", "parent_id")] == "c", "子文件夹随父级一起删（CASCADE）"


def test_deleting_a_parent_folder_row_cascades_and_unfiles_at_the_database_level(
    bundle,  # type: ignore[no-untyped-def]
    pg_stores,  # type: ignore[no-untyped-def]
) -> None:
    """不走服务层，直接删父文件夹那一行：子文件夹消失、笔记还在且回到未归档。

    与上面那条互补：上面查的是**声明**，这条查的是**行为**（声明写对了但没生效，
    在生产里是同一种故障）。``pg_stores`` 与 ``bundle`` 指向同一个临时库。
    """
    service = NotesService(bundle)
    parent = service.create_folder(user_id="u1", name="工作")
    child = service.create_folder(user_id="u1", name="会议", parent_id=parent.id)
    note = service.create(user_id="u1", title="周会", folder_id=child.id)

    with pg_stores.session() as conn:
        conn.execute("DELETE FROM note_folders WHERE id = %s", (parent.id,))
        folders = conn.execute("SELECT id FROM note_folders").fetchall()
        row = conn.execute(
            "SELECT folder_id, title FROM notes WHERE id = %s", (note.id,)
        ).fetchone()

    assert folders == [], "子文件夹应当随父级级联删掉"
    assert row is not None and row["title"] == "周会", "笔记不该跟着文件夹消失"
    assert row["folder_id"] is None, "失去归属的笔记回到未归档"


# ------------------------------------------------------------------ 加入知识库


class _FakeIngest:
    def __init__(self, *, duplicate: bool = False, replace_error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.replaced: list[dict] = []
        self._duplicate = duplicate
        self._replace_error = replace_error

    def submit(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        document = type("Doc", (), {"id": "doc_1"})()
        return type("Outcome", (), {"document": document, "is_duplicate": self._duplicate})()

    def replace(self, document_id: str, **kwargs):  # type: ignore[no-untyped-def]
        if self._replace_error is not None:
            raise self._replace_error
        self.replaced.append({"document_id": document_id, **kwargs})
        return type("Doc", (), {"id": document_id})()


class _FakeDocuments:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue_ingest(self, document_id: str, force: bool = False):  # type: ignore[no-untyped-def]
        self.enqueued.append(document_id)
        return object()


def test_attach_to_kb_submits_markdown_and_records_document(bundle) -> None:  # type: ignore[no-untyped-def]
    ingest = _FakeIngest()
    documents = _FakeDocuments()
    service = NotesService(bundle, ingest=ingest, documents=documents)
    record = service.create(user_id="u1", title="眼轴笔记", content_md="# 眼轴\n每三个月测一次")

    updated = service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")

    assert updated.kb_id == "kb_1"
    assert updated.doc_id == "doc_1"
    assert documents.enqueued == ["doc_1"]
    call = ingest.calls[0]
    assert call["knowledge_base_id"] == "kb_1"
    assert call["filename"] == "眼轴笔记.md"
    assert call["content"].decode("utf-8") == "# 眼轴\n每三个月测一次"
    assert call["mime_type"] == "text/markdown"


def test_attach_duplicate_does_not_enqueue_again_but_still_links(bundle) -> None:  # type: ignore[no-untyped-def]
    """同一份内容重复入库时返回已有文档：不重复排队，但笔记仍要指向它。"""
    ingest = _FakeIngest(duplicate=True)
    documents = _FakeDocuments()
    service = NotesService(bundle, ingest=ingest, documents=documents)
    record = service.create(user_id="u1", title="t", content_md="正文")

    updated = service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")

    assert updated.doc_id == "doc_1"
    assert documents.enqueued == []


def test_attach_rejects_empty_content(bundle) -> None:  # type: ignore[no-untyped-def]
    service = NotesService(bundle, ingest=_FakeIngest(), documents=_FakeDocuments())
    record = service.create(user_id="u1", title="空的")

    with pytest.raises(InvalidRequestError):
        service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")


def test_attach_without_pipeline_is_rejected(bundle) -> None:  # type: ignore[no-untyped-def]
    service = NotesService(bundle)
    record = service.create(user_id="u1", title="t", content_md="正文")

    with pytest.raises(InvalidRequestError):
        service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")


# ------------------------------------------------------- 改动回流到知识库（v0.12）


def _attached(bundle):  # type: ignore[no-untyped-def]
    """建一条已入库的笔记，返回（服务、假摄入、假文档、笔记 id）。"""
    ingest = _FakeIngest()
    documents = _FakeDocuments()
    service = NotesService(bundle, ingest=ingest, documents=documents)
    record = service.create(user_id="u1", title="眼轴笔记", content_md="# 眼轴\n每三个月测一次")
    service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")
    documents.enqueued.clear()  # 只关心"编辑之后"的入队
    return service, ingest, documents, record.id


def test_editing_an_attached_note_syncs_the_knowledge_base_copy(bundle) -> None:  # type: ignore[no-untyped-def]
    """**本轮修的核心缺陷**：改了笔记，库里那一份不能还是旧的。

    此前 ``update`` 只写笔记表，于是"笔记改了、库里没变"——界面上看不出任何异常，
    直到某天检索出一段自己已经改掉的话。
    """
    service, ingest, documents, note_id = _attached(bundle)

    service.update(note_id, user_id="u1", content_md="# 眼轴\n改成每半年测一次")

    assert len(ingest.replaced) == 1
    call = ingest.replaced[0]
    assert call["document_id"] == "doc_1"
    assert call["content"].decode("utf-8") == "# 眼轴\n改成每半年测一次"
    assert call["filename"] == "眼轴笔记.md"
    # 内容变了就要重跑一遍流水线，否则新的正文永远不会被索引
    assert documents.enqueued == ["doc_1"]


def test_edit_uses_the_new_title_for_the_filename(bundle) -> None:  # type: ignore[no-untyped-def]
    """标题也跟着改时，库里那份文件名要一起变——它是用户在文档列表里认它的依据。"""
    service, ingest, _documents, note_id = _attached(bundle)

    service.update(note_id, user_id="u1", title="眼轴监测规范", content_md="# 新正文")

    assert ingest.replaced[0]["filename"] == "眼轴监测规范.md"


def test_editing_a_note_outside_any_kb_touches_no_pipeline(bundle) -> None:  # type: ignore[no-untyped-def]
    """没入库的笔记改动不该惊动摄入流水线。"""
    ingest = _FakeIngest()
    documents = _FakeDocuments()
    service = NotesService(bundle, ingest=ingest, documents=documents)
    record = service.create(user_id="u1", content_md="还没入库")

    service.update(record.id, user_id="u1", content_md="改一下")

    assert ingest.replaced == []
    assert documents.enqueued == []


def test_editing_only_tags_does_not_resync(bundle) -> None:  # type: ignore[no-untyped-def]
    """只改标签不是内容变化：重新索引一遍白烧算力。"""
    service, ingest, documents, note_id = _attached(bundle)

    service.update(note_id, user_id="u1", tags=["眼科"])

    assert ingest.replaced == []
    assert documents.enqueued == []


def test_identical_content_does_not_resync(bundle) -> None:  # type: ignore[no-untyped-def]
    """正文没变时不该重跑——前端"保存"按钮常常原样提交。"""
    service, ingest, documents, note_id = _attached(bundle)

    service.update(note_id, user_id="u1", content_md="# 眼轴\n每三个月测一次")

    assert ingest.replaced == []
    assert documents.enqueued == []


def test_clearing_the_body_leaves_the_kb_copy_alone(bundle) -> None:  # type: ignore[no-untyped-def]
    """清空正文时**不同步**：库里保留最后那版内容。

    变成一份空文档更糟——空文档检索不到，会让"这篇还在库里"凭空消失。
    """
    service, ingest, documents, note_id = _attached(bundle)

    service.update(note_id, user_id="u1", content_md="   ")

    assert ingest.replaced == []
    assert documents.enqueued == []


def test_sync_failure_leaves_the_note_unchanged(bundle) -> None:  # type: ignore[no-untyped-def]
    """同步失败时**整次更新都不做**——不能留下"笔记改了、库里没改"。

    实测里最容易触发的失败是"那份文档正在处理中"（``IngestService.replace``
    在检测到未结束的任务时会拒绝）。顺序上先同步再写笔记，所以失败时笔记是干净的。
    """
    ingest = _FakeIngest(replace_error=ConflictError("这份文档正在处理中，请等它处理完再改"))
    documents = _FakeDocuments()
    service = NotesService(bundle, ingest=ingest, documents=documents)
    record = service.create(user_id="u1", title="t", content_md="原正文")
    service.attach_to_kb(record.id, user_id="u1", kb_id="kb_1")

    with pytest.raises(ConflictError):
        service.update(record.id, user_id="u1", content_md="新正文")

    assert service.get(record.id).content_md == "原正文"


# ------------------------------------------------------------------ 配图


def _png() -> bytes:
    # 最小合法 PNG（1x1）；这里只关心字节进出，不关心它长什么样
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "05570a2e0000000049454e44ae426082"
    )


def test_upload_image_returns_content_addressed_name(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="带图")

    path, name = notes.upload_image(
        record.id, user_id="u1", filename="截图 2026.PNG", content=_png()
    )

    assert name.endswith(".png") and len(name) == 16 + 4  # 哈希前缀 + 后缀
    assert path.endswith(name)
    assert notes.image_bytes(record.id, name) == _png()  # 读得回来


def test_upload_image_rejects_svg_and_unknown_suffix(notes: NotesService) -> None:
    """不收 SVG：它能内嵌脚本，而图片 URL 是给 <img> 直接加载的。"""
    record = notes.create(user_id="u1", title="t")

    with pytest.raises(InvalidRequestError):
        notes.upload_image(record.id, user_id="u1", filename="x.svg", content=b"<svg/>")
    with pytest.raises(InvalidRequestError):
        notes.upload_image(record.id, user_id="u1", filename="x.exe", content=b"x")


def test_upload_image_rejects_empty_and_oversized(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="t")

    with pytest.raises(InvalidRequestError):
        notes.upload_image(record.id, user_id="u1", filename="a.png", content=b"")
    with pytest.raises(InvalidRequestError):
        notes.upload_image(
            record.id, user_id="u1", filename="a.png", content=b"x" * (11 * 1024 * 1024)
        )


def test_upload_image_hides_other_peoples_notes(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="t")

    with pytest.raises(NotFoundError):
        notes.upload_image(record.id, user_id="u2", filename="a.png", content=_png())


def test_image_bytes_rejects_path_traversal(notes: NotesService) -> None:
    record = notes.create(user_id="u1", title="t")

    for bad in ("../secret", "..", "a/b.png", ""):
        with pytest.raises(NotFoundError):
            notes.image_bytes(record.id, bad)
