"""数据源端点（M6 / T6.1–T6.3）。

四个动作：**登记、列出、启停、拉取**。

**为什么拉取要分同步/异步两条**：
- ``POST /data-sources/{id}/sync`` 入队（默认）——一个源几十条，每条都要解析
  与向量化，同步做会把 HTTP 请求挂几分钟；
- ``POST ...?wait=true`` 立刻做完并返回结果——给"我就想立刻看到抓到了什么"用，
  测试也走这条。

**权限**：读写。拉取会往知识库里加文档，只读密钥不该能做。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.auth import WRITE, check_kb_scope, require_read, require_write
from app.api.v1.schemas import (
    DataSourceCreateIn,
    DataSourceListOut,
    DataSourceOut,
    SyncResultOut,
)
from app.core.services import Services, get_services
from app.services.api_key import Caller

router = APIRouter(tags=["data-sources"])


def _out(services: Services, record) -> DataSourceOut:  # type: ignore[no-untyped-def]
    return DataSourceOut(
        id=record.id,
        knowledge_base_id=record.knowledge_base_id,
        kind=record.kind.value,
        name=record.name,
        url=str(record.config.get("url") or ""),
        max_items=int(record.config.get("max_items") or 0) or None,
        enabled=record.enabled,
        etag=record.etag,
        last_pulled_at=record.last_pulled_at,
    )


@router.get(
    "/knowledge-bases/{kb_id}/data-sources",
    response_model=DataSourceListOut,
    summary="某知识库的数据源",
)
def list_data_sources(
    kb_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> DataSourceListOut:
    check_kb_scope(services, caller, [kb_id])
    return DataSourceListOut(
        items=[_out(services, item) for item in services.sources.list(kb_id)]
    )


@router.post(
    "/knowledge-bases/{kb_id}/data-sources",
    response_model=DataSourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记数据源（HTML / RSS）",
)
def create_data_source(
    kb_id: str,
    payload: DataSourceCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> DataSourceOut:
    """登记一个数据源。

    **登记不等于拉取**：刚登记完不会立刻有文档，要么等定时任务，
    要么显式点一次"拉取"。界面上要说清这一点，否则用户会以为登记完就有了。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    from app.models.enums import DataSourceKind

    record = services.sources.create(
        knowledge_base_id=kb_id,
        kind=DataSourceKind(payload.kind),
        name=payload.name,
        url=payload.url,
        max_items=payload.max_items,
    )
    return _out(services, record)


@router.patch(
    "/data-sources/{source_id}/enabled",
    response_model=DataSourceOut,
    summary="启用 / 停用数据源",
)
def toggle_data_source(
    source_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    enabled: bool = Query(description="是否启用"),
) -> DataSourceOut:
    record = services.sources.get(source_id)
    check_kb_scope(services, caller, [record.knowledge_base_id], need=WRITE)
    return _out(services, services.sources.set_enabled(source_id, enabled=enabled))


@router.delete(
    "/data-sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除数据源（已抓取的文档保留）",
)
def delete_data_source(
    source_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    """删数据源，**已抓取的文档保留**——它们是知识库的正式内容，可能已被引用。
    停掉订阅不等于要撤销已经收集的资料。
    """
    record = services.sources.get(source_id)
    check_kb_scope(services, caller, [record.knowledge_base_id], need=WRITE)
    services.sources.delete(source_id)


@router.post(
    "/data-sources/{source_id}/sync",
    response_model=SyncResultOut,
    summary="拉取一次（默认入队；wait=true 立刻做完）",
)
def sync_data_source(
    source_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    wait: bool = Query(default=False, description="true 则同步做完并返回统计"),
) -> SyncResultOut:
    """拉一次。

    ``wait=false``（默认）只入队并返回任务 id；``wait=true`` 直接跑完，
    返回"取回几条、新入库几条、重复几条、失败几条"。
    """
    record = services.sources.get(source_id)
    check_kb_scope(services, caller, [record.knowledge_base_id], need=WRITE)
    if not wait:
        task_id = services.sources.sync_async(source_id)
        return SyncResultOut(task_id=task_id)

    outcome = services.sources.sync_now(source_id)
    return SyncResultOut(**outcome.as_dict())  # type: ignore[arg-type]
