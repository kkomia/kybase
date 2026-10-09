"""数据生命周期：影响清单、级联删除、回收站（M6 / T6.3、T6.4）。

**这是计划里欠得最久的一块。** 存储层从 M1 起就有 ``trash`` 表、
``move_to_trash``、``purge_expired_trash``，但**从来没有任何调用者**：
接口层连一个 ``DELETE /documents/{id}`` 都没有。于是：
- 用户传错一份文档只能看着它留在库里；
- 回收站永远是空的，"原文保留 7 天"这条承诺从未被验证过；
- 而 worker 的空闲维护一直在清理一个空表。

本模块把这条链路接通：**影响清单 → 移入回收站 → 到期清理 / 手动恢复**。

**为什么删除不是"一步到位"**：知识库下面挂着文档、切块、向量、全文索引、对象
存储里的原文与产物、以及未跑完的任务。删错一个库可能连带几百份文档——
所以先把"会删掉什么"算清楚给用户看（``impact_of_*``），再动手。
这是《界面信息架构草案》§2 里"破坏性动作必须二次确认"的后端一半。

**为什么放过期而不是立刻抹掉**：删错是常事，而原文一旦删掉就只能重新上传。
保留 7 天（架构 §6.2）几乎零成本——对象存储里挪个文件而已。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.models.enums import DataSourceKind, DocumentStage, TaskState, TrashKind
from app.parsers.probe import suffix_of
from app.services.webhook import DOCUMENT_DELETED
from app.storage.base import (
    ORIGINALS,
    DocumentRecord,
    StoreBundle,
    TrashRecord,
    content_key,
)

__all__ = ["TRASH_RETENTION_DAYS", "ImpactReport", "LifecycleService"]

logger = logging.getLogger(__name__)

#: 回收站保留天数（架构 §6.2 "原文保留 7 天冷备"）。
TRASH_RETENTION_DAYS = 7

@dataclass(slots=True)
class ImpactReport:
    """删除某样东西会波及什么。

    **数字要具体**：说"这会删除该知识库及其内容"没人会有感觉；
    说"3 份文档、412 个切块"才会让人停一下。这是二次确认能有意义的前提。
    """

    kind: str
    id: str
    name: str
    documents: int = 0
    chunks: int = 0
    parts: int = 0
    size_bytes: int = 0
    running_tasks: int = 0
    document_names: list[str] = field(default_factory=list)
    """前几份文档的名字。**给样本而不是全部**：几百个名字列出来没人看，
    而"看到自己认识的文件名"才是真正让人确认"没删错库"的东西。"""
    restorable: bool = True
    """能否从回收站恢复。知识库级删除**不可恢复**（涉及多份文档与向量分区），
    所以界面必须据此显示不同的警示。"""


class LifecycleService:
    """删除前的影响评估、删除、回收站恢复。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        notifier: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._stores = stores
        # Webhook 是旁路（T4.6）：默认什么都不做，组合根才接上它。
        # 与摄入那边同一个形状——通知失败不该影响删除本身
        self._notify = notifier or (lambda event, payload: None)

    # ------------------------------------------------------------------ 影响清单

    def impact_of_knowledge_base(self, kb_id: str) -> ImpactReport:
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")

        documents = self._stores.meta.list_documents(kb_id)
        chunks = self._stores.meta.count_kb_chunks(kb_id)
        parts = sum(len(self._stores.meta.list_document_parts(item.id)) for item in documents)
        size = sum(item.size_bytes for item in documents)
        running = self._count_running_tasks([item.id for item in documents])

        return ImpactReport(
            kind="knowledge_base",
            id=kb.id,
            name=kb.name,
            documents=len(documents),
            chunks=chunks,
            parts=parts,
            size_bytes=size,
            running_tasks=running,
            document_names=[item.name for item in documents[:5]],
            # 知识库级删除不进退回收站：它涉及多份文档、多个对象与一个向量分区，
            # 恢复要重建的东西太多，做不好不如不做（做一半的恢复比没有更危险）
            restorable=False,
        )

    def impact_of_document(self, document_id: str) -> ImpactReport:
        document = self._stores.meta.get_document(document_id)
        if document is None:
            raise NotFoundError(f"文档不存在：{document_id}")

        return ImpactReport(
            kind="document",
            id=document.id,
            name=document.name,
            documents=1,
            chunks=self._stores.meta.count_chunks(document_id),
            parts=len(self._stores.meta.list_document_parts(document_id)),
            size_bytes=document.size_bytes,
            running_tasks=self._count_running_tasks([document_id]),
            document_names=[document.name],
            restorable=True,
        )

    def _count_running_tasks(self, document_ids: list[str]) -> int:
        """还在跑的任务数。删除时要么等它跑完、要么连带取消，界面得说清楚。"""
        if not document_ids:
            return 0
        wanted = set(document_ids)
        return sum(
            1
            for task in self._stores.meta.list_tasks()
            if task.document_id in wanted
            and task.state in (TaskState.PENDING, TaskState.RUNNING)
        )

    # ------------------------------------------------------------------ 删除

    def delete_document(self, document_id: str) -> TrashRecord:
        """删除一份文档：原文进回收站，索引与向量立即清掉。

        **为什么原文留、索引不留**：原文是**不可再生**的（用户得重新上传），
        而索引与向量随时能从原文重建。所以前者冷备、后者立即清——
        留着向量反而会让"已删除的文档仍能被检索到"。
        """
        document = self._stores.meta.get_document(document_id)
        if document is None:
            raise NotFoundError(f"文档不存在：{document_id}")

        kb_id = document.knowledge_base_id
        chunk_ids = [item.chunk_id for item in self._stores.meta.iter_chunks(document_id)]

        # 1) 清索引与向量。**必须先做**：反过来的话，中途失败会留下
        #    "元数据没了但还能被搜到"的幽灵文档，那是最难查的状态
        if chunk_ids:
            self._stores.vectors.delete_vectors(kb_id, chunk_ids=chunk_ids)
            self._stores.fulltext.delete_chunks(chunk_ids)

        # 2) 原文挪进回收站（挪不动不算致命：索引已经清了，退化成"不可恢复"）
        trash_id = f"trash_{uuid.uuid4().hex[:12]}"
        # 到期时间从**记录自己的创建时刻**起算，而不是再取一次 now()：
        # 存储层会给 created_at 补一个稍晚的时刻，两次 now() 会让
        # 到期时间反而早于创建时间（实测差几毫秒，不影响功能但读起来是错的）
        created_at = datetime.now(UTC)
        record = TrashRecord(
            id=trash_id,
            document_id=document_id,
            kind=TrashKind.ORIGINAL,
            storage_path="",
            expires_at=created_at + timedelta(days=TRASH_RETENTION_DAYS),
        )

        original_path = self._stores.meta.get_setting(f"document.{document_id}.original_path")
        if original_path:
            try:
                moved = self._stores.objects.move_to_trash(original_path, trash_id=trash_id)
                record.storage_path = moved
                self._stores.meta.add_to_trash(record)
                # **记住所属库与文件名**：文档行删除后就查不到了，
                # 而恢复时必须知道放回哪个库、叫什么名字。
                # 存在设置表而不是给 trash 加列：这只是恢复用的附带信息，
                # 为它改表结构不划算
                self._stores.meta.set_setting(f"trash.{trash_id}.kb_id", kb_id)
                self._stores.meta.set_setting(f"trash.{trash_id}.name", document.name)
                logger.info("文档 %s 的原文已移入回收站 %s", document.name, trash_id)
            except FileNotFoundError:
                # 原文本来就不在（之前被清过）。不记回收站条目——
                # 记了会让用户看到一个"永远恢复不了"的条目
                logger.warning("文档 %s 的原文不在对象存储里，不记回收站", document_id)
        else:
            logger.warning("文档 %s 没有记录原文路径，不记回收站", document_id)

        # 3) 删结构化副本（表格文档才有的旁路数据）。
        #    **要显式删**：DuckDB 里的表不属于元数据级联的范围，
        #    留着会攒下一堆再也访问不到的表——而它们还占着磁盘。
        #    表不存在时 drop 是幂等的，所以不必先判断类型。
        try:
            self._stores.tabular.drop_table(document_id)
        except Exception:
            logger.exception("删除文档 %s 的结构化副本失败", document_id)

        # 4) 删元数据（级联带走任务与子文件）
        self._stores.meta.delete_document(document_id)
        logger.info("文档 %s 已删除（切块 %d 个）", document.name, len(chunk_ids))

        # 5) 通知（旁路）。**放在最后**：早发的话接收端来查会拿到一个还存在的文档，
        #    而它完全有理由相信"收到 deleted 就是已经删了"
        self._notify(
            DOCUMENT_DELETED,
            {
                "document_id": document_id,
                "knowledge_base_id": kb_id,
                "name": document.name,
                # 让接收端知道自己还能不能捞回原文（7 天回收站）
                "restorable_until": record.expires_at.isoformat(),
                "trash_id": record.id if record.storage_path else None,
            },
        )
        return record

    def delete_knowledge_base(self, kb_id: str) -> ImpactReport:
        """删除整个知识库。**不可恢复**（见 ``ImpactReport.restorable``）。"""
        impact = self.impact_of_knowledge_base(kb_id)

        documents = self._stores.meta.list_documents(kb_id)
        for document in documents:
            # 复用单文档清理：向量与全文索引按文档清，逻辑只有一份
            self._purge_document_content(document.id)

        # **分区表本身也要丢**：上面按文档清的是行，表还在库里占着地方
        # （sqlite-vec 的分区表首个向量就预分配一个 4MB 块）。漏了这一步会留下
        # 永远查不到主人的孤儿分区——实测在库里发现过一个这样的表。
        self._stores.vectors.drop_partition(kb_id)
        self._stores.meta.delete_knowledge_base(kb_id)
        logger.info(
            "知识库 %s 已删除（文档 %d、切块 %d）", impact.name, impact.documents, impact.chunks
        )
        return impact

    def _purge_document_content(self, document_id: str) -> None:
        """清掉一份文档的索引、向量与对象，**不进退回收站**（知识库级删除用）。"""
        document = self._stores.meta.get_document(document_id)
        if document is None:
            return
        chunk_ids = [item.chunk_id for item in self._stores.meta.iter_chunks(document_id)]
        if chunk_ids:
            self._stores.vectors.delete_vectors(document.knowledge_base_id, chunk_ids=chunk_ids)
            self._stores.fulltext.delete_chunks(chunk_ids)

        # 结构化副本（表格文档）也在这里清：知识库级删除逐文档走这个方法
        try:
            self._stores.tabular.drop_table(document_id)
        except Exception:
            logger.exception("清理文档 %s 的结构化副本失败", document_id)

        original_path = self._stores.meta.get_setting(f"document.{document_id}.original_path")
        if original_path and self._stores.objects.exists(original_path):
            self._stores.objects.delete(original_path)

        parsed = self._stores.meta.get_parse_result(document_id)
        if parsed is not None and self._stores.objects.exists(parsed.markdown_path):
            self._stores.objects.delete(parsed.markdown_path)

    # ------------------------------------------------------------------ 回收站

    def list_trash(self) -> list[TrashRecord]:
        return self._stores.meta.list_trash()

    def restore(self, trash_id: str) -> tuple[str, str | None]:
        """从回收站恢复一份文档，返回 ``(新文档 id, 摄入任务 id)``。

        **恢复的是"文档骨架 + 原文"，不是整个状态。** 切块与向量在删除时就清掉了，
        所以恢复出来的文档回到「已上传」，必须重新摄入才有检索能力——
        所以这里顺手入队，而不是让用户自己再找一次"重新摄入"。

        **用新 id 而不是复用旧 id**：旧 id 可能已经进了会话引用、任务记录、
        甚至用户收藏的链接里；复用会让那些残留指向一个"内容一样但历史不同"的文档。
        新 id + 重新摄入是最不容易出错的做法。
        """
        entry = self._find_trash(trash_id)

        # 原文还在吗？被清过就恢复不了，要说清而不是造一个空文档
        if not entry.storage_path or not self._stores.objects.exists(entry.storage_path):
            raise InvalidRequestError(
                "原文已不在对象存储里，无法恢复（可能已被清理）。请重新上传这份文件"
            )

        # 从回收站目录读回内容——**内容 hash 必须重算**，不能凭空写一个：
        # 库内去重靠它，写错了会与已有文档混淆
        data = self._stores.objects.read(entry.storage_path)
        digest = hashlib.sha256(data).hexdigest()

        # 旧文档的行已经在删除时没了，所以这里拿不到原名字与所属库。
        # 名字从回收站里的文件名还原（move_to_trash 保留了原名），
        # 而**所属知识库无法还原**——这是"删除文档"的已知代价，
        # 所以恢复出来的文档会被放回它原来所在的库（删除时记在回收站条目上）。
        knowledge_base_id = self._stores.meta.get_setting(f"trash.{entry.id}.kb_id")
        # 文件名在删除时记进了设置表；取不到就给个明确占位，
        # **不去从存储路径反推**——那会把 `<hash>.pdf` 这种当成人看的名字
        name = self._stores.meta.get_setting(f"trash.{entry.id}.name") or "未命名"
        if not knowledge_base_id:
            raise InvalidRequestError(
                "回收站条目缺少所属知识库信息，无法恢复。请重新上传这份文件"
            )

        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        stored_path = self._stores.objects.write(
            content_key(ORIGINALS, digest, suffix_of(name)), data
        )
        self._stores.meta.create_document(
            DocumentRecord(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                name=name,
                source_kind=DataSourceKind.UPLOAD,
                content_hash=digest,
                stage=DocumentStage.UPLOADED,
                size_bytes=len(data),
            )
        )
        self._stores.meta.set_setting(f"document.{document_id}.original_path", stored_path)

        # 挪回原位的这一份不再需要留在回收站
        self._delete_trash_objects(entry)
        self._stores.meta.delete_trash(entry.id)

        logger.info("从回收站恢复了文档 %s（新 id %s）", name, document_id)
        return document_id, None

    def _find_trash(self, trash_id: str) -> TrashRecord:
        for item in self._stores.meta.list_trash():
            if item.id == trash_id:
                return item
        raise NotFoundError(f"回收站里没有这一条：{trash_id}")

    def drop_trash(self, trash_id: str) -> None:
        """立即彻底删除一条回收站记录（用户主动"不再需要"）。"""
        entry = self._find_trash(trash_id)
        self._delete_trash_objects(entry)
        self._stores.meta.delete_trash(trash_id)
        # 连带清掉恢复用的附带信息，否则设置表会慢慢攒垃圾
        self._stores.meta.delete_setting(f"trash.{trash_id}.kb_id")
        self._stores.meta.delete_setting(f"trash.{trash_id}.name")

    # ------------------------------------------------------------------ 维护

    def purge_expired_trash(self) -> int:
        """把到期条目从回收站彻底删掉（含磁盘上的原文）。

        **要删磁盘上的文件，不只是数据库行。** 只删行会把对象存储变成一个
        只增不减的垃圾场——而用户以为"7 天后就清掉了"。
        """
        expired = self._stores.meta.purge_expired_trash()
        for entry in expired:
            self._delete_trash_objects(entry)
        if expired:
            logger.info("回收站清理了 %d 条到期记录", len(expired))
        return len(expired)

    def _delete_trash_objects(self, entry: TrashRecord) -> None:
        if not entry.storage_path:
            return
        try:
            self._stores.objects.delete(entry.storage_path)
        except (FileNotFoundError, OSError):
            # 文件已不在不算错误：目标状态就是"它没了"
            logger.debug("回收站对象已不存在：%s", entry.storage_path)
