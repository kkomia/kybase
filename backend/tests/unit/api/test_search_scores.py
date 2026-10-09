"""``/search`` 命中里两个分数字段的**口径**（方案 14）。

镜像同构：``app/api/v1/schemas.py::SearchHitOut`` → 本文件。

为什么值得单独钉：真机现场里这两个字段被混用过——`score` 是**名次分**
（``1/(k+rank)`` 量级，向量档第 1 名恒为 0.0164），而"这条像不像"要看**真实余弦**。
界面/调用方拿 `score` 当相似度显示，就会得出"相似度 0.02"或"地板永远不触发"这类结论。
"""

from __future__ import annotations

import pytest

from app.api.v1.schemas import SearchHitOut
from app.services.retrieval.types import RetrievalHit


def _hit(**overrides: object) -> RetrievalHit:
    values: dict[str, object] = {
        "chunk_id": "c1",
        "document_id": "d1",
        "knowledge_base_id": "kb_1",
        "text": "一段正文",
        "score": 0.0164,  # 名次分（向量档第 1 名的实测值）
        "similarity": 0.7027,  # 真实余弦（同一批实测）
    }
    values.update(overrides)
    return RetrievalHit(**values)  # type: ignore[arg-type]


def test_the_wire_carries_both_numbers_separately() -> None:
    """两个字段各自落到线上，含义不同、**不许互相顶替**。"""
    out = SearchHitOut.model_validate(_hit())

    assert out.score == pytest.approx(0.0164)
    assert out.similarity == pytest.approx(0.7027)
    assert out.similarity != out.score


def test_a_fulltext_only_hit_has_no_similarity() -> None:
    """只被全文捞上来的命中没有向量分：``similarity`` 必须是 ``None``，不能编一个。"""
    out = SearchHitOut.model_validate(_hit(similarity=None, channels=("fulltext",)))

    assert out.similarity is None
    assert out.score > 0  # 名次分照旧有（融合分与通道无关）


def test_the_field_docs_say_which_one_is_similarity() -> None:
    """文档串要说清口径：被误用的成本比多写两行注释高得多。"""
    score_doc = SearchHitOut.model_fields["score"].description or ""
    similarity_doc = SearchHitOut.model_fields["similarity"].description or ""

    assert "名次" in score_doc
    assert "相似度" in similarity_doc
