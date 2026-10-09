"""存储维护端点（v17）。

**管理员专属**：它能回答"这台机器上到底存了多少东西"（与 `/tasks/health` 同一档），
而"整理存储"会重写整个数据库文件——耗时且影响全站，不该给普通成员。
权限判定沿用 `tasks.py` 的同款内联写法（能给出中文原因，而不是一个 403 空壳）。

两个端点都**不做任何自动触发**：VACUUM 只在用户明确点「整理存储」时跑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_read
from app.api.v1.schemas import StorageOverviewOut
from app.core.exceptions import ForbiddenError
from app.core.services import Services, get_services
from app.services.api_key import Caller

router = APIRouter(tags=["maintenance"])


def _require_admin(caller: Caller) -> None:
    if caller.user is not None and not caller.is_admin:
        raise ForbiddenError("存储维护需要管理员身份")


@router.get("/maintenance/storage", response_model=StorageOverviewOut, summary="存储空间概览")
def storage_overview(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> StorageOverviewOut:
    """文件占用、可回收空间、向量分区数，以及无主分区。

    "可回收"不是估算：它就是 SQLite 的 freelist（删数据后留下的空页）。
    不显式暴露这个数字，用户看到的是"我删了东西，磁盘却没变"，
    只能怀疑系统在偷偷存。
    """
    _require_admin(caller)
    return StorageOverviewOut.model_validate(services.maintenance.overview())


@router.post("/maintenance/compact", response_model=StorageOverviewOut, summary="整理存储")
def compact_storage(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> StorageOverviewOut:
    """丢掉无主向量分区并 VACUUM，返回整理**之后**的概览。

    **只动无主数据**：有主的向量、切块、文档一律不碰。所以它安全到可以随便点，
    代价只是时间（VACUUM 会重写整个库文件）。
    """
    _require_admin(caller)
    return StorageOverviewOut.model_validate(services.maintenance.compact())
