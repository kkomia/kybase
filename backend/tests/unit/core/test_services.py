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
    "stats",
    "runtime",
    "api_keys",
    "idempotency",
    "chunks",
    "models",
    "usage",
    "search_stats",
    "users",
    "auth",
    "shares",
    "lifecycle",
    "sources",
    "observability",
    "tabular",
    "folders",
    "batch",
    "maintenance",
    "summaries",
    "kb_prompt",
    "wiki",
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


def test_generative_services_all_go_through_the_same_llm_entry(bundle) -> None:  # type: ignore[no-untyped-def]
    """KB 侧四处生成式（摘要 / 出题 / Wiki / 库提示词）**共用同一个模型来源**。

    它们原先各自拿着一个 `ChatService` 当 LLM 网关；现在拿的是同一个
    `runtime`（`RuntimeConfigService`），每次调用经 `llm.chat_for(runtime)` 现取配置
    ——**一处口径**：模型选择、没配时那句可操作的报错、思考开关的翻译都只有一份。
    """
    services = build_services(stores=bundle)

    for service in (
        services.summaries,
        services.suggested_questions,
        services.wiki,
        services.kb_prompt,
    ):
        assert service._runtime is services.runtime


def test_worker_pool_size_follows_the_concurrency_setting(bundle) -> None:  # type: ignore[no-untyped-def]
    """``KYLAB_WORKER_CONCURRENCY`` 决定**进程里有几个消费者**（12.115）。

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


def test_the_pipeline_is_the_real_one(bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**摄入流水线是进程内那套真服务**（M3 阶段 3 / R11）。

    旧版在组合根里曾把 `ingest` / `documents` 换成两个"打远端"的窄网关，
    那一整支随旧版客户端拆除了——这一条钉住"换线没有以任何形式漏回来"：

    - `Services.ingest` / `Services.documents` 是真服务（不是只有 `submit` 的窄视图）；
    - 数据源与批量动作拿到的也是它们（一份实现，不是各建各的）。
    """
    from app.core.services import reset_services
    from app.services.documents import DocumentService
    from app.services.ingest import IngestService

    reset_services()
    services = build_services(stores=bundle)

    assert isinstance(services.ingest, IngestService)
    assert isinstance(services.documents, DocumentService)
    assert services.sources._ingest is services.ingest
    assert services.sources._documents is services.documents
    assert services.batch._documents is services.documents
