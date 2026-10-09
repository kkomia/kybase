"""笔记（v20，对标腾讯 ima「笔记」的最小可用版）。

定位：**轻编辑器 + AI 写作 + 知识库联动**三件事，不是一个独立的笔记软件。
kylab 已有知识库、对话（SSE 流式）、摄入流水线三大底座，这里补上缺的那一小块：

1. **笔记 CRUD**：Markdown 是唯一事实源（``content_md``），编辑器只负责渲染与编辑；
2. **加入知识库**：把笔记当成一份 ``text/markdown`` 文档走**现有摄入流水线**——
   切块、嵌入、检索全部复用，入库后回填 ``kb_id``/``doc_id``，检索命中可跳回笔记；
3. **问答存为笔记**：前端把一轮问答写成 ``source_kind='chat'`` 的笔记，本层不特殊处理；
4. **文件夹（v14）**：左栏的层级文件夹 + 笔记归属，见下面"文件夹"那一节。

**刻意不做**（与本产品"单机零依赖"的定位冲突，调研报告 §1 已明确）：
协作编辑（CRDT/Yjs）、云端多端同步、模板市场、语音听记。

搜索用 ``LIKE`` 子串匹配而不是 FTS5：笔记是个人规模的数据，子串对中文天然可用，
也没有"改了正文忘了同步索引"这类只在几周后才暴露的静默故障（见 meta_store 的说明）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.storage.base import IMAGES, NoteFolderRecord, NoteRecord, StoreBundle

__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "NOTE_FOLDER_NAME_MAX_CHARS",
    "NoteFolderOverview",
    "NotesService",
    "image_resource",
    "normalize_tags",
]

logger = logging.getLogger(__name__)

#: 标题上限：与"自动标题"同一个量级，够认出是哪一条即可。
NOTE_TITLE_MAX_CHARS = 80
#: 每条笔记最多几个标签、单个标签多长。标签是"顺手贴的分类"，不是分类体系。
MAX_TAGS = 8
MAX_TAG_CHARS = 24
#: 文件夹名上限。与知识库目录（``folder.FOLDER_NAME_MAX_CHARS``）同一个数：
#: 够写清"2026 Q1 合同"，又不至于把左栏那棵树撑爆。
NOTE_FOLDER_NAME_MAX_CHARS = 64
#: 允许的来源类型。``manual`` 手记 / ``chat`` 问答存为 / ``clip`` 剪藏。
SOURCE_KINDS = frozenset({"manual", "chat", "clip"})

#: 笔记配图上限。比文档上传（200MB）小得多：它要内联在正文里，
#: 大图会让笔记自身变得难以加载。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: 只收光栅图。**刻意不收 SVG**：SVG 可以内嵌脚本，而图片 URL 是给 ``<img>``
#: 直接加载的（无自定义头、靠签名授权），内联渲染 SVG 等于给自己开一个 XSS 口子。
ALLOWED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def image_resource(note_id: str, name: str) -> str:
    """被签名的资源标识。

    公开导出：API 层签发与校验都要用**同一个**函数算被签内容，
    两处各写一遍字符串拼接迟早会漂（漂了就是图片全部 401）。
    """
    return f"note-image:{note_id}:{name}"


def _image_key(note_id: str, name: str) -> str:
    """对象存储里的键。按笔记分目录，删笔记时能整目录清理。"""
    return f"{IMAGES}/notes/{note_id}/{name}"


def normalize_tags(tags: list[str] | None) -> list[str]:
    """清洗标签：去空白、去重、限长限量，保持用户给的顺序。"""
    out: list[str] = []
    for raw in tags or []:
        tag = (raw or "").strip()[:MAX_TAG_CHARS]
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def derive_title(content_md: str) -> str:
    """没有标题时从正文首行提取：取第一个非空行，去掉 Markdown 记号。

    比"未命名笔记"好认——列表里满屏"未命名"等于没有标题。
    """
    for line in content_md.splitlines():
        text = line.strip().lstrip("#").strip().strip("*_> ").strip()
        if text:
            return text[:NOTE_TITLE_MAX_CHARS]
    return ""


class NotesService:
    """笔记的读写与入库。"""

    def __init__(self, stores: StoreBundle, *, ingest=None, documents=None) -> None:  # type: ignore[no-untyped-def]
        self._stores = stores
        # 入库是可选能力：不接摄入流水线时笔记功能照常（列表、编辑、删除）。
        # 这样单测与不配模型的部署也能用。
        self._ingest = ingest
        self._documents = documents

    # ------------------------------------------------------------------ 写

    def create(
        self,
        *,
        user_id: str | None,
        title: str = "",
        content_md: str = "",
        source_kind: str = "manual",
        source_ref: str | None = None,
        tags: list[str] | None = None,
        folder_id: str | None = None,
    ) -> NoteRecord:
        kind = source_kind if source_kind in SOURCE_KINDS else "manual"
        body = content_md or ""
        clean_title = title.strip()[:NOTE_TITLE_MAX_CHARS] or derive_title(body)
        return self._stores.meta.create_note(
            NoteRecord(
                id=f"note_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                title=clean_title,
                content_md=body,
                source_kind=kind,
                source_ref=source_ref,
                tags=normalize_tags(tags),
                # 顺手建在某文件夹里（左栏选中文件夹时点"+"就是这条路径）。
                # 文件夹必须先校验归属：否则能把笔记挂到别人的文件夹下——
                # 那条笔记此后在列表里就"消失"了（列表按文件夹过滤时查不出来）。
                folder_id=self._folder_id_for_owner(folder_id, user_id),
            )
        )

    def update(
        self,
        note_id: str,
        *,
        user_id: str | None,
        title: str | None = None,
        content_md: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> NoteRecord:
        record = self.get_for_owner(note_id, user_id)
        body = record.content_md if content_md is None else content_md
        raw_title = record.title if title is None else title
        clean_title = raw_title.strip()[:NOTE_TITLE_MAX_CHARS]
        if title is None and content_md is not None and not clean_title:
            # 正文被清空后标题不必跟着清掉；但原标题为空时要重新推导
            clean_title = derive_title(body)

        # 正文真的变了、而且这条笔记已经进过知识库 → 库里那一份要跟着变（v0.12）。
        # **放在写笔记之前**：同步失败（比如那份文档正在处理中）时整次更新都不做，
        # 用户看到明确报错，而不会留下"笔记改了、库里还是旧的"这种没人发现的不一致。
        if content_md is not None and content_md != record.content_md:
            self._sync_kb_copy(record, body=body, title=clean_title)

        self._stores.meta.update_note(
            note_id,
            title=clean_title,
            content_md=body,
            pinned=record.pinned if pinned is None else pinned,
            updated_at=datetime.now(UTC),
            tags=None if tags is None else normalize_tags(tags),
        )
        return self.get(note_id)

    def _sync_kb_copy(self, record: NoteRecord, *, body: str, title: str) -> None:
        """把知识库里那一份同步成当前正文。

        **为什么必须做**：此前 ``update`` 只写笔记表，于是"笔记改了、库里还是旧的"。
        用户在界面上看不到任何异常，直到某天检索出一段自己已经改掉的话——
        这不是缺个功能，是静默的不一致，而静默的不一致比报错难查得多。

        两处刻意的选择：

        - **原地替换而不是"删了重加"**：文档 id 是引用的锚点（对话出处、笔记关联），
          换 id 会打断引用，旧版还会白占一次回收站；
        - **正文被清空时不同步**：库里保留最后那版内容，而不是变成一份空文档——
          空文档检索不到，会让"这篇还在库里"这件事凭空消失，那比留个旧版更糟。
        """
        if self._ingest is None or self._documents is None or not record.doc_id:
            return
        stripped = body.strip()
        if not stripped:
            return
        self._ingest.replace(
            record.doc_id,
            filename=f"{title or '未命名笔记'}.md",
            content=stripped.encode("utf-8"),
            mime_type="text/markdown",
        )
        # 不带 force：replace 已经把阶段推回 uploaded，这一趟要**从头**走
        # （解析 → 切块 → 向量化）。用 force 会把它直接推到 CHUNKING，
        # 于是拿着上一次的解析产物切块——切出来还是旧内容
        self._documents.enqueue_ingest(record.doc_id)

    def delete(self, note_id: str, *, user_id: str | None) -> None:
        self.get_for_owner(note_id, user_id)
        self._stores.meta.delete_note(note_id)

    def move_note(self, note_id: str, *, user_id: str | None, folder_id: str | None) -> NoteRecord:
        """把笔记移进文件夹 / 移回未归档（``folder_id=None``）。

        **为什么不在 ``PATCH /notes/{id}`` 里带上 ``folder_id``**：那条路径是编辑器的
        自动保存（800ms 一次、可能落后于用户在树上的操作）。归属与正文走同一条 PATCH，
        就会出现"用户刚在左栏把笔记移到 A，而编辑器手上那份草稿的 folder_id 还是旧的"
        ——保存回来把归属又刷回去。分成两条路径之后，移动是移动、保存是保存，
        两者不会互相覆盖。文档那边（``PATCH /documents/{id}/folder``）也是这么分的。
        """
        record = self.get_for_owner(note_id, user_id)
        self._stores.meta.set_note_folder(record.id, self._folder_id_for_owner(folder_id, user_id))
        return self.get(note_id)

    # ------------------------------------------------------------------ 文件夹（v14）

    def folder_overview(self, *, user_id: str | None) -> NoteFolderOverview:
        """左栏那棵树要的全部数字：文件夹、各自条数、未归档条数、总条数。

        **一次聚合算全**（``count_notes_by_folder`` 按 ``folder_id`` GROUP BY，
        未归档那一行就是键为 ``None`` 的那条）：逐个文件夹查一次就是 N+1，
        而这三组数字每次移动笔记都要一起变。
        """
        folders = self._stores.meta.list_note_folders(user_id=user_id)
        counts = self._stores.meta.count_notes_by_folder(user_id=user_id)
        by_folder = {key: value for key, value in counts.items() if key is not None}
        unfiled = counts.get(None, 0)
        return NoteFolderOverview(
            folders=folders,
            counts=by_folder,
            unfiled=unfiled,
            total=sum(counts.values()),
        )

    def get_folder_for_owner(self, folder_id: str, user_id: str | None) -> NoteFolderRecord:
        """取文件夹；**越主即 404**（与笔记同一口径，理由见 ``get_for_owner``）。"""
        record = self._stores.meta.get_note_folder(folder_id)
        if record is None:
            raise NotFoundError(f"文件夹不存在：{folder_id}")
        if user_id is not None and record.user_id != user_id:
            raise NotFoundError(f"文件夹不存在：{folder_id}")
        return record

    def create_folder(
        self, *, user_id: str | None, name: str, parent_id: str | None = None
    ) -> NoteFolderRecord:
        cleaned = _clean_folder_name(name)
        if parent_id is not None:
            self.get_folder_for_owner(parent_id, user_id)
        self._reject_duplicate_name(user_id, parent_id, cleaned)
        return self._stores.meta.create_note_folder(
            NoteFolderRecord(
                id=f"fld_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                name=cleaned,
                parent_id=parent_id,
            )
        )

    def rename_folder(
        self, folder_id: str, *, user_id: str | None, name: str
    ) -> NoteFolderRecord:
        record = self.get_folder_for_owner(folder_id, user_id)
        cleaned = _clean_folder_name(name)
        if cleaned != record.name:
            self._reject_duplicate_name(user_id, record.parent_id, cleaned)
            self._stores.meta.rename_note_folder(folder_id, cleaned)
        return self.get_folder_for_owner(folder_id, user_id)

    def move_folder(
        self, folder_id: str, *, user_id: str | None, parent_id: str | None
    ) -> NoteFolderRecord:
        """把文件夹挪到另一个文件夹下；``parent_id=None`` = 挪回根级。

        两条要挡住的：**移进自己**、**移进自己的子孙**——环一旦写进库，
        树就再也长不出来（前端遍历会把那一圈无限展开），而这是**用户点得出来**的
        操作（菜单里那两个选项不该出现，但接口不能只靠界面自觉）。
        """
        record = self.get_folder_for_owner(folder_id, user_id)
        if parent_id is not None:
            parent = self.get_folder_for_owner(parent_id, user_id)
            if parent.id == record.id:
                raise InvalidRequestError("不能把文件夹移进它自己")
            if record.id in self._ancestor_ids(parent.id, user_id):
                raise InvalidRequestError("不能把文件夹移进它自己的子文件夹里")
        # 换了位置就要按**新位置的兄弟**比一次重名：根级与子级各有各的名册
        if parent_id != record.parent_id:
            self._reject_duplicate_name(user_id, parent_id, record.name)
            self._stores.meta.set_note_folder_parent(folder_id, parent_id)
        return self.get_folder_for_owner(folder_id, user_id)

    def delete_folder(self, folder_id: str, *, user_id: str | None) -> None:
        """删文件夹：**子文件夹跟着删（级联），里面的笔记回到未归档**。

        这两条都是**表定义上的语义**（见 schema.py 迁移 v14 与 ``NoteFolderRecord``），
        不是本方法的判断——所以这里只发一条 DELETE。界面对此负责：
        确认框里会说清"N 个子文件夹会被删、M 篇笔记会回到未归档、笔记本身不会删"。

        与知识库目录（非空则拒绝）的不同是有意的：那里的目录是单层容器，
        "先把文件移走再删"只是两步；这里的文件夹是**用户自己的层级**，
        逐个清空再自底向上删一层层点，成本远高于收益——而真正不可恢复的东西
        （笔记正文）本来就没有跟着消失。
        """
        self.get_folder_for_owner(folder_id, user_id)
        self._stores.meta.delete_note_folder(folder_id)

    def _ancestor_ids(self, folder_id: str, user_id: str | None) -> set[str]:
        """从某个文件夹往上走到的全部祖先 id（不含它自己）。

        整份名册一次读出、在内存里走：文件夹是个人规模（几十个），
        每步回库查一次父级"不会更准，只会更慢"。
        链上出现断点（父不在名册里）或成环时**停在那一步**——
        库里本不该有这两种状态（写入路径都校验过），但真出现了也不该让这里转死循环。
        """
        parents = {
            item.id: item.parent_id for item in self._stores.meta.list_note_folders(user_id=user_id)
        }
        seen: set[str] = set()
        cursor = parents.get(folder_id)
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            cursor = parents.get(cursor)
        return seen

    def _reject_duplicate_name(
        self, user_id: str | None, parent_id: str | None, name: str
    ) -> None:
        """同一层里不许重名。

        先查一次是为了**给出人的话**（数据库那条唯一索引（v14）报的是约束名，
        用户读到的是英文报错）；索引仍然是最后一道，两者是"提示"与"保证"的关系。
        比的是同级：两个不同的文件夹下面各自有一个「会议记录」是正常的。
        """
        siblings = self._stores.meta.list_note_folders(user_id=user_id)
        if any(item.parent_id == parent_id and item.name == name for item in siblings):
            raise ConflictError(f"这一层已经有一个同名文件夹：{name}")

    def _folder_id_for_owner(self, folder_id: str | None, user_id: str | None) -> str | None:
        """把"要放进哪个文件夹"校验成可写入的 ``folder_id``（``None`` 原样返回）。"""
        if folder_id is None:
            return None
        return self.get_folder_for_owner(folder_id, user_id).id

    def attach_to_kb(self, note_id: str, *, user_id: str | None, kb_id: str) -> NoteRecord:
        """把笔记作为一份 Markdown 文档加入知识库（走现有摄入流水线）。

        **内容哈希去重会生效**：同一份内容重复入库时 ``submit`` 返回已有文档，
        这里仍把 ``doc_id`` 回填到笔记上——用户要的是"这条笔记能在库里被检索到"，
        而不是"库里再多一份副本"。
        """
        if self._ingest is None or self._documents is None:
            raise InvalidRequestError("当前部署未接入摄入流水线，无法把笔记加入知识库")
        record = self.get_for_owner(note_id, user_id)
        body = record.content_md.strip()
        if not body:
            raise InvalidRequestError("笔记内容为空，无法加入知识库")
        filename = f"{record.title or '未命名笔记'}.md"
        outcome = self._ingest.submit(
            knowledge_base_id=kb_id,
            filename=filename,
            content=body.encode("utf-8"),
            mime_type="text/markdown",
            uploaded_by=user_id,
        )
        if not outcome.is_duplicate:
            self._documents.enqueue_ingest(outcome.document.id)
        self._stores.meta.attach_note_document(
            note_id, kb_id=kb_id, doc_id=outcome.document.id
        )
        return self.get(note_id)

    # ------------------------------------------------------------------ 配图

    def upload_image(
        self, note_id: str, *, user_id: str | None, filename: str, content: bytes
    ) -> tuple[str, str]:
        """存下一张笔记配图，返回 ``(存储路径, 文件名)``。

        文件名用**内容哈希**而不是用户给的原名：同一张图重复插入只存一份，
        也避免中文名/空格带来的转义问题。后缀只在白名单内保留。
        """
        self.get_for_owner(note_id, user_id)
        if not content:
            raise InvalidRequestError("图片内容为空")
        if len(content) > MAX_IMAGE_BYTES:
            raise InvalidRequestError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限")
        suffix = PurePosixPath(filename or "").suffix.lower()
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            allowed = "、".join(sorted(ALLOWED_IMAGE_SUFFIXES))
            raise InvalidRequestError(f"不支持的图片格式，请使用：{allowed}")
        name = f"{hashlib.sha256(content).hexdigest()[:16]}{suffix}"
        path = self._stores.objects.write(_image_key(note_id, name), content)
        return path, name

    def image_bytes(self, note_id: str, name: str) -> bytes:
        """读回一张配图。``name`` 只允许纯文件名，防止拼出越界路径。"""
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise NotFoundError(f"图片不存在：{name}")
        try:
            return self._stores.objects.read(_image_key(note_id, name))
        except FileNotFoundError as exc:
            raise NotFoundError(f"图片不存在：{name}") from exc

    # ------------------------------------------------------------------ 读

    def get(self, note_id: str) -> NoteRecord:
        record = self._stores.meta.get_note(note_id)
        if record is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        return record

    def get_for_owner(self, note_id: str, user_id: str | None) -> NoteRecord:
        """成员视角取笔记：**越主即 404**，不泄露"这条笔记存在但不是你的"。

        与对话同一口径（``ConversationService.get_for_owner``）：笔记是私有数据，
        用 403 会把别人的笔记 id 变成可探测的存在性 oracle。

        ``user_id=None`` 表示**不校验归属**（管理员/API Key 通道）——与 ``list`` 的
        过滤口径一致：列表能看到就必须点得开，否则会出现"列表里有、点进去 404"
        这种自相矛盾的状态（实测踩到）。
        """
        record = self.get(note_id)
        if user_id is not None and record.user_id != user_id:
            raise NotFoundError(f"笔记不存在：{note_id}")
        return record

    def list(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[NoteRecord], int]:
        """返回（当页笔记，过滤后的总数）。总数一次带回，省一次往返。

        ``folder_id`` / ``unfiled`` 是过滤器，**不校验文件夹是否存在**：
        别的标签页刚删掉那个文件夹时，这里返回空列表比抛 404 更好
        （界面自己会在下一轮读数里发现"这个文件夹没了"并退回"全部"）。
        """
        items = self._stores.meta.list_notes(
            user_id=user_id,
            query=query,
            tag=tag,
            folder_id=folder_id,
            unfiled=unfiled,
            limit=limit,
            offset=offset,
        )
        total = self._stores.meta.count_notes(
            user_id=user_id, query=query, tag=tag, folder_id=folder_id, unfiled=unfiled
        )
        return items, total

    def tags(self, *, user_id: str | None) -> list[tuple[str, int]]:
        return self._stores.meta.list_note_tags(user_id=user_id)


@dataclass(frozen=True, slots=True)
class NoteFolderOverview:
    """左栏文件夹树的读数（一次给全，见 ``NotesService.folder_overview``）。

    ``counts`` 只含**真实存在的文件夹**；某个文件夹一篇笔记都没有时它不在这个字典里
    （调用方按 0 处理）——聚合是按"有笔记的文件夹"分组的，凭空补齐零值只会让
    存储层替调用方猜它想要多少项。
    """

    folders: list[NoteFolderRecord] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    unfiled: int = 0
    total: int = 0


def _clean_folder_name(name: str) -> str:
    """压平空白并校验长度：名字进的是左栏那棵树，带换行会把行高撑歪。"""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise InvalidRequestError("文件夹名不能为空")
    if len(cleaned) > NOTE_FOLDER_NAME_MAX_CHARS:
        raise InvalidRequestError(f"文件夹名不能超过 {NOTE_FOLDER_NAME_MAX_CHARS} 个字符")
    return cleaned
