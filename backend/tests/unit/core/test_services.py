"""组合根装配的冒烟测试（`core/services.py`）。

它是最容易被"改一处忘一处"的地方：新增一个服务却忘了接进 `Services`、
回调闭包漏了某个装配，都不会在别处报错，直到那个功能被用到。
这里只钉"整张图装配齐了、且不落盘"——具体行为归各自的单测。
"""

from __future__ import annotations

from app.core.services import build_services

#: 必须装配齐全的字段（新增服务时同步加进来；漏了就在这里红）。
_EXPECTED = (
    "knowledge_bases",
    "documents",
    "ingest",
    "retrieval",
    "chat",
    "stats",
    "runtime",
    "api_keys",
    "idempotency",
    "chunks",
    "models",
    "usage",
    "users",
    "auth",
    "shares",
    "lifecycle",
    "sources",
    "observability",
    "tabular",
    "conversations",
    "suggested_questions",
    "webhooks",
    "embedder",
    "reranker",
    "worker",
    "workers",
    "load",
)


def test_build_services_wires_every_component(bundle) -> None:  # type: ignore[no-untyped-def]
    services = build_services(stores=bundle)

    missing = [name for name in _EXPECTED if getattr(services, name, None) is None]
    assert missing == [], f"这些组件没有装配：{missing}"


def test_build_services_is_pure_wiring_when_stores_are_given(bundle) -> None:  # type: ignore[no-untyped-def]
    """给了 stores 就不该再去建库/建目录——测试与 MCP 进程都靠这条。"""
    before = bundle.meta.list_knowledge_bases()

    services = build_services(stores=bundle)

    assert services.knowledge_bases is not None
    assert bundle.meta.list_knowledge_bases() == before


def test_suggested_questions_and_chat_share_the_same_chat_service(bundle) -> None:  # type: ignore[no-untyped-def]
    """示例问题用的是同一个对话服务（否则两处的模型选择与未配置报错口径会分叉）。"""
    services = build_services(stores=bundle)

    assert services.suggested_questions._chat is services.chat


def test_worker_pool_size_follows_the_concurrency_setting(bundle) -> None:  # type: ignore[no-untyped-def]
    """``KYLAB_WORKER_CONCURRENCY`` 决定**进程里有几个消费者**（§12.115）。

    钉三件事，每一件写错了都会很安静：数量、owner 互不相同、面板上的槽位与它同源。
    **owner 必须互不相同**：租约按 owner 校验，同名会让两个消费者互相认领对方的租约
    （心跳返回真、终态互相覆盖）。
    """
    from app.core.config import Settings

    services = build_services(settings=Settings(worker_concurrency=3), stores=bundle)

    assert len(services.workers) == 3
    assert services.worker is services.workers[0]
    assert len({worker.owner for worker in services.workers}) == 3
    # 面板上的"并发槽位"与这里同源：上限不能是另一处硬编码的数字
    slots = services.load.snapshot().queue.slots
    assert slots == 3


def test_single_worker_is_the_default(bundle) -> None:  # type: ignore[no-untyped-def]
    """默认只 1 个消费者。

    并发要花 CPU 与内存（切词、向量化都在进程内），"默认炸内存"比"默认慢"更糟。
    """
    from app.core.config import Settings

    services = build_services(settings=Settings(worker_concurrency=1), stores=bundle)

    assert len(services.workers) == 1


def test_server_deployment_keeps_the_real_pipeline(bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**服务器档一切不变**（M3 阶段 3 / R11）：本机档那条换线一个字都不许漏过来。

    本机档在组合根新增的那一支（建提供者、把 `ingest` / `documents` 换成两个网关）如果
    写漏了档位判断，服务器档会**静默地**失去摄入流水线：`Services.ingest` 变成那个
    只有 `submit` 的窄视图、`ChatService` 拿着一个远端 KB 客户端（于是"进程内检索"
    这条主路径被悄悄换掉）。这一条按**可观察的结果**钉住它（组合根里那个 `provider`
    是局部变量，不对外露）：

    - KB 接缝是 `None`（服务器档走进程内检索）；
    - `Services.ingest` / `Services.documents` 仍是真服务；
    - 笔记与产物拿到的是**同一个**真 `IngestService`（三个构造点仍然是"一份实现"）。

    （本机档那一半在 `tests/unit/services/test_client_seams.py` 的守卫用例里：它要建
    本机档的 SQLite 图，所以标了 `local`；这一条跟着本文件用 PG 夹具。）
    """
    from app.core.config import get_settings
    from app.core.services import reset_services
    from app.services.documents import DocumentService
    from app.services.ingest import IngestService

    # 档位是**启动时定一次**的东西（``Settings`` 与 ``get_services`` 都是单例缓存）：
    # 这里把前提钉死，不吃上一条用例留下的档位
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "server")
    get_settings.cache_clear()
    reset_services()

    services = build_services(stores=bundle)

    assert get_settings().deployment == "server"
    assert services.chat._knowledge is None, "服务器档不该有远端 KB 客户端"
    assert isinstance(services.ingest, IngestService)
    assert isinstance(services.documents, DocumentService)
    # 三个构造点**仍是同一份**（本机档那对网关没有漏到这里来）
    assert services.notes._ingest is services.ingest
    assert services.artifacts._ingest is services.ingest
    assert services.notes._documents is services.documents
    assert services.artifacts._documents is services.documents
