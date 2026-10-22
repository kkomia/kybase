"""FastAPI 入口（分层规则 3.1）。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import API_VERSION, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.http import close_shared_client
from app.core.logging import add_file_handler, log_file_for, setup_logging
from app.core.services import get_services
from app.core.storage import close_stores
from app.workers.queue_worker import TaskWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动：备好存储（建库/迁移/目录）并按配置拉起内嵌任务消费者。"""
    settings = get_settings()
    services = get_services()

    stop = asyncio.Event()
    worker_tasks: list[asyncio.Task[None]] = []
    if settings.run_worker:
        # 一个消费者 = 一个协程（``KYLAB_WORKER_CONCURRENCY`` 个）。
        # 它们各自领活、互不阻塞：任务表本身就是队列，``claim_task`` 原子单语句，
        # 所以"多消费者"不需要额外的调度器（见 services/_build_workers 的说明）。
        # 兜底用 ``services.worker``：装配点已经保证 ``workers`` 非空，
        # 但"消费者一个都没起"是最难查的一类故障（任务永远排队），宁可这里多一句
        consumers = services.workers or [services.worker]
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
        title="kybase 知识库服务",
        version=settings.app_version,
        docs_url=f"/api/{API_VERSION}/docs",
        redoc_url=f"/api/{API_VERSION}/redoc",
        openapi_url=f"/api/{API_VERSION}/openapi.json",
        # FastAPI 默认会注册 /docs/oauth2-redirect，不带版本前缀，必须显式归位
        swagger_ui_oauth2_redirect_url=f"/api/{API_VERSION}/docs/oauth2-redirect",
        description=(
            "自托管知识库服务。"
            "对外只返回检索结果原文，不做 LLM 预处理。"
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
    # **只有一张路由表**（v0.1.0 精简）：原先按部署档挂 ``api_router`` / ``local_router``
    # 两张表，而旧版那一张已随旧版客户端一起拆除——这一份就是知识库服务对外的那张表。
    app.include_router(api_router, prefix=f"/api/{API_VERSION}")
    return app


app = create_app()
