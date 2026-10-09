"""相关度下限的取值来源：逐库解析嵌入模型 → （余弦下限, 词面覆盖下限）。

镜像同构：``app/services/retrieval/service.py`` 的 ``_relevance_floors`` → 本文件。

**为什么不放在集成测试里**：这一层是"读一个模型 id 然后查表"，不需要真库——
用桩把"这个库用的哪个模型"喂进去就够了。真库里该验的那一半（候选到底被拦掉几条）
留在 ``tests/integration/services/test_retrieval.py``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.retrieval import RetrievalMode, RetrievalQuery, RetrievalService
from app.services.retrieval.rerank import NoopReranker
from app.services.retrieval.service import (
    DEFAULT_MIN_TERM_COVERAGE,
    MIN_VECTOR_SCORE_BY_MODEL,
)


class _Resolver:
    """``EmbeddingResolver`` 的最小替身：只回答"这个库用哪个模型"。"""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id

    def for_kb(self, kb: object) -> object:  # type: ignore[no-untyped-def]
        return SimpleNamespace(model_id=self._model_id)


def _service(model_id: str) -> RetrievalService:
    stores = SimpleNamespace(
        meta=SimpleNamespace(get_knowledge_base=lambda kb_id: SimpleNamespace(id=kb_id))
    )
    return RetrievalService(  # type: ignore[arg-type]
        stores,
        embedder=SimpleNamespace(model_id=model_id),
        reranker=NoopReranker(),
        embedders=_Resolver(model_id),
    )


def _floors(model_id: str, **overrides: float) -> tuple[float, float]:
    service = _service(model_id)
    request = RetrievalQuery(query="问点什么", kb_ids=["kb_1"], **overrides)
    return service._relevance_floors(request)["kb_1"]


def test_calibrated_model_turns_both_floors_on() -> None:
    """本机（bge-m3）标定过：语向下限 + 词面下限一起生效。"""
    vector, coverage = _floors("BAAI/bge-m3")

    assert vector == MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]
    assert coverage == DEFAULT_MIN_TERM_COVERAGE


def test_wemm_models_get_their_own_conservative_floor() -> None:
    """WeMM 多模态那台也标定过：**0.33**（真库噪声头 0.305 之上、真命中 0.386 之下）。

    用户登记的模型名带量化后缀（``WeMM-Embedding-2B-Q4_K_M.gguf``），查表用"包含"
    匹配，所以两种写法都命中——要求把名字写得一字不差，等于这条标定对大多数人不生效。
    """
    for model_id in ("WeMM-Embedding-2B-Q4_K_M.gguf", "WeMM-Embedding-2B", "wemm-embedding-2b"):
        assert _floors(model_id) == (0.33, DEFAULT_MIN_TERM_COVERAGE), model_id

    # 它比 bge-m3 那个 0.531 低——两个模型的余弦尺度本来就不是一回事
    assert MIN_VECTOR_SCORE_BY_MODEL["wemm-embedding-2b"] < MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]


def test_the_old_scale_and_the_new_scale_agree_point_by_point() -> None:
    """bge-m3 那条是**旧刻度 0.89 的逐点换算**（0.531），判定要与改动前一致。

    旧刻度是欧氏公式算出来的虚高值（``1 - d²/2``），换算回真余弦是
    ``v = 1 - √(2(1 - 旧值))``。同一条射线上的候选，两个刻度给出的"过不过线"
    必须**完全相同**——否则改相似度换算就等于悄悄改了检索行为。
    """
    old_floor, new_floor = 0.89, MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]

    def old_scale(true_cosine: float) -> float:
        distance = 1.0 - true_cosine  # 仓储给的余弦距离
        return 1.0 - distance * distance / 2.0

    assert new_floor == pytest.approx(1.0 - (2.0 * (1.0 - old_floor)) ** 0.5, abs=1e-3)
    for true_cosine in (0.30, 0.45, 0.514, 0.52, 0.531, 0.55, 0.60, 0.70, 0.90):
        assert (old_scale(true_cosine) >= old_floor) is (true_cosine >= new_floor), true_cosine


def test_wemm_floor_lets_real_hits_through_and_stops_noise() -> None:
    """地板两侧的行为：低于它走"资料里没有"，高于它**不误杀**。

    数字取自真机实测（真余弦：相关 0.386–0.717、不相关 0.131–0.305），见
    ``MIN_VECTOR_SCORE_BY_MODEL`` 上面那段标定依据。
    """
    from app.services.retrieval.distribution import FIT_NONE, FIT_STRONG, summarize

    floor = MIN_VECTOR_SCORE_BY_MODEL["wemm-embedding-2b"]

    def verdict(scores: list[float]):
        return summarize(
            [(f"doc_{index}", score, f"d{index}") for index, score in enumerate(scores)],
            floor=0.0,
            baseline=floor,
            keep_cap=6,
        )

    # 噪声（「有人在弹钢琴」那 8 条里的 3 条）→ 判"答不了"，建议条数 0
    noise = verdict([0.305, 0.170, 0.131])
    assert noise.fit == FIT_NONE
    assert noise.suggested_keep == 0
    assert "答不了" in noise.note

    # 真命中（"文案描述画面"那几条）→ 不误杀：判 strong、建议条数 > 0
    hits = verdict([0.717, 0.642, 0.510])
    assert hits.fit == FIT_STRONG
    assert hits.suggested_keep > 0
    assert "答不了" not in hits.note


def test_uncalibrated_model_keeps_both_floors_off() -> None:
    """没标定过的模型（含开发用哈希、别的供应商）：**整套关闭**。

    余弦的尺度是模型属性，说不出"多少算相关"时，词面那一条也一起关——
    它是"或"里的第二条，单独留着只会把召回砍一截。
    """
    assert _floors("dev/deterministic-hash") == (0.0, 0.0)
    assert _floors("text-embedding-3-small") == (0.0, 0.0)


def test_explicit_zero_disables_each_floor() -> None:
    """参数要能关：显式传 0 即关闭（分别可关，不是一个总开关）。"""
    assert _floors("BAAI/bge-m3", min_vector_score=0.0) == (0.0, 0.0)
    assert _floors("BAAI/bge-m3", min_term_coverage=0.0) == (
        MIN_VECTOR_SCORE_BY_MODEL["bge-m3"],
        0.0,
    )


def test_explicit_values_override_the_calibration() -> None:
    """显式传值就是显式：不信标定表的调用方（调试台）能自己定。"""
    assert _floors("BAAI/bge-m3", min_vector_score=0.7, min_term_coverage=0.3) == (0.7, 0.3)
    # 未标定的模型也能被显式打开（这时词面那一条是唯一的判定依据）
    assert _floors("dev/deterministic-hash", min_term_coverage=0.5) == (0.0, 0.5)


def test_floors_are_resolved_per_knowledge_base() -> None:
    """跨库检索时每个库各算一次：模型是库属性，下限跟着库走。"""
    service = _service("BAAI/bge-m3")
    request = RetrievalQuery(query="问点什么", kb_ids=["kb_1", "kb_2"])
    floors = service._relevance_floors(request)

    assert set(floors) == {"kb_1", "kb_2"}


def test_model_lookup_failure_degrades_to_no_floor() -> None:
    """拿不到模型身份（注册表坏了）只是不设限，**不能让检索跟着失败**。"""
    service = _service("BAAI/bge-m3")
    service._embedder_for = lambda kb_id: (_ for _ in ()).throw(RuntimeError("注册表连不上"))  # type: ignore[method-assign]

    assert service._relevance_floors(
        RetrievalQuery(query="问点什么", kb_ids=["kb_1"], mode=RetrievalMode.HYBRID)
    ) == {"kb_1": (0.0, 0.0)}


@pytest.mark.parametrize("mode", list(RetrievalMode.ALL))
def test_floors_do_not_depend_on_the_mode(mode: str) -> None:
    """全文档也要有词面下限（那一档没有向量分，词面是唯一的依据）。"""
    assert _floors("BAAI/bge-m3", mode=mode) == (
        MIN_VECTOR_SCORE_BY_MODEL["bge-m3"],
        DEFAULT_MIN_TERM_COVERAGE,
    )


def test_importing_the_service_yields_the_same_constants() -> None:
    """``__init__`` 转发的那两个常量必须就是判定用的那一份（接口层要拿它写文档）。"""
    import app.services.retrieval as package
    import app.services.retrieval.service as service_module

    assert package.MIN_VECTOR_SCORE_BY_MODEL is service_module.MIN_VECTOR_SCORE_BY_MODEL
    assert package.DEFAULT_MIN_TERM_COVERAGE is service_module.DEFAULT_MIN_TERM_COVERAGE
