"""切块人工干预（调研报告 G3）。

**为什么需要**：解析器一定会出错——表格被切碎、公式被拆开、页眉页脚混进正文。
原先这些块**只能整篇重跑**，而重跑得到的是同一份结果。成熟产品（RAGFlow / FastGPT /
MaxKB）都允许用户直接改块，因为"解析错了用户自己能修"是知识库质量的最后兜底。

三个动作，语义刻意分开：

| 动作 | 做什么 | 什么时候用 |
|------|--------|-----------|
| **禁用** | 保留块，但**不再参与检索** | 这块切得不好，但还想留着、可能改回来 |
| **编辑** | 改正文并**重新向量化** | 块内容基本对，只有个别字/格式问题 |
| **删除** | 从三处彻底移除（元数据、全文索引、向量） | 这块是垃圾（乱码、页眉页脚） |

**为什么禁用与删除都要有**：只给删除的话，用户面对"可能只是切得不好"的块
只能二选一——忍着或毁掉。而向量/索引都还在，改回来是零成本的。

**跨仓储编排收在这里**：删一个块要同时动 `chunks` 表、FTS 索引、向量分区三处。
按分层纪律（工程规范 §3.3），这种编排属于 services/；存储层各自只负责自己那部分。
"""

from __future__ import annotations

import logging

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.chunking import content_hash_of
from app.services.embedding.base import EmbeddingProvider
from app.services.embedding.resolver import EmbeddingResolver
from app.services.suggested_questions import SuggestedQuestionsService
from app.storage.base import ChunkRecord, StoreBundle

__all__ = ["ChunkService"]

logger = logging.getLogger(__name__)

#: 单个块的正文长度上限。与摄入时的切分上限同量级——
#: 允许用户把块改得比这更长，就等于让"块"这个概念失去意义（检索粒度会崩）
MAX_CHUNK_CHARS = 4000


class ChunkService:
    """块的读、改、禁用、删除。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        embedder: EmbeddingProvider,
        embedders: EmbeddingResolver | None = None,
        questions: SuggestedQuestionsService | None = None,
    ) -> None:
        self._stores = stores
        self._embedder = embedder
        # 手工改块要重算向量，必须用**这个库自己的**嵌入模型（v11）
        self._embedders = embedders
        #: 改正文要重出这一段的题（v23）：不给就只清空，不再生成
        self._questions = questions

    # ------------------------------------------------------------------ 读

    def get(self, chunk_id: str) -> ChunkRecord:
        records = self._stores.meta.get_chunks([chunk_id])
        if not records:
            raise NotFoundError(f"切块不存在：{chunk_id}")
        return records[0]

    # ------------------------------------------------------------------ 改

    def update_text(self, chunk_id: str, text: str) -> ChunkRecord:
        """改正文并重新向量化。

        **必须重新 embedding**：全文索引与向量都指向这段旧文本，只改 ``chunks.text``
        会让三处不一致——检索命中的是旧向量、返回的是新文本，用户看到的
        "为什么这条会被搜出来"完全对不上。这是最隐蔽的一类不一致。

        顺序上**先算向量再落库**：向量算不出来（模型没配、网络抖动）时要整体失败，
        不能留下"文本改了但向量还是旧的"这种半成品状态。
        """
        cleaned = text.strip()
        if not cleaned:
            raise InvalidRequestError("切块正文不能为空")
        if len(cleaned) > MAX_CHUNK_CHARS:
            raise InvalidRequestError(
                f"切块正文超过 {MAX_CHUNK_CHARS} 字上限。"
                "内容确实这么长的话，更好的做法是拆成两块——"
                "块太长会稀释检索精度"
            )

        record = self.get(chunk_id)
        previous_text = record.text
        previous_questions = tuple(record.questions)

        # 1) 先算新问题与新向量（失败即整体失败，不留半成品）
        #
        # 正文改了，**旧问题是照着旧正文出的**——必须一起重出，否则那些问题会把
        # 用户带到一段已经不存在的正文上。重出失败就清空：宁可这一段没有题，
        # 也不能留一批对不上正文的问题在索引里（检索会一直命中它）。
        record.text = cleaned
        record.questions = self._regenerate_questions(record)
        vector = self._embedder_for(record.knowledge_base_id).embed([record.index_text])[0]

        # 2) 更新元数据（questions 一起写回，见 update_chunk 的 SQL）
        record.content_hash = content_hash_of(cleaned)
        self._stores.meta.update_chunk(record)

        # 3) 同步三处：全文索引（整体替换该块）、向量（upsert）
        try:
            self._stores.fulltext.delete_chunks([chunk_id])
            self._stores.fulltext.index_chunks([record])
            self._stores.vectors.upsert_vectors(
                record.knowledge_base_id, items=[(chunk_id, vector)]
            )
        except Exception:
            # 索引没跟上，但元数据已经改了——回滚文本，保证三处一致（宁可回到旧状态）
            logger.exception("切块 %s 的索引更新失败，回滚正文", chunk_id)
            record.text = previous_text
            record.questions = previous_questions
            record.content_hash = content_hash_of(previous_text)
            self._stores.meta.update_chunk(record)
            self._stores.fulltext.delete_chunks([chunk_id])
            self._stores.fulltext.index_chunks([record])
            raise

        logger.info("切块 %s 正文已更新并重新向量化", chunk_id)
        return record

    def _regenerate_questions(self, record: ChunkRecord) -> tuple[str, ...]:
        """为刚改过的这一段重出题；库上关着或出题失败就返回空（= 清空）。

        **清空而不是保留旧的**：问题指向的是旧正文，留着它等于给检索埋了一条
        "命中一段已经改掉的文字"的假线索。
        """
        if self._questions is None:
            return ()
        kb = self._stores.meta.get_knowledge_base(record.knowledge_base_id)
        if kb is None or not kb.suggested_enabled:
            return ()
        try:
            generated = self._questions.generate_for_chunks(
                [record],
                model_pk=kb.suggested_model_pk,
                count=kb.suggested_count,
                prompt=kb.suggested_prompt,
            )
        except Exception:
            logger.warning("切块 %s 重出题失败，清空它的问题", record.chunk_id, exc_info=True)
            return ()
        return tuple(generated.get(record.chunk_id, ()))

    # ------------------------------------------------------------------ 禁用

    def set_disabled(self, chunk_id: str, *, disabled: bool) -> ChunkRecord:
        """禁用/恢复一个块。

        **只动一个标记**，不碰向量与索引：检索侧按标记过滤（见
        ``RetrievalService``）。这样恢复是零成本的——不用重新 embedding，
        也不会因为"删了又加"而丢掉原有的向量。
        """
        record = self.get(chunk_id)
        self._stores.meta.set_chunk_disabled(chunk_id, disabled=disabled)
        record.disabled = disabled
        logger.info("切块 %s %s", chunk_id, "已禁用" if disabled else "已恢复")
        return record

    # ------------------------------------------------------------------ 删

    def delete(self, chunk_id: str) -> None:
        """彻底删除一个块。

        **三处都要清**，少一处就会留下幽灵：
        - 元数据：块本身；
        - 全文索引：不清的话仍会被 BM25 召回，然后"命中了却查不到正文"；
        - 向量：不清的话向量通道仍会召回它。

        删除后**重排该文档剩余块的 ordinal**：ordinal 是界面上的"第 N 块"，
        留着空洞会让用户看到"第 1、2、4、5 块"而以为丢了数据。
        """
        record = self.get(chunk_id)
        document_id = record.document_id
        kb_id = record.knowledge_base_id

        self._stores.vectors.delete_vectors(kb_id, chunk_ids=[chunk_id])
        self._stores.fulltext.delete_chunks([chunk_id])
        self._stores.meta.delete_chunk(chunk_id)

        self._renumber(document_id)
        logger.info("切块 %s 已删除（连同索引与向量）", chunk_id)

    def _embedder_for(self, kb_id: str) -> EmbeddingProvider:
        if self._embedders is None:
            return self._embedder
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            return self._embedder
        return self._embedders.for_kb(kb)

    def _renumber(self, document_id: str) -> None:
        """把该文档剩余的块重新编号成连续的 0..n-1。"""
        remaining = list(self._stores.meta.iter_chunks(document_id))
        for index, record in enumerate(remaining):
            if record.ordinal == index:
                continue
            record.ordinal = index
            self._stores.meta.update_chunk(record)
