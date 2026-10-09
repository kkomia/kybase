"""失败原因 → 用户能读懂的一句话（v0.53）。

镜像同构：``app/services/failures.py`` → 本文件。

这一层的错法只有一种，但**用户每一次都会看到**：把那句话换成了异常原文
（端点地址、上游返回体、``httpx.ReadTimeout`` 这类类名）。所以这里钉两条：

1. **已知的几类失败各自有一句人话**（模型不可达/超时、鉴权、限流、上下文超长、请求体不合法…）；
2. **任何一句里都不许出现异常原文**——不是"看起来像人话"，而是断言那句原文字串
   一次都没出现（含上游返回体的 JSON、异常类名、栈帧字样）。
"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import (
    ForbiddenError,
    InvalidRequestError,
    KylabError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from app.services.embedding.base import EmbeddingError, EmbeddingNotConfiguredError
from app.services.failures import _GENERIC, failure_text
from app.services.llm import RETRYABLE_REASONS, ChatError

#: 异常原文里那些"内部语言"的标记：判定"没有外泄"就看它们一个都不出现。
_LEAK_MARKS = (
    "http://",
    "https://",
    "Traceback",
    "File \"",
    "httpx.",
    "psycopg",
    "Expecting value",
    "Internal Server Error",
    "{\"error",
    "Errno",
)


def _assert_human(text: str, raw: str) -> None:
    assert text and text.strip() == text
    for mark in _LEAK_MARKS:
        assert mark not in text, f"这句话里带出了内部语言：{mark} → {text}"
    assert raw not in text or raw == "", "整段异常原文出现在给用户的那句话里"


def test_status_codes_get_their_own_sentences() -> None:
    """鉴权 / 找不到模型 / 请求体不合法：三类**动作不同**，句子必须不同。"""
    auth = failure_text(ChatError("x", status=401))
    missing = failure_text(ChatError("x", status=404))
    bad = failure_text(ChatError("x", status=400))

    assert len({auth, missing, bad}) == 3
    assert "密钥" in auth and "模型" in missing


def test_rate_limited_and_server_error_by_reason() -> None:
    limited = failure_text(ChatError("x", reason="rate_limited", status=429))
    server = failure_text(ChatError("x", reason="server_error", status=503))

    assert "限流" in limited or "额度" in limited
    assert "稍后重试" in server


def test_timeout_and_network_are_different_sentences() -> None:
    """一个"慢"、一个"没通上"：用户的下一步动作不同，不能合并成一句。"""
    assert failure_text(
        ChatError("x", reason="timeout")
    ) != failure_text(ChatError("x", reason="network_error"))


@pytest.mark.parametrize("reason", sorted(RETRYABLE_REASONS | {"stream_idle_timeout"}))
def test_every_classified_reason_has_a_sentence(reason: str) -> None:
    """``llm.py`` 里判过类的失败原因，这一层都得有对应的一句（否则就落到通用句）。"""
    text = failure_text(ChatError("上游返回体：{\"error\":\"boom\"}", reason=reason))

    assert text != failure_text(RuntimeError("x")), f"{reason} 退回了通用句"


def test_raw_upstream_body_never_reaches_the_user() -> None:
    """最有代表性的一条：401 的响应体里有上游原文与 URL，给用户的句子里一个字都不能有。"""
    raw = "对话端点鉴权失败（401）：请检查 API Key。{\"error\":\"invalid api key\"} https://api.example.com/v1"
    exc = ChatError(raw, status=401)

    text = failure_text(exc)

    _assert_human(text, raw)
    assert "api.example.com" not in text


def test_unknown_exception_falls_back_to_a_generic_human_sentence() -> None:
    """没归类的异常：**宁可变笼统，也不能把内部语言漏出去**。"""
    exc = RuntimeError("psycopg.OperationalError: connection to server at \"10.0.0.9\" failed")

    text = failure_text(exc)

    _assert_human(text, str(exc))


def test_domain_errors_do_not_pass_their_detail_through() -> None:
    """领域异常的 ``detail`` 多数是我们自己写的人话，但有几处拼了异常原文。

    这一层要能无条件担保"出口的话一定干净"，所以**不认 detail、只认类型**
    （``code`` → 这一类该怎么办）——逐处去修那些拼串是另一件事，
    不靠这一层替它们兜。
    """
    exc = InvalidRequestError('读不了这个文件：FileNotFoundError(2, "No such file")')

    text = failure_text(exc)

    _assert_human(text, exc.detail)
    assert "FileNotFoundError" not in text
    assert NotFoundError("会话不存在").detail not in text


def test_a_domain_error_still_says_what_kind_of_trouble_it_is() -> None:
    """按类型给动作：**不能**把"参数不对"说成"服务端出错"。

    全退回通用句是安全的，但用户照着"稍后重试"试十次也没用——这一类失败要的是
    "检查参数 / 重新登录 / 换格式"这种立刻能做掉的事。
    """
    assert "不合法" in failure_text(InvalidRequestError("x"))
    assert "登录" in failure_text(UnauthorizedError("x"))
    assert "权限" in failure_text(ForbiddenError("x"))
    # 认不出的类型（含 internal_error）照旧笼统
    assert failure_text(KylabError("x")) == _GENERIC


def test_embedding_errors_get_their_own_sentences() -> None:
    """没配向量化模型 ≠ 调用失败：前者要"去选一个模型"，后者才是"稍后重试"。"""
    unconfigured = failure_text(EmbeddingNotConfiguredError("未配置嵌入模型"))
    failed = failure_text(EmbeddingError("向量化调用失败"))

    assert unconfigured != failed
    assert "设置" in unconfigured


def test_transport_layer_errors_are_classified() -> None:
    timeout = failure_text(httpx.ReadTimeout("timed out"))
    connect = failure_text(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))

    assert timeout != connect
    _assert_human(timeout, "timed out")


def test_generic_upstream_error_stays_generic() -> None:
    text = failure_text(UpstreamError("rerank 不可用：HTTPSConnectionPool(host='x')"))

    _assert_human(text, "HTTPSConnectionPool(host='x')")
