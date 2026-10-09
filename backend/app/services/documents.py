"""文档与任务服务（M4 支撑）。

API 层只做协议适配，所以"列文档""入队""查任务"这些动作都收在这里，
路由里不出现任何存储调用（工程规范 §3.3 的 L1 规则）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.core.downloads import media_type_of, suffix_of
from app.core.exceptions import (
    ConflictError,
    InvalidRequestError,
    KylabError,
    NotFoundError,
    UnsupportedContentError,
)
from app.core.page_markers import strip_page_markers
from app.core.signing import DEFAULT_TTL_SECONDS, sign_resource
from app.models.enums import DocumentStage, TaskKind, TaskState
from app.pipeline.state_machine import can_transition
from app.services.observability import ObservabilityService
from app.services.timeline import DocumentProgress, DocumentTimeline, build_timeline, progress_of
from app.storage.base import (
    ChunkRecord,
    DocumentPartRecord,
    DocumentRecord,
    StoreBundle,
    TaskRecord,
)

__all__ = [
    "DocumentContent",
    "DocumentService",
    "TaskCancelItem",
    "signature_resource",
]

ACTIVE_TASK_STATES = (TaskState.PENDING, TaskState.RUNNING)
"""这两个状态下重复入队没有意义——同一文档不该同时跑两个摄入任务。"""

DOCUMENT_NAME_MAX_CHARS = 200
"""文件名长度上限。比目录名（64）宽得多：真文件名的确可以很长，
这里只是拦住"把一整段正文粘进文件名"那种。"""


@dataclass(frozen=True, slots=True)
class TaskCancelItem:
    """撤销排队任务的一条结果。``error`` 为空即成功。"""

    task_id: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """一次下载的内容与元信息。"""

    data: bytes
    filename: str
    media_type: str
    kind: str = "binary"
    """怎么展示它：``markdown`` / ``pdf`` / ``image`` / ``docx`` / ``pptx`` /
    ``excel`` / ``binary``。

    判定放在**服务层**而不是前端：前端不该为了"该不该渲染"去猜文件后缀；
    而且下载与预览两条路径必须给出同一答案——同一次请求能渲染、下载却变二进制会很怪。

    Office 三件套单列 kind，是因为它们要在**前端**用库渲染（浏览器不会原生显示），
    与 pdf/image 那种"给个地址让浏览器自己画"不是一回事。
    """


#: 可以在浏览器里直接渲染的图片格式
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}
)


#: 能交给前端库渲染的 Office 格式（OOXML）。**不含** .doc/.ppt/.xls：
#: 那是 OLE2 二进制，预览库解析不了，硬试只会得到一句 zip 报错，不如直接给下载。
_OFFICE_KINDS = {".docx": "docx", ".pptx": "pptx", ".xlsx": "excel"}

def content_kind(filename: str, *, has_markdown: bool = False) -> str:
    """这份内容该怎么展示。

    **有解析产物就优先当 markdown**：那是流水线归一化后的文本，
    是检索真正依据的东西——用户要核对"解析对不对"，看它比看原始版式更直接。
    PDF/Office 只有在没有产物时才回落到"原始版式"。

    想看原始版式而不看解析文本时，走 ``DocumentService.original_view``——
    那里的 ``has_markdown`` 恒为 False，因为用户已经明确说了"我要看原文"。
    """
    if has_markdown:
        return "markdown"
    suffix = suffix_of(filename)
    if suffix == ".pdf":
        return "pdf"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _OFFICE_KINDS:
        return _OFFICE_KINDS[suffix]
    if suffix in {".md", ".markdown", ".txt", ".text", ".csv", ".json", ".log"}:
        # 纯文本类即使没走完整流水线也可以直接按文本读
        return "markdown"
    return "binary"


# 媒体类型判定与 `Content-Disposition` 已上移到 `app/core/downloads.py`（共享底座，
# 见那里的模块头）：KB 侧下文档与 Agent 侧导出会话要用同一套判定，各写一遍迟早只改一边。
# 这里 import 进来直接用（`suffix_of` 给 `content_kind`，`media_type_of` 给下载那条路）。


def signature_resource(document_id: str, fmt: str) -> str:
    """被签名的资源标识。

    **必须带上 ``fmt``**：否则一条"下载 Markdown"的链接会被改个参数拿去下原文，
    而两者是不同的东西（原文可能含用户不想外传的原始版式）。

    公开导出：API 层校验签名时要用**同一个**函数算出被签内容，
    两处各写一遍字符串拼接迟早会漂（漂了就是全员下载 401）。
    """
    return f"document:{document_id}:{fmt}"


def _stem(name: str) -> str:
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


class DocumentService:
    """文档查询、重跑入队与任务查询。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        observability: ObservabilityService | None = None,
    ) -> None:
        self._stores = stores
        # "停滞"这一档必须与任务中心用**同一个判据**（租约还在不在续），所以注入
        # 可观测性服务而不是自己比时间戳——两套阈值就是同一件事有两种说法。
        # 不给（手工构造的测试）就退化成"不判停滞"，其余字段照常。
        self._observability = observability

    # ------------------------------------------------------------------ 文档

    def list_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """列文档；可按目录、文件名、状态、来源收窄，并按 ``limit``/``offset`` 分页。

        ``folder_id`` 会校验它属于这个库：传一个别的库的目录 id 时，静默返回空列表
        会让人以为"这个目录是空的"，而不是"你查错了库"。

        ``q`` 为空串等同于不过滤：前端输入框清空后仍会带一个空串上来，
        把它当"搜空串"会匹配到全部——结果相同，但让"有没有在搜"这件事变得含糊。

        ``limit=None``（默认）返回全量：统计、批处理、生命周期这些内部调用点
        本来就要全部文档。列表接口显式传 ``limit``/``offset``。
        """
        self._validate_document_scope(kb_id, folder_id)
        return self._stores.meta.list_documents(
            kb_id,
            folder_id=folder_id,
            root_only=root_only,
            q=q.strip() if q else None,
            stage=stage,
            source_kind=source_kind,
            limit=limit,
            offset=offset,
        )

    def count_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
    ) -> int:
        """同一套过滤条件下的文档总数（分页界面的"共 N 篇"）。

        ``q`` 的清洗口径与 ``list_documents`` 保持一致——否则搜空串时列表有内容、
        总数却对不上。
        """
        self._validate_document_scope(kb_id, folder_id)
        return self._stores.meta.count_documents(
            kb_id,
            folder_id=folder_id,
            root_only=root_only,
            q=q.strip() if q else None,
            stage=stage,
            source_kind=source_kind,
        )

    def _validate_document_scope(self, kb_id: str, folder_id: str | None) -> None:
        """库与目录都必须存在、且目录属于这个库。列表与计数同一条判定。"""
        if self._stores.meta.get_knowledge_base(kb_id) is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")
        if folder_id is not None:
            folder = self._stores.meta.get_folder(folder_id)
            if folder is None or folder.kb_id != kb_id:
                raise NotFoundError(f"目录不存在：{folder_id}")

    def chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
        """批量取切块数。列表页用它，避免每个文档查一次库。"""
        return self._stores.meta.count_chunks_by_documents(document_ids)

    @staticmethod
    def is_searchable(record: DocumentRecord, *, chunks: int) -> bool:
        """这份文档**现在**能不能被检索到（对外语义，P1 修）。

        判据**不是** ``stage``：``stage`` 是**流水线的进度**，与"检索索引里有没有它"
        是两件事。切块与全文索引在 ``ingest._chunk`` 里是紧挨着落的（``chunked`` 起
        关键词就查得到），向量在 ``embedding`` 的末尾落库；而阶段推进是**之后再写**的
        一次更新。中间那一瞬——或者进程正好在两次写入之间被杀——会让一份
        **已经能检索**的文档停在 ``embedding``。实测那三份卡住的素材里 xlsx 正是如此：
        卡在 ``embedding``，检索能命中它，而模型读到 ``searchable=false`` 就不再搜了
        （2026-09-29 能力实测第 6 条）。

        所以判据落到**产物**上：有切块（全文索引与切块同一步落库）且**没被停用**
        （停用语义就是"不参与检索"，见 ``DocumentRecord.disabled``）。

        **宁可高估也不低估**：高估的代价是模型搜一次、没命中就如实说"库里没有"；
        低估的代价是它**根本不去搜**——那正是上面那条实测里丢掉答案的方式。
        残留的一处不精确：全文索引那一步单独失败时会高估（切块已落、索引没落），
        而那种情况任务会重试，下一次成功即对齐。
        """
        return not record.disabled and chunks > 0

    def question_stats(self, document_ids: list[str]) -> dict[str, tuple[int, int]]:
        """批量取 ``{document_id: (有题块数, 问题总数)}``（v24，列表页显示出题情况）。"""
        return self._stores.meta.question_stats_by_documents(document_ids)

    def active_question_documents(self, document_ids: list[str]) -> set[str]:
        """这些文档里还有出题任务在队列里的那几个（列表显示"生成中"并继续轮询）。"""
        return self._stores.meta.active_question_documents(document_ids)

    def timeline(self, document_id: str) -> DocumentTimeline:
        """这篇文档的处理进度时间线（v24）。

        数据源是阶段事件（每次进入某阶段一条），折成"共几步 / 现在第几步 /
        每步各花多久"。**跑着时最后一步的耗时是"到现在为止"**，所以前端每次轮询
        都会看到它在长——这正是"还在动"的证据。
        """
        record = self.get(document_id)
        events = self._stores.meta.list_document_stage_events(document_id)
        return build_timeline(
            document_id=document_id,
            stage=record.stage.value,
            events=events,
            now=datetime.now(UTC),
        )

    def progress_by_documents(
        self, records: Sequence[DocumentRecord]
    ) -> dict[str, DocumentProgress]:
        """一批文档的进度摘要（列表行上的分段进度条）。

        **批量取事件与任务**：一页 20 篇，逐篇查就是 40 次往返。这里两条 SQL
        拿到全部输入，剩下的折叠是纯函数（``build_timeline`` + ``progress_of``）。
        """
        ids = [record.id for record in records]
        if not ids:
            return {}
        events = self._stores.meta.list_document_stage_events_for_documents(ids)
        tasks = self._stores.meta.active_tasks_by_documents(ids)
        now = datetime.now(UTC)

        result: dict[str, DocumentProgress] = {}
        for record in records:
            timeline = build_timeline(
                document_id=record.id,
                stage=record.stage.value,
                events=events.get(record.id, []),
                now=now,
            )
            result[record.id] = progress_of(
                timeline, stalled=self._is_stalled(tasks.get(record.id), now)
            )
        return result

    def _is_stalled(self, task: TaskRecord | None, now: datetime) -> bool:
        """这条未结束的任务是不是"跑着但没人管"。

        判据来自 ``ObservabilityService``（租约过期 = 没有 worker 在续约）——
        与任务中心那一列是同一个结论，不是另算一个。
        """
        if task is None or self._observability is None:
            return False
        return self._observability.assess(task, now=now).status == "stalled"

    def get(self, document_id: str) -> DocumentRecord:
        record = self._stores.meta.get_document(document_id)
        if record is None:
            raise NotFoundError(f"文档不存在：{document_id}")
        return record

    def get_documents_by_ids(self, document_ids: list[str]) -> dict[str, DocumentRecord]:
        """按 id 批量取文档（不存在的 id 不在结果里）。给"手上有一批 id"的场景用。"""
        return self._stores.meta.get_documents_by_ids(document_ids)

    def rename(self, document_id: str, name: str) -> DocumentRecord:
        """改显示名。**不动内容、不重跑解析**——索引里是切块，与文件名无关。"""
        record = self.get(document_id)
        cleaned = name.strip()
        if not cleaned:
            raise InvalidRequestError("文件名不能为空")
        if len(cleaned) > DOCUMENT_NAME_MAX_CHARS:
            raise InvalidRequestError(f"文件名最多 {DOCUMENT_NAME_MAX_CHARS} 个字符")
        if cleaned == record.name:
            return record
        self._stores.meta.rename_document(document_id, cleaned)
        record.name = cleaned
        return record

    def set_disabled(self, document_id: str, disabled: bool) -> DocumentRecord:
        """停用/恢复一个文档。

        **只动标记**：切块与向量原样保留，检索在两条通道上都按标记过滤
        （全文在 SQL 里裁，向量在融合后裁），所以恢复是零成本——
        这与切块级 `disabled`（§G3）是同一套设计，只是范围是整份文档。
        """
        record = self.get(document_id)
        if record.disabled == disabled:
            return record
        self._stores.meta.set_document_disabled(document_id, disabled)
        record.disabled = disabled
        return record

    def cancel(self, document_id: str) -> DocumentRecord:
        """叫停一个还在跑的摄入。

        **两步都要做**：把文档置为 ``canceled`` 让正在跑的那次摄入在下一个阶段
        边界看见并停手（见 :meth:`IngestService._advance`）；把任务收成
        ``canceled`` 则是让"排队中、还没轮到"的那份不再被领取。
        少做任一步，用户都会看到"点了取消但状态还在动"。
        """
        record = self.get(document_id)
        if not can_transition(record.stage, DocumentStage.CANCELED):
            # 已经 indexed / 已失败，没有在跑的解析可取消——这是状态冲突，不是参数错
            raise ConflictError("这个文档当前没有可取消的处理")
        self._stores.meta.cancel_tasks_for_document(document_id)
        self._stores.meta.update_document_stage(
            document_id, DocumentStage.CANCELED, error="已取消"
        )
        record.stage = DocumentStage.CANCELED
        record.error = "已取消"
        return record

    def cancel_tasks(self, task_ids: list[str]) -> list[TaskCancelItem]:
        """撤销一批**还没结束**的任务，逐条返回结果（v24）。

        为什么要有它：队列里堆着几十条 pending 时，用户唯一的刹车是"逐篇取消文档"——
        而我们自己文档里还写着"取消排队中的任务"是任务中心该有的能力。这里补上。

        三种情况分开处理，**不能只改任务行的状态**：

        - ``pending``：直接把任务标 canceled。``claim_task`` 只领 pending，所以它不会再
          被领取——文档留在原阶段（uploaded 之类），之后还能重新入队，不是不可逆的。
        - ``running`` 且挂在文档上：光改任务行没用，worker 手上那一次不会因此停手，
          跑完还会把状态写成 succeeded。所以**连同文档一起置 canceled**——摄入在下一个
          阶段边界看见就停（``IngestService._advance``）。云端解析那种没法中断的调用
          也只能在边界才停得住，这与 ``cancel`` 是同一套语义。
        - ``running`` 但不是文档任务（数据源拉取 / Wiki 生成）：没有阶段可置，
          只能明确拒绝并说清原因，而不是给一个"看起来取消了、其实还在跑"的假象。
        """
        results: list[TaskCancelItem] = []
        for task_id in task_ids:
            task = self._stores.meta.get_task(task_id)
            if task is None:
                results.append(TaskCancelItem(task_id, False, "任务不存在"))
                continue
            if task.state not in (TaskState.PENDING, TaskState.RUNNING):
                results.append(TaskCancelItem(task_id, False, f"任务已结束（{task.state.value}）"))
                continue
            if task.state is TaskState.RUNNING and task.document_id:
                try:
                    self.cancel(task.document_id)
                except KylabError as exc:
                    results.append(TaskCancelItem(task_id, False, str(exc)))
                else:
                    results.append(TaskCancelItem(task_id, True))
                continue
            if task.state is TaskState.RUNNING:
                results.append(
                    TaskCancelItem(task_id, False, "这类任务没有可中断的阶段，等它跑完或重启服务")
                )
                continue
            changed = self._stores.meta.cancel_tasks([task_id])
            # rowcount 为 0 = 这条在"读到"和"改"之间已经结束了（并发下的正常结果）
            results.append(
                TaskCancelItem(task_id, True)
                if changed
                else TaskCancelItem(task_id, False, "任务已结束")
            )
        return results

    def list_parts(self, document_id: str) -> list[DocumentPartRecord]:
        """子文件树（大文件切分的产物，UI 点击展开）。"""
        self.get(document_id)
        return self._stores.meta.list_document_parts(document_id)

    def chunk_count(self, document_id: str) -> int:
        return self._stores.meta.count_chunks(document_id)

    def list_chunks(self, document_id: str, *, limit: int | None = None) -> list[ChunkRecord]:
        """按序取切块（文档详情页的正文预览用）。

        先确认文档存在：否则"文档不存在"和"文档还没切块"都返回空列表，
        调用方分不清是地址写错了还是流水线还没跑到，只能靠猜。
        """
        self.get(document_id)
        return list(self._stores.meta.iter_chunks(document_id, limit=limit))

    # ------------------------------------------------------------------ 下载

    def content(self, document_id: str, *, fmt: str = "original") -> DocumentContent:
        """取下载内容：``original``（原文件，默认）或 ``markdown``（解析产物）。

        这是 T4.5 的"下载双选项"。**默认原文件**是刻意的：用户上传的是什么，
        下载回来的就该是什么；Markdown 是我们加工的中间产物，想要它的人会自己说。
        """
        if fmt not in ("original", "markdown"):
            raise UnsupportedContentError(f"不支持的下载格式：{fmt}")

        document = self.get(document_id)
        parsed = self._stores.meta.get_parse_result(document_id)

        if fmt == "markdown":
            if parsed is None:
                # 还没解析完。这不是"文件不存在"，要说清是"还没到时候"
                raise UnsupportedContentError(
                    "该文档还没有 Markdown 产物（解析尚未完成或已失败）"
                )
            # 落盘的 Markdown 带着页标记（chunker 靠它把页码写进 chunk），
            # 但标记是内部的锚点，**不该出现在用户读到的正文或下载的文件里**。
            raw = self._stores.objects.read(parsed.markdown_path).decode("utf-8", errors="replace")
            return DocumentContent(
                data=strip_page_markers(raw).encode("utf-8"),
                filename=f"{_stem(document.name)}.md",
                media_type="text/markdown; charset=utf-8",
                kind="markdown",
            )

        path = self._stores.meta.get_setting(f"document.{document_id}.original_path")
        if not path or not self._stores.objects.exists(path):
            raise NotFoundError("原文不在对象存储里（可能已被回收站清理）")
        return DocumentContent(
            data=self._stores.objects.read(path),
            filename=document.name,
            # 声明缺失或笼统时按后缀兜底：octet-stream 的响应会被浏览器下载而不是渲染
            media_type=media_type_of(document.name, document.mime_type),
            # 有解析产物时，原文也按 markdown 展示会给前端"读归一化文本"的
            # 一致体验；没有产物才回落到 PDF/图片预览
            kind=content_kind(document.name, has_markdown=parsed is not None),
        )

    def reading_view(self, document_id: str) -> DocumentContent:
        """「阅读」视角的内容：优先给解析产物，其次给原始版式。

        与 ``content`` 的区别是**它自己挑**而不是等调用方指定 format：
        用户点"阅读"时不该先决定"我要看原文还是看 Markdown"——
        那是我们的实现细节，不该成为他的选择题。
        """
        parsed = self._stores.meta.get_parse_result(document_id)
        if parsed is not None:
            return self.content(document_id, fmt="markdown")
        return self.content(document_id, fmt="original")

    def original_view(self, document_id: str) -> DocumentContent:
        """原始版式：**不管有没有解析产物**，都按文件本身判断怎么展示。

        与 ``reading_view`` 的差别是它明确回答"那个文件长什么样"——用户点了
        "原文版式"就是要看原件，此时再返回解析后的 Markdown 是答非所问。
        """
        document = self.get(document_id)
        content = self.content(document_id, fmt="original")
        return replace(content, kind=content_kind(document.name, has_markdown=False))

    def download_url(
        self,
        document_id: str,
        *,
        fmt: str,
        secret: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        disposition: str = "attachment",
    ) -> tuple[str, int]:
        """签发一条内容链接，返回 ``(相对 URL, 到期时间戳)``。

        返回相对路径而不是绝对地址：绝对地址需要知道对外域名，而那个信息
        只有部署时才知道（反代、端口、协议）。让前端拿相对路径自己拼
        比在这里猜一个 ``http://localhost:8000`` 可靠。

        ``disposition`` 只影响响应头，**不参与签名**（签名绑的是"哪个文档、哪种格式"）：
        它不是一个权限参数——需要直接打开（iframe 里渲染、`<img>` 显示）时用
        ``inline``，需要落盘时用默认的 ``attachment``。所以调用方改它不会越权，
        真正决定能不能 inline 的是协议层那份媒体类型白名单。
        """
        self.get(document_id)  # 文档不存在就别签发
        resource = signature_resource(document_id, fmt)
        signature, expires_at = sign_resource(
            resource, secret, ttl_seconds=ttl_seconds
        )
        url = (
            f"/api/v1/documents/{document_id}/content"
            f"?format={fmt}&expires={expires_at}&signature={signature}"
        )
        if disposition != "attachment":
            url += f"&disposition={disposition}"
        return url, expires_at

    # ------------------------------------------------------------------ 任务

    def enqueue_ingest(
        self, document_id: str, *, kind: TaskKind = TaskKind.PARSE, force: bool = False
    ) -> TaskRecord:
        """把文档排进摄入队列。

        默认**幂等**：同一文档已有待执行/执行中的任务时返回既有的那个，
        避免用户连点几次上传就在队列里堆出重复任务。
        ``force=True`` 用于"重跑"按钮，会先确认没有在跑的任务。

        **``force`` 还必须把阶段推回 ``CHUNKING``**（v23 修）：摄入是"按产物断点续跑"
        的，已 ``indexed`` 的文档若原样入队，``_resume_stage`` 会返回 ``indexed``，
        三个 ``_before(...)`` 全是 false——整次摄入**什么都不做**，只重新发一遍
        indexed 通知。也就是说界面上那个「重新摄入全部文档」对成功索引的文档
        一直是个空操作，而文档里却写着"改完切分参数要重新摄入才生效"。

        回到 ``CHUNKING``（而不是 ``UPLOADED``）：重新切分 + 重新向量化，
        **不重新解析**——解析可能是收费的云端服务，而解析产物还在库里。
        """
        document = self.get(document_id)
        if document.stage is DocumentStage.INDEXED and not force:
            raise ConflictError(f"文档 {document_id} 已索引完成；如需重跑请带 force=true")

        existing = self._find_active_task(document_id, kind)
        if existing is not None:
            return existing

        if force and can_transition(document.stage, DocumentStage.CHUNKING):
            self._stores.meta.update_document_stage(document_id, DocumentStage.CHUNKING)
            document.stage = DocumentStage.CHUNKING

        return self._stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=kind,
                state=TaskState.PENDING,
                payload={"document_id": document_id},
                document_id=document_id,
            )
        )

    def enqueue_questions(self, document_id: str) -> TaskRecord:
        """把一篇**已索引**文档排进"补生成问题"队列（v24）。

        与 ``enqueue_ingest`` 是两条路，所以单独一个方法：

        - **只收已索引的文档**。别的阶段要么还没切块、要么正在被摄入重写，
          这时候出题会被随后的重切覆盖，白花模型调用。给一条明确的拒绝，
          而不是排一个注定被覆盖的任务。
        - **不碰文档阶段**。出题不改内容与切块，跑完文档仍是 indexed，
          所以这里不能走 ``force``（那会把阶段推回 CHUNKING，连带重新切块）。
        """
        document = self.get(document_id)
        if document.stage is not DocumentStage.INDEXED:
            raise ConflictError(
                f"「{document.name}」当前处于 {document.stage.value}，"
                "只有已索引完成的文档才能生成问题"
            )
        existing = self._find_active_task(document_id, TaskKind.QUESTIONS)
        if existing is not None:
            return existing
        return self._stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=TaskKind.QUESTIONS,
                state=TaskState.PENDING,
                payload={"document_id": document_id},
                document_id=document_id,
            )
        )

    def list_tasks(
        self, state: TaskState | None = None, *, kb_ids: list[str] | None = None
    ) -> list[TaskRecord]:
        """``kb_ids``（v10 成员视角）：只留这些库里的文档任务。

        没有挂文档的任务（数据源拉取等）对成员隐藏——那属于全局运维面。
        """
        tasks = self._stores.meta.list_tasks(state)
        if kb_ids is None:
            return tasks
        visible = set(kb_ids)
        # 一次批量取文档，而不是在循环里逐条 get_document（任务列表可能上千条）
        documents = self._stores.meta.get_documents_by_ids(
            [task.document_id for task in tasks if task.document_id]
        )
        result: list[TaskRecord] = []
        for task in tasks:
            if task.document_id is None:
                continue
            document = documents.get(task.document_id)
            if document is not None and document.knowledge_base_id in visible:
                result.append(task)
        return result

    def get_task(self, task_id: str) -> TaskRecord:
        task = self._stores.meta.get_task(task_id)
        if task is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return task

    def _find_active_task(self, document_id: str, kind: TaskKind) -> TaskRecord | None:
        for state in ACTIVE_TASK_STATES:
            for task in self._stores.meta.list_tasks(state):
                if task.document_id == document_id and task.kind is kind:
                    return task
        return None
