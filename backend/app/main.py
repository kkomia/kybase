"""FastAPI 入口（工程规范 §3.1）。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router, local_router
from app.core.config import API_VERSION, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.http import close_shared_client
from app.core.logging import add_file_handler, log_file_for, setup_logging
from app.core.services import get_services
from app.core.storage import close_stores
from app.storage.base import IMPORT_UNFINISHED_STATES
from app.workers.local_worker import bind_local_scheduler, run_local_scheduler
from app.workers.queue_worker import TaskWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动：备好存储（建库/迁移/目录）并按配置拉起内嵌任务消费者。"""
    settings = get_settings()
    services = get_services()

    # 记忆/人设文件（v0.1.1）：**启动时幂等铺一遍**，不等"第一次对话"。
    # 新容器里还没有任何对话，这是唯一能让「记忆」页一打开就有东西可看的时机
    # （浏览/编辑那几个文件本来就不受 memory.enabled 那道闸管，见 services/memory.py）。
    # 只补共享桶（data/memory/）：按账号的工作区在各自第一次对话时补。
    # 写不出来只警告不拦启动——与上面那条日志处理器同一个口径。
    try:
        created = services.memory.seed_persona()
    except OSError:
        logger.warning("记忆/人设模板建不出来：%s", services.memory.workspace, exc_info=True)
    else:
        if created:
            logger.info("记忆/人设模板已就位：%s", "、".join(created))

    # 旧取值写回新档（2026-09-27 拆分权限轴时的一次迁移）：库里可能还留着
    # 四档模式时代的 `build` / `edit` / `yolo`。只在读的时候映射的话，设置页那个下拉
    # 会因为"没有匹配的选项"显示成空——**界面与引擎不一致**正是最难查的一类问题。
    # 失败只警告：迁移不成不该拦住整个服务起来（引擎侧本来就有 coerce 兜底）。
    try:
        migrated = services.runtime.normalize_modes()
    except Exception:
        logger.warning("旧模式取值写回失败", exc_info=True)
    else:
        if migrated:
            logger.info("旧的模式/权限取值已写回新档：%s", "；".join(migrated))

    stop = asyncio.Event()
    worker_tasks: list[asyncio.Task[None]] = []
    # **本机档的消费者与服务器档不是同一个**（M2 §4.1 + 本节）：
    # 服务器那个跑的是摄取流水线（解析 / 切块 / 嵌入 / 向量索引），而本机根本没有那些表
    # ——知识库在 NAS 上（本机是它的客户端）。起了它，
    # 表现是"进程里有个协程每隔几秒去撞一次不可用的库"，日志天天刷错却什么也做不成。
    #
    # 但**本机档确实需要消费者**：定时任务的记录与"到点"判定都在本机库里
    # （队列表在 NAS 上，所以那一档换成就地跑），而"入库/回收站"那几件收尾也都是
    # NAS 自己的事。两件事的落点与边界写在 `workers/local_worker.py` 的模块头。
    if settings.deployment == "local":
        if settings.run_worker:
            scheduler = bind_local_scheduler(services)
            worker_tasks = [asyncio.create_task(run_local_scheduler(scheduler, stop))]
            logger.info("本机档：已启动本机消费者（定时任务到点跑 + 本机库空闲维护）")
        else:
            logger.warning("KYLAB_RUN_WORKER=false：本机档未起消费者，定时任务到点不会跑")
        # R1 的"启动时看一眼"：上次旧会话导入要是被杀在半路，账在库里（状态是
        # planned/running）。**只报不重试**——重跑是用户的决定（来源可能都不在了），
        # 而"重跑同一个来源就接着往下走"这条承诺由会话级幂等兜着（见 services/legacy_import.py）。
        _report_unfinished_imports(services)
    elif settings.run_worker:
        # 一个消费者 = 一个协程（``KYLAB_WORKER_CONCURRENCY`` 个）。
        # 它们各自领活、互不阻塞：任务表本身就是队列，``claim_task`` 原子单语句，
        # 所以"多消费者"不需要额外的调度器（见 services/_build_workers 的说明）。
        # 兜底用 ``services.kb.worker``：装配点已经保证 ``workers`` 非空，
        # 但"消费者一个都没起"是最难查的一类故障（任务永远排队），宁可这里多一句
        consumers = services.kb.workers or [services.kb.worker]
        worker_tasks = [asyncio.create_task(_run_worker(worker, stop)) for worker in consumers]
        logger.info(
            "内嵌任务消费者已启动 %d 个：%s",
            len(worker_tasks),
            "、".join(worker.owner for worker in consumers),
        )
    else:
        logger.warning("KYLAB_RUN_WORKER=false：未启动任务消费者，上传的文档不会被处理")

    try:
        yield
    finally:
        stop.set()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        # 释放 PG 连接池：进程级资源，不还回去会拖住连接直到进程被回收
        close_stores()
        # 出站 HTTP 客户端同理（见 core/http.py）：连接池也是进程级资源
        close_shared_client()


def _report_unfinished_imports(services) -> None:  # type: ignore[no-untyped-def]
    """启动时如实报一句"还有几笔导入没结"（R1；**不自动重试**，只报）。

    读台账失败只警告：它不该拦住整个服务起来（与上面"记忆模板建不出来"同一口径）。
    """
    importer = services.legacy_import
    if importer is None:  # pragma: no cover - 本机档一定有；服务器档根本走不到这里
        return
    try:
        unfinished = [
            batch
            for batch in importer.recent_batches(limit=20)
            if batch.state in IMPORT_UNFINISHED_STATES
        ]
    except Exception:
        logger.warning("读导入台账失败（不影响启动）", exc_info=True)
        return
    if unfinished:
        logger.warning(
            "有 %d 个旧会话导入批次没跑完（%s）：重跑同一个来源即可续上（会话级幂等）",
            len(unfinished),
            "、".join(batch.id for batch in unfinished),
        )


async def _run_worker(worker: TaskWorker, stop: asyncio.Event) -> None:
    """跑消费循环。

    关停时 ``stop`` 置位只是"别再领新任务"；真正让卡在
    ``await asyncio.to_thread(...)`` 上的循环动起来的是取消。收尾语义由
    ``TaskWorker.run_forever`` 自己保证（取消后仍等手上那份文档写完），
    所以这里对 ``CancelledError`` 只做原样上抛——吞掉它 asyncio 会误以为
    任务正常结束，取消语义就丢了。

    **只把 ``stop`` 交给 run_forever，不额外传租约事件**：租约被回收时 worker
    自己就会停手（``run_once`` 拒绝再领任务、两个循环都检查 ``_lease_lost``），
    这里再插一手只会让"谁负责退出"变成两处判断。
    关停时 ``asyncio.gather`` 会等它收完手，语义不变。
    """
    try:
        await worker.run_forever(stop=stop)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("任务消费者异常退出")


def create_app() -> FastAPI:
    """组装应用：配置、中间件、异常映射、路由、生命周期。"""
    settings = get_settings()
    setup_logging(settings.log_level)
    # **文件日志在启动时挂上**（库代码不该决定往磁盘写什么）：
    # 控制台日志活不过一次重启，而"昨晚那次为什么慢"只能从文件里翻。
    # 路径不能拼错也没法失败退出——写不出来只影响日志，不该让服务起不来。
    try:
        location = add_file_handler(log_file_for(settings.data_dir))
        logger.info("日志文件：%s", location)
    except OSError:
        logger.warning("日志文件建不出来，本次只输出到控制台", exc_info=True)

    app = FastAPI(
        title="KYLAB 知识库服务",
        version=settings.app_version,
        docs_url=f"/api/{API_VERSION}/docs",
        redoc_url=f"/api/{API_VERSION}/redoc",
        openapi_url=f"/api/{API_VERSION}/openapi.json",
        # FastAPI 默认会注册 /docs/oauth2-redirect，不带版本前缀，必须显式归位
        swagger_ui_oauth2_redirect_url=f"/api/{API_VERSION}/docs/oauth2-redirect",
        description=(
            "轻量知识库产品（架构设计 v0.2）。"
            "产品边界：对外只返回检索结果原文，不做任何 LLM 预处理。"
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    # **按档挂哪一张路由表**（M2 §4.1）：服务器档全量（一位行为不变），
    # 本机档只挂白名单那几张（理由与清单见 `api/v1/router.py` 的 `local_router`）。
    # 档位在 `Settings` 上，而 `get_settings()` 是进程级单例——所以它只在启动时定一次，
    # 运行期换不了（那正是我们要的：中途换档会让"数据写哪儿"变成两处判断）。
    app.include_router(
        api_router if settings.deployment == "server" else local_router,
        prefix=f"/api/{API_VERSION}",
    )
    return app


app = create_app()
