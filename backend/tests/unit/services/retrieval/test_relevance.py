"""相关度下限的两条证据（v0.53）。

镜像同构：``app/services/retrieval/coverage.py`` + ``service._passes_relevance`` → 本文件。

标定数据来自本机四个真实库（2026-09-24，bge-m3），这里钉的是**由数据得出的那几条判断**：

1. **余弦下限按模型走**：没标定过的模型一律 0（不设限）——余弦的绝对值是模型属性，
   拿 bge-m3 的尺度去拦别的模型会把它的真命中全砍掉；
2. **词面覆盖是"第二条证据"**：语义够近或词面够像，满足其一即可。真命中的下包络
   实测低到 0.852（人名「郭正茂」那条），这类只能靠词面这一路救；
3. **词面覆盖不用全文通道的原始分**（``ts_rank_cd``）：实测噪声头 10.1 > 真命中头
   8.9（跨库），绝对值选不出不误杀的阈值；
4. **切法必须与索引一致**：两处一旦分叉，"词面覆盖"和全文通道看的就不是同一批词了。
"""

from __future__ import annotations

import pytest

from app.services.retrieval.coverage import MIN_TERM_CHARS, content_terms, term_coverage
from app.services.retrieval.service import (
    DEFAULT_MIN_TERM_COVERAGE,
    MIN_VECTOR_SCORE_BY_MODEL,
    _passes_relevance,
    calibrated_vector_floor,
)
from app.storage.text import cut


def _gate(**kwargs: object) -> bool:
    """跑一次判定：默认是"标定过的那套"（余弦 0.89 + 覆盖 0.5），可按需覆盖。"""
    return _passes_relevance(
        kwargs.pop("raw", {"vector": 0.95}),  # type: ignore[arg-type]
        kwargs.pop("text", ""),  # type: ignore[arg-type]
        title=kwargs.pop("title", ""),  # type: ignore[arg-type]
        vector_floor=kwargs.pop("vector_floor", 0.89),  # type: ignore[arg-type]
        coverage_floor=kwargs.pop("coverage_floor", DEFAULT_MIN_TERM_COVERAGE),  # type: ignore[arg-type]
        terms=kwargs.pop("terms", content_terms("真空镀膜机的靶材更换周期")),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------- 切词


def test_terms_drop_single_char_function_words() -> None:
    """单字虚词（的／了／是）不进判定：实测噪声头就是靠「的」命中的。

    切法用**精确模式**（``jieba.lcut``），所以「真空镀膜」是一个词——索引那档
    （``cut_for_search``）才会再切出「真空」「镀膜」这些子词；判定这档不要子词，
    理由见 ``coverage.py``（子词会让"蹭一个词头"也算命中）。
    """
    assert MIN_TERM_CHARS == 2
    terms = content_terms("真空镀膜机的靶材更换周期")

    assert "的" not in terms and "机" not in terms
    assert terms == ("真空镀膜", "靶材", "更换", "周期")


def test_terms_are_casefolded_and_unique() -> None:
    """与全文索引对齐（tsvector 对 ASCII 折叠大小写），重复词只算一次。"""
    assert content_terms("NDVI ndvi NDVI") == ("ndvi",)


def test_tokenization_is_a_subset_of_the_index_tokenization() -> None:
    """**切法同源的机械保证**：业务层不能 import 存储层的 ``text``（分层纪律），
    两边各调一次 jieba 就成了"迟早分叉"的形状——这条用例盯着它们的包含关系。

    索引那边是 ``cut_for_search``（宽召回，长词再切出子词），判定这边是 ``lcut``
    （要的是证据，子词会让"蹭一个词头"也算命中）。同源但不同档，所以断言是
    **包含**而不是相等。
    """
    for query in [
        "白内障手术的并发症有哪些",
        "学校绿地空间与儿童近视的关系",
        "如何用 Rust 写一个 HTTP 服务器",
        "sqlite-vec 分区",
        "NDVI",
    ]:
        index_tokens = {word.casefold() for word in cut(query)}
        assert set(content_terms(query)) <= index_tokens


def test_empty_terms_never_filter() -> None:
    """查询里没有实词（只输入了「的」/标点）时这一侧无从判断：放行，宁可少拦。"""
    assert term_coverage((), "任意正文") == 1.0
    assert content_terms("的了是") == ()


# --------------------------------------------------------------------- 覆盖


def test_coverage_ratio() -> None:
    """整句问法的词面覆盖可能只有一半——那类真命中靠**语义**那一路留下，不靠这一条。

    实测：这段正文是「学校绿地空间与儿童近视的关系」的真命中，覆盖率 3/6 = 0.5
    （词面只沾上绿地/空间/近视），而它的余弦是 0.966。判定是"或"，所以它照样留下；
    反过来说，词面这一条不该被当成"必须有"。
    """
    terms = content_terms("学校绿地空间与儿童近视的关系")
    hit = "绿地空间暴露和户外活动时长对中小学生筛查性近视的独立作用分析"

    assert term_coverage(terms, hit) == pytest.approx(0.5)
    assert _gate(raw={"vector": 0.966}, text=hit, terms=terms) is True
    assert term_coverage(terms, "屈光手术与角膜地形图的测量差异") == 0.0


def test_default_coverage_is_just_above_the_measured_noise_ceiling() -> None:
    """取值的根据：噪声最高覆盖 2/4（实测），真命中一侧最低的要求是 3/4。

    与向量下限同一条纪律——**卡在实测噪声的最高覆盖之上，且不越过真命中的下包络**。
    """
    assert DEFAULT_MIN_TERM_COVERAGE > 0.5, "0.5 时实测有三条噪声查询漏进来"
    assert DEFAULT_MIN_TERM_COVERAGE <= 0.75, "再高就会拦掉真命中（实测 0.75 那条是下限）"


# --------------------------------------------------------------------- 判定


def test_close_enough_semantically_passes_without_any_word_overlap() -> None:
    """换个说法的问法：词面一个都不沾，余弦够近就得留下。"""
    assert _gate(raw={"vector": 0.95}, text="The school is the principal space for…")


def test_weak_semantics_but_full_word_overlap_passes() -> None:
    """人名/罕见词：余弦低（实测 0.852），但查询的词就在这段里。"""
    terms = content_terms("郭正茂")
    assert _gate(raw={"vector": 0.852}, text="郭正茂，杨剑，漆昌柱，等", terms=terms)


def test_noise_is_rejected_by_both_evidence() -> None:
    """噪声查询的两类候选：余弦 0.86 的、以及全文单独捞上来的——两条证据都不足。

    正文取自医学库上「真空镀膜机的靶材更换周期」的真实返回（噪声命中）。
    """
    noise_text = "而，对照研究没有发现其与红斑痤疮有相关性。毛囊蠕螨，一种人类头发中的永久性外寄"

    assert not _gate(raw={"vector": 0.864}, text=noise_text)
    assert not _gate(raw={"fulltext": 9.9}, text=noise_text)

    # 蹭一个词头不算：索引那档会把「服务器」切出「服务」，判定这档不认
    partial = _gate(
        raw={"fulltext": 1.3},
        text="类似的阅读用助视器……通常是一个很好的起点",
        terms=content_terms("如何用 Rust 写一个 HTTP 服务器"),
    )
    assert partial is False


def test_document_name_counts_as_word_evidence() -> None:
    """按**文档名**找东西（版本号、标题）是真命中，不算它就会误杀。

    实测：笔记库上「0.1.0版本体验修了什么」命中的正是同名那份文档，而它正文里
    并没有"版本""体验"这些字样——只看正文的话覆盖率 1/4，会被拦掉。
    """
    terms = content_terms("0.1.0版本体验修了什么")

    assert _gate(
        raw={"vector": 0.882},
        text="1.技能安装报错：技能里不收这类文件",
        title="0.1.0版本体验修.md",
        terms=terms,
    )
    # 同一段正文，去掉文档名这一条证据就拦掉了（说明救它的是文档名）
    assert not _gate(
        raw={"vector": 0.882},
        text="1.技能安装报错：技能里不收这类文件",
        terms=terms,
    )


def test_both_floors_zero_keeps_everything() -> None:
    """两条下限都关（没标定过的模型走的就是这条路）：**不改变既有行为**。"""
    assert _passes_relevance(
        {}, "完全无关的正文", vector_floor=0.0, coverage_floor=0.0, terms=content_terms("查询")
    )


def test_explicit_coverage_only_still_gates() -> None:
    """调用方只给了词面下限（向量那侧关着）：词面这一条要照常生效。"""
    assert _passes_relevance(
        {"vector": 0.2},
        "郭正茂，杨剑",
        vector_floor=0.0,
        coverage_floor=0.5,
        terms=content_terms("郭正茂"),
    )
    assert not _passes_relevance(
        {"vector": 0.2},
        "毫不相干的正文",
        vector_floor=0.0,
        coverage_floor=0.5,
        terms=content_terms("郭正茂"),
    )


# --------------------------------------------------------------------- 标定值


def test_only_calibrated_models_carry_a_floor() -> None:
    """标定表按"包含"匹配（注册表里的 id 带供应商前缀/版本后缀），
    没标定过的模型一律 0——**宁可少拦**。"""
    assert MIN_VECTOR_SCORE_BY_MODEL["bge-m3"] > 0
    assert calibrated_vector_floor("BAAI/bge-m3") == MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]
    assert calibrated_vector_floor("bge-m3:latest") == MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]

    assert calibrated_vector_floor("dev/deterministic-hash") == 0.0
    assert calibrated_vector_floor("text-embedding-3-small") == 0.0
    assert calibrated_vector_floor("") == 0.0


def test_floor_is_just_above_every_noise_head_we_measured() -> None:
    """取值的根据（本机四库实测，2026-09-24，bge-m3）：**刚好高过实测最高那条噪声头**。

    那次记下来的是**旧刻度**（当时"相似度"按欧氏公式算，虚高，见
    ``test_fusion.test_cosine_distance_maps_to_the_real_cosine``）：

    - 噪声查询的**头部** 0.844–0.882（「今天北京的天气怎么样」0.882 最高）；
    - 真查询的常态 0.92–0.97。

    换算到真余弦（``v = 1 - √(2(1 - 旧值))``）就是：噪声头 → 0.514、真查询 → 0.60–0.755，
    而表里的 0.531 正好落在那条缝里 ✓。代价仍然真实：真命中里最弱的一类（罕见词/人名，
    旧刻度 0.852–0.861 → 真余弦 0.456–0.473）会被这一条拦掉——它们靠**词面覆盖**那一路
    救回（见 ``test_weak_semantics_but_full_word_overlap_passes``）。
    所以这个数**不往上抬**：抬到 0.56 以上会开始拦真查询里较弱的那些（「眼科」旧刻度
    0.908 → 0.571、「NDVI」0.919 → 0.590），而噪声并不会因此少拦一条。
    """
    floor = MIN_VECTOR_SCORE_BY_MODEL["bge-m3"]

    def true_cosine(old_scale: float) -> float:
        return 1.0 - (2.0 * (1.0 - old_scale)) ** 0.5

    assert floor > true_cosine(0.882), "低于最高的那条噪声头就等于没拦"
    assert floor <= true_cosine(0.908), "再往上就开始碰真查询的头（「眼科」旧刻度 0.908）"
