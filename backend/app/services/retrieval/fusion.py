"""RRF 融合（M3 T3.3）。

Reciprocal Rank Fusion：``score(d) = Σ_r 1 / (k + rank_r(d))``，``rank`` 从 1 开始。

为什么用 RRF 而不是把两路分数加权求和：向量距离与 BM25 分数的**量纲完全不可比**，
归一化也各有各的坑；RRF 只看**名次**，天然免疫量纲问题，这是它成为混合检索默认做法的原因。

``k`` 的作用是压平头部：k 越大，前几名之间的差距越小，融合越"民主"；默认 60 来自原论文。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

DEFAULT_RRF_K = 60


@dataclass(slots=True)
class _Fused:
    chunk_id: str
    score: float = 0.0
    channels: list[str] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)


def rrf_fuse(
    channels: Sequence[tuple[str, Sequence[str]]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float, tuple[str, ...], dict[str, int]]]:
    """把多路有序结果融合成一个有序列表。

    参数 ``channels`` 是 ``[(通道名, 按相关性降序的 chunk_id 序列)]``。

    返回 ``[(chunk_id, 融合分, 命中通道, 各通道名次)]``，按**分数降序、chunk_id 升序**排列——
    加上第二关键字是为了让结果**可复现**，否则同分时顺序随字典实现漂移，测试会间歇性失败。
    """
    if k < 0:
        raise ValueError("RRF 的 k 不能为负")

    merged: dict[str, _Fused] = {}
    for channel, ranked_ids in channels:
        for position, chunk_id in enumerate(ranked_ids, start=1):
            entry = merged.setdefault(chunk_id, _Fused(chunk_id=chunk_id))
            entry.score += 1.0 / (k + position)
            entry.channels.append(channel)
            entry.ranks[channel] = position

    ordered = sorted(merged.values(), key=lambda entry: (-entry.score, entry.chunk_id))
    return [
        (entry.chunk_id, entry.score, tuple(entry.channels), entry.ranks) for entry in ordered
    ]


def take_top(
    fused: Iterable[tuple[str, float, tuple[str, ...], dict[str, int]]], limit: int
) -> list[tuple[str, float, tuple[str, ...], dict[str, int]]]:
    """截断到前 ``limit`` 条。"""
    return list(fused)[: max(limit, 0)]
