"""Webhook 订阅与投递（M4 / T4.6）的单元测试。

三块各测各的，因为它们出错的方式完全不同：

- **签名**：错了就是安全洞（能伪造或能重放），而且**错了不会报错**——
  签名算法两边不一致时，接收端只会一直拒绝，很难定位到是哪边错；
- **投递与重试**：决定了"事件到底能不能到"，而 4xx 重试会造成实际伤害
  （在对方日志里刷一片错误）；
- **事件时机**：决定了接收端拿到事件时能不能立刻取到内容。

`httpx.MockTransport` 而不是真起一个 HTTP 服务：要验的是**我们这个 client
发了什么、怎么处理各种响应**，不是 socket。而且这样能精确造出 500 → 200
这种"重试后成功"的序列，真服务很难稳定复现。
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app.services.webhook import (
    DOCUMENT_DELETED,
    DOCUMENT_FAILED,
    DOCUMENT_INDEXED,
    EVENTS,
    MAX_ATTEMPTS,
    WebhookService,
    sign_payload,
    verify_payload,
)

SHA = "supersecret"


def make_service(bundle, handler, *, sleeps: list[float] | None = None) -> WebhookService:
    """用 MockTransport 组一个服务。

    ``sleep`` 被替换成记录调用：真等 1+2+4 秒的测试没人愿意跑，
    而没人跑的测试等于没有。断言"退避的确是加倍的"比断言"等够了时间"更有价值。
    """
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    recorded = sleeps if sleeps is not None else []
    return WebhookService(bundle, client=client, sleep=recorded.append)


def ok_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler


# ------------------------------------------------------------------ 订阅管理


class TestSubscriptions:
    def test_新建订阅并列出(self, bundle) -> None:
        service = make_service(bundle, ok_handler([]))
        created = service.create(url="https://example.com/hook", secret=SHA)

        assert created.id.startswith("wh_")
        assert [item.id for item in service.list()] == [created.id]

    def test_未知事件名被拒(self, bundle) -> None:
        # 拼错事件名的表现是"订阅成功但永远收不到"——那是最难查的一类问题，
        # 所以在写入这一步就挡住
        service = make_service(bundle, ok_handler([]))
        with pytest.raises(ValueError, match="不支持的事件"):
            service.create(url="https://example.com/hook", events=["document.done"])

    def test_地址必须是_http_s(self, bundle) -> None:
        service = make_service(bundle, ok_handler([]))
        with pytest.raises(ValueError, match="http"):
            service.create(url="ftp://example.com/hook")

    def test_启停与删除(self, bundle) -> None:
        service = make_service(bundle, ok_handler([]))
        created = service.create(url="https://example.com/hook")

        assert service.set_enabled(created.id, False).enabled is False
        assert service.get(created.id).enabled is False

        service.delete(created.id)
        assert service.get(created.id) is None
        assert service.list() == []

    def test_没有订阅时发事件不报错(self, bundle) -> None:
        # 事件在摄入链路上是无条件发出的，没有订阅者是常态而不是异常
        service = make_service(bundle, ok_handler([]))
        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert outcome.results == []
        assert outcome.delivered == 0

    def test_未知事件被拒(self, bundle) -> None:
        service = make_service(bundle, ok_handler([]))
        with pytest.raises(ValueError, match="未知事件"):
            service.emit("document.exploded", {})


# ------------------------------------------------------------------ 签名


class TestSignature:
    def test_自己签的自己能验(self) -> None:
        header, _ = sign_payload(SHA, b'{"a":1}')
        assert verify_payload(SHA, b'{"a":1}', header) is True

    def test_换个体就验不过(self) -> None:
        header, _ = sign_payload(SHA, b'{"a":1}')
        assert verify_payload("other-secret", b'{"a":1}', header) is False

    def test_改一个字节就验不过(self) -> None:
        header, _ = sign_payload(SHA, b'{"a":1}')
        assert verify_payload(SHA, b'{"a":2}', header) is False

    def test_格式与_Stripe_一致(self) -> None:
        # 接收端是按这个形状解析的；改形状等于让所有接收端同时失效
        header, _timestamp = sign_payload(SHA, b"x", timestamp=1737000000)
        assert header.startswith("t=1737000000,v1=")
        assert len(header.split("v1=")[1]) == 64  # sha256 hex

    def test_时间戳参与签名(self) -> None:
        """这条是防重放的核心。

        只签 body 的话，攻击者可以把一个几小时前的合法请求**原样**重发，
        接收端算出来还是对得上——因为它就是原样的。
        时间戳进了签名，改 t 就必然导致 v1 对不上。
        """
        _, _moment = sign_payload(SHA, b"x", timestamp=1737000000)
        # 攻击者把时间戳改成"现在"，但 v1 还是老的
        old_header, _ = sign_payload(SHA, b"x", timestamp=1737000000)
        forged = f"t={int(time.time())},v1={old_header.split('v1=')[1]}"
        assert verify_payload(SHA, b"x", forged) is False


    def test_超出容忍窗口的时间戳被拒(self) -> None:
        # 用 1 小时前的时间戳签一份，接收端（容忍 5 分钟）必须拒
        header, _ = sign_payload(SHA, b"x", timestamp=int(time.time()) - 3600)
        assert verify_payload(SHA, b"x", header) is False
        # 容忍窗口放到 2 小时就放行——说明拒绝的原因确实是时间而不是签名
        assert verify_payload(SHA, b"x", header, tolerance_seconds=7200) is True

    def test_丢失或残缺的头一律拒(self) -> None:
        for bad in ("", "abc", "t=1", "v1=deadbeef", "t=,v1="):
            assert verify_payload(SHA, b"x", bad) is False, bad

    def test_用的是恒定时间比较(self) -> None:
        """`hmac.compare_digest` 而不是 `==`。

        逐字节比较会在第一个不同的字节上提前返回，攻击者能靠计时把签名
        一个字节一个字节试出来。这条测试只看源码用了哪个函数——
        因为计时攻击在单测里测不出来，只能守住实现方式。
        """
        import inspect

        from app.services import webhook

        source = inspect.getsource(webhook.verify_payload)
        assert "compare_digest" in source
        assert "v1\"] ==" not in source

    def test_签名头真的挂在请求上(self, bundle) -> None:
        seen: list[httpx.Request] = []
        service = make_service(bundle, ok_handler(seen))
        service.create(url="https://example.com/hook", secret=SHA)
        service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})

        assert len(seen) == 1
        header = seen[0].headers["X-Kylab-Signature"]
        # 用**收到的 body** 验签，证明签名覆盖的是真实载荷
        assert verify_payload(SHA, seen[0].content, header) is True

    def test_没有密钥时不带签名头(self, bundle) -> None:
        # 不带密钥的订阅是允许的（内网回调图省事）。那种情况下必须
        # **明确地不带头**，而不是带一个空头——空头会让接收端以为验签失败了
        seen: list[httpx.Request] = []
        service = make_service(bundle, ok_handler(seen))
        service.create(url="https://example.com/hook", secret=None)
        service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert "X-Kylab-Signature" not in seen[0].headers


# ------------------------------------------------------------------ 投递与重试


class TestDelivery:
    def test_载荷形状(self, bundle) -> None:
        seen: list[httpx.Request] = []
        service = make_service(bundle, ok_handler(seen))
        service.create(url="https://example.com/hook")

        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1", "chunk_count": 3})
        body = json.loads(seen[0].content)

        assert body["id"] == outcome.event_id
        assert body["event"] == DOCUMENT_INDEXED
        assert body["data"]["chunk_count"] == 3
        assert "created_at" in body
        assert seen[0].headers["X-Kylab-Event"] == DOCUMENT_INDEXED
        assert seen[0].headers["X-Kylab-Delivery"] == outcome.event_id

    def test_只投给订阅了该事件的(self, bundle) -> None:
        seen: list[httpx.Request] = []
        service = make_service(bundle, ok_handler(seen))
        service.create(url="https://a.example.com/hook", events=[DOCUMENT_FAILED])
        service.create(url="https://b.example.com/hook", events=[DOCUMENT_INDEXED])

        service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert [str(r.url) for r in seen] == ["https://b.example.com/hook"]

    def test_停用的不投(self, bundle) -> None:
        seen: list[httpx.Request] = []
        service = make_service(bundle, ok_handler(seen))
        created = service.create(url="https://example.com/hook")
        service.set_enabled(created.id, False)

        service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert seen == []

    def test_5xx_会重试并最终成功(self, bundle) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500 if calls["n"] < 3 else 200)

        service = make_service(bundle, handler, sleeps=sleeps)
        service.create(url="https://example.com/hook")
        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})

        assert calls["n"] == 3
        assert outcome.delivered == 1
        # 退避是加倍的：1s、2s（第 3 次成功所以不再睡）
        assert sleeps == [1.0, 2.0]

    def test_4xx_不重试(self, bundle) -> None:
        """请求本身有问题或对方不认，重试只会重复伤害。

        把 401 重试四遍既浪费，又会在对方日志里刷出一片错误——
        而运维看到的就是"这个服务在反复打我们"。
        """
        calls = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401)

        service = make_service(bundle, handler, sleeps=sleeps)
        service.create(url="https://example.com/hook")
        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})

        assert calls["n"] == 1
        assert sleeps == []
        assert outcome.failed == 1

    def test_网络错误也重试(self, bundle) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("连不上", request=request)

        service = make_service(bundle, handler, sleeps=sleeps)
        service.create(url="https://example.com/hook")
        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})

        assert calls["n"] == MAX_ATTEMPTS
        assert sleeps == [1.0, 2.0, 4.0]
        assert outcome.failed == 1

    def test_投递失败不抛给调用方(self, bundle) -> None:
        """webhook 是**旁路**：它挂了不能让用户的文档卡住。

        所以 ``emit`` 只把失败记在返回值里，绝不 raise——
        摄入链路上没有 try/except 等着接它。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上", request=request)

        service = make_service(bundle, handler, sleeps=[])
        service.create(url="https://example.com/hook")
        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert outcome.failed == 1  # 报出来了，但没有抛

    def test_一个订阅失败不影响另一个(self, bundle) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(500) if "bad" in str(request.url) else httpx.Response(200)

        service = make_service(bundle, handler, sleeps=[])
        service.create(url="https://bad.example.com/hook")
        service.create(url="https://good.example.com/hook")

        outcome = service.emit(DOCUMENT_INDEXED, {"document_id": "doc_1"})
        assert outcome.delivered == 1
        assert outcome.failed == 1
        assert "https://good.example.com/hook" in seen

    def test_接收端慢不会拖住太久(self, bundle) -> None:
        # 超时是 client 级的；这里验的是我们确实设了一个有限的值，
        # 而不是用 httpx 的默认（5 秒也还行，但我们显式声明了自己的意图）
        from app.services.webhook import REQUEST_TIMEOUT_SECONDS

        assert 0 < REQUEST_TIMEOUT_SECONDS <= 30


# --------------------------------------------------------------- 事件清单


class TestEventCatalogue:
    def test_事件名是常量且不重复(self) -> None:
        assert len(set(EVENTS)) == len(EVENTS)
        assert DOCUMENT_INDEXED in EVENTS
        assert DOCUMENT_FAILED in EVENTS
        assert DOCUMENT_DELETED in EVENTS

    def test_事件名是点分小写(self) -> None:
        # 接收端按名字做路由；大小写混用会让人反复查"为什么没触发"
        for name in EVENTS:
            assert name == name.lower()
            assert "." in name
            assert " " not in name


# --------------------------------------------------- 摄入链路的事件时机


class TestIngestEmitsEvents:
    """事件**什么时候**发，与事件发不发同等重要。

    接收端拿到 ``document.indexed`` 就会来取内容。如果我们提前发，
    它查到的是"还在跑"——而它完全有理由相信事件说的是事实。
    所以这里断言的不是"收到了事件"，而是"收到时状态已经落库"。
    """

    @staticmethod
    def _ingest(bundle, notifier):
        from app.parsers.base import ParseResult
        from app.services.embedding.deterministic import DeterministicEmbedder
        from app.services.ingest import IngestService
        from app.services.parser_router import ParserRouter

        class Markdown:
            name = "Markdown"

            def supports(self, *, filename, mime_type, probe) -> bool:
                return True

            def parse(self, *, content, filename, mime_type=None, probe=None):
                return ParseResult(markdown="# 标题\n\n正文。\n", parser_name=self.name)

        return IngestService(
            bundle,
            router=ParserRouter([Markdown()]),
            embedder=DeterministicEmbedder(dim=8),
            notifier=notifier,
        )

    def test_成功时发_indexed_且状态已落库(self, bundle, kb) -> None:
        from app.models.enums import DocumentStage

        seen: list[tuple[str, dict]] = []
        service = self._ingest(bundle, lambda event, payload: seen.append((event, payload)))

        submitted = service.submit(
            knowledge_base_id=kb.id, filename="a.md", content=b"# t\n\nbody\n"
        )
        service.ingest(submitted.document.id)

        events = [name for name, _ in seen]
        assert events == [DOCUMENT_INDEXED]
        payload = seen[0][1]
        assert payload["document_id"] == submitted.document.id
        assert payload["stage"] == "indexed"
        assert payload["chunk_count"] >= 1

        # **这条是关键的时机断言**：事件里报的 stage 必须与库里的一致。
        # 提前发的话这里会是 parsing/embedding
        record = bundle.meta.get_document(submitted.document.id)
        assert record.stage is DocumentStage.INDEXED
        assert payload["stage"] == record.stage.value

    def test_失败时发_failed_而不是_indexed(self, bundle, kb) -> None:
        """**只推成功是最常见的 webhook 设计错误。**

        调用方等一个永远不会来的 indexed，而文档早就 failed 了——
        它只能靠超时猜，或者退回轮询，那 webhook 就白做了。
        """
        from app.parsers.base import ParseError
        from app.services.embedding.deterministic import DeterministicEmbedder
        from app.services.ingest import IngestError, IngestService
        from app.services.parser_router import ParserRouter

        class Broken:
            name = "Broken"

            def supports(self, *, filename, mime_type, probe) -> bool:
                return True

            def parse(self, *, content, filename, mime_type=None, probe=None):
                raise ParseError("上游返回 500", stage="parsing")

        seen: list[tuple[str, dict]] = []
        service = IngestService(
            bundle,
            router=ParserRouter([Broken()]),
            embedder=DeterministicEmbedder(dim=8),
            notifier=lambda event, payload: seen.append((event, payload)),
        )

        submitted = service.submit(
            knowledge_base_id=kb.id, filename="a.md", content=b"# t\n\nbody\n"
        )
        with pytest.raises(IngestError):
            service.ingest(submitted.document.id)

        assert [name for name, _ in seen] == [DOCUMENT_FAILED]
        assert seen[0][1]["error"] == "上游返回 500"
        assert seen[0][1]["stage"] == "parsing"

    def test_没有_notifier_也能跑(self, bundle, kb) -> None:
        # 默认是 no-op：单测与脚本里常常不想接通知，
        # 而"必须传 notifier"会让每个调用点都多一行噪音
        from app.services.embedding.deterministic import DeterministicEmbedder
        from app.services.ingest import IngestService
        from app.services.parser_router import ParserRouter

        class Noop:
            name = "Noop"

            def supports(self, *, filename, mime_type, probe) -> bool:
                return True

            def parse(self, **kwargs):  # pragma: no cover - 本用例不会走到
                raise AssertionError("不该被调用")

        service = IngestService(
            bundle, router=ParserRouter([Noop()]), embedder=DeterministicEmbedder(dim=8)
        )
        assert service._notify("document.indexed", {}) is None


class TestLifecycleEmitsDeleted:
    def test_删除后发_deleted_并带可恢复期限(self, bundle, kb) -> None:
        from app.services.embedding.deterministic import DeterministicEmbedder
        from app.services.ingest import IngestService
        from app.services.lifecycle import LifecycleService
        from app.services.parser_router import ParserRouter

        seen: list[tuple[str, dict]] = []
        class Noop:
            name = "Noop"

            def supports(self, *, filename, mime_type, probe) -> bool:
                return True

            def parse(self, **kwargs):  # pragma: no cover - 本用例不摄入
                raise AssertionError("不该被调用")

        ingest = IngestService(
            bundle, router=ParserRouter([Noop()]), embedder=DeterministicEmbedder(dim=8)
        )
        submitted = ingest.submit(
            knowledge_base_id=kb.id, filename="a.md", content=b"# t\n\nbody\n"
        )

        lifecycle = LifecycleService(bundle, notifier=lambda e, p: seen.append((e, p)))
        lifecycle.delete_document(submitted.document.id)

        assert [name for name, _ in seen] == [DOCUMENT_DELETED]
        payload = seen[0][1]
        assert payload["document_id"] == submitted.document.id
        assert payload["name"] == "a.md"
        # **可恢复期限要带上**：接收端据此知道还能不能捞回原文，
        # 不给的话它只能假设"永久没了"
        assert payload["restorable_until"]
