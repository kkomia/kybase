"""待确认登记表（v0.41）。

镜像同构：``app/services/approvals.py`` → 本文件。

这一层只有一件事，但它有三个"差一点就错"的地方，所以用例就按那三处来写：

1. **三方不同线程**：登记在执行器的线程池里、等待在生成器的线程上、决定来自
   另一个请求线程。所以每条用例都是"一个线程在等，另一个线程交决定"，
   而不是自己给自己 set 一下（那样测不出线程安全）。
2. **超时必须按"没批准"处理**：等不到人却放行，是这一层最严重的失败。
3. **失效要说得出话**（`decide` 返回假）：超时之后回一句"已记录"，
   用户就会以为命令执行了——而那一头早就按"没有回应"往下跑了。
"""

from __future__ import annotations

import threading
import time

from app.services import approvals as approval_service
from app.services.approvals import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    DENY,
    TIMEOUT,
    ApprovalRegistry,
)


def _answer_when_asked(
    registry: ApprovalRegistry, approval_id: str, decision: str
) -> threading.Thread:
    """起一个线程去交决定——**模拟另一个请求**（真实链路里那是 FastAPI 的线程池）。"""

    def shot() -> None:
        # 先给等待方一点时间真的进到 wait 里（拿不到锁也无所谓，Event 会记住）
        time.sleep(0.05)
        registry.decide(approval_id, decision)

    worker = threading.Thread(target=shot, daemon=True)
    worker.start()
    return worker


def test_wait_blocks_until_the_decision_arrives() -> None:
    """等待是**真的阻塞**，而且决定交回来之后立刻返回——它就是"停下来问"的全部。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls -la")

    worker = _answer_when_asked(registry, request.approval_id, ALLOW_ONCE)
    started = time.monotonic()
    decision = registry.wait(request.approval_id)

    assert decision == ALLOW_ONCE
    assert time.monotonic() - started >= 0.04, "应当在等到决定之前真的停住"
    worker.join(1)


def test_timeout_means_no_permission() -> None:
    """没人回应 → ``TIMEOUT``（**绝不是放行**）：调用方据此按"没批准"处理。"""
    registry = ApprovalRegistry(timeout=0.05)
    request = registry.open(tool="run_command", label="执行命令", args="ls")

    started = time.monotonic()
    assert registry.wait(request.approval_id) == TIMEOUT
    assert time.monotonic() - started >= 0.04


def test_a_late_decision_is_refused_and_says_so() -> None:
    """已经超时之后再来的决定**必须被拒绝**，并且回话（``False``）。

    它对应的界面现象是"用户点了允许，但这一轮已经按没有回应继续了"——
    这时候不能回"已记录"，那是在告诉他命令跑了。
    """
    registry = ApprovalRegistry(timeout=0.05)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    assert registry.wait(request.approval_id) == TIMEOUT

    assert registry.decide(request.approval_id, ALLOW_ONCE) is False
    assert registry.decide("从来没有过的 id", ALLOW_ONCE) is False


def test_the_last_decision_wins_and_the_entry_is_consumed() -> None:
    """一条确认只被消费一次：交过决定之后这条就没了（第二个决定拿不到它）。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")

    assert registry.decide(request.approval_id, ALLOW_ALWAYS) is True
    assert registry.wait(request.approval_id) == ALLOW_ALWAYS
    assert registry.decide(request.approval_id, DENY) is False


def test_unknown_decision_value_is_refused() -> None:
    """只认那三个取值：别的（"yes"、空串）一律不放行。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    assert registry.decide(request.approval_id, "yes") is False
    assert registry.decide(request.approval_id, "") is False


def test_expired_entries_are_pruned_on_the_next_open() -> None:
    """没人来等的那些（没有界面的链路会登记完就走）要**自己消失**，否则会随进程一直长。"""
    registry = ApprovalRegistry(timeout=0.02)
    first = registry.open(tool="run_command", label="执行命令", args="ls")
    time.sleep(0.05)
    registry.open(tool="run_command", label="执行命令", args="ls2")

    assert registry.decide(first.approval_id, ALLOW_ONCE) is False


def test_wait_on_a_never_registered_id_returns_timeout() -> None:
    """id 不对（或者已经被处理掉）时**按没批准处理**，不抛异常。

    这条路是防御性的：抛出去会让整轮对话因为"一个内部 id 对不上"而失败，
    而它该有的结果是"这条没批准，照旧不执行"。
    """
    assert ApprovalRegistry().wait("nope") == TIMEOUT


def test_request_carries_what_the_ui_must_show() -> None:
    """登记的那几条字段就是确认条上要给用户看的东西——少一样他都没法判断。"""
    registry = ApprovalRegistry(timeout=7)
    request = registry.open(
        tool="run_command",
        label="执行命令",
        args="git push --force",
        detail="在 bwrap 隔离里执行；已断网",
        rule="Bash(git:*)",
    )
    assert request.label == "执行命令"
    assert request.args == "git push --force"
    assert "bwrap" in request.detail
    assert request.rule == "Bash(git:*)"
    assert request.timeout_seconds == 7
    assert request.approval_id


def test_the_three_decisions_are_the_only_public_vocabulary() -> None:
    """对外的词表只有三个（端点也按它校验）：超时与"没有人可问"是**执行侧的结论**，
    不该能从请求里伪造——伪造了就等于给了一条绕过"等用户点头"的路。"""
    assert approval_service.DECISIONS == (ALLOW_ONCE, ALLOW_ALWAYS, DENY)
    assert TIMEOUT not in approval_service.DECISIONS
    assert approval_service.UNAVAILABLE not in approval_service.DECISIONS


def test_the_deny_reason_travels_with_the_decision() -> None:
    """拒绝时用户填的那句话**跟着决定一起回来**（P2-1，照 ZCode 的确认弹窗）。

    它是这个输入框的全部价值所在：工具循环拿它拼进回灌给模型的文本，
    下一轮模型据此改路子（见 ``tool_loop._with_reason``）。
    """
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    worker = threading.Thread(
        target=lambda: (time.sleep(0.05), registry.decide(request.approval_id, DENY, "先别动")),
        daemon=True,
    )
    worker.start()

    answer = registry.wait_decision(request.approval_id)
    worker.join(2)

    assert answer.decision == DENY
    assert answer.reason == "先别动"


def test_a_reason_is_flattened_and_capped() -> None:
    """那句话会被**压成一行、限长**：它随后要进提示词，多行会让"一句理由"
    在模型眼里变成几条并列的指令（见 ``_clean_reason``）。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    long_reason = "别在\n生产库上跑\t这条\n" + "很长" * 400
    assert registry.decide(request.approval_id, DENY, long_reason) is True

    answer = registry.wait_decision(request.approval_id)
    assert "\n" not in answer.reason
    assert "\t" not in answer.reason
    assert len(answer.reason) <= approval_service.MAX_REASON_CHARS


def test_without_a_reason_nothing_changes() -> None:
    """不填理由：取值与老调用方（``wait``）拿到的完全是老样子。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    assert registry.decide(request.approval_id, DENY) is True

    assert registry.wait(request.approval_id) == DENY


def test_a_whitespace_only_reason_counts_as_empty() -> None:
    """"什么都没填"与"填了几个空格"是同一件事，都归空串——
    否则回灌文本里会多出一句"理由是："，后面什么都没有。"""
    registry = ApprovalRegistry(timeout=5)
    request = registry.open(tool="run_command", label="执行命令", args="ls")
    registry.decide(request.approval_id, DENY, "   \n ")

    assert registry.wait_decision(request.approval_id).reason == ""
