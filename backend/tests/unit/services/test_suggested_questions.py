"""推荐问题：入库出题（写端）与空状态取题（读端）。

镜像同构：``app/services/suggested_questions.py`` → 本文件。

要紧的四条：
1. **写端从不抛**——它是摄入链路上的旁路，出题失败只能让这一段没有题，
   不能让整篇文档 failed；
2. 写端**分批**（一份 100 段的文档不能变成 100 次模型调用）；
3. 解析时**宁缺勿错**——编号对不上的块整块丢掉，不能把 A 段的问题写到 B 段上；
4. 读端**不调模型**，只从库里已存的问题里取；没有就返回空（前端回退静态样例）。
"""

from __future__ import annotations

import time

from app.models.enums import DataSourceKind, DocumentStage
from app.services.suggested_questions import (
    _CHUNKS_PER_CALL,
    SuggestedQuestionsService,
    _parse_blocks,
    _parse_questions,
)
from app.storage.base import ChunkRecord, DocumentRecord, KnowledgeBaseRecord


class FakeChat:
    """假的对话模型：只实现 ``ask_raw``，记录调用次数、收到的提示词与 model_pk。"""

    def __init__(self, reply: str = "1. 第一个问题？\n2. 第二个问题？") -> None:
        self.reply = reply
        self.calls = 0
        self.last_prompt = ""
        self.last_model: str | None = None

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_prompt = messages[-1].content
        self.last_model = model_pk
        return self.reply


class BoomChat:
    def __init__(self) -> None:
        self.calls = 0

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("上游挂了")


def _chunk(
    kb_id: str,
    document_id: str,
    ordinal: int,
    *,
    prefix: str = "c",
    questions: tuple[str, ...] = (),
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{prefix}{ordinal}",
        document_id=document_id,
        knowledge_base_id=kb_id,
        part_id=None,
        ordinal=ordinal,
        text=f"第 {ordinal} 段原文，讲的是眼轴测量与近视防控。",
        content_hash=f"h{prefix}{ordinal}",
        questions=questions,
    )


def _seed_chunks(
    bundle,  # type: ignore[no-untyped-def]
    kb_id: str,
    document_id: str,
    count: int = 2,
    prefix: str = "c",
    questions: dict[int, tuple[str, ...]] | None = None,
) -> None:
    bundle.meta.replace_chunks(
        document_id,
        [
            _chunk(
                kb_id,
                document_id,
                index,
                prefix=prefix,
                questions=(questions or {}).get(index, ()),
            )
            for index in range(count)
        ],
    )


# --------------------------------------------------------------------- 清洗


def test_parse_strips_numbering_quotes_and_dedupes() -> None:
    raw = '1. 第一个问题？\n- 第一个问题？\n"第二个问题？"\n\n第三个问题？\n2) 第四个问题？'

    assert _parse_questions(raw, limit=5) == [
        "第一个问题？",
        "第二个问题？",
        "第三个问题？",
        "第四个问题？",
    ]


def test_parse_keeps_a_question_that_starts_with_a_number() -> None:
    """只吃行首的编号前缀，不能把"2024 年…"这类以数字开头的问题削掉。"""
    assert _parse_questions("2024 年的指南怎么说？", limit=3) == ["2024 年的指南怎么说？"]


def test_parse_respects_the_limit_and_drops_overlong_lines() -> None:
    raw = "问题一？\n问题二？\n问题三？\n" + "很长" * 80 + "？"
    assert _parse_questions(raw, limit=2) == ["问题一？", "问题二？"]


# --------------------------------------------------------------------- 按段拆块


def test_parse_blocks_maps_each_block_to_its_chunk() -> None:
    chunks = [_chunk("kb", "d", 0), _chunk("kb", "d", 1)]
    raw = "###片段1\n眼轴怎么测？\n多久测一次？\n\n###片段2\n近视怎么防控？"

    assert _parse_blocks(raw, chunks, limit=3) == {
        "c0": ["眼轴怎么测？", "多久测一次？"],
        "c1": ["近视怎么防控？"],
    }


def test_parse_blocks_is_tolerant_about_the_header_style() -> None:
    chunks = [_chunk("kb", "d", 0), _chunk("kb", "d", 1)]
    raw = "## 片段1\n问题一？\n### 片段 2\n问题二？"

    assert _parse_blocks(raw, chunks, limit=3) == {"c0": ["问题一？"], "c1": ["问题二？"]}


def test_parse_blocks_drops_out_of_range_indices() -> None:
    """编号对不上的块整块丢掉——**宁可这一段没题，也不能把题写到别的段上**
    （那会让检索把用户带到一段完全无关的正文）。"""
    chunks = [_chunk("kb", "d", 0)]
    raw = "###片段1\n问题一？\n###片段7\n这是别的段的题？"

    assert _parse_blocks(raw, chunks, limit=3) == {"c0": ["问题一？"]}


def test_parse_blocks_treats_a_lone_chunk_as_the_whole_output() -> None:
    """只带了一段时，模型经常"贴心"地省掉块头——这时整份输出都算它的。"""
    chunks = [_chunk("kb", "d", 0)]

    assert _parse_blocks("眼轴怎么测？\n多久测一次？", chunks, limit=3) == {
        "c0": ["眼轴怎么测？", "多久测一次？"]
    }


# --------------------------------------------------------------------- 写端


def test_generate_asks_once_per_batch(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """一批（8 段）一次请求：一份 100 段的文档不能变成 100 次调用。"""
    _seed_chunks(bundle, kb.id, document.id, count=_CHUNKS_PER_CALL + 2)
    chat = FakeChat(reply="###片段1\n问题一？")
    service = SuggestedQuestionsService(bundle, chat)

    chunks = list(bundle.meta.iter_chunks(document.id))
    service.generate_for_chunks(chunks, count=2)

    assert chat.calls == 2


def test_generate_returns_questions_per_chunk(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed_chunks(bundle, kb.id, document.id, count=2)
    chat = FakeChat(reply="###片段1\n眼轴怎么测？\n###片段2\n近视怎么防控？")
    service = SuggestedQuestionsService(bundle, chat)

    generated = service.generate_for_chunks(
        list(bundle.meta.iter_chunks(document.id)), count=2, model_pk="mdl_out"
    )

    assert generated == {"c0": ["眼轴怎么测？"], "c1": ["近视怎么防控？"]}
    assert chat.last_model == "mdl_out"


def test_generate_passes_the_custom_prompt_with_the_count(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed_chunks(bundle, kb.id, document.id, count=1)
    chat = FakeChat()
    service = SuggestedQuestionsService(bundle, chat)

    service.generate_for_chunks(
        list(bundle.meta.iter_chunks(document.id)), count=2, prompt="按诊断标准出题。{n}"
    )

    assert "按诊断标准出题。2" in chat.last_prompt
    assert "资料片段：" in chat.last_prompt


def test_generate_falls_back_to_the_builtin_prompt(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed_chunks(bundle, kb.id, document.id, count=1)
    chat = FakeChat()
    service = SuggestedQuestionsService(bundle, chat)

    service.generate_for_chunks(list(bundle.meta.iter_chunks(document.id)), count=3)

    assert "各写 3 个中文问题" in chat.last_prompt


def test_generate_swallows_model_failures(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """**从不抛**：出题是摄入链路的旁路，失败只能让这一段没有题。"""
    _seed_chunks(bundle, kb.id, document.id, count=2)
    service = SuggestedQuestionsService(bundle, BoomChat())

    assert service.generate_for_chunks(list(bundle.meta.iter_chunks(document.id))) == {}


def test_generate_without_chunks_makes_no_call(bundle) -> None:  # type: ignore[no-untyped-def]
    chat = FakeChat()
    service = SuggestedQuestionsService(bundle, chat)

    assert service.generate_for_chunks([]) == {}
    assert chat.calls == 0


# --------------------------------------------------------------------- 读端


def test_list_questions_reads_what_was_stored(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed_chunks(
        bundle,
        kb.id,
        document.id,
        count=2,
        questions={0: ("眼轴怎么测？", "多久测一次？"), 1: ("近视怎么防控？",)},
    )
    chat = FakeChat()
    service = SuggestedQuestionsService(bundle, chat)

    questions = service.list_questions(kb_ids=[kb.id], limit=10)

    assert set(questions) == {"眼轴怎么测？", "多久测一次？", "近视怎么防控？"}
    # **不再调模型**：这是 v23 与之前最大的区别
    assert chat.calls == 0


def test_list_questions_dedupes_and_caps(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    _seed_chunks(
        bundle,
        kb.id,
        document.id,
        count=3,
        questions={0: ("同一个问题？",), 1: ("同一个问题？", "第二个问题？"), 2: ("第三个问题？",)},
    )
    service = SuggestedQuestionsService(bundle, FakeChat())

    questions = service.list_questions(kb_ids=[kb.id], limit=2)

    assert len(questions) == 2
    assert len(set(questions)) == 2


def test_list_questions_without_any_stored_question_is_empty(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """库还没开这个功能（或文档还没重新摄入）→ 空列表，界面回退静态样例。"""
    _seed_chunks(bundle, kb.id, document.id, count=2)
    service = SuggestedQuestionsService(bundle, FakeChat())

    assert service.list_questions(kb_ids=[kb.id]) == []


def test_list_questions_only_uses_the_given_kbs(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    other = bundle.meta.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_2", name="别的库", embedding_model_id="m", embedding_dim=8)
    )
    bundle.meta.create_document(
        DocumentRecord(
            id="doc_2",
            knowledge_base_id=other.id,
            name="乙.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="hash-2",
            stage=DocumentStage.UPLOADED,
            size_bytes=64,
        )
    )
    _seed_chunks(bundle, kb.id, document.id, count=1, prefix="a", questions={0: ("我的问题？",)})
    _seed_chunks(bundle, other.id, "doc_2", count=1, prefix="b", questions={0: ("别人的问题？",)})
    service = SuggestedQuestionsService(bundle, FakeChat())

    assert service.list_questions(kb_ids=[kb.id]) == ["我的问题？"]


def test_list_questions_without_kb_ids_is_empty(bundle) -> None:  # type: ignore[no-untyped-def]
    service = SuggestedQuestionsService(bundle, FakeChat())

    assert service.list_questions(kb_ids=[]) == []


# --------------------------------------------------------------------- 检索文本


def test_index_text_appends_the_questions_to_the_original() -> None:
    """索引文本 = 原文 + 问题；原文那一列**一个字都不改**（引用预览读的是它）。"""
    plain = _chunk("kb", "d", 0)
    with_questions = _chunk("kb", "d", 0, questions=("眼轴怎么测？", "多久测一次？"))

    assert plain.index_text == plain.text
    assert with_questions.index_text == f"{with_questions.text}\n眼轴怎么测？\n多久测一次？"


def test_list_questions_ignores_the_questionless_majority(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """库里的块**绝大多数没有题**，抽样必须在"有题的块"里做。

    这是 v24 的用户反馈：库里已经有上百条问题，对话空状态却还是静态样例。
    原因就是读端在全库随机抽 40 块，而有题的块只占极小一部分——10 次调用有 5 次
    一条都抽不到，于是回退静态样例（看起来像"功能没生效"）。

    这里故意造 3000 块无题 + 2 块有题：若抽样回到"全库随机"，连续三次都抽中
    有题的块的概率约是 0.02%，用例会稳定地红。
    """
    _seed_chunks(
        bundle,
        kb.id,
        document.id,
        count=2,
        questions={0: ("眼轴怎么测？",), 1: ("多久测一次？",)},
    )
    bulk = bundle.meta.create_document(
        DocumentRecord(
            id="doc_bulk",
            knowledge_base_id=kb.id,
            name="大量无题文档.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="hash-bulk",
            stage=DocumentStage.INDEXED,
        )
    )
    _seed_chunks(bundle, kb.id, bulk.id, count=3000, prefix="bulk")
    service = SuggestedQuestionsService(bundle, FakeChat())

    for _ in range(3):
        questions = service.list_questions(kb_ids=[kb.id], limit=6)
        assert set(questions) <= {"眼轴怎么测？", "多久测一次？"}
        assert questions, "有题的块存在时，读端不该返回空（那会让界面退回静态样例）"


# ----------------------------------------------------------- 并发（v24 的性能修）

class SlowChat:
    """记录**同时在飞的调用数**：并发生效的唯一可靠证据。

    只断言"总共调了几次"是验不出并发的——串行也会调同样多次。
    """

    def __init__(self, delay: float = 0.05, fail_on: int | None = None) -> None:
        import threading

        self.calls = 0
        self.inflight = 0
        self.peak = 0
        self._delay = delay
        self._fail_on = fail_on
        self._lock = threading.Lock()

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        with self._lock:
            self.calls += 1
            index = self.calls
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self._delay)
            if self._fail_on is not None and index == self._fail_on:
                raise RuntimeError("这一批上游挂了")
            return "###片段1\n问题一？"
        finally:
            with self._lock:
                self.inflight -= 1


def test_batches_are_sent_concurrently(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """**批与批要并发发**：出题是摄入链路里最慢的一步，而这些调用在等远端。

    实机量级：一份 400 段的文档 = 50 批，每批一次模型调用（思考型模型 1~2 分钟），
    串行就是 50~100 分钟，还把后面排队的所有文档一起堵住（§12.113）。
    """
    _seed_chunks(bundle, kb.id, document.id, count=_CHUNKS_PER_CALL * 4)
    chat = SlowChat(delay=0.08)
    service = SuggestedQuestionsService(bundle, chat, batch_concurrency=4)

    service.generate_for_chunks(list(bundle.meta.iter_chunks(document.id)), count=1)

    assert chat.calls == 4
    assert chat.peak > 1, "批次是串行发的（同时在飞的调用数始终是 1）"


def test_batch_concurrency_of_one_stays_serial(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """显式配成 1 时按串行走：模型的 RPM 限额是真约束，得留一条能踩刹车的路。"""
    _seed_chunks(bundle, kb.id, document.id, count=_CHUNKS_PER_CALL * 3)
    chat = SlowChat(delay=0.02)
    service = SuggestedQuestionsService(bundle, chat, batch_concurrency=1)

    service.generate_for_chunks(list(bundle.meta.iter_chunks(document.id)), count=1)

    assert chat.calls == 3
    assert chat.peak == 1


def test_a_failed_batch_does_not_take_the_others_down(bundle, kb, document) -> None:  # type: ignore[no-untyped-def]
    """并发**不改变**"失败只丢那几段"这条边界：出题是旁路，不能让文档 failed。"""
    _seed_chunks(bundle, kb.id, document.id, count=_CHUNKS_PER_CALL * 3)
    chat = SlowChat(delay=0.02, fail_on=2)
    service = SuggestedQuestionsService(bundle, chat, batch_concurrency=3)

    generated = service.generate_for_chunks(
        list(bundle.meta.iter_chunks(document.id)), count=1
    )

    # 3 批都发了，其中一批炸掉 → 只有两批的题入库。
    # **不断言"哪一批"炸**：并发下调用顺序不确定，钉具体 chunk_id 会变成随机失败的用例。
    assert chat.calls == 3
    assert len(generated) == 2
