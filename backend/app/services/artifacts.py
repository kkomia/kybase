"""会话的文件区：Agent 做出来的文件**落在哪**、怎么浏览、什么时候**进知识库**（v0.26）。

"文件区"是这一层唯一的抽象——**每条会话恰好有一个**。浏览、上传、预览、下载四个动作
都走这一份判断，所以界面上的文件面板（文件抽屉）不需要知道自己在看的是哪一种。

**v0.55 改了一次落点划分**（用户报的"同一项目里，这次对话上传的文件和上次的分不开"）。
改之前是"挂了工作区就把上传也写进那个真实目录"，于是同一项目下所有会话共用一堆文件，
而"这份是谁传的"在数据里**根本不存在**——实测那条会话的产物记录数是 **0**：
后端连"这次传过什么"都没记，界面想分也分不了。现在按**用途**分：

- 什么 → 落在哪 → 为什么

- **用户上传**（"给它看看这份东西"）→ **对象存储**，按会话记账。
  它是**这次对话的输入**，不是用户的项目文件；按会话存才分得开。
- **Agent 产物**（导出的 docx 等）→ 挂了工作区就落**用户的真实目录**，没挂就落对象存储。
  那是"做出来的成果"，落进他的项目他打开就看得见。
- **显式入库** → 知识库（复制一份，见 :meth:`ArtifactService.ingest`）：用户点名要的才做。

于是"文件区"有**两个视图**（``list_files(scope=…)``）：

- ``conversation``（默认）：**这条会话的文件**——上传的与产出的都在这儿（按产物记录列，平铺）；
- ``project``：会话挂着的**工作区目录**（可进子目录）——那是用户自己的项目文件，
  只有挂了工作区才有这一档。

生命周期跟着落点走：对象存储那份**删会话时一起清掉**（只许诺会话期间有效），
工作区里那份**保留**（那是他项目里的一份真文件，删掉一次对话不该让它消失）。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.sandbox import resolve_in
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ARTIFACT_IN_WORKSPACE,
    SAFE_KEY_CHARS,
    ConversationArtifactRecord,
    StoreBundle,
)

__all__ = [
    "ARTIFACT_SCOPE_CONVERSATION",
    "ARTIFACT_SCOPE_PROJECT",
    "MAX_READ_BYTES",
    "ArtifactService",
    "ArtifactSpot",
    "FileEntry",
    "FileListing",
    "file_signature_resource",
    "safe_filename",
    "split_filename",
]

logger = logging.getLogger(__name__)

ARTIFACT_SCOPE_CONVERSATION = "conversation"
"""``list_files`` 的第一档视图（默认）：**这条会话的文件**——上传的与产出的都在，平铺。"""

ARTIFACT_SCOPE_PROJECT = "project"
"""``list_files`` 的第二档视图：会话挂着的**工作区目录**（可进子目录，只有工作区会话才有）。"""

CONVERSATION_PREFIX = "conversations"
"""对象存储里会话临时产物的前缀。**与内容寻址的 ``originals/`` 分开**：
后者是"入库的原文、按内容 hash 去重"，这里的是"还没决定要不要长期留下的东西"。
混在同一个前缀下，清理策略就没法只作用于其中一类。"""

_UNSAFE_IN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
"""文件名里不能出现的字符（含 Windows 的保留字符与控制字符）。

**这是一道安全边界，不是显示偏好**：文件名来自模型，它会写出 ``../../.env``
这类"它以为这个项目里有"的路径。落到工作区时，那是往用户的真实目录里写文件——
一次越界的拼接就能覆盖用户的项目文件。
"""

_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
"""Windows 的保留设备名。叫这个名字的文件在那台机器上根本建不出来，
而报错会发生在写盘那一刻（`OSError: [Errno 22]`），离"名字有问题"很远。"""

MAX_NAME_CHARS = 120

MAX_LIST_ENTRIES = 300
"""一层最多列多少条。真实项目目录里 `node_modules` 动辄几万条——
全列出来既慢又没人看，而**截断必须如实上报**（见 ``FileListing.truncated``）。"""

MAX_READ_BYTES = 32 * 1024 * 1024
"""预览/下载单个文件的上限。这个端点走 HTTP 响应体，不是流式；
一个 2GB 的模型文件会把进程内存吃干净。超过就明确拒绝并提示下载。"""


def split_filename(filename: str) -> tuple[str, str]:
    """把 ``报告.docx`` 拆成 ``("报告", "docx")``。没有扩展名时后缀为空串。"""
    name = (filename or "").strip()
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot + 1 :].lower()


def safe_filename(filename: str, *, fallback: str = "产物") -> str:
    """把模型给的文件名收敛成一个**能安全落盘**的名字。

    处理顺序有讲究：先取基名（``os.path.basename`` 对 ``..\\a`` 这类不完整路径
    不认，所以正反斜杠各切一次），再删非法字符，最后处理保留名与长度。
    **不能只做"删掉 ``..``"**：``....//x`` 删完还是能拼出越界路径。
    """
    base = (filename or "").strip().replace("\\", "/").split("/")[-1]
    stem, suffix = split_filename(base)
    stem = _UNSAFE_IN_NAME.sub("", stem).strip(" .")
    suffix = _UNSAFE_IN_NAME.sub("", suffix).strip(" .")
    if not stem:
        stem = fallback
    if stem.lower() in _RESERVED_NAMES:
        # 加下划线前缀而不是报错：模型给这个名字没有恶意，只是巧了
        stem = f"_{stem}"
    if len(stem) > MAX_NAME_CHARS:
        stem = stem[:MAX_NAME_CHARS]
    return f"{stem}.{suffix}" if suffix else stem


def _clean_segments(name: str) -> list[str]:
    """把 ``图表/第二季度.png`` 切成**逐段清洗过**的段（上传与"列哪一层"共用这一份）。

    三段清洗，缺一不可：

    1. 反斜杠统一成正斜杠（Windows 上传的那条路）；
    2. 丢掉空段与 ``.`` / ``..``——名字虽然只是"显示用"，但它会被下游拿去
       拼对象 Key、也可能被落盘路径用上，**这道闸不能靠"反正现在不落盘"**；
    3. 每段再删非法字符与首尾的点/空格（与 :func:`safe_filename` 同一份判据）。
    """
    segments = [
        _UNSAFE_IN_NAME.sub("", segment).strip(" .")
        for segment in (name or "").replace("\\", "/").split("/")
    ]
    return [
        segment[:MAX_NAME_CHARS]
        for segment in segments
        if segment and segment not in (".", "..")
    ]


def _upload_name(filename: str) -> str:
    """上传文件的名字：**允许保留相对路径**（v0.55 起支持上传文件夹）。

    与 :func:`safe_filename` 的差别只有一处：它保留 ``/``。上传文件夹时前端把
    "目录/子目录/文件"整条相对路径交过来（`Composer.asRelativePath` 就是这么改名的），
    丢掉路径就等于把文件夹拍平——而用户选的正是文件夹，他要的是那棵结构。
    """
    kept = _clean_segments(filename)
    if not kept:
        return "文件"
    # 总长也收一道：一个几千层的路径本身就不该进这个系统（它只用来显示与拼 Key）
    return "/".join(kept)[:MAX_NAME_CHARS * 4]


def _upload_prefix(path: str) -> str:
    """界面点开的那一层目录 → **名字前缀**（D20，与 `_upload_name` 同一套逐段清洗）。

    会话文件区的层级**就在名字里**（`_upload_name` 保留的那条相对路径），所以"列出哪一层"
    等于"取名字的前缀"。归一用的是同一份判据：``a//b/../c`` 与上传时的 ``a/b/c`` 落成
    同一个前缀，于是"界面能点进去的目录"与"文件名里真有的目录"永远对得上。
    """
    return "/".join(_clean_segments(path))


@dataclass(frozen=True, slots=True)
class ArtifactSpot:
    """产物该落在哪儿——**这次落点的全部信息**。

    单独一个类型而不是在服务里现算：模型看到的说明（"已存到工作区「我的项目」"）
    与真正写文件用的是同一份判断，两者不可能漂。
    """

    conversation_id: str
    workspace_id: str | None = None
    root: Path | None = None
    """工作区目录。给了就落真实文件，否则落对象存储。"""
    workspace_name: str = ""

    @property
    def in_workspace(self) -> bool:
        return self.root is not None

    @property
    def label(self) -> str:
        """界面上那句"存到哪儿了"。**用户看得懂的说法**，不是路径。"""
        if self.root is None:
            return "本会话"
        return f"工作区「{self.workspace_name}」" if self.workspace_name else "工作区"

    @property
    def mode(self) -> str:
        """给界面判断用的一档：``workspace``（能进子目录）或 ``object``（平铺）。"""
        return ARTIFACT_IN_WORKSPACE if self.root is not None else ARTIFACT_IN_OBJECTS


@dataclass(frozen=True, slots=True)
class FileEntry:
    """文件面板里的一行。

    ``key`` 是**在这个文件区里唯一指代它**的东西，两种模式不一样：

    - 工作区：相对工作区根的路径（``报告/初稿.docx``）——它同时是磁盘上的真实位置；
    - 临时区：产物 id（``art_xxx``）——那里是平铺的，文件名可以重名。

    界面把它当作不透明字符串用（下载、预览都原样回传），所以两套 key 不会串。
    """

    key: str
    name: str
    is_dir: bool = False
    size_bytes: int = 0
    modified_at: datetime | None = None
    kind: str = ""
    """扩展名小写（``docx`` / ``md`` / ``png``…）。**界面按它选渲染器**，
    所以它由服务端算好——两处各算一遍迟早会分叉。"""


@dataclass(frozen=True, slots=True)
class FileListing:
    """一层目录（临时区是唯一的一层）。"""

    mode: str
    label: str
    path: str = ""
    parent: str | None = None
    entries: tuple[FileEntry, ...] = ()
    truncated: bool = False
    """条目被截断过。**要如实告诉界面**——否则"这个项目只有 300 个文件"
    与"我只给你看了 300 个"看起来一模一样。"""


class ArtifactService:
    """产物的落盘、读取、入库与清理。

    ``ingest`` / ``documents`` 是**可选能力**：不接摄入流水线时，落盘、下载、清理
    照常工作，只有"存进知识库"那一个动作会明确报错。这与笔记那边同一套口径——
    单测与不配模型的部署不该因为"某个旁路能力没接"就整个用不了。
    """

    def __init__(self, stores: StoreBundle, *, ingest=None, documents=None) -> None:  # type: ignore[no-untyped-def]
        self._stores = stores
        self._ingest = ingest
        self._documents = documents

    # ------------------------------------------------------------------ 落点

    def spot_for(self, conversation_id: str) -> ArtifactSpot:
        """这条会话的产物该落在哪儿。

        **工作区目录不存在时明确报错，不悄悄改落点**。目录没了说明用户的项目
        出了问题，那是他必须知道的事；替他把文件塞进临时区，他会以为"东西在项目里"，
        而这正是"落点不可预期"那类问题的形状。
        """
        conversation = self._stores.meta.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        if not conversation.workspace_id:
            return ArtifactSpot(conversation_id=conversation_id)
        workspace = self._stores.meta.get_workspace(conversation.workspace_id)
        if workspace is None:
            # 走不到这里：外键是 `ON DELETE SET NULL`，工作区一删，会话就退回未归档。
            # 留着是为了"记录对不上"时退到临时区，而不是抛一个用户看不懂的错。
            return ArtifactSpot(conversation_id=conversation_id)
        root = Path(workspace.root_path)
        if not root.is_dir():
            raise InvalidRequestError(
                f"这个会话挂在工作区「{workspace.name}」上，但它的目录不在了"
                f"（{workspace.root_path}）。产物没有落盘——请在工作区设置里检查这个路径，"
                "或者把这次要的东西改成存进知识库"
            )
        return ArtifactSpot(
            conversation_id=conversation_id,
            workspace_id=workspace.id,
            root=root,
            workspace_name=workspace.name,
        )

    # ------------------------------------------------------------------ 写

    def save(
        self,
        *,
        conversation_id: str,
        filename: str,
        content: bytes,
        kind: str,
        owner_id: str | None = None,
    ) -> ConversationArtifactRecord:
        """把一份**产物**落盘并登记。

        ``kind`` 是扩展名（``docx`` / ``xlsx`` / ``pptx`` / ``pdf``）——
        **以调用方生成的内容为准，不是以文件名的后缀为准**：文件名是模型写的，
        而字节是我们自己造的；两者不一致时，信字节。

        落点**按用途**（v0.55 的划分，见模块头）：挂了工作区 → 用户的真实目录
        （他打开项目就看得见）；没挂 → 对象存储。**用户上传不走这里**——
        那是 :meth:`write_file`，它一律按会话记账。
        """
        spot = self.spot_for(conversation_id)
        name = safe_filename(filename, fallback=f"产物.{kind}" if kind else "产物")

        if spot.root is not None:
            # `safe_filename` 已经保证名字里没有路径分隔符，`resolve_in` 是**第二道**：
            # 往用户的真实目录里写文件的这条路上，一道校验不够（见 sandbox.py 的说明）。
            path = _write_new(resolve_in(spot.root, name), content)
            return self._stores.meta.create_artifact(
                ConversationArtifactRecord(
                    id=f"art_{uuid.uuid4().hex[:12]}",
                    conversation_id=conversation_id,
                    name=name,
                    format=kind,
                    size_bytes=len(content),
                    storage=ARTIFACT_IN_WORKSPACE,
                    location=str(path),
                    workspace_id=spot.workspace_id,
                    owner_id=owner_id,
                )
            )
        return self._record_bytes(
            conversation_id=conversation_id,
            name=name,
            content=content,
            kind=kind,
            owner_id=owner_id,
        )

    def _record_bytes(
        self,
        *,
        conversation_id: str,
        name: str,
        content: bytes,
        kind: str,
        owner_id: str | None = None,
    ) -> ConversationArtifactRecord:
        """把字节落进**对象存储**并登记一条产物记录（v0.55）。

        :meth:`save`（产物，没挂工作区那半边）与 :meth:`write_file`（上传，恒走这条）
        共用它：Key 规则、落点、记账三件事只写一份，两边各抄一遍迟早会漂。
        """
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        key = _object_key(conversation_id, artifact_id, name)
        location = self._stores.objects.write(key, content)
        return self._stores.meta.create_artifact(
            ConversationArtifactRecord(
                id=artifact_id,
                conversation_id=conversation_id,
                name=name,
                format=kind,
                size_bytes=len(content),
                storage=ARTIFACT_IN_OBJECTS,
                location=location,
                workspace_id=None,
                owner_id=owner_id,
            )
        )

    # ------------------------------------------------------------------ 文件区（浏览）

    def list_files(
        self, conversation_id: str, path: str = "", *, scope: str = ARTIFACT_SCOPE_CONVERSATION
    ) -> FileListing:
        """列文件区的一层。``scope`` 两档（v0.55，见模块头）：

        - ``conversation``（默认）：**这条会话的文件**——上传的与产出的都在
          （按产物记录列，所以挂不挂工作区都一样）。``path`` 从 D20 起也认：
          上传文件夹时名字里就带着相对路径（``图表/第二季度.png``），
          所以这一档能进子目录，与项目档同一个形状；
        - ``project``：会话挂着的**工作区目录**（可进子目录）。只有挂了工作区才有，
          没挂时**明确说清**，而不是悄悄给一个空列表（空列表会被读成"这个项目里没有文件"）。
        """
        spot = self.spot_for(conversation_id)
        if scope == ARTIFACT_SCOPE_PROJECT:
            if spot.root is None:
                raise InvalidRequestError("这条会话没有挂工作区，没有「项目文件」可看")
            return self._list_directory(spot, path)
        return self._list_conversation(spot, path)

    def _list_conversation(self, spot: ArtifactSpot, path: str = "") -> FileListing:
        """这条会话的产物记录（上传的 + 产出的），**按名字里的目录分层列**（D20）。

        层级**已经在名字里**（见 `_upload_name`），所以这里不新增字段、也不改记录：
        把"当前目录那一段"切出来，目录项是**合成的**（它没有记录、也就没有产物 id），
        文件项的 key 仍是产物 id——预览、下载、`read_file` 那条路一行都不用改。

        `path` 来自浏览器，先过 `_upload_prefix` 归一（丢空段与 ``.`` / ``..``、逐段清
        非法字符）：会话档没有磁盘路径可越界，但归一之后"列的目录"与"文件名里的目录"
        才是同一套判据。列不存在的目录给**空列表**（不是报错）：上一次进来之后
        那些层可能已经被删会话清掉了，而空列表会被读成"这条会话里没有文件"——
        所以 `mode` / `label` 照旧说着这是哪一档，界面据此说清。
        """
        prefix = _upload_prefix(path)
        directories: dict[str, FileEntry] = {}
        files: list[FileEntry] = []
        for record in self._stores.meta.list_artifacts(spot.conversation_id):
            rest = _under_prefix(record.name, prefix)
            if rest is None:
                continue
            head, slash, _tail = rest.partition("/")
            if slash:
                # 合成的目录项：key 是它在这一档里的路径（与项目档的 key 同一个形状），
                # 于是界面那套"点目录 → path = entry.key"两档通用
                directories.setdefault(
                    head,
                    FileEntry(key=_join_key(prefix, head), name=head, is_dir=True, kind="dir"),
                )
                continue
            files.append(
                FileEntry(
                    key=record.id,
                    name=head,
                    size_bytes=record.size_bytes,
                    modified_at=record.created_at,
                    kind=record.format or _suffix_of(head),
                )
            )
        entries = [*directories.values(), *files]
        # 目录在前、各自按名字（不区分大小写）——与 `_list_directory` 同一个习惯
        entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        truncated = len(entries) > MAX_LIST_ENTRIES
        return FileListing(
            mode=ARTIFACT_IN_OBJECTS,
            label="本会话的文件",
            path=prefix,
            parent=_parent_of_prefix(prefix),
            entries=tuple(entries[:MAX_LIST_ENTRIES]),
            truncated=truncated,
        )

    def _list_directory(self, spot: ArtifactSpot, path: str) -> FileListing:
        directory = self._directory(spot, path)
        try:
            raw = list(directory.iterdir())
        except OSError as exc:
            raise InvalidRequestError(f"读不了这个目录（{path or '根目录'}）：{exc}") from exc

        entries: list[FileEntry] = []
        for item in raw:
            if item.name.startswith("."):
                # 隐藏项不进列表：`.git`、`.venv` 这些在"我的文件"里没有意义，
                # 而它们动辄上万个（node_modules 同理，那个不是隐藏项，见下面的截断）
                continue
            try:
                stat = item.stat()
            except OSError:
                # 断掉的符号链接、权限不足的条目：跳过它而不是整层失败
                continue
            is_dir = item.is_dir()
            entries.append(
                FileEntry(
                    key=_relative_key(spot.root, item),
                    name=item.name,
                    is_dir=is_dir,
                    size_bytes=0 if is_dir else stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
                    kind="dir" if is_dir else _suffix_of(item.name),
                )
            )
        # 目录在前、各自按名字（不区分大小写）——与文件管理器一个习惯
        entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        truncated = len(entries) > MAX_LIST_ENTRIES
        return FileListing(
            mode=spot.mode,
            label=spot.label,
            path=_relative_key(spot.root, directory) if directory != spot.root else "",
            parent=_parent_of(spot.root, directory),
            entries=tuple(entries[:MAX_LIST_ENTRIES]),
            truncated=truncated,
        )

    def _directory(self, spot: ArtifactSpot, path: str) -> Path:
        root = spot.root
        if root is None:  # 只有工作区模式会走到这里，调用方已判过
            raise InvalidRequestError("这条会话没有目录（文件在临时区里）")
        if not path.strip():
            return root
        target = resolve_in(spot.root, path)
        if not target.is_dir():
            raise NotFoundError(f"目录不存在：{path}")
        return target

    def read_file(self, conversation_id: str, key: str) -> tuple[bytes, str]:
        """取一份文件的字节与显示名（预览与下载共用）。

        两种 key 都认（v0.55 起两条视图并存）：

        - **产物 id**（``art_…``，``scope=conversation`` 那一档给的）：按记录取，
          字节可能在对象存储、也可能在用户的目录里（导出的产物）；
        - **工作区里的相对路径**（``scope=project`` 那一档给的）：落到磁盘上读，
          必须过 ``resolve_in``——key 是从浏览器回来的，而它最终拼成了文件路径。

        顺序是"**先记录、后路径**"：产物 id 是 ``art_`` 前缀的固定形状，
        相对路径不可能长成那样，两者不会撞。
        """
        record = self._stores.meta.get_artifact(key)
        if record is not None:
            if record.conversation_id != conversation_id:
                # 越会话取产物：不暴露"它在别处存在"，与 API 层同一口径
                raise NotFoundError(f"文件不存在：{key}")
            return self.content(record), record.name

        spot = self.spot_for(conversation_id)
        target = resolve_in(spot.root, key) if spot.root is not None else None
        if target is None or not target.is_file():
            raise NotFoundError(f"文件不存在：{key}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise InvalidRequestError(
                f"这个文件 {size // (1024 * 1024)}MB，超过预览上限 "
                f"{MAX_READ_BYTES // (1024 * 1024)}MB。下载它用本机的程序打开"
            )
        try:
            return target.read_bytes(), target.name
        except OSError as exc:
            raise InvalidRequestError(f"读不了这个文件（{key}）：{exc}") from exc

    def write_file(
        self, conversation_id: str, *, path: str = "", filename: str, content: bytes
    ) -> FileEntry:
        """往**这条会话的文件区**里放一份文件（界面上的"上传"）。

        **一律落对象存储、按会话记账**（v0.55）。改之前是"挂了工作区就写进那个真实
        目录"，后果是同一项目下所有会话共用一堆文件，而"这份是谁传的"没有任何记录
        （用户报的"上传的文件分不开"就是它，实测那条会话的产物记录数是 0）。
        上传是**这次对话的输入**，按会话存才分得开；该进项目目录的是产物
        （:meth:`save`）与用户显式的动作，不是"随手传上来给它看的文件"。

        ``filename`` **可以带相对路径**（``图表/第二季度.png``）：前端上传文件夹时
        靠它保留目录结构；清洗在 :func:`_upload_name` 里逐段做（去非法字符、丢 ``..``）。

        ``path`` 是**旧接口留下的目录参数**：会话文件区是平铺的，收下但不用——
        留着只是为了旧客户端多传一个字段时不至于报错。
        """
        name = _upload_name(filename)
        record = self._record_bytes(
            conversation_id=conversation_id,
            name=name,
            content=content,
            kind=_suffix_of(name),
        )
        return FileEntry(
            key=record.id,
            name=record.name,
            size_bytes=record.size_bytes,
            modified_at=record.created_at,
            kind=record.format,
        )

    def import_from_project(self, conversation_id: str, path: str) -> FileEntry:
        """把**这条会话自己的工作区**里的一份文件复制进会话文件区（D20）。

        用户的原话是"项目文件也没法移到会话"：项目档只是只读地列着他那个真实目录，
        而"这次对话要用这份文件"的唯一替代是把它拖进输入框插一条 ``@路径``——
        那条引用只到输入框，文件区里看不见它，换一条会话更是拿不到。

        五条分寸：

        1. **源路径只走 `resolve_in`**（与读文件、预览同一条闸）：绝对路径、``..``、
           解析后跳出根（含**符号链接**指到外面）一律拒——路径是浏览器回来的，
           这里不许自己拼 `Path`；
        2. **只认这条会话自己的工作区**：``spot`` 是这条会话的落点，别的工作区够不着
           （会话之间因此不会互相取文件）；
        3. **复制，不是搬**：项目目录里那份一个字节都不动（那是用户的真文件），
           会话区这份是对象存储里的新记录，按会话记账、删会话时一起清；
        4. **大小沿用 `MAX_READ_BYTES`**（与预览同一个数）：这条链路把整份字节读进内存，
           没有分片；超了明确说清，而不是让服务端自己去扛一个 2GB 的文件；
        5. **同名不覆盖、按规则改名**：会话区是给人翻的，两行一模一样的名字分不出哪份是
           刚取进来的，所以退到 ``名字 (2).ext``（与产物落盘 `_write_new` 同一个习惯）。
           ——注意这与"上传"那条路不同：上传是用户自己一个个选的，他看得见自己传了什么；
           而这一步是**从项目里复制**，同名多半是"上次已经取过一份"。
        """
        spot = self.spot_for(conversation_id)
        if spot.root is None:
            raise InvalidRequestError("这条会话没有挂工作区，没有「项目文件」可取")
        source = resolve_in(spot.root, path)
        if not source.is_file():
            raise NotFoundError(f"文件不存在：{path}")
        size = source.stat().st_size
        if size > MAX_READ_BYTES:
            raise InvalidRequestError(
                f"这个文件 {size // (1024 * 1024)}MB，超过取用上限 "
                f"{MAX_READ_BYTES // (1024 * 1024)}MB。先在项目里把它拆小，"
                "或者用「下载」在别处打开"
            )
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise InvalidRequestError(f"读不了这个文件（{path}）：{exc}") from exc

        taken = {
            record.name.lower() for record in self._stores.meta.list_artifacts(conversation_id)
        }
        name = _unique_name(_upload_name(_relative_key(spot.root, source)), taken)
        record = self._record_bytes(
            conversation_id=conversation_id,
            name=name,
            content=content,
            kind=_suffix_of(name),
        )
        return FileEntry(
            key=record.id,
            name=record.name,
            size_bytes=record.size_bytes,
            modified_at=record.created_at,
            kind=record.format,
        )

    # ------------------------------------------------------------------ 读

    def get(self, artifact_id: str) -> ConversationArtifactRecord:
        record = self._stores.meta.get_artifact(artifact_id)
        if record is None:
            raise NotFoundError(f"产物不存在：{artifact_id}")
        return record

    def list_for_conversation(self, conversation_id: str) -> list[ConversationArtifactRecord]:
        return self._stores.meta.list_artifacts(conversation_id)

    def label_for(self, record: ConversationArtifactRecord) -> str:
        """这份产物在哪儿——**给用户看的那句话**（不是路径）。"""
        if record.storage != ARTIFACT_IN_WORKSPACE:
            return "本会话"
        workspace = (
            self._stores.meta.get_workspace(record.workspace_id) if record.workspace_id else None
        )
        return f"工作区「{workspace.name}」" if workspace is not None else "工作区"

    def describe(self, record: ConversationArtifactRecord) -> dict[str, object]:
        """给界面用的那份形状。

        **SSE 的步骤事件与 REST 的产物列表共用这一个函数**：同一份产物在两处
        必须是同一个形状，否则"刷新之后卡片显示的与刚才不一样"就成了必然。
        """
        payload: dict[str, object] = {
            "artifact_id": record.id,
            "name": record.name,
            "size_bytes": record.size_bytes,
            "format": record.format,
            "storage": record.storage,
            "where": self.label_for(record),
        }
        if record.storage == ARTIFACT_IN_WORKSPACE:
            # 只有工作区那份的"路径"对用户有意义（他要去那儿拿）。
            # 对象存储的 Key 对他没有用处，露出来只会让人以为那是个能点的地址。
            payload["path"] = record.location
        if record.knowledge_base_id:
            payload["knowledge_base_id"] = record.knowledge_base_id
        if record.document_id:
            payload["document_id"] = record.document_id
        return payload

    def content(self, record: ConversationArtifactRecord) -> bytes:
        """取回产物的字节。

        两条落点各读各的：工作区那份从磁盘读，对象存储那份从对象存储读。
        **路径就在记录里**，不再做一次"猜它在哪"的判断。
        """
        if record.storage == ARTIFACT_IN_WORKSPACE:
            path = Path(record.location)
            if not path.is_file():
                raise NotFoundError(f"文件不在了：{record.location}（可能已被移动或删除）")
            return path.read_bytes()
        return self._stores.objects.read(record.location)

    # ------------------------------------------------------------------ 入库（显式）

    def ingest(
        self,
        record: ConversationArtifactRecord,
        *,
        knowledge_base_id: str,
        uploaded_by: str | None = None,
    ) -> tuple[str, bool]:
        """把这份产物**复制一份**进知识库，返回 ``(文档 id, 是不是库里的已有内容)``。

        **复制，不是搬家**：产物还在原处（工作区里那份仍在工作区），知识库里多一份
        可检索的。用户要的这两件事本来就不冲突，搬家反而会让"我刚导出的文件去哪了"
        变成一个新问题。

        走的是与界面上传**同一条链路**（``IngestService.submit``）：不另开一条
        "把产物写进库"的通道，否则库里会出现两种来源、两种格式。
        """
        if self._ingest is None or self._documents is None:
            raise RuntimeError("这条部署没有接上摄入链路，产物无法入库")
        content = self.content(record)
        outcome = self._ingest.submit(
            knowledge_base_id=knowledge_base_id,
            filename=record.name,
            content=content,
            uploaded_by=uploaded_by,
        )
        if not outcome.is_duplicate:
            self._documents.enqueue_ingest(outcome.document.id)
        self._stores.meta.mark_artifact_ingested(
            record.id, knowledge_base_id=knowledge_base_id, document_id=outcome.document.id
        )
        record.knowledge_base_id = knowledge_base_id
        record.document_id = outcome.document.id
        return outcome.document.id, outcome.is_duplicate

    # ------------------------------------------------------------------ 清理

    def discard_for_conversation(self, conversation_id: str) -> int:
        """删会话之前清掉**临时**产物（对象存储那些），返回清掉的份数。

        **工作区里的那些一份都不动**：它们已经在用户的目录里，是他的文件。
        删一次对话顺手删掉他项目里的 docx，是最不该有的"贴心"。
        """
        removed = 0
        for record in self._stores.meta.list_artifacts(conversation_id):
            if record.storage != ARTIFACT_IN_OBJECTS:
                continue
            try:
                self._stores.objects.delete(record.location)
            except Exception:
                logger.warning("清理会话产物失败：%s", record.location, exc_info=True)
                continue
            removed += 1
        return removed

    # ------------------------------------------------------------------ 下载

    def file_url(
        self,
        conversation_id: str,
        key: str,
        *,
        secret: str,
        ttl_seconds: int | None = None,
        inline: bool = False,
    ) -> tuple[str, int]:
        """签发一条短期文件链接（与文档下载同一套签名，见 ``core/signing.py``）。

        **为什么不能直接给路径**：工作区那份是服务器上的绝对路径、临时区那份是对象
        存储的 Key，两者都只有服务端读得到；而预览（`<iframe>`、`<img>`）与下载
        （`<a download>`）都带不了 Authorization 头。签名 URL 把"你有权取这份"
        编码进链接本身，并给它一个到期时间。

        ``inline`` 只影响响应头、**不参与签名**（签名绑的是"哪条会话的哪份文件"）：
        它不是一个权限参数——能不能内联渲染由协议层按后缀复核，见 API 里那张白名单。
        """
        from app.core.signing import DEFAULT_TTL_SECONDS, sign_resource

        signature, expires_at = sign_resource(
            file_signature_resource(conversation_id, key),
            secret,
            ttl_seconds=ttl_seconds or DEFAULT_TTL_SECONDS,
        )
        url = (
            f"/api/v1/conversations/{conversation_id}/files/content"
            f"?key={_quote_key(key)}&expires={expires_at}&signature={signature}"
        )
        if inline:
            url += "&disposition=inline"
        return url, expires_at


def file_signature_resource(conversation_id: str, key: str) -> str:
    """被签名的资源标识。

    **必须带上会话 id**：内容端点是 ``/conversations/{会话}/files/content?key=…``，
    两者都在 URL 里。只签 key 的话，签名与 URL 的另一半没有绑定关系——
    换一条会话去请求同一份签名，端点会先过一遍"这条会话里有没有这个文件"，
    而这层校验本该由签名本身保证。

    **key 里可能有斜杠**（工作区里的相对路径），所以拼进来之前要转义：
    不转的话 ``a/b`` 与 ``a:b`` 会撞成同一个资源标识，一条签名能换到另一份文件。
    """
    return f"file:{conversation_id}:{_quote_key(key)}"


def _quote_key(key: str) -> str:
    from urllib.parse import quote

    return quote(key, safe="")


def _suffix_of(name: str) -> str:
    """扩展名（小写、不带点）。界面按它选渲染器。"""
    return split_filename(name)[1]


def _relative_key(root: Path, target: Path) -> str:
    """磁盘路径 → 文件区里的 key（相对工作区根，用正斜杠）。

    **统一用正斜杠**：URL 查询串与前端都用它，Windows 的反斜杠在这里只会制造
    "同一份文件在界面上有两个 key"的问题。
    """
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        # 不该发生：调用方都先过了 `resolve_in`。真发生时报出去，而不是编一个 key
        raise InvalidRequestError(f"路径不在这个工作区里：{target}") from None


def _parent_of(root: Path, directory: Path) -> str | None:
    """上一层目录的 key。

    - 已经在根上 → ``None``（界面据此隐藏"返回上级"）；
    - 上一层就是根 → **空串**（根目录的 key 是 ``""``，与列根目录时的 ``path`` 一致）。
    """
    if directory == root:
        return None
    parent = directory.parent
    return "" if parent == root else _relative_key(root, parent)


def _join_key(prefix: str, name: str) -> str:
    """把当前层前缀与一个名字拼成 key（前缀为空时就是名字本身）。"""
    return f"{prefix}/{name}" if prefix else name


def _under_prefix(name: str, prefix: str) -> str | None:
    """``name`` 在 ``prefix`` 这一层之下的部分；不在这一层之下给 ``None``。

    **按段比而不是 `startswith`**：``报告`` 与 ``报告集`` 是两回事，
    `startswith("报告/")` 那一下必须带上分隔符才分得开（这里用前缀相等或前缀 + ``/``）。
    """
    if not prefix:
        return name
    if name == prefix:
        # 记录的名字正好等于这一层（理论上是"没有文件名"的形状）：不列，
        # 否则它会在自己那一层里变成一个没有名字的行
        return None
    head = f"{prefix}/"
    return name[len(head) :] if name.startswith(head) else None


def _parent_of_prefix(prefix: str) -> str | None:
    """会话档"上一层目录"的 key：根那一层是 ``None``（与 :func:`_parent_of` 同口径）。"""
    return "/".join(prefix.split("/")[:-1]) if prefix else None


def _unique_name(name: str, taken: set[str]) -> str:
    """同名时退到 ``名字 (2).ext``（与 `_write_new` 同一个习惯）。

    ``taken`` 是这条会话里**已经用掉的名字**（全路径，含目录），比较不区分大小写：
    会话区最终是给人翻的，``报告.md`` 与 ``报告.MD`` 并排两行同样分不出来。
    """
    if name.lower() not in taken:
        return name
    stem, suffix = split_filename(name)
    for index in range(2, 1000):
        candidate = f"{stem} ({index}){f'.{suffix}' if suffix else ''}"
        if candidate.lower() not in taken:
            return candidate
    raise InvalidRequestError(f"这个名字的同名文件太多了：{name}")


def _object_key(conversation_id: str, artifact_id: str, name: str) -> str:
    """对象存储里的 Key：``conversations/<会话>/<产物>.<后缀>``。

    用产物 id 而不是文件名当主体：同名文件（模型很爱叫 ``报告.docx``）不会互相覆盖，
    而显示名存在库里、不依赖 Key。Key 里保留后缀是为了**直接看桶时认得出类型**。
    """
    safe_conversation = "".join(ch for ch in conversation_id if ch in SAFE_KEY_CHARS)
    if not safe_conversation:
        raise InvalidRequestError(f"会话 id 不含可用字符：{conversation_id!r}")
    _, suffix = split_filename(name)
    tail = f".{suffix}" if suffix else ""
    return f"{CONVERSATION_PREFIX}/{safe_conversation}/{artifact_id}{tail}"


def _write_new(target: Path, content: bytes) -> Path:
    """写入一份**不会覆盖既有文件**的新文件，返回实际落点。

    同名时退到 ``名字 (2).docx``，与系统的"另存为"同一套习惯。
    **直接覆盖是不可接受的**：用户目录里那个 ``报告.docx`` 可能比这次导出的重要得多，
    而"导出一次，旧文件没了"没有任何提示能补救。

    用**独占创建**（``xb``）而不是"先查在不在、再写"：那两步之间隔着一次系统调用，
    而工具循环会把一批调用并发跑（v0.27）——两个同名导出会同时查到"不存在"，
    然后后写的那份把先写的**悄悄盖掉**。``xb`` 撞了就换下一个候选名，
    谁先建成谁算数：这是文件系统替我们做的原子判定，比我们自己加锁更可靠
    （它还挡住了另一个进程）。
    """
    for candidate in _candidates(target):
        try:
            with candidate.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            continue
        except OSError as exc:
            # 权限、磁盘满、路径过长……**报出来**，不要让模型以为文件已经在那儿了
            raise InvalidRequestError(
                f"写不进去（{candidate}）：{exc}。对方需要检查这个目录的权限或磁盘空间"
            ) from exc
        return candidate
    # 兜底：不给出无限循环的机会（一个目录里 999 个同名文件本身就该有人来看看了）
    raise InvalidRequestError(f"这个目录里同名文件太多了：{target.name}")


def _candidates(target: Path) -> Iterator[Path]:
    """先试原名，再试 ``名字 (2)``、``名字 (3)``……（上限见 ``_write_new``）。"""
    yield target
    stem, suffix = split_filename(target.name)
    tail = f".{suffix}" if suffix else ""
    for index in range(2, 1000):
        yield target.with_name(f"{stem} ({index}){tail}")
