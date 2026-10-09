"""动态返回的分布统计（v0.54）。

镜像同构：``app/services/retrieval/distribution.py`` → 本文件。

这里钉的是**由本机实测得出的那几条判断**（2026-09-28，医学知识库 21580 chunk，bge-m3）：

1. **真问题的分布是一条窄带**：37 条候选挤在 0.593–0.805（旧刻度 0.918–0.981），
   相邻落差最大 0.027——所以"最大断崖切一刀"这条路走不通，``band`` 必须报出来；
2. **无关问题整条分布往下挪**：「今天北京的天气怎么样？」落在 0.460–0.520（旧刻度
   0.854–0.885），而基线是 0.531（旧刻度 0.89）→ ``fit="none"``，返回 0 条。
   **契合度是从分布的位置读出来的**，不是从单条最高分读出来的；
3. **条数与文档冗余是两件事**：同一个问题被 4–5 本手册各答一遍（实测 3 条、4 条来自同一本），
   所以建议里必须带 ``per_doc``；
4. **没有向量分的候选不许污染分数统计**：拿融合分（RRF）当相似度算分位数，实测会把
   min 拉到 0.007、带宽拉到 0.974——整份分布就废了。

本文件的数字都在**真余弦**刻度上（2026-09-29 换口径，旧值按
``v = 1 - √(2(1 - 旧值))`` 换算而来；理由见 ``service.similarity_from_distance``）。
"""

from __future__ import annotations

import pytest

from app.services.retrieval.distribution import (
    FIT_NONE,
    FIT_STRONG,
    FIT_UNKNOWN,
    FIT_WEAK,
    STRONG_KEEP_LIMIT,
    percentile,
    summarize,
)

BASELINE = 0.531  # 旧刻度 0.89
FLOOR = 0.37  # 旧刻度 0.80


def _flat_band() -> list[tuple[str, float | None, str]]:
    """实测形状：一条 0.593–0.805 的窄带，分布在 14 篇里，头三篇各 15+ 条。"""
    items: list[tuple[str, float | None, str]] = []
    for index in range(86):
        # 分数从 0.805 缓降到 0.593（带宽 0.212，旧刻度是 0.065），前三篇各占十几条
        score = 0.805 - 0.212 * index / 85
        doc = f"doc{index % 14}"
        items.append((f"手册{index % 14}", score, doc))
    return items


def test_percentile_interpolates_instead_of_snapping() -> None:
    """分位数走插值：样本少的时候"最近秩"会让 p90 永远等于 max，那半点信息就没了。"""
    values = [0.9, 0.92, 0.94, 0.96, 0.98]
    assert percentile(values, 0.5) == 0.94
    assert percentile(values, 0.0) == 0.9
    assert percentile(values, 1.0) == 0.98
    # 位置 2.4 → 落在 0.94 与 0.96 之间（最近秩会给 0.94，插值给 0.948）
    assert percentile(values, 0.6) == pytest.approx(0.948)
    assert percentile([], 0.5) == 0.0
    assert percentile([0.5], 0.9) == 0.5


def test_the_thresholds_are_on_the_true_cosine_scale() -> None:
    """这套阈值与余量都在**真余弦**刻度上（换口径那次一并换算过来的）。

    钉住这三个数是为了防"照着旧文档填回去"：旧刻度（欧氏公式的虚高值）→ 真余弦
    是 ``v = 1 - √(2(1 - 旧值))``，本机那批实测的换算是
    ``兜底阈值 0.80 → 0.368``、``strong 余量 0.04（基线 0.89 处）→ 0.095``。
    """
    from app.services.retrieval.distribution import STRONG_MARGIN
    from app.services.runtime_config import DEFAULTS

    def to_true(old_scale: float) -> float:
        return 1.0 - (2.0 * (1.0 - old_scale)) ** 0.5

    # 一条变换两处都核对：换错公式就会立刻红
    assert to_true(0.80) == pytest.approx(0.3675, abs=1e-3)
    margin = to_true(0.89 + 0.04) - to_true(0.89)
    assert margin == pytest.approx(0.095, abs=1e-3)

    assert float(DEFAULTS["retrieval.floor_score"]) == pytest.approx(0.37)
    strong_margin = STRONG_MARGIN
    assert strong_margin == pytest.approx(0.095)
    # 基线保持 "0 = 自动"（按嵌入模型查标定表），不许写死某个模型的值
    assert DEFAULTS["retrieval.baseline_score"] == "0"


def test_a_strong_flat_band_suggests_a_small_converged_set() -> None:
    """实测那个医学问题：``strong`` + 窄带 → 建议**每篇 1 条、总共 4 条**。

    这就是"上下文太长"的解法：不是把阈值往上抬（带只有 0.065 宽，抬了也只切掉尾巴），
    而是**按文档收敛**——同一件事被 4 本手册各答一遍时只留最相关的那一本。
    """
    distribution = summarize(_flat_band(), floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.fit == FIT_STRONG
    assert distribution.count == 86
    assert distribution.above_baseline == 86
    assert distribution.documents == 14
    assert distribution.band < 0.25, "窄带：实测（换算到真余弦）0.212"
    assert distribution.suggested_per_doc == 1
    assert distribution.suggested_keep == STRONG_KEEP_LIMIT == 4
    assert distribution.suggested_min_score == BASELINE
    # 建议不能超过 keep_cap（调用方说"我最多要 3 条"时不许给 4 条）
    capped = summarize(_flat_band(), floor=FLOOR, baseline=BASELINE, keep_cap=3)
    assert capped.suggested_keep == 3


def test_an_off_topic_question_is_judged_by_where_the_whole_band_sits() -> None:
    """无关问题：整条分布压在基线之下 → ``none``、返回 0 条、回话要说"资料里没有"。

    实测「今天北京的天气怎么样？」30 条落在 0.460–0.520（基线 0.531，旧刻度
    0.854–0.885 / 0.89）——注意**最高分 0.520 并不低**，只看单条最高分是判不出来的。
    """
    items = [("手册", 0.520 - 0.002 * index, f"doc{index % 45}") for index in range(30)]
    distribution = summarize(items, floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.fit == FIT_NONE
    assert distribution.maximum < BASELINE
    assert distribution.above_baseline == 0
    assert distribution.suggested_keep == 0
    assert distribution.suggested_min_score is None
    assert "答不了" in distribution.note


def test_a_partial_match_is_weak_and_keeps_only_a_couple() -> None:
    """刚过基线 = ``weak``：建议 2 条，回话让模型"只回答问到的那一点"。"""
    items = [("手册", 0.60, "doc1"), ("手册", 0.5915, "doc2"), ("手册", 0.585, "doc3")]
    distribution = summarize(items, floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.fit == FIT_WEAK
    assert distribution.suggested_keep == 2
    assert "对上了一部分" in distribution.note


def test_hits_without_a_vector_score_do_not_pollute_the_score_stats() -> None:
    """没有相似度的候选（只有全文通道）计入条数与文档，但**不进分数统计**。

    实测教训：拿融合分（RRF）当相似度混进来，min 会变成 0.007、带宽变成 0.974。
    """
    items: list[tuple[str, float | None, str]] = [
        ("手册", 0.70, "doc1"),
        ("手册", 0.65, "doc2"),
        ("全文命中", None, "doc3"),
    ]
    distribution = summarize(items, floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.count == 3
    assert distribution.documents == 3
    assert distribution.minimum == 0.65
    assert distribution.band < 0.06
    assert distribution.maximum == 0.70


def test_without_any_semantic_score_it_does_not_judge_the_fit() -> None:
    """一条向量分都没有（纯关键词那一档）：**不判拟合度**，也不收敛，并如实说出来。

    与 ``_passes_relevance`` 同一条纪律：没有语义证据就不下结论。这时条数仍然可信。
    """
    items: list[tuple[str, float | None, str]] = [("全文", None, "doc1"), ("全文", None, "doc2")]
    distribution = summarize(items, floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.basis == "none"
    assert distribution.fit == FIT_UNKNOWN
    assert distribution.suggested_min_score is None
    assert distribution.suggested_per_doc is None
    assert distribution.suggested_keep == 2
    assert "判不了" in distribution.note
    payload = distribution.as_payload()
    assert "scores" not in payload, "没有分数就别报分位数"


def test_an_uncalibrated_model_means_no_judgement_and_no_convergence() -> None:
    """**安全阀**：``baseline=None``（嵌入模型没标定过）时只报分布，判不了、也不收敛。

    为什么必须有这一条：标定表只覆盖 bge-m3，别的模型上套它的基线去判会把它们的真命中
    全判成噪声（实测：确定性嵌入的集成用例里整库被判成 ``fit=none``、**返回 0 条**，
    知识库直接哑掉）。这时退回"按调用方要的条数返回"= 这个机制上线前的行为。
    """
    distribution = summarize(_flat_band(), floor=FLOOR, baseline=None, keep_cap=6)

    assert distribution.fit == FIT_UNKNOWN
    assert distribution.baseline == 0.0
    assert distribution.suggested_keep == 6, "不收敛：按调用方要的条数走"
    assert distribution.suggested_min_score is None
    assert distribution.suggested_per_doc is None
    assert "没有标定过" in distribution.note
    # 分布本身照报（条数与文档冗余是尺度无关的，仍然有用）
    assert distribution.count == 86 and distribution.documents == 14
    assert distribution.maximum > 0.8


def test_an_empty_window_says_so() -> None:
    """兜底阈值之上一无所有：条数为 0，回话是"如实说明，不要凭常识编"。"""
    distribution = summarize([], floor=FLOOR, baseline=BASELINE, keep_cap=6)

    assert distribution.count == 0
    assert distribution.fit == FIT_NONE
    assert distribution.suggested_keep == 0
    assert "不要凭常识编" in distribution.note


def test_an_empty_window_on_an_uncalibrated_model_still_renders() -> None:
    """**空窗口 + 没标定**这个组合要能生成载荷（曾经在这里炸过）。

    回归：``baseline=None`` 一路传到 ``as_payload`` 的 ``round(None, 3)`` →
    ``TypeError: type NoneType doesn't define __round__``。三条集成用例抓到它
    （`test_tools.py` 的 search 三条）。字段按构造保证是 float，None 在这一层就该消掉。
    """
    distribution = summarize([], floor=FLOOR, baseline=None, keep_cap=6)

    assert distribution.baseline == 0.0
    payload = distribution.as_payload()
    assert payload["baseline"] == 0.0
    assert payload["count"] == 0


def test_the_payload_is_compact_and_rounded() -> None:
    """给模型的载荷要小：分位数三位小数（实测分辨率就是 0.03 级），字段名要短。"""
    distribution = summarize(_flat_band(), floor=FLOOR, baseline=BASELINE, keep_cap=6)
    payload = distribution.as_payload()

    assert set(payload) == {
        "floor",
        "baseline",
        "basis",
        "count",
        "above_baseline",
        "fit",
        "suggested",
        "note",
        "scores",
        "documents",
        "per_document",
    }
    assert payload["floor"] == 0.37 and payload["baseline"] == 0.531
    scores = payload["scores"]
    assert isinstance(scores, dict)
    assert len(str(round(scores["max"], 3)).split(".")[-1]) <= 3
    # 前几篇文档（条数最多的）必须报出来：冗余就藏在这里
    per_document = payload["per_document"]
    assert isinstance(per_document, list) and per_document
    assert all(len(item) == 2 for item in per_document)
