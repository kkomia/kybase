"""文档摘要（v25）。

镜像同构：``app/services/summary.py`` → 本文件。

关键就两条：**采样要覆盖整篇**（只取开头会拿到标题页、只取结尾会拿到参考文献——
用户那次答不好的问题正是踩在"命中了一条参考文献"上），以及**摘要必须短**：
它是一次问答里要被复用的东西，超过 220 字就不再比"直接给片段"省 token。
"""

from __future__ import annotations

from app.models.enums import DataSourceKind, DocumentStage
from app.services.summary import (
    SUMMARY_MAX_CHARS,
    DocumentSummaryService,
    _clean,
    _sample,
)
from app.storage.base import ChunkRecord, DocumentRecord, KnowledgeBaseRecord


class FakeChat:
    """假对话模型：只实现 ``ask_raw``，记录收到的提示词。"""

    def __init__(
        self, reply: str = "这是一篇关于绿地与近视的系统综述，梳理了队列研究证据。"
    ) -> None:
        self.reply = reply
        self.calls = 0
        self.last_prompt = ""

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_prompt = messages[-1].content
        return self.reply


class BoomChat:
    def __init__(self) -> None:
        self.calls = 0

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("上游挂了")


def _chunk(ordinal: int, text: str = "", document_id: str = "doc_1") -> ChunkRecord:
    # chunk_id 要带文档前缀：多篇文档共用 c0/c1 会撞主键（第一版就是这么挂的）
    return ChunkRecord(
        chunk_id=f"{document_id}-c{ordinal}",
        document_id=document_id,
        knowledge_base_id="kb_1",
        part_id=None,
        ordinal=ordinal,
        text=text or f"第 {ordinal} 段：这里是正文内容。",
        content_hash=f"h{ordinal}",
    )


# --------------------------------------------------------------------- 采样


def test_sample_spreads_across_the_whole_document() -> None:
    """跨整篇均匀取，**结尾一定要在**（结论/局限在最后几段）。"""
    chunks = [_chunk(index) for index in range(50)]

    picked = _sample(chunks)

    assert len(picked) == 5
    assert picked[0].ordinal == 0
    assert picked[-1].ordinal == 49  # 尾巴必须被取到
    # 不能只取开头：中间要有
    assert any(10 <= chunk.ordinal <= 40 for chunk in picked)


def test_sample_keeps_everything_when_the_document_is_short() -> None:
    chunks = [_chunk(index) for index in range(3)]

    assert [chunk.ordinal for chunk in _sample(chunks)] == [0, 1, 2]


# --------------------------------------------------------------------- 清洗


def test_clean_strips_prefix_and_caps_length() -> None:
    assert _clean("摘要：这是一篇讲绿地的文档。") == "这是一篇讲绿地的文档。"
    assert _clean("  多   空格\n换行  ") == "多 空格 换行"
    assert len(_clean("啊" * 500)) == SUMMARY_MAX_CHARS


# --------------------------------------------------------------------- 生成


def test_summarize_writes_to_the_document(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed(bundle, kb, document, 12)
    chat = FakeChat()

    summary = DocumentSummaryService(bundle, chat).summarize(document.id)

    assert summary.startswith("这是一篇关于绿地")
    assert bundle.meta.get_document(document.id).summary == summary
    # 输入里要有文件名（线索）与若干片段
    assert "片段" in chat.last_prompt


def test_the_prompt_tells_the_model_the_filename_may_be_wrong(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """学术 PDF 的文件名常是"作者_年份_标题"的拼接，不能让模型照抄成标题。"""
    _seed(bundle, kb, document, 6)
    chat = FakeChat()

    DocumentSummaryService(bundle, chat).summarize(document.id)

    assert "可能不准确" in chat.last_prompt


def test_summarize_never_raises(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """摘要是旁路能力：模型挂了只该让这篇"没有摘要"，绝不能让文档 failed。"""
    _seed(bundle, kb, document, 6)
    chat = BoomChat()

    summary = DocumentSummaryService(bundle, chat).summarize(document.id)

    assert summary == ""
    assert bundle.meta.get_document(document.id).summary == ""
    assert chat.calls == 1


def test_disabled_makes_zero_model_calls(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """关掉时**一次调用都不发**——这是那个开关的主要意义（它在花真金白银）。"""
    _seed(bundle, kb, document, 6)
    chat = FakeChat()

    service = DocumentSummaryService(bundle, chat, enabled=lambda: False)

    assert service.summarize(document.id) == ""
    assert service.summarize_missing() == 0
    assert chat.calls == 0


def test_document_without_chunks_gets_no_summary(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    chat = FakeChat()

    assert DocumentSummaryService(bundle, chat).summarize(document.id) == ""
    assert chat.calls == 0  # 没内容就别浪费一次调用


# --------------------------------------------------------------------- 补漏


def test_summarize_missing_only_touches_indexed_documents(
    bundle, kb, document
) -> None:  # type: ignore[no-untyped-def]
    """只补**已索引且没有摘要**的：没跑完的文档块还没定稿，摘要写出来就得重写。"""
    _seed(bundle, kb, document, 4)
    pending = _second_document(bundle, kb, "doc_pending", DocumentStage.PARSING)
    _seed(bundle, kb, pending, 4, DocumentStage.PARSING)
    chat = FakeChat()

    done = DocumentSummaryService(bundle, chat).summarize_missing(limit=5)

    assert done == 1
    assert bundle.meta.get_document(document.id).summary != ""
    assert bundle.meta.get_document(pending.id).summary == ""


def test_summarize_missing_stops_at_the_batch_size(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """一次只补一小批：它跑在空闲分支上，做太多会把空闲时间与配额一次吃光。"""
    _seed(bundle, kb, document, 3)
    for index in range(3):
        other = _second_document(bundle, kb, f"doc_x{index}", DocumentStage.INDEXED)
        _seed(bundle, kb, other, 3)
    chat = FakeChat()

    done = DocumentSummaryService(bundle, chat).summarize_missing(limit=2)

    assert done == 2
    assert chat.calls == 2


def test_summarize_missing_is_idempotent(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """补过的不会再补（第二次应当一篇都不处理）。"""
    _seed(bundle, kb, document, 3)
    chat = FakeChat()
    service = DocumentSummaryService(bundle, chat)

    assert service.summarize_missing(limit=5) == 1
    assert service.summarize_missing(limit=5) == 0
    assert chat.calls == 1


# --------------------------------------------------------------------- 助手


def _seed(
    bundle, kb, document, count: int, stage: DocumentStage = DocumentStage.INDEXED
) -> None:  # type: ignore[no-untyped-def]
    """给文档塞 count 个块，并把它推到指定阶段（默认 indexed）。

    补漏只处理已索引的文档，而 fixture 建出来是 uploaded——不推阶段的话
    "补漏"这条路径根本看不到它。
    """
    chunks = [_chunk(index, document_id=document.id) for index in range(count)]
    bundle.meta.replace_chunks(document.id, chunks)
    bundle.meta.update_document_stage(document.id, stage)


def _second_document(
    bundle, kb: KnowledgeBaseRecord, document_id: str, stage: DocumentStage
) -> DocumentRecord:  # type: ignore[no-untyped-def]
    record = DocumentRecord(
        id=document_id,
        knowledge_base_id=kb.id,
        name=f"{document_id}.pdf",
        source_kind=DataSourceKind.UPLOAD,
        content_hash=f"hash-{document_id}",
        stage=stage,
    )
    bundle.meta.create_document(record)
    return record
