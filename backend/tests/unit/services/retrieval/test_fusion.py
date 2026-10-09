"""RRF 融合与相似度换算的单元测试。

镜像同构：``app/services/retrieval/{fusion,service}.py``
→ ``tests/unit/services/retrieval/test_fusion.py``。
"""

import pytest

from app.services.retrieval.fusion import DEFAULT_RRF_K, rrf_fuse, take_top
from app.services.retrieval.service import similarity_from_distance


def test_default_k_matches_rrf_paper() -> None:
    assert DEFAULT_RRF_K == 60


def test_single_channel_preserves_order() -> None:
    fused = rrf_fuse([("vector", ["a", "b", "c"])])
    assert [chunk_id for chunk_id, _, _, _ in fused] == ["a", "b", "c"]


def test_document_in_both_channels_outranks_single_channel_hits() -> None:
    """RRF 的核心价值：两路都认可的条目应当排到前面。"""
    fused = rrf_fuse(
        [
            ("vector", ["a", "b", "c"]),
            ("fulltext", ["c", "d"]),
        ]
    )
    assert fused[0][0] == "c"  # 向量第 3 + 全文第 1，胜过只在一路出现的 a


def test_scores_follow_the_formula() -> None:
    fused = rrf_fuse([("vector", ["a"])], k=60)
    assert fused[0][1] == pytest.approx(1 / 61)


def test_ranks_and_channels_are_recorded() -> None:
    """调试台要显示"这条是怎么被捞上来的"。"""
    fused = rrf_fuse([("vector", ["a", "b"]), ("fulltext", ["b"])])
    entry = next(item for item in fused if item[0] == "b")

    _, _, channels, ranks = entry
    assert set(channels) == {"vector", "fulltext"}
    assert ranks == {"vector": 2, "fulltext": 1}


def test_ties_are_broken_deterministically() -> None:
    """同分时按 chunk_id 升序，保证结果可复现（否则测试会间歇性失败）。"""
    fused = rrf_fuse([("vector", ["b", "a"])])
    assert [chunk_id for chunk_id, _, _, _ in fused] == ["b", "a"]

    tied = rrf_fuse([("vector", ["z"]), ("fulltext", ["a"])])
    assert [chunk_id for chunk_id, _, _, _ in tied] == ["a", "z"]


def test_larger_k_flattens_head_differences() -> None:
    """k 越大，头部名次之间的分差越小——这是"民主化"程度的旋钮。"""
    sharp = rrf_fuse([("vector", ["a", "b"])], k=0)
    flat = rrf_fuse([("vector", ["a", "b"])], k=1000)

    sharp_gap = sharp[0][1] - sharp[1][1]
    flat_gap = flat[0][1] - flat[1][1]
    assert sharp_gap > flat_gap


def test_empty_channels_yield_nothing() -> None:
    assert rrf_fuse([]) == []
    assert rrf_fuse([("vector", [])]) == []


def test_negative_k_is_rejected() -> None:
    with pytest.raises(ValueError):
        rrf_fuse([("vector", ["a"])], k=-1)


def test_take_top_truncates() -> None:
    fused = rrf_fuse([("vector", ["a", "b", "c"])])
    assert len(take_top(fused, 2)) == 2
    assert take_top(fused, 0) == []
    assert len(take_top(fused, -1)) == 0


# --------------------------------------------------------------------- 相似度换算
#
# **盯住"仓储给的是哪个距离"**：pgvector 那边用的是 ``<=>``（余弦距离 = 1 - cos），
# 所以换算是一句减法。曾经这里用欧氏公式 ``1 - d²/2``，在 d = 0 处两者一致（所以
# "精确命中"的用例一直是绿的），但只要有一点差异就虚高：实测 d = 0.483245
# （真余弦 0.5168）被算成 0.883237，于是界面上所有"相似度"都虚高。


def test_zero_distance_means_full_similarity() -> None:
    assert similarity_from_distance(0.0) == pytest.approx(1.0)


def test_similarity_decreases_with_distance() -> None:
    assert similarity_from_distance(0.5) > similarity_from_distance(1.0)


def test_cosine_distance_maps_to_the_real_cosine() -> None:
    """余弦距离 → 余弦相似度：**一句减法**（实测值那一组就是这条的现场）。"""
    # 实测：文本查询与媒体向量的真余弦 0.5168，仓储给的距离 0.483245
    assert similarity_from_distance(0.483245) == pytest.approx(0.516755, abs=1e-6)
    # 摆好的 0.95 / 0.70：仓储会给 0.05 / 0.30
    assert similarity_from_distance(0.05) == pytest.approx(0.95, abs=1e-9)
    assert similarity_from_distance(0.30) == pytest.approx(0.70, abs=1e-9)


def test_orthogonal_vectors_give_zero_similarity() -> None:
    """正交时余弦距离是 **1**（不是 √2——那是欧氏距离的写法），相似度 0。"""
    assert similarity_from_distance(1.0) == pytest.approx(0.0, abs=1e-9)


def test_opposite_vectors_give_minus_one() -> None:
    """余弦距离最大是 2（完全相反）→ -1，正好落在合法区间端点。"""
    assert similarity_from_distance(2.0) == pytest.approx(-1.0, abs=1e-9)


def test_similarity_is_clamped_to_valid_range() -> None:
    assert similarity_from_distance(10.0) == -1.0
