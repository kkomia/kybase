"""开发用确定性嵌入的单元测试。

镜像同构：``app/services/embedding/deterministic.py``
→ ``tests/unit/services/embedding/test_deterministic.py``。
"""

import math

import pytest

from app.services.embedding.deterministic import DeterministicEmbedder, tokenize


@pytest.fixture(scope="module")
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=128)


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_dimension_is_respected(embedder: DeterministicEmbedder) -> None:
    assert len(embedder.embed(["一段文本"])[0]) == 128


def test_batch_order_is_preserved(embedder: DeterministicEmbedder) -> None:
    vectors = embedder.embed(["甲", "乙", "丙"])
    assert len(vectors) == 3
    assert vectors[0] == embedder.embed(["甲"])[0]


def test_same_text_gives_same_vector(embedder: DeterministicEmbedder) -> None:
    """确定性是它能当测试夹具的前提。"""
    assert embedder.embed(["向量检索"])[0] == embedder.embed(["向量检索"])[0]


def test_vectors_are_l2_normalized(embedder: DeterministicEmbedder) -> None:
    vector = embedder.embed(["这是一段用于归一化检查的文本"])[0]
    assert math.isqrt(int(sum(value * value for value in vector) * 1_000_000)) > 0
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_empty_text_yields_zero_vector_without_crashing(embedder: DeterministicEmbedder) -> None:
    vector = embedder.embed([""])[0]
    assert len(vector) == 128
    assert all(value == 0.0 for value in vector)


def test_overlapping_wording_is_closer_than_unrelated(embedder: DeterministicEmbedder) -> None:
    """它没有语义，但**词面重合**要能反映出来，否则连开发期演示都没法看。"""
    base = embedder.embed(["向量检索与全文检索的混合召回"])[0]
    similar = embedder.embed(["全文检索与向量检索的混合"])[0]
    unrelated = embedder.embed(["今天午饭吃了拉面和煎饺"])[0]

    assert _cosine(base, similar) > _cosine(base, unrelated)


def test_is_marked_as_development_implementation(embedder: DeterministicEmbedder) -> None:
    """接口层要靠这个标志提示"检索质量不代表真实效果"。"""
    assert embedder.is_development is True
    assert embedder.model_id == "dev/deterministic-hash"


def test_zero_dimension_is_rejected() -> None:
    with pytest.raises(ValueError):
        DeterministicEmbedder(dim=0)


def test_tokenize_keeps_chinese_words_and_characters() -> None:
    tokens = tokenize("知识库检索")
    assert any("检索" in token for token in tokens)
    assert "知" in tokens  # 补单字：短查询更容易命中


def test_tokenize_lowercases_latin() -> None:
    assert "sqlite" in tokenize("SQLite 向量扩展")


def test_tokenize_truncates_very_long_text() -> None:
    """超长文本不该拖慢测试。"""
    assert len(tokenize("词" * 5000)) <= 512
