"""摄入编排（M2 T2.2 的服务侧）。

把"上传 → 探测 → 解析 → 切分 → 向量化 → 可检索"串成一条可重入的链路：

- **每一步都先落状态再做事**，失败时把 ``error`` 与失败阶段写进文档记录，
  界面据此显示"卡在哪一步、为什么"（架构 §4、§12）；
- **断点续跑**：``ingest`` 从文档当前阶段继续，已完成的上游步骤不重跑；
- **模型锁定**：库内已有向量就拒绝换模型；库内还没有向量则允许更新模型与维度
  （架构 §6.4 的原话是"库内已有向量化文件 → 不允许更换"，没向量时不必逼用户重建库）。

事件循环/队列不在这里：本类是纯同步逻辑，便于直接测试；异步 worker 只负责调用它。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.downloads import content_disposition
from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.models.enums import TERMINAL_STAGES, DataSourceKind, DocumentStage, TaskKind, TaskState
from app.parsers.base import ParseError, ParseResult, ParserProvider
from app.parsers.media_direct import MediaDirectParser
from app.parsers.probe import ProbeResult, probe, suffix_of
from app.pipeline.state_machine import InvalidTransition, assert_transition
from app.services.chunking import ChunkingConfig, chunk_markdown, config_from_record
from app.services.embedding.base import EmbeddingError, EmbeddingProvider
from app.services.embedding.resolver import EmbeddingResolver
from app.services.parser_router import ParserRouter, RoutingDecision, hint_for
from app.services.splitting import (
    PageRangeSplitter,
    PartOutcome,
    SplitPlan,
    build_result,
    plan_split,
)
from app.services.suggested_questions import SuggestedQuestionsService
from app.services.summary import DocumentSummaryService
from app.services.tabular import TABULAR_EXTENSIONS, parse_tabular
from app.services.webhook import DOCUMENT_FAILED, DOCUMENT_INDEXED
from app.storage.base import (
    MARKDOWN,
    ORIGINALS,
    DocumentPartRecord,
    DocumentRecord,
    KnowledgeBaseRecord,
    ParseResultRecord,
    StoreBundle,
    TaskRecord,
    content_key,
)

logger = logging.getLogger(__name__)

__all__ = [
    "IngestCanceled",
    "IngestError",
    "IngestOutcome",
    "IngestService",
    "content_disposition",
    "normalize_filename",
]


def _degrade_message(filename: str, failures: list[tuple[str, str]]) -> str:
    """所有候选都失败时的错误：**把每个引擎为什么失败都写出来**。

    只回最后一条会误导——用户看到"PaddleOCR 超时"，以为换个引擎就行，
    其实 MinerU 早就因为额度用尽失败了（那才是要处理的那件事）。
    """
    detail = "；".join(f"{name}：{reason}" for name, reason in failures)
    return f"所有解析通道都失败了（{filename}）：{detail}"


def _parse_hint(document: DocumentRecord, probe_result: ProbeResult) -> str:
    """解析通道全失败之后，补一句"下一步怎么办"（视频 / 扫描件那两类最需要）。

    走的是路由那一层**同一句** `hint_for`，这里不另写文案：那句话同时用在
    "一个候选都没有"的路径上，两处说法不一致会让人怀疑自己看错了设置。

    实测（方案 13，2026-09-29）：一份自造 `.webm` 上传即失败，原文只有
    ``[parsing] MinerU 申请上传地址失败（-60002）：MinerU 不支持该文件格式`` ——
    而它其实是**能进库的**（配好多模态之后同一条链路 3.7 秒就 indexed）。
    缺的就是这一句"视频只能走多模态嵌入：请到设置…"。
    """
    return hint_for(probe_result, document.name)


def _part_id(document_id: str, index: int) -> str:
    """子文件 id：由文档 id + 段序号派生，**必须可重入**。

    随机 id 会让"失败重跑"每次都建一批新的子文件记录——
    于是界面上子文件树越跑越长，用户看到 5 段变成 10 段。
    """
    return f"{document_id}#part{index:03d}"


class IngestError(Exception):
    """摄入失败。带 ``stage`` 让 worker 决定从哪一步重试。"""

    def __init__(self, message: str, *, document_id: str, stage: DocumentStage) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.stage = stage


class IngestCanceled(Exception):
    """摄入被用户叫停（**不是失败**）。

    worker 据此把任务收成 ``canceled`` 而不是 ``failed``——否则用户主动停止的
    动作会在任务中心里长成一条红色失败记录，下次看到会以为解析器坏了。
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(f"文档 {document_id} 的摄入已被取消")
        self.document_id = document_id


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """一次摄入的结果。``is_duplicate`` 对应架构 §6.3 的"检测到相同文件"。"""

    document: DocumentRecord
    chunk_count: int
    is_duplicate: bool = False


class IngestService:
    """摄入流水线编排。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        router: ParserRouter,
        embedder: EmbeddingProvider,
        chunk_config: ChunkingConfig | None = None,
        notifier: Callable[[str, dict], None] | None = None,
        embedders: EmbeddingResolver | None = None,
        questions: SuggestedQuestionsService | None = None,
        summaries: DocumentSummaryService | None = None,
    ) -> None:
        self._stores = stores
        self._router = router
        self._embedder = embedder
        # 按库解析嵌入模型（v11）。不给就退回全局 embedder，行为与改动前一致
        self._embedders = embedders
        #: 分段出题（v23）。库上开着才用，且**失败不影响摄入**——
        #: 与 notifier 同一套理由：旁路能力的失败不该把文档打成 failed。
        #: 不给就完全不生成（老测试与"不配模型也能跑通摄入"的路径）。
        self._questions = questions
        #: 文档摘要（v25）。与出题同一条边界：旁路能力，失败只记日志。
        #: 它的价值在**别处**——问答上下文靠它省 token（见 services/summary.py）。
        self._summaries = summaries
        #: 显式注入的切分参数（测试与特殊调用用）。**为 None 时按库读取**——
        #: 生产走的就是那条路：每个库有自己的块长/重叠（v17）。
        #: 与 `_embedders` 同一套写法：不给就退回"按库解析"。
        self._chunk_config = chunk_config
        # Webhook 是**旁路**（T4.6）：默认什么都不做，组合根才把它接上。
        # 做成回调而不是直接依赖 WebhookService，是为了不让摄入服务
        # 反过来依赖通知服务——那会让"发通知失败"有机会影响摄入本身
        self._notify = notifier or (lambda event, payload: None)

    # ------------------------------------------------------------------ 上传与去重

    def submit(
        self,
        *,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        document_id: str | None = None,
        uploaded_by: str | None = None,
        folder_id: str | None = None,
    ) -> IngestOutcome:
        """登记一份上传：按内容 hash 去重 → 存原文 → 建文档记录（``uploaded``）。

        ``uploaded_by`` 是使用者名册里的 id（G6）。**只是归属标注**，
        不参与鉴权——凭据是三档 API 身份那套，两者刻意分开。

        ``folder_id``（v13）是要放进哪个目录；目录必须属于同一个库，
        否则文档建出来后按目录查会"消失"（那种问题最难排查）。
        """
        kb = self._require_kb(knowledge_base_id)
        filename = normalize_filename(filename)
        digest = hashlib.sha256(content).hexdigest()
        if folder_id is not None:
            folder = self._stores.meta.get_folder(folder_id)
            if folder is None or folder.kb_id != kb.id:
                raise InvalidRequestError(f"目录不存在：{folder_id}")

        existing = self._stores.meta.get_document_by_hash(kb.id, digest)
        if existing is not None:
            return IngestOutcome(
                document=existing,
                chunk_count=self._stores.meta.count_chunks(existing.id),
                is_duplicate=True,
            )

        stored_path = self._stores.objects.write(
            content_key(ORIGINALS, digest, _suffix_of(filename)), content
        )
        document = self._stores.meta.create_document(
            DocumentRecord(
                id=document_id or f"doc_{uuid.uuid4().hex[:12]}",
                knowledge_base_id=kb.id,
                name=filename or "未命名",
                source_kind=DataSourceKind.UPLOAD,
                content_hash=digest,
                stage=DocumentStage.UPLOADED,
                size_bytes=len(content),
                mime_type=mime_type,
                uploaded_by=uploaded_by,
                folder_id=folder_id,
            )
        )
        self._stores.meta.set_setting(f"document.{document.id}.original_path", stored_path)
        return IngestOutcome(document=document, chunk_count=0)

    def replace(
        self,
        document_id: str,
        *,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> DocumentRecord:
        """用新内容**原地替换**一份文档（v0.12）。

        与 ``submit`` 的分工很清楚：``submit`` 是"来了一份文件"——同内容去重、
        不同内容**新建一份**；而这里是"**这份**文档的内容变了"。

        为什么要原地替换、而不是"删掉重新入库"：

        - **文档 id 是引用的锚点**——对话里的出处、笔记上的关联都指向它，
          换 id 等于把所有引用打断；
        - 删掉会把旧版送进回收站，用户会看到一堆"自己没删过的"东西。

        内容没变就直接返回：重新入队会白跑一遍切分与向量化。

        **有任务在跑时拒绝**：worker 正在旧内容上写产物，这时替换会让新旧交叉
        （比如它把旧内容的切块写回库里）。宁可报错让人稍后再改，也不留下错乱的产物。
        """
        document = self._require_document(document_id)
        kb = self._require_kb(document.knowledge_base_id)
        filename = normalize_filename(filename)
        digest = hashlib.sha256(content).hexdigest()
        if digest == document.content_hash:
            return document

        if self._stores.meta.active_tasks_by_documents([document_id]):
            raise ConflictError("这份文档正在处理中，请等它处理完再改")

        stored_path = self._stores.objects.write(
            content_key(ORIGINALS, digest, _suffix_of(filename)), content
        )
        # 旧产物**先清干净**：不清的话它们还带着旧正文，重新索引完成之前
        # 检索会同时命中新旧两版内容——"我明明改了，搜出来还是旧的"就是这个。
        #
        # 三处都要清，缺一不可：向量（检索用）、全文索引（检索用）、
        # **chunks 表本身**（抽屉与"同小节扩读"读的是它）。删除文档那条路不用管
        # chunks——那是外键级联删的；替换保留了文档行，所以得自己删。
        # 用 `replace_chunks(doc, [])` 而不是新增一个删除方法：它就是
        # "这份文档现在没有块了"，语义正好，且先删后插本来就在它的事务里。
        chunk_ids = [item.chunk_id for item in self._stores.meta.iter_chunks(document_id)]
        if chunk_ids:
            self._stores.vectors.delete_vectors(kb.id, chunk_ids=chunk_ids)
            self._stores.fulltext.delete_chunks(chunk_ids)
            self._stores.meta.replace_chunks(document_id, [])

        self._stores.meta.replace_document_content(
            document_id,
            content_hash=digest,
            name=filename,
            size_bytes=len(content),
            mime_type=mime_type,
        )
        self._stores.meta.set_setting(f"document.{document_id}.original_path", stored_path)
        # 内容变了就是重新走一遍流水线：阶段推回 uploaded（走 update_document_stage
        # 而不是直接改列，这样时间线上会多一段"重新开始"的记录）
        self._stores.meta.update_document_stage(document_id, DocumentStage.UPLOADED)
        return self._require_document(document_id)

    # ------------------------------------------------------------------ 主链路

    def ingest(self, document_id: str) -> IngestOutcome:
        """把文档推进到 ``indexed``；已完成的步骤会按当前阶段跳过（断点续跑）。

        **取消是协作式的**：用户在界面上点"取消解析"时，接口把文档置为
        ``canceled``；正在线程里跑的这次摄入会在**下一个阶段边界**（见 ``_advance``）
        发现这件事并抛出 :class:`IngestCanceled`。已经发出去的云端解析请求没法
        中途掐断，但它返回之后不会再往下走——绝不会出现"我取消了，它却继续向量化"。
        """
        document = self._require_document(document_id)
        kb = self._require_kb(document.knowledge_base_id)
        self._resolve_model(kb)

        # failed 态下不能拿 stage 直接比大小（它不是主链路的一环），
        # 而是**按已有产物推断该从哪一步续跑**：有 chunk 就只差向量化，
        # 有解析产物就从切分开始，什么都没有就从头来。
        resume_from = self._resume_stage(document)

        try:
            if _before(resume_from, DocumentStage.PARSED):
                parse_result = self._probe_and_parse(document)
            else:
                parse_result = None
                self._reuse_parse_result(document)

            if _before(resume_from, DocumentStage.CHUNKED):
                chunks = self._chunk(document, kb, parse_result)
            else:
                chunks = list(self._stores.meta.iter_chunks(document.id))

            if _before(resume_from, DocumentStage.INDEXED):
                self._embed_and_index(document, kb, chunks)
        except (ParseError, EmbeddingError, InvalidTransition) as exc:
            stage = getattr(exc, "stage", "parsing")
            self._fail(document, str(exc), stage=stage)
            self._announce_failure(document, str(exc), stage)
            raise IngestError(str(exc), document_id=document.id, stage=stage) from exc

        refreshed = self._require_document(document.id)
        chunk_count = self._stores.meta.count_chunks(document.id)
        # **事件在状态真的落到 indexed 之后发**：早发的话接收端来查会看到还在跑，
        # 而它完全有理由相信"收到 indexed 就能取到内容"
        self._notify(
            DOCUMENT_INDEXED,
            {
                "document_id": refreshed.id,
                "knowledge_base_id": refreshed.knowledge_base_id,
                "name": refreshed.name,
                "stage": refreshed.stage.value,
                "chunk_count": chunk_count,
                "page_count": refreshed.page_count,
                "size_bytes": refreshed.size_bytes,
            },
        )
        return IngestOutcome(document=refreshed, chunk_count=chunk_count)

    def _announce_failure(self, document: DocumentRecord, message: str, stage: str) -> None:
        """失败也要通知。

        **只推成功是最常见的 webhook 设计错误**：调用方等一个永远不会来的
        `indexed`，而文档早就 failed 了。它只能靠超时猜，或者靠轮询兜底——
        那 webhook 就白做了。
        """
        self._notify(
            DOCUMENT_FAILED,
            {
                "document_id": document.id,
                "knowledge_base_id": document.knowledge_base_id,
                "name": document.name,
                "stage": stage,
                "error": message,
            },
        )

    def _resume_stage(self, document: DocumentRecord) -> DocumentStage:
        """推断续跑起点。产物是唯一可信依据——状态可能因为崩溃而停在半路。"""
        if document.stage not in (DocumentStage.FAILED, DocumentStage.CANCELED):
            return document.stage
        if self._stores.meta.count_chunks(document.id) > 0:
            return DocumentStage.CHUNKED  # 只差向量化
        if self._stores.meta.get_parse_result(document.id) is not None:
            return DocumentStage.PARSED  # 从切分开始
        return DocumentStage.UPLOADED

    # ------------------------------------------------------------------ 各阶段

    def _probe_and_parse(self, document: DocumentRecord) -> ParseResult:
        self._advance(document, DocumentStage.PROBING)
        original = self._read_original(document)
        probe_result = probe(original, filename=document.name, mime_type=document.mime_type)

        # 页数是**探测的产物**，先落库：它在下面决定要不要切分，
        # 也是界面上"共 N 页"的唯一来源（此前这一列恒为 null，见 §12.18）
        self._stores.meta.update_document_page_count(document.id, probe_result.page_count)

        self._advance(document, DocumentStage.PARSING)
        # **拿全部候选而不是一个**：云端引擎会失败（额度、抖动、半截结果），
        # 而"这个文件谁都能解"很常见——扫描件有 MinerU/PaddleOCR 两条路，
        # 文字型 PDF 还有本地直提。只试第一个会让后面的通道白放着（架构 §4.1）
        decisions = self._router.candidates(
            filename=document.name, mime_type=document.mime_type, probe=probe_result
        )
        if not decisions:
            # 一个都不支持：让 decide() 抛出带"下一步动作"的那条错误
            self._router.decide(
                filename=document.name, mime_type=document.mime_type, probe=probe_result
            )
            raise AssertionError("unreachable")  # pragma: no cover

        # 大文件强制切分（架构 §4.2 / T2.7）：超过渠道页数上限的 PDF 直接提交
        # 只会拿到 -60006 然后整个文件失败，而它其实完全可以被解析。
        split_plan = plan_split(probe_result.page_count)

        failures: list[tuple[str, str]] = []
        result: ParseResult | None = None
        for index, decision in enumerate(decisions):
            try:
                result = self._parse_once(
                    document=document,
                    original=original,
                    probe_result=probe_result,
                    decision=decision,
                    split_plan=split_plan,
                )
            except ParseError as exc:
                # **只对 ParseError 降级**：取消（IngestCanceled）与程序错误都不该
                # 触发"再试下一个引擎"——那会把"用户不想跑了"变成"换个引擎继续花钱"
                failures.append((decision.parser_name, str(exc)))
                if index == len(decisions) - 1:
                    hint = _parse_hint(document, probe_result)
                    # **只有一个候选时原样抛出**：包一层"所有通道都失败"只会让
                    # 那条本来可读的原因（"云端额度用尽"）前面多一段废话——
                    # 但**下一步怎么办**要补上（见 `_parse_hint`）：
                    # 实测 .webm 上传就是这么失败的（MinerU 拒收 + 没配多模态，
                    # 用户看到的只有"MinerU 不支持该文件格式"，而手上那份视频
                    # 其实一配就能进库——补上那句之后他知道该去哪一栏）。
                    if len(failures) == 1:
                        if not hint:
                            raise
                        raise ParseError(f"{exc}{hint}", stage="parsing") from exc
                    raise ParseError(
                        _degrade_message(document.name, failures) + hint, stage="parsing"
                    ) from exc
                logger.warning(
                    "文档 %s 用 %s 解析失败，降级到 %s：%s",
                    document.name,
                    decision.parser_name,
                    decisions[index + 1].parser_name,
                    exc,
                )
                continue
            if index > 0:
                # 降级成功要把原因写进产物说明：界面上"这个文件是谁解析的"
                # 必须能解释"为什么不是首选那个"
                decision = RoutingDecision(
                    parser=decision.parser,
                    reason=(
                        f"{failures[0][0]} 解析失败（{failures[0][1]}），"
                        f"已改用 {decision.parser_name}"
                    ),
                    probe=probe_result,
                )
            break

        if result is None:  # pragma: no cover - 上面的分支已覆盖
            raise ParseError(f"没有可用的解析器：{document.name}", stage="parsing")

        self._save_parse_artifacts(document, result, decision.reason, probe_result)
        self._advance(document, DocumentStage.PARSED)
        self._store_tabular_copy(document, original)
        return result

    def _parse_once(
        self,
        *,
        document: DocumentRecord,
        original: bytes,
        probe_result,
        decision,  # type: ignore[no-untyped-def]
        split_plan: SplitPlan,
    ) -> ParseResult:
        """用一个解析器跑一遍（大文件走分段）。"""
        if split_plan.needed:
            return self._parse_in_parts(
                document=document,
                original=original,
                plan=split_plan,
                parser=decision.parser,
                probe_result=probe_result,
            )
        return decision.parser.parse(
            content=original,
            filename=document.name,
            mime_type=document.mime_type,
            probe=probe_result,
        )

    def _parse_in_parts(
        self,
        *,
        document: DocumentRecord,
        original: bytes,
        plan: SplitPlan,
        parser: ParserProvider,
        probe_result,
    ) -> ParseResult:
        """按页范围切分后逐段解析，再合并成一份产物（架构 §4.2）。

        **子文件记录先建再跑**：这样界面上"第 1 段在跑、第 2 段还没开始"是可见的，
        而不是全程只有一个转圈。跑完按段回写成功/失败。
        """
        self._stores.meta.delete_document_parts(document.id)
        self._stores.meta.create_document_parts(
            [
                DocumentPartRecord(
                    id=_part_id(document.id, index),
                    document_id=document.id,
                    part_index=index,
                    page_start=part.start,
                    page_end=part.end,
                    stage=DocumentStage.PARSING,
                )
                for index, part in enumerate(plan.parts)
            ]
        )
        # 主文档标记为已切分：界面据此把它渲染成**可展开**的父行，
        # 而不是一个和普通文件一样的单行（架构 §4.2 "UI 显示为单个文件，点击展开子文件树"）
        self._stores.meta.mark_document_split(document.id)

        logger.info(
            "文档 %s 共 %d 页，%s",
            document.id,
            plan.total_pages,
            plan.reason,
        )

        splitter = PageRangeSplitter(
            parser,
            part_id_of=lambda index: _part_id(document.id, index),
            on_part_done=self._record_part_outcome,
        )
        outcome = splitter.run(
            content=original,
            filename=document.name,
            plan=plan,
            mime_type=document.mime_type,
            probe=probe_result,
        )

        failed = outcome.failed
        if failed:
            # **一段失败就整体失败**——但不能只说"失败了"：
            # 用户要知道是哪几段，才能只重跑那几段（架构 §4.2）
            detail = "、".join(f"{item.part}：{item.error}" for item in failed)
            raise ParseError(
                f"{len(failed)}/{len(outcome.outcomes)} 段解析失败——{detail}",
                stage="parsing",
            )

        return build_result(outcome, parser_name=parser.name, probe=probe_result)

    def _record_part_outcome(self, outcome: PartOutcome) -> None:
        self._stores.meta.update_part_stage(
            outcome.part_id,
            DocumentStage.PARSED if outcome.ok else DocumentStage.FAILED,
            error=outcome.error,
        )

    def _save_parse_artifacts(
        self,
        document: DocumentRecord,
        result: ParseResult,
        route_reason: str,
        probe_result,
    ) -> None:
        markdown_path = self._stores.objects.write(
            f"{MARKDOWN}/{document.id}.md", result.markdown.encode("utf-8")
        )
        self._stores.meta.save_parse_result(
            ParseResultRecord(
                document_id=document.id,
                part_id=None,
                parser_name=result.parser_name,
                markdown_path=markdown_path,
                probe_meta={
                    "kind": probe_result.kind,
                    "text_coverage": probe_result.text_coverage,
                    "route_reason": route_reason,
                    **probe_result.detail,
                },
            )
        )

    def _store_tabular_copy(self, document: DocumentRecord, original: bytes) -> None:
        """表格文档：另写一份结构化副本进 DuckDB（T2.11 双写）。

        **失败只记日志、不影响摄入**：结构化副本是**旁路**——
        检索用的行文本已经由 ``TabularParser`` 产出并向量化，
        副本写不进去只是"不能精确查询"，不该让整份文档摄入失败。
        （这与"解析失败必须报错"不同：那是主链路，这是附加品。）

        判断"是不是表格"用后缀而不是"解析器是谁"：解析器的选择是路由结果，
        而这里要的是文件类型这个客观事实。
        """
        if _suffix_of(document.name) not in TABULAR_EXTENSIONS:
            return
        try:
            parsed = parse_tabular(filename=document.name, content=original)
            rows = self._stores.tabular.write_table(
                table=document.id, columns=parsed.columns, rows=parsed.rows
            )
            logger.info("文档 %s 写入结构化副本 %d 行", document.name, rows)
        except Exception:
            logger.exception("文档 %s 的结构化副本写入失败（不影响摄入）", document.name)

    def _reuse_parse_result(self, document: DocumentRecord) -> None:
        """断点续跑：已有解析产物就直接用，不重复调用解析引擎（可能是收费的云服务）。"""
        if self._stores.meta.get_parse_result(document.id) is None:
            raise ParseError(
                f"文档 {document.id} 处于 {document.stage.value}，但找不到解析产物，无法续跑",
                stage="parsing",
            )

    def _chunk(
        self,
        document: DocumentRecord,
        kb: KnowledgeBaseRecord,
        parse_result: ParseResult | None,
    ) -> list:
        self._advance(document, DocumentStage.CHUNKING)
        markdown = parse_result.markdown if parse_result else self._read_markdown(document)

        chunks = chunk_markdown(
            markdown,
            document_id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            config=self._chunking_for(kb),
        )
        # 出题**必须在落库之前**：问题要跟着块一起写进去（不然后面还得再 UPDATE 一遍），
        # 而且紧接着的全文索引读的就是含问题的 `index_text`。
        self._attach_questions(kb, chunks)
        self._stores.meta.replace_chunks(document.id, chunks)
        self._stores.fulltext.index_chunks(chunks)
        self._advance(document, DocumentStage.CHUNKED)
        # 摘要放在**落库之后**：它读的是库里的块（与出题同一份输入），
        # 而且它自己的失败与块无关——先让块落地，摘要没写出来也不影响入库。
        self._attach_summary(document)
        return chunks

    def _attach_questions(self, kb: KnowledgeBaseRecord, chunks: list) -> None:
        """按库设置给每个分段出题，就地写进 ``chunk.questions``。

        **整段包在 try 里**：出题是旁路能力，模型没配、上游挂了、输出解析不出来，
        都只该让这篇文档"这一段没有题"，绝不能让摄入失败——`ingest()` 里任何
        漏出来的异常都会被 worker 当成可重试失败，最后把文档打成 failed。
        （服务内部已经逐批兜了异常，这里再兜一层是防"服务本身没接上"。）

        关掉时**一次模型调用都不发**，这是那个开关的主要意义。
        """
        if self._questions is None or not kb.suggested_enabled:
            return
        try:
            generated = self._questions.generate_for_chunks(
                chunks,
                model_pk=kb.suggested_model_pk,
                count=kb.suggested_count,
                prompt=kb.suggested_prompt,
            )
        except Exception:
            logger.warning("分段出题整体失败，文档 %s 不带问题入库", kb.id, exc_info=True)
            return
        for chunk in chunks:
            chunk.questions = tuple(generated.get(chunk.chunk_id, ()))

    def _attach_summary(self, document: DocumentRecord) -> None:
        """给这篇文档写一段摘要（v25）。

        **整段包在 try 里**（与出题同一条边界）：摘要是旁路能力，
        模型没配、上游挂了都只该让这篇文档"没有摘要"，绝不能让摄入失败——
        `ingest()` 里漏出来的异常会被 worker 当成可重试失败，最后把文档打成 failed。

        它**不参与阶段机**（不推进阶段、不写阶段事件）：摘要不是流水线上的一环，
        而是内容层的一笔补充。所以它在 `CHUNKED` 之后跑，跑完文档继续往向量化走。
        """
        if self._summaries is None:
            return
        try:
            self._summaries.summarize(document.id)
        except Exception:
            logger.warning("文档摘要失败，文档 %s 不带摘要入库", document.id, exc_info=True)

    def _chunking_for(self, kb: KnowledgeBaseRecord) -> ChunkingConfig:
        """这个库该用哪套切分参数。

        显式注入的配置优先（测试与批量导入等场景），否则**按库读**——
        这就是切分参数从"存了没人用"变成"真的生效"的那一处接线。
        ``config_from_record`` 会钳位越界的历史值，避免一条老配置把摄入打挂。
        """
        if self._chunk_config is not None:
            return self._chunk_config
        return config_from_record(kb)

    def _embedder_for(self, kb: KnowledgeBaseRecord) -> EmbeddingProvider:
        """这个库该用哪个嵌入实现：显式选了模型就用它，否则全局默认。"""
        if self._embedders is None:
            return self._embedder
        return self._embedders.for_kb(kb)

    def _embed_and_index(self, document: DocumentRecord, kb: KnowledgeBaseRecord, chunks) -> None:
        self._advance(document, DocumentStage.EMBEDDING)
        if not chunks:
            self._advance(document, DocumentStage.INDEXED)
            return

        embedder = self._embedder_for(kb)
        # 维度超过索引上限时仓储会把"这个库没有向量索引"作为一句话回给我们
        # （不报错：库照用，只是精确扫描）。**必须落日志**——否则用户只会觉得"检索变慢了"。
        advisory = self._stores.vectors.ensure_partition(kb.id, dim=embedder.dim)
        if advisory:
            logger.warning("文档 %s：%s", document.id, advisory)
        # **index_text 而不是 text**：含该段生成的问题，于是"换个问法"也能命中
        # （见 ChunkRecord.index_text）。原文那一列不动，引用预览里不会多出问题。
        texts = [chunk.index_text for chunk in chunks]
        media = self._media_blob(document, embedder)
        vectors = vectors_for(embedder, texts=texts, media=media)
        if media is not None:
            logger.info(
                "文档 %s 的向量来自媒体接口（%d 个分段共用同一份媒体向量）",
                document.id,
                len(chunks),
            )
        self._stores.vectors.upsert_vectors(
            kb.id,
            items=[(chunk.chunk_id, vector) for chunk, vector in zip(chunks, vectors, strict=True)],
        )
        self._advance(document, DocumentStage.INDEXED)

    def _media_blob(
        self, document: DocumentRecord, embedder: EmbeddingProvider
    ) -> bytes | None:
        """这份文档的向量该不该由**媒体接口**给（图片 / 视频）。

        第一条判据是**它是媒体直通解析出来的**（``parse_results.parser_name``）——
        **不能只看后缀**：配了 OCR 的图片走的是 OCR 文本，它的向量必须还是文本向量，
        否则同一张图"配没配 OCR"会落进两个不同的语义空间，检索结果前后矛盾而没有人
        看得出来。

        第二条判据是当前嵌入实现**支持媒体**。这里**报错而不是退回文本**：那份最小
        Markdown 里写着"本文件由媒体向量索引"，退回文本就等于让出处说假话（用户点开
        看到的是文件名，而向量其实来自这段文字）。报错会说清该改哪一栏。

        读的是**原文**：媒体向量必须来自文件本身（服务端自己解码），而不是我们转出来的
        Markdown。整份文件会进一次内存——这与解析那一侧同一条路（原文本来就在内存里），
        不额外引入一个"分片读文件"的复杂度。
        """
        parsed = self._stores.meta.get_parse_result(document.id)
        if parsed is None or parsed.parser_name != MediaDirectParser.name:
            return None
        if not embedder.supports_media:
            raise EmbeddingError(
                f"「{document.name}」是图片 / 视频，但知识库当前用的嵌入模型 "
                f"{embedder.model_id or '(未配置)'} 不支持媒体向量。请在「设置 → 模型注册」"
                "里给这个模型声明协议 wemm（或在「向量化」里把它设为默认），"
                "或者换一个支持图片 / 视频的嵌入模型"
            )
        return self._read_original(document)

    # ------------------------------------------------------------------ 补出题（v24）

    def generate_questions(self, document_id: str) -> int:
        """为**已索引**文档的分段补生成问题，返回落库的问题条数。

        与入库时的出题（``_attach_questions``）走同一个出题服务，但**不走阶段机**：
        文档已经 indexed，这里不重新解析、也不重新切块，只读现有的块出题，
        再把含问题的 ``index_text`` 重新向量化并重建全文索引——问题进不了这两条索引，
        "换个问法也能命中同一段"这件事就不会发生。

        与入库时的一条关键差别：入库出题是旁路（失败只让这段没有题，不能让文档 failed），
        这里是**用户显式点出来的动作**，一条题都没出出来就直接抛错，
        让任务以失败收场并带上原因，而不是静默地"成功但什么都没发生"。
        """
        document = self._stores.meta.get_document(document_id)
        if document is None:
            raise NotFoundError(f"文档不存在：{document_id}")
        if document.stage is not DocumentStage.INDEXED:
            raise InvalidRequestError(
                f"这份文档当前处于「{document.stage.value}」，只有已索引完成的文档才能补生成问题"
                "——等它摄入完成，或先用「重新摄入」把它跑完"
            )
        if self._questions is None:
            raise InvalidRequestError("当前服务没有接出题能力，无法生成问题")
        kb = self._stores.meta.get_knowledge_base(document.knowledge_base_id)
        if kb is None:
            raise NotFoundError(f"知识库不存在：{document.knowledge_base_id}")

        chunks = list(self._stores.meta.iter_chunks(document_id))
        if not chunks:
            raise InvalidRequestError("这份文档还没有分段，无法出题")

        generated = self._questions.generate_for_chunks(
            chunks,
            model_pk=kb.suggested_model_pk,
            count=kb.suggested_count,
            prompt=kb.suggested_prompt,
        )
        if not any(generated.values()):
            raise InvalidRequestError(
                "没有生成出任何问题：请确认已配置可用的对话模型，或换一份内容更实的文档再试"
            )

        for chunk in chunks:
            chunk.questions = tuple(generated.get(chunk.chunk_id, ()))
        # 三处用同一份记录同步：元数据（questions 列）→ 全文索引（index_text 分词）
        # → 向量（index_text 嵌入）。顺序与 ChunkService.update_text 一致。
        for chunk in chunks:
            self._stores.meta.update_chunk(chunk)
        self._stores.fulltext.index_chunks(chunks)
        embedder = self._embedder_for(kb)
        self._stores.vectors.ensure_partition(kb.id, dim=embedder.dim)
        vectors = embedder.embed([chunk.index_text for chunk in chunks])
        self._stores.vectors.upsert_vectors(
            kb.id,
            items=[(chunk.chunk_id, vector) for chunk, vector in zip(chunks, vectors, strict=True)],
        )
        total = sum(len(chunk.questions) for chunk in chunks)
        logger.info("文档 %s 补生成问题 %d 条（共 %d 段）", document_id, total, len(chunks))
        return total

    # ------------------------------------------------------------------ 状态与校验

    def _advance(self, document: DocumentRecord, target: DocumentStage) -> None:
        # 取消检查放在**写状态之前**：抢在这一次推进落库前叫停，DB 里才留得住
        # 用户点出来的 canceled。检查放在这里而不是每个阶段的入口，是因为
        # 每个阶段结束都要 _advance —— 它天然就是"上一个动作已完成、下一个还没开始"
        # 的安全切点，云端解析那种没法中途掐断的调用也只有在这里才停得住。
        self._raise_if_canceled(document)
        assert_transition(document.stage, target)
        self._stores.meta.update_document_stage(document.id, target)
        document.stage = target

    def _raise_if_canceled(self, document: DocumentRecord) -> None:
        """发现"本次运行期间被取消"就抛 :class:`IngestCanceled`。

        **判据是"内存里的阶段还不是 canceled、库里的已是"**，而不是"库里是 canceled"：
        后者会把"取消后重新摄入"也当成取消——那次运行的起点本来就是 canceled。
        """
        if document.stage is DocumentStage.CANCELED:
            return
        current = self._stores.meta.get_document(document.id)
        if current is not None and current.stage is DocumentStage.CANCELED:
            raise IngestCanceled(document.id)

    def _fail(self, document: DocumentRecord, message: str, *, stage: str) -> None:
        """失败要落到文档记录上：界面才能显示卡在哪一步、为什么（架构 §12）。"""
        if document.stage not in (DocumentStage.FAILED, DocumentStage.CANCELED):
            self._stores.meta.update_document_stage(
                document.id, DocumentStage.FAILED, error=f"[{stage}] {message}"
            )
            document.stage = DocumentStage.FAILED

    def _resolve_model(self, kb: KnowledgeBaseRecord) -> None:
        """模型锁定（架构 §6.4）：库内已有向量就拒绝换模型；还没有向量则允许更正配置。

        **刻意放在 try 之外**：这是配置错误而非文档处理失败——重试没有意义，
        所以既不把文档标成 ``failed``，也不抛可重试的 :class:`IngestError`。
        """
        embedder = self._embedder_for(kb)
        same_model = kb.embedding_model_id == embedder.model_id
        same_dim = kb.embedding_dim == embedder.dim
        if same_model and same_dim:
            return
        if self._stores.meta.count_kb_chunks(kb.id) == 0:
            self._stores.meta.update_knowledge_base_embedding(
                kb.id,
                model_id=embedder.model_id,
                dim=embedder.dim,
                base_url=None,
            )
            kb.embedding_model_id = embedder.model_id
            kb.embedding_dim = embedder.dim
            return
        raise EmbeddingError(
            f"知识库 {kb.id} 内已有向量化文件（模型 {kb.embedding_model_id}，"
            f"{kb.embedding_dim} 维），不允许改用 {embedder.model_id}"
            f"（{embedder.dim} 维）；需更换模型请新建知识库"
        )

    def _require_kb(self, kb_id: str) -> KnowledgeBaseRecord:
        record = self._stores.meta.get_knowledge_base(kb_id)
        if record is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")
        return record

    def _require_document(self, document_id: str) -> DocumentRecord:
        record = self._stores.meta.get_document(document_id)
        if record is None:
            raise NotFoundError(f"文档不存在：{document_id}")
        return record

    def _read_original(self, document: DocumentRecord) -> bytes:
        path = self._stores.meta.get_setting(f"document.{document.id}.original_path")
        if not path:
            raise ParseError(f"文档 {document.id} 找不到原文路径", stage="probing")
        return self._stores.objects.read(path)

    def _read_markdown(self, document: DocumentRecord) -> str:
        parsed = self._stores.meta.get_parse_result(document.id)
        if parsed is None:
            raise ParseError(f"文档 {document.id} 找不到解析产物", stage="chunking")
        return self._stores.objects.read(parsed.markdown_path).decode("utf-8")


def _before(current: DocumentStage, target: DocumentStage) -> bool:
    """当前阶段是否还在 ``target`` 之前（决定该步骤要不要跑）。"""
    order = (
        DocumentStage.UPLOADED,
        DocumentStage.PROBING,
        DocumentStage.PARSING,
        DocumentStage.PARSED,
        DocumentStage.CHUNKING,
        DocumentStage.CHUNKED,
        DocumentStage.EMBEDDING,
        DocumentStage.INDEXED,
    )
    if current not in order:
        return False
    return order.index(current) < order.index(target)


def _suffix_of(filename: str) -> str:
    return suffix_of(filename)


# --------------------------------------------------------------- 入库看门狗（P0）

STALL_SECONDS = 240.0
"""卡住多久算"这个任务丢了"（首轮阈值）。

**依据**：2026-09-29 能力实测里那 3 份素材（`probe-shapes.jpg` 43 KB、
`probe-复杂表头.xlsx` 5.3 KB、`probe-pure-pixels.png` 5.7 KB）停在 `embedding`
**15~30 分钟**、`error` 为空；而同一天对它们各点一次"重新处理"，**每份约 3 秒**
就跑完了——也就是说"正常该跑多久"是**秒级**，4 分钟离它有三个数量级的余量，
不会被一次慢解析误判（真在跑的解析有租约，压根进不了这个判据，见 `sweep_stalled`）。
看门狗每分钟扫一次（`DEFAULT_WATCHDOG_INTERVAL`），所以最迟 **5 分钟**被发现——
正好落在方案 8 要的"≤5 分钟自动完成或失败"。
"""

STALL_MAX_ROUNDS = 3
"""最多自动重排几次。

**别无限重排**：一次重排可能真的再去花云端额度与 embedding 配额，而卡住的原因
（比如那份文件的解析产物根本对不上）重排 100 次也一样。3 次之后**明确置 `failed`
并写清卡在哪一步、重排过几次**——把它从"无声无息"变成一条用户看得见的原因，
那正是这条 P0 要的东西。"""

STALL_BACKOFF = 2.0
"""每重排一次，下一次判定的阈值翻倍（4 → 8 → 16 分钟）。

与队列自己的指数退避同一个思路（见 `queue_worker.backoff_for`）：真在慢慢跑的任务
（大 PDF 的云端解析）第一轮误判之后，第二轮要等更久才轮得到它，避免反复插队
把正在干活的那一份搅乱。"""


@dataclass(frozen=True, slots=True)
class StalledSweep:
    """一轮看门狗做了什么（便于日志、测试与调用方观察）。"""

    requeued: tuple[tuple[str, str, int], ...] = ()
    """``(document_id, stage, 第几次重排)``。"""

    failed: tuple[tuple[str, str], ...] = ()
    """``(document_id, 给人看的原因)``。"""

    @property
    def touched(self) -> bool:
        return bool(self.requeued or self.failed)


def sweep_stalled(
    stores: StoreBundle,
    *,
    now: datetime | None = None,
    stall_seconds: float = STALL_SECONDS,
    max_rounds: int = STALL_MAX_ROUNDS,
) -> StalledSweep:
    """找出"任务已经没了、文档却停在半路"的那些，重排或判失败。

    **为什么需要它**（这是既有机制的一个真空）：租约回收（`reclaim_expired_tasks`）
    只管"任务还是 `running`、租约过期了"；而**任务已经收场**（成功、失败、
    或重试次数用尽被判失败）**但文档阶段停在主链路中间**时，谁都不会再碰它一下：
    没有待领任务、没有 `error`、界面只说"还在处理"。实测就是这么停 15~30 分钟的。

    判据三条，缺一条都会误伤正在干活的任务：

    1. **文档阶段不是终态**（`TERMINAL_STAGES` 之外）——`indexed` / `failed` /
       `canceled` 都不管，取消是用户的决定，不该被"救"回来；
    2. **它身上没有待执行/执行中的任务**（`active_tasks_by_documents`）——排队中或
       正跑着的一律跳过，这一步让"真在慢慢解析"天然免疫（那些任务还在，且有租约）；
    3. **离最近一次阶段变更已超过本轮阈值**（阈值 = `STALL_SECONDS` × 退避）。

    时间取的是**阶段事件表**里最后一条（`list_document_stage_events`）而不是
    `documents.updated_at`：后者会被改名、写摘要、归档目录这些**与摄入无关**的动作
    顺手刷新，用它当"卡了多久"会让一份真的卡住的文档因为改了个名字就重新计时。
    """
    moment = now or datetime.now(UTC)
    documents = [
        document
        for kb in stores.meta.list_knowledge_bases()
        for document in stores.meta.list_documents(kb.id)
    ]
    candidates = [doc for doc in documents if doc.stage not in TERMINAL_STAGES]
    if not candidates:
        return StalledSweep()

    active = stores.meta.active_tasks_by_documents([doc.id for doc in candidates])
    rounds = _watchdog_rounds(stores)

    requeued: list[tuple[str, str, int]] = []
    failed: list[tuple[str, str]] = []
    for document in candidates:
        if document.id in active:
            # 还有活在它身上：排队中或正在跑（后者有租约在续），不插手
            continue
        done = rounds.get(document.id, 0)
        limit = stall_seconds * (STALL_BACKOFF**done)
        stuck = _stalled_seconds(stores, document, moment)
        if stuck is None or stuck < limit:
            continue

        minutes = stuck / 60.0
        if done >= max_rounds:
            reason = (
                f"[watchdog] 这份文档在「{document.stage.value}」停了 {minutes:.0f} 分钟，"
                f"已自动重排 {done} 次仍未完成；请点「重新处理」，"
                "或到任务中心看它的失败原因（最近一条任务里写着）"
            )
            stores.meta.update_document_stage(document.id, DocumentStage.FAILED, error=reason)
            logger.error(
                "入库看门狗：文档 %s「%s」在 %s 停了 %.0f 分钟，重排 %d 次仍未完成，判为失败",
                document.id,
                document.name,
                document.stage.value,
                minutes,
                done,
            )
            failed.append((document.id, reason))
            continue

        stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=TaskKind.PARSE,
                state=TaskState.PENDING,
                # `watchdog_round` 是**重排次数唯一的落地处**：任务中心里也看得见
                # "这条为什么又冒出来一条"，而下一轮扫描据此算退避与上限。
                payload={"document_id": document.id, "watchdog_round": done + 1},
                document_id=document.id,
            )
        )
        logger.warning(
            "入库看门狗：文档 %s「%s」在 %s 停了 %.0f 分钟（没人跑它了），第 %d 次自动重排",
            document.id,
            document.name,
            document.stage.value,
            minutes,
            done + 1,
        )
        requeued.append((document.id, document.stage.value, done + 1))

    return StalledSweep(requeued=tuple(requeued), failed=tuple(failed))


def _watchdog_rounds(stores: StoreBundle) -> dict[str, int]:
    """每份文档已经被看门狗重排过几次（取它所有看门狗任务里最大的那个轮次）。"""
    rounds: dict[str, int] = {}
    for task in stores.meta.list_tasks():
        if not task.document_id:
            continue
        value = task.payload.get("watchdog_round")
        if isinstance(value, int):
            rounds[task.document_id] = max(rounds.get(task.document_id, 0), value)
    return rounds


def _stalled_seconds(
    stores: StoreBundle, document: DocumentRecord, moment: datetime
) -> float | None:
    """这份文档处在当前阶段多久了（秒）。取不到时间就返回 ``None`` = 不判它。"""
    events = stores.meta.list_document_stage_events(document.id)
    entered = events[-1].entered_at if events else document.updated_at
    if entered is None:
        return None
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=UTC)
    return max(0.0, (moment - entered).total_seconds())


#: 超过这个维度的库**没有向量索引**（pgvector 的 HNSW 上限，见
#: `storage/postgres_impl/vector_store.HNSW_MAX_DIM`）：仍然完全可用，只是检索走
#: 精确扫描。仓储那一层会把这件事作为一句"要告诉用户的话"**返回**（`ensure_partition`），
#: 摄入这一层负责写进日志——用户不必从"检索怎么变慢了"里猜出来。
HNSW_MAX_DIM = 2000


def vectors_for(
    embedder: EmbeddingProvider,
    *,
    texts: list[str],
    media: bytes | None,
) -> list[list[float]]:
    """这一份文档的向量：**文本批量**，或者**一份媒体向量**（每个分段一份）。

    "文本还是媒体"这件事**只在这里分岔一次**（`_embed_and_index` 只负责把
    `media` 算出来）。分成两处判会出现"算的是媒体向量、记的是文本通道"这种错位，
    而它只在检索结果里显形。

    为什么一份媒体向量可以给多个分段：媒体向量描述的是**整份文件**（"这段视频里是什么"），
    而媒体直通那份 Markdown 只有两三行、通常只切出一个分段；真有多个分段时，
    "它们说的都是这一份文件"本身就是对的（语义上不该为此编出几份不同的向量）。
    """
    if media is None:
        return embedder.embed(texts)
    vector = embedder.embed_media(media)
    return [vector for _ in texts]


def normalize_filename(filename: str) -> str:
    """还原 multipart 头里被按 latin-1 解出来的 UTF-8 文件名。

    HTTP 的 ``Content-Disposition`` 头只允许 latin-1，浏览器会把 UTF-8 文件名的
    原始字节直接塞进去。Starlette 按 latin-1 解码这些字节，于是中文名在控制台上
    整片变成乱码，而文件本身没问题（具体样貌见 tests/unit/services/
    test_filename_normalization.py，此处不写进注释：那段乱码里含 U+201E 等字符，
    会被本仓库自己的 emoji 扫描当成违规字符）。

    修法是把这段字符串按 latin-1 编回字节、再按 UTF-8 解一次。**只在能成功还原时采用**
    （`strict`），否则原样返回：纯 ASCII 名或本来就不是这个成因的名字不该被改坏。
    """
    if not filename or filename.isascii():
        return filename
    try:
        recovered = filename.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return filename
    return recovered or filename
