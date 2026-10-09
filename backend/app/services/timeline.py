"""文档处理进度时间线：把"阶段进入事件"折成用户能看懂的环节清单（v24）。

数据源是 ``document_stage_events``（每次进入某阶段追加一条）。这里做两件事：

1. **把细阶段归并成粗环节**：摄入链路的内部阶段有 8 个
   （``uploaded → probing → parsing → parsed → chunking → chunked → embedding → indexed``），
   其中 ``parsed``/``chunked`` 只是"某一步做完了"的中间态，对用户没有意义。
   界面上要的是**几步**（对照 WeKnora 的 docreader/chunking/embedding/postprocess
   那种粒度），所以这里归并成 6 个环节。
2. **算每步耗时**：事件只记"什么时候进来的"，所以某次停留的耗时 =
   下一条事件的时间 − 这一条的时间；最后一条 = 现在 − 进入时间。
   按环节把多次停留累加，**重试/重新摄入花的时间也算得进去**
   （这正是"为什么这篇特别慢"要回答的问题）。

纯函数，不碰存储：用例可以只造一串事件就验完所有分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "PIPELINE_STEPS",
    "DocumentTimeline",
    "StageSpan",
    "TimelineStep",
    "build_timeline",
    "progress_of",
]


@dataclass(frozen=True, slots=True)
class _StepDef:
    key: str
    label: str
    stages: tuple[str, ...]


#: 界面上显示的环节清单（顺序即进度条的分段顺序）。
#: ``stages`` 是归属它的内部阶段：中间态（parsed/chunked）与主阶段归并到同一步，
#: 否则进度条会在"解析→解析完成"之间莫名多出一段。
PIPELINE_STEPS: tuple[_StepDef, ...] = (
    # 第一步的耗时 = 排队等了多久（文档是带着 uploaded 建出来的，到下一条事件之间
    # 就是它躺在队列里的时间）。所以标签叫「排队等待」而不是「已接收」——
    # 后者配上"已用 26 分钟"会被读成"接收花了 26 分钟"（实机看到的就是这个）。
    _StepDef("uploaded", "排队等待", ("uploaded",)),
    _StepDef("probing", "探测文件", ("probing",)),
    _StepDef("parsing", "解析内容", ("parsing", "parsed")),
    _StepDef("chunking", "切分与出题", ("chunking", "chunked")),
    _StepDef("embedding", "向量化", ("embedding",)),
    _StepDef("indexed", "完成索引", ("indexed",)),
)

#: 终态不是"环节"，而是整条流水线的结局。
_TERMINAL = {"failed": "failed", "canceled": "canceled"}

#: 反查：内部阶段 → 环节下标。
_STAGE_INDEX = {
    stage: index for index, step in enumerate(PIPELINE_STEPS) for stage in step.stages
}


@dataclass(frozen=True, slots=True)
class StageSpan:
    """一次停留（进入某阶段 → 下一条事件）。"""

    stage: str
    entered_at: datetime
    duration_ms: int


@dataclass(frozen=True, slots=True)
class TimelineStep:
    """一个环节的汇总（界面上一段进度 + 一行明细）。"""

    key: str
    label: str
    status: str
    """``done`` / ``running`` / ``pending`` / ``failed`` / ``canceled``。"""
    duration_ms: int
    visits: int
    """进入过几次。>1 说明重试或重新摄入过——界面上要能看出来。"""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentTimeline:
    document_id: str
    status: str
    """整条流水线的状态：``running`` / ``done`` / ``failed`` / ``canceled``。"""
    current_index: int
    """当前在第几个环节（1-based；已完成时 = 最后一个）。"""
    step_total: int
    total_ms: int
    steps: list[TimelineStep] = field(default_factory=list)
    spans: list[StageSpan] = field(default_factory=list)
    """逐次停留的原始明细。抽屉里可以展开看"第 2 次解析花了 4 分钟"。"""


@dataclass(frozen=True, slots=True)
class DocumentProgress:
    """列表行上那一条进度所需要的最小信息（``DocumentTimeline`` 的摘要）。

    **为什么不直接把时间线塞进列表**：一页 20 篇 × 6 个环节 × 每次停留的明细，
    响应体会膨胀到与页面信息量完全不成比例——而列表要的只有
    "第几步 / 这一步叫什么 / 已经花了多久 / 是不是卡住了"。完整那棵树归抽屉。
    """

    status: str
    """``running`` / ``done`` / ``failed`` / ``canceled``。"""
    step_index: int
    """当前第几步（1-based）。终态时是**停下时那一步**，不是总数——
    "炸在第 3 步"比"共 6 步"有用得多。"""
    step_total: int
    step_label: str
    """当前环节的中文名（"解析内容"）。"""
    elapsed_ms: int
    """当前这一步已经花了多久。跑着时它一直在涨。"""
    total_ms: int
    """整条流水线累计耗时（含重试与重新摄入）。"""
    retries: int = 0
    """进入过当前环节几次 − 1。>0 说明这一步重试过，
    而"卡住"与"反复重试"要看的处置完全不同。"""
    stalled: bool = False
    """执行租约已过期 = 没有 worker 在续约（判据来自 ``ObservabilityService``，
    这里只承载结果）。**跑着但没人管**是唯一能确定说"卡住"的情形。"""


def progress_of(timeline: DocumentTimeline, *, stalled: bool = False) -> DocumentProgress:
    """时间线 → 列表行要的摘要。

    ``visits`` 折成 ``retries``（次数 − 1），**只算当前环节**：进过一次是正常路径，
    把 1 显示成"重试 1 次"等于每篇文档都挂个假标记；而已经走过去的那一步也不再算
    ——"重试过解析、如今在正常切分"的文档不该永远带着历史标记（各步的进出次数
    在抽屉的明细里都还在）。
    """
    current = next(
        (step for step in timeline.steps if _ORDER[step.key] == timeline.current_index - 1),
        None,
    )
    if current is None:  # pragma: no cover - current_index 一定落在环节表里
        return DocumentProgress(
            status=timeline.status,
            step_index=timeline.current_index,
            step_total=timeline.step_total,
            step_label="",
            elapsed_ms=0,
            total_ms=timeline.total_ms,
            stalled=stalled,
        )
    return DocumentProgress(
        status=timeline.status,
        step_index=timeline.current_index,
        step_total=timeline.step_total,
        step_label=current.label,
        elapsed_ms=current.duration_ms,
        total_ms=timeline.total_ms,
        retries=max(0, current.visits - 1),
        stalled=stalled and timeline.status == "running",
    )


def build_timeline(
    *,
    document_id: str,
    stage: str,
    events: list,  # DocumentStageEventRecord（storage 的记录类型）
    now: datetime,
) -> DocumentTimeline:
    """把阶段事件折成环节时间线。

    ``stage`` 传文档当前阶段（``events`` 的最后一条可能还停在更早的阶段，
    比如刚推进完还没写事件——以文档上的为准更准）。
    """
    spans: list[StageSpan] = []
    for index, event in enumerate(events):
        # 这次停留持续到下一条事件为止；最后一条是"到现在"
        end = events[index + 1].entered_at if index + 1 < len(events) else now
        delta = end - event.entered_at
        spans.append(
            StageSpan(
                stage=event.stage,
                entered_at=event.entered_at,
                duration_ms=max(0, int(delta.total_seconds() * 1000)),
            )
        )

    # 按环节聚合。**用 spans 而不是 events**：耗时挂在"停留"上，不是挂在"进入"上。
    totals: dict[str, int] = {}
    visits: dict[str, int] = {}
    for span in spans:
        key = _STEP_KEY.get(span.stage)
        if key is None:
            continue
        totals[key] = totals.get(key, 0) + span.duration_ms
        # **只数"主阶段"的进入**：一个环节里含两个阶段（主阶段 + `parsed`/`chunked`
        # 这种"做完了"的中间态），而正常的 `parsing → parsed` 会数成两次进入——
        # 于是每一次正常解析都被标成"进入 2 次"，也就是把正常路径报成了重试。
        # 实机第一次跑就撞上了（界面写着"解析内容 进入 2 次 7 秒"）。
        if span.stage == _STEP_ENTRY[key]:
            visits[key] = visits.get(key, 0) + 1

    terminal = _TERMINAL.get(stage)
    # 失败/取消时"炸在哪一步"：**往回找最后一个真正落到环节上的事件**，而不是
    # 拿 stage 本身去查表——`failed` 不在环节表里，直接兜底会退到第一个环节，
    # 界面上就变成"炸在'已接收'"（用例抓到过）。
    fallback_key = next(
        (key for span in reversed(spans) if (key := _STEP_KEY.get(span.stage)) is not None),
        PIPELINE_STEPS[0].key,
    )
    current_key = _STEP_KEY.get(stage, fallback_key)
    current_idx = _order(current_key)

    steps: list[TimelineStep] = []
    for step in PIPELINE_STEPS:
        entered = visits.get(step.key, 0) > 0
        if step.key == current_key:
            if terminal is not None:
                status = terminal
            else:
                status = "done" if stage == "indexed" else "running"
        elif _order(step.key) < current_idx and entered:
            # **进过才算完成**：没记录的环节（老文档没有事件、或这些文件根本不走那一步）
            # 标成"完成"是撒谎，标 pending 才是实话
            status = "done"
        else:
            status = "pending"
        error = None
        if status in ("failed", "canceled"):
            error = next(
                (event.error for event in reversed(events) if event.error),
                None,
            )
        steps.append(
            TimelineStep(
                key=step.key,
                label=step.label,
                status=status,
                duration_ms=totals.get(step.key, 0),
                visits=visits.get(step.key, 0),
                error=error,
            )
        )

    if terminal is not None:
        status = terminal
    elif stage == "indexed":
        status = "done"
    else:
        status = "running"

    return DocumentTimeline(
        document_id=document_id,
        status=status,
        current_index=_order(current_key) + 1,
        step_total=len(PIPELINE_STEPS),
        total_ms=sum(item.duration_ms for item in steps),
        steps=steps,
        spans=spans,
    )


#: 内部阶段 → 环节 key（模块级，避免每次调用重建）。
_STEP_KEY = {
    stage: step.key for step in PIPELINE_STEPS for stage in step.stages
}
#: 环节 key → 它的**主阶段**（`stages[0]`）。次进次数的判定用它，见上面那句注释。
_STEP_ENTRY = {step.key: step.stages[0] for step in PIPELINE_STEPS}
_ORDER = {step.key: index for index, step in enumerate(PIPELINE_STEPS)}


def _order(key: str) -> int:
    return _ORDER.get(key, 0)
