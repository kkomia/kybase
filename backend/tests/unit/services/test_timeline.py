"""处理进度时间线：把阶段事件折成"共几步 / 现在第几步 / 每步多久"（v24）。

镜像同构：``app/services/timeline.py`` → 本文件。

要紧的四条：

1. **归并中间态**：``parsed``/``chunked`` 不是独立环节，混进去会让进度条平白多两段；
2. **最后一步的耗时是"到现在"**——跑着的文档要能看到它在长（这是"还在动"的证据）；
3. **重试与重新摄入的时间都算得进去**（同一环节 visits > 1）；
4. 失败/取消是**整条流水线的结局**，不是某个环节——"炸在哪一步"取最后一个有事件的环节。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.timeline import PIPELINE_STEPS, build_timeline, progress_of

T0 = datetime(2026, 9, 15, 1, 0, 0, tzinfo=UTC)


class _Event:
    def __init__(self, stage: str, offset_s: float, error: str | None = None) -> None:
        self.stage = stage
        self.entered_at = T0 + timedelta(seconds=offset_s)
        self.error = error


def _at(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def test_steps_are_the_coarse_pipeline_not_raw_stages() -> None:
    """界面上是 6 个环节，不是状态机那 8 个阶段。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="uploaded",
        events=[_Event("uploaded", 0)],
        now=_at(5),
    )

    assert [step.key for step in timeline.steps] == [
        "uploaded",
        "probing",
        "parsing",
        "chunking",
        "embedding",
        "indexed",
    ]
    assert timeline.step_total == len(PIPELINE_STEPS) == 6


def test_middle_states_merge_into_their_step() -> None:
    """`parsed`/`chunked` 归并进"解析"/"切分"，不另起一段。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="chunked",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 10),
            _Event("parsed", 40),
            _Event("chunking", 45),
            _Event("chunked", 60),
        ],
        now=_at(70),
    )

    by_key = {step.key: step for step in timeline.steps}
    # parsed 的 5 秒算进"解析"（parsing→parsed 是这一步的一部分）
    assert by_key["parsing"].duration_ms == 35_000
    # chunking→chunked 15 秒 + chunked→now 10 秒 = 25 秒，全在"切分"上
    assert by_key["chunking"].duration_ms == 25_000
    assert by_key["chunking"].status == "running"
    assert by_key["probing"].visits == 0 and by_key["probing"].status == "pending"


def test_last_step_grows_while_running() -> None:
    """跑着时最后一步 = 到现在：换一个 now，耗时跟着变。"""
    events = [_Event("uploaded", 0), _Event("embedding", 30)]

    early = build_timeline(document_id="doc_1", stage="embedding", events=events, now=_at(40))
    later = build_timeline(document_id="doc_1", stage="embedding", events=events, now=_at(100))

    assert early.total_ms == 40_000 and later.total_ms == 100_000
    assert (early.current_index, early.status) == (5, "running")
    assert later.status == "running"


def test_retries_accumulate_and_are_visible() -> None:
    """重试/重新摄入：同一环节进出多次，耗时累加、visits > 1。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="indexed",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 10),
            _Event("failed", 70, error="上游超时"),
            _Event("parsing", 80),  # 重试
            _Event("chunking", 100),
            _Event("embedding", 110),
            _Event("indexed", 120),
        ],
        now=_at(130),
    )

    parsing = next(step for step in timeline.steps if step.key == "parsing")
    # 第一次解析 60s + 重试那次 20s
    assert parsing.visits == 2
    assert parsing.duration_ms == 80_000
    assert timeline.status == "done" and timeline.current_index == 6


def test_failed_is_a_pipeline_outcome_not_a_step() -> None:
    """失败不是环节：进度条停在"炸在哪一步"，并带上原因。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="failed",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 10),
            _Event("failed", 90, error="所有解析器都失败"),
        ],
        now=_at(100),
    )

    assert timeline.status == "failed"
    assert timeline.current_index == 3  # 停在"解析内容"
    parsing = next(step for step in timeline.steps if step.key == "parsing")
    assert parsing.status == "failed"
    assert parsing.error == "所有解析器都失败"
    # 后面几步仍是 pending，没有假装跑过
    later = ("chunking", "embedding", "indexed")
    assert all(step.status == "pending" for step in timeline.steps if step.key in later)


def test_canceled_marks_the_step_it_stopped_at() -> None:
    timeline = build_timeline(
        document_id="doc_1",
        stage="canceled",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 5),
            _Event("canceled", 20, error="已取消"),
        ],
        now=_at(30),
    )

    assert timeline.status == "canceled"
    parsing = next(step for step in timeline.steps if step.key == "parsing")
    assert parsing.status == "canceled" and parsing.error == "已取消"


def test_no_events_yields_an_empty_timeline_without_crashing() -> None:
    """老数据（这个机制之前入库的文档）没有事件：如实给一条空时间线。"""
    timeline = build_timeline(document_id="doc_old", stage="indexed", events=[], now=_at(0))

    assert timeline.status == "done"
    assert timeline.total_ms == 0
    assert all(step.duration_ms == 0 for step in timeline.steps)


# --------------------------------------------------------------- 列表行的进度摘要


def test_progress_folds_a_running_timeline() -> None:
    """列表要的是"第几步 / 叫什么 / 花了多久"，不是那棵树。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="parsing",
        events=[_Event("uploaded", 0), _Event("probing", 2), _Event("parsing", 5)],
        now=_at(125),
    )

    progress = progress_of(timeline)

    assert progress.status == "running"
    assert (progress.step_index, progress.step_total) == (3, 6)
    assert progress.step_label == "解析内容"
    # 当前这一步：5s 进入，到现在 125s
    assert progress.elapsed_ms == 120_000
    assert progress.total_ms == 125_000


def test_progress_counts_retries_of_the_current_step_only() -> None:
    """重试数说的是**当前这一步**：进过一次是正常路径，多进的才算重试。

    两个方向都要钉住：
    - 正在重试的那一步：visits=2 → retries=1（"它在反复重试"是列表上要看见的）；
    - 已经走过去的那一步：**不再算**——一篇重试过解析、如今在正常切分的文档
      不该永远挂着一个"重试 1 次"的标记，那是历史而不是现状。
    """
    first_try = build_timeline(
        document_id="doc_1",
        stage="parsing",
        events=[_Event("uploaded", 0), _Event("parsing", 5)],
        now=_at(30),
    )
    assert progress_of(first_try).retries == 0

    retrying = build_timeline(
        document_id="doc_1",
        stage="parsing",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 5),
            _Event("failed", 40, error="超时"),
            _Event("parsing", 50),
        ],
        now=_at(70),
    )
    assert progress_of(retrying).retries == 1

    moved_on = build_timeline(
        document_id="doc_1",
        stage="chunking",
        events=[
            _Event("uploaded", 0),
            _Event("parsing", 5),
            _Event("failed", 40, error="超时"),
            _Event("parsing", 50),
            _Event("chunking", 60),
        ],
        now=_at(70),
    )
    assert progress_of(moved_on).retries == 0


def test_progress_reports_the_step_it_died_on() -> None:
    """"炸在第 3 步"比"共 6 步"有用得多。"""
    timeline = build_timeline(
        document_id="doc_1",
        stage="failed",
        events=[_Event("uploaded", 0), _Event("parsing", 10), _Event("failed", 90)],
        now=_at(100),
    )

    progress = progress_of(timeline)

    assert (progress.status, progress.step_index, progress.step_label) == ("failed", 3, "解析内容")


def test_progress_only_calls_it_stalled_while_running() -> None:
    """停滞 = "跑着但没人管"。已完成的文档没有租约，别给它扣这顶帽子。"""
    running = build_timeline(
        document_id="doc_1", stage="embedding", events=[_Event("embedding", 0)], now=_at(10)
    )
    assert progress_of(running, stalled=True).stalled is True

    done = build_timeline(
        document_id="doc_1", stage="indexed", events=[_Event("indexed", 0)], now=_at(10)
    )
    assert progress_of(done, stalled=True).stalled is False


def test_normal_run_does_not_look_like_a_retry() -> None:
    """**一次正常跑完不算重试**（实机抓到的误报）。

    一个环节里含两个阶段（主阶段 + `parsed`/`chunked` 这种"做完了"的中间态），
    按"进入过几次"数就会把 `parsing → parsed` 数成两次进入——于是每一篇正常
    解析的文档都写着"进入 2 次"，也就是把正常路径报成了重试。实机上第一份文档
    就是这个样子（"解析内容 进入 2 次 7 秒"），而它根本没重试过。
    """
    timeline = build_timeline(
        document_id="doc_1",
        stage="indexed",
        events=[
            _Event("uploaded", 0),
            _Event("probing", 1),
            _Event("parsing", 2),
            _Event("parsed", 50),  # 中间态：同一步的"做完了"，不是第二次进入
            _Event("chunking", 51),
            _Event("chunked", 60),
            _Event("embedding", 61),
            _Event("indexed", 70),
        ],
        now=_at(80),
    )

    # 每一步都只进过一次（中间态不计），所以**没有任何一步看起来像重试过**
    assert all(step.visits == 1 for step in timeline.steps)
    assert progress_of(timeline).retries == 0
    # 但耗时仍然把中间态那段算进去（解析 2s → parsed 50s，那 48 秒就是在解析）
    parsing = next(step for step in timeline.steps if step.key == "parsing")
    assert parsing.duration_ms == 49_000


def test_the_first_step_is_the_queue_wait() -> None:
    """第一步叫「排队等待」而不是「已接收」：它的耗时就是"等了多久"。

    实机上第一份文档显示"已接收 26 分 26 秒"——读起来像"接收花了 26 分钟"，
    而实际是它在队列里躺了 26 分钟（单消费者被前一篇占着）。
    """
    timeline = build_timeline(
        document_id="doc_1",
        stage="probing",
        events=[_Event("uploaded", 0), _Event("probing", 1570)],
        now=_at(1580),
    )

    first = timeline.steps[0]
    assert first.label == "排队等待"
    assert first.key == "uploaded"
    assert first.duration_ms == 1_570_000
