"""PaddleOCR 云端解析器的可靠性边界（不打网络）。

镜像同构：``app/parsers/paddleocr_api.py`` → ``tests/unit/parsers/test_paddleocr_api.py``。

与 MinerU 通道同一个缺口：轮询无上限会让摄入线程永久阻塞。这里钉住它的
``ParseError`` 抛出路径，避免两条云端通道的可靠性边界不一致。
"""

from __future__ import annotations

import pytest

from app.parsers.base import ParseError
from app.parsers.paddleocr_api import (
    POLL_DEADLINE_SECONDS,
    PaddleOCRApiParser,
    PaddleOCRConfig,
)

# PaddleOCR 的信封是 {"data": {...}}（与 MinerU 的 {"code", "data"} 不同）
RUNNING = {
    "data": {"state": "running", "extractProgress": {"extractedPages": 1, "totalPages": 9}}
}
DONE = {"data": {"state": "done", "resultUrl": {"jsonUrl": "https://example.test/r.jsonl"}}}


class _Response:
    def __init__(self, payload, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):  # type: ignore[no-untyped-def]
        return self._payload


class _FakeClient:
    def __init__(self, payload) -> None:  # type: ignore[no-untyped-def]
        self._payload = payload
        self.calls = 0

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return _Response(self._payload)


def _parser(client: _FakeClient) -> PaddleOCRApiParser:
    return PaddleOCRApiParser(
        PaddleOCRConfig(token="test-token"), client=client, poll_interval=0
    )


def _advancing_clock(step: float):  # type: ignore[no-untyped-def]
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += step
        return clock["now"]

    return monotonic


def test_paddleocr_polling_gives_up_after_the_deadline(monkeypatch) -> None:
    """云端永远 running 时必须抛 ParseError，而不是无限等下去。"""
    client = _FakeClient(RUNNING)
    monkeypatch.setattr(
        "app.parsers.paddleocr_api.time.monotonic",
        _advancing_clock(POLL_DEADLINE_SECONDS / 4),
    )

    with pytest.raises(ParseError) as excinfo:
        _parser(client)._poll(client, "job-1")  # type: ignore[arg-type]

    assert "放弃等待" in str(excinfo.value)
    assert client.calls >= 1
    assert client.calls < 100


def test_paddleocr_polling_returns_url_when_done(monkeypatch) -> None:
    """正常完成不受上限影响。"""
    client = _FakeClient(DONE)
    monkeypatch.setattr("app.parsers.paddleocr_api.time.monotonic", _advancing_clock(1.0))

    assert _parser(client)._poll(client, "job-2") == "https://example.test/r.jsonl"  # type: ignore[arg-type]


# --------------------------------------------------------------------- 页码


def test_parse_marks_every_page_with_its_real_number(monkeypatch) -> None:
    """PaddleOCR 逐页产出，是拿到**准确页码**的最好机会。

    空页不插标记（没有内容可标），但**页号不压缩**——第 3 页就是第 3 页，
    不能因为第 2 页是空的就把它叫成第 2 页。
    """
    from app.parsers.paddleocr_api import PaddleOCRApiParser, PaddleOCRConfig, _Page

    parser = PaddleOCRApiParser(PaddleOCRConfig(token="sk-test"), client=object())  # type: ignore[arg-type]
    monkeypatch.setattr(parser, "_submit", lambda *a, **k: "job")  # type: ignore[method-assign]
    monkeypatch.setattr(parser, "_poll", lambda *a, **k: "url")  # type: ignore[method-assign]
    pages = [_Page("第一页正文。"), _Page(""), _Page("第三页正文。")]
    monkeypatch.setattr(parser, "_collect", lambda *a, **k: pages)  # type: ignore[method-assign]

    result = parser.parse(content=b"x", filename="a.pdf", mime_type="application/pdf")

    assert "<!-- page:1 -->" in result.markdown
    assert "<!-- page:3 -->" in result.markdown
    assert "<!-- page:2 -->" not in result.markdown
    assert result.page_count == 3
