"""检索质量评测的打分口径（金标集）。

镜像同构：``app/services/retrieval_eval.py`` → 本文件。

**这个文件的重点是"口径别漂"**：指标一旦被改动（比如 MRR 从"第一个期望命中的名次"
改成"第一个命中"），历史跑分就失去可比性，而"上次 0.62、这次 0.58"这种对比正是
调参唯一的依据。所以每条口径都在这里钉住。
"""

from __future__ import annotations

import pytest

from app.services.retrieval_eval import (
    EvalCase,
    evaluate,
    matches,
    run_case,
    summarize,
)


def _case(query: str = "问题", expect: tuple[str, ...] = ("眼轴",)) -> EvalCase:
    return EvalCase(query=query, expect=expect)


class TestMatching:
    def test_忽略大小写与多余空白(self) -> None:
        assert matches("2021年眼轴专家共识.PDF", "眼轴")
        assert matches("Eye Axis", "eye axis")
        assert matches("眼轴   专家共识", "眼轴 专家")

    def test_空期望不命中任何东西(self) -> None:
        # 空串是最危险的情况：如果用 `in` 直接判，"" 会在每个文档名里"命中"
        assert not matches("任何文档", "")
        assert not matches("任何文档", "   ")

    def test_子串而不是全名匹配(self) -> None:
        # 金标集是人手写的，绑全名会在文件改名后全废
        assert matches("2023年眼轴长度应用专家共识.pdf", "眼轴长度")


class TestRunCase:
    def test_命中且名次靠前(self) -> None:
        result = run_case(_case(), ["眼轴专家共识.pdf", "其它.pdf"])

        assert result.hit
        assert result.recall == 1.0
        assert result.reciprocal_rank == 1.0
        assert result.missed == ()

    def test_命中但排在第4条_MRR只给四分之一(self) -> None:
        """它惩罚"找是找到了，但排在第 9 条"——那在实际对话里常常等于没找到。"""
        result = run_case(_case(), ["a.pdf", "b.pdf", "c.pdf", "眼轴共识.pdf"])

        assert result.hit
        assert result.reciprocal_rank == pytest.approx(0.25)

    def test_多期望时给的是召回率(self) -> None:
        result = run_case(_case(expect=("甲", "乙")), ["甲文档.pdf", "丙.pdf"])

        assert result.hit
        assert result.recall == 0.5
        assert result.missed == ("乙",)

    def test_同一文档在多个块里命中不重复计数(self) -> None:
        """前 k 条常常是同几份文档的不同块：按**文档**去重，否则召回率会虚高。"""
        result = run_case(_case(expect=("甲", "乙")), ["甲.pdf", "甲.pdf", "甲.pdf", "乙.pdf"])

        assert result.recall == 1.0

    def test_全部漏掉(self) -> None:
        result = run_case(_case(), ["无关.pdf"])

        assert not result.hit
        assert result.recall == 0.0
        assert result.reciprocal_rank == 0.0
        assert result.missed == ("眼轴",)

    def test_空期望视为不评分而不是算失败(self) -> None:
        """写金标可以先攒问题后补期望；把未填的算成失败会让跑分一开始就红。"""
        result = run_case(EvalCase(query="待补", expect=()), ["随便.pdf"])

        assert result.hit
        assert result.recall == 1.0


class TestSummarize:
    def test_三个指标的平均口径(self) -> None:
        results = [
            run_case(_case(), ["眼轴.pdf"]),  # MRR 1.0
            run_case(_case(), ["a.pdf", "眼轴.pdf"]),  # MRR 0.5
        ]

        summary = summarize(results)

        assert summary.total == 2
        assert summary.hits == 2
        assert summary.hit_rate == 1.0
        assert summary.mean_recall == 1.0
        assert summary.mrr == pytest.approx(0.75)

    def test_空集给全零而不是抛错(self) -> None:
        summary = summarize([])

        assert (summary.total, summary.hit_rate, summary.mrr) == (0, 0.0, 0.0)

    def test_evaluate_把检索注入进来(self) -> None:
        """评测逻辑不关心结果从哪来：脚本、接口、测试共用同一份口径。"""
        calls: list[str] = []

        def search(query: str) -> list[str]:
            calls.append(query)
            return ["眼轴共识.pdf"]

        summary = evaluate([_case(query="甲"), _case(query="乙")], search)

        assert calls == ["甲", "乙"]
        assert summary.hit_rate == 1.0
