"""检索质量评测（金标集打分）。

**为什么需要它**：切块大小、rerank 开关、混合权重、查询改写——这些改动都会让
"感觉更好了"，而没有一个数字就无法判断是不是真的更好，更发现不了悄悄变差的那些。
所以先把评测做出来，再谈调参。这也是《知识库产品对标调研》把它列为
"只做三件事"之一的理由。

**指标与口径**（都是检索领域的通行定义，选它们是因为好解释、不好糊弄）：

- ``hit@k``：前 k 条里**有没有**期望文档。最粗的一档，但决定了"用户能不能找到"。
- ``recall@k``：期望文档**被找到的比例**。多期望文档时才有区别（比如"这份资料里
  三处都相关"）。只填一个期望时它就退化成 hit。
- ``MRR``：第一个期望文档名次的倒数（第 1 名 1.0、第 2 名 0.5…）。它惩罚
  "找是找到了，但排在第 9 条"——那在实际对话里常常等于没找到。

**匹配用文档名子串、不绑 id**：金标集是人手写的，绑 id 就只能在这一台机器上用，
换个环境或重建库就全废。子串匹配（忽略大小写）要求期望写**文件名里有辨识度的那段**
（例如 ``眼轴`` 或 ``专家共识``），而不是 ``.pdf`` 这种谁都有的一截。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

__all__ = [
    "CaseResult",
    "EvalCase",
    "EvalSummary",
    "matches",
    "run_case",
    "summarize",
]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """一条金标：一个问题，以及"应该命中哪些文档"。"""

    query: str
    expect: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: EvalCase
    ranked_documents: tuple[str, ...]
    """按名次排列的命中文档名（同一条里可能重复出现，评测里按文档去重）。"""
    hit: bool
    recall: float
    reciprocal_rank: float

    @property
    def missed(self) -> tuple[str, ...]:
        """没被找到的期望文档——直接告诉写金标的人"漏了哪一份"。"""
        return tuple(item for item in self.case.expect if not _found(item, self.ranked_documents))


@dataclass(slots=True)
class EvalSummary:
    """一次评测的汇总。"""

    total: int
    hits: int
    mean_recall: float
    mrr: float
    results: list[CaseResult] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


def matches(document_name: str, expect: str) -> bool:
    """期望串是否命中这份文档。

    **忽略大小写与空白**：人写金标时不会刻意对齐大小写，为这个让用例失败
    只会让人把期望串改得越来越"精确"，最后精确到只能匹配当前这一版文件。
    """
    pattern = " ".join(expect.split()).lower()
    if not pattern:
        return False
    return pattern in " ".join(document_name.split()).lower()


def _found(expect: str, ranked: Sequence[str]) -> bool:
    return any(matches(name, expect) for name in ranked)


def run_case(case: EvalCase, ranked_documents: Sequence[str]) -> CaseResult:
    """给一条金标打分。``ranked_documents`` 是前 k 条命中的文档名（按名次）。"""
    if not case.expect:
        # 空期望是"只记录问题、暂不评分"——写金标的人可以先把问题攒起来
        return CaseResult(case, tuple(ranked_documents), True, 1.0, 1.0)

    deduped = list(dict.fromkeys(ranked_documents))
    found = [item for item in case.expect if _found(item, deduped)]
    recall = len(found) / len(case.expect)
    reciprocal = 0.0
    for index, name in enumerate(deduped, start=1):
        if any(matches(name, item) for item in case.expect):
            reciprocal = 1.0 / index
            break
    return CaseResult(case, tuple(ranked_documents), bool(found), recall, reciprocal)


def summarize(results: Iterable[CaseResult]) -> EvalSummary:
    """把逐条结果汇成三个平均指标。空集返回全 0 而不是抛错（没有金标时也该有输出）。"""
    items = list(results)
    if not items:
        return EvalSummary(total=0, hits=0, mean_recall=0.0, mrr=0.0, results=[])
    return EvalSummary(
        total=len(items),
        hits=sum(1 for item in items if item.hit),
        mean_recall=sum(item.recall for item in items) / len(items),
        mrr=sum(item.reciprocal_rank for item in items) / len(items),
        results=items,
    )


def evaluate(
    cases: Sequence[EvalCase],
    search: Callable[[str], Sequence[str]],
) -> EvalSummary:
    """跑完整金标集。``search(query)`` 返回该查询命中的文档名（按名次）。

    把检索**注入**进来（而不是在这里连 HTTP/存储）：评测逻辑与"怎么取结果"无关，
    这样脚本、接口、单测可以共用同一份打分口径——口径分家是最容易让数字失去意义的事。
    """
    return summarize(run_case(case, search(case.query)) for case in cases)
