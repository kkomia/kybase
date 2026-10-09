"""知识库 Wiki 端点（v24）。

四件事：看目录与状态、读一页（带出处）、触发重建、清空。

**生成是异步的**（`TaskKind.WIKI` + 任务队列）：一次生成要跑"规划 + 每页一次
模型调用"，同步接口会占着连接几分钟。所以 POST 只入队并回任务 id，
前端轮询 `GET /knowledge-bases/{kb_id}/wiki` 的 `status`。

页面正文里的 `[n]` 与 `sources[].index` 一一对应——**出处是这一层的核心承诺**，
没有出处的自动 Wiki 没人敢信（调研报告 §3.5）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.auth import WRITE, check_kb_scope, require_read, require_write
from app.api.v1.schemas import (
    WikiGenerateOut,
    WikiOverviewOut,
    WikiPageDetailOut,
    WikiPageOut,
    WikiSourceOut,
)
from app.core.services import Services, get_services
from app.services.api_key import Caller

router = APIRouter(tags=["wiki"])

# 页面/出处的组装放在这里，但**不 import 存储层的记录类型**（工程规范 §3.3 L1：
# 协议适配层只能转发 services/）。与 documents.py 的 `_to_out` 同一套写法——
# 参数不加类型标注，形状由 services 返回的对象保证。


def _page_out(record) -> WikiPageOut:  # type: ignore[no-untyped-def]
    return WikiPageOut(
        id=record.id,
        parent_id=record.parent_id,
        level=record.level,
        ord=record.ord,
        title=record.title,
        brief=record.brief,
        status=record.status,
        generated_at=record.generated_at,
    )


def _sources_out(services: Services, sources) -> list[WikiSourceOut]:  # type: ignore[no-untyped-def]
    """把出处的 ``document_id`` 解析成文档名（一次批量查，不逐条）。

    ``heading_path`` / ``page`` 不在这里回查 chunks——它们是生成时抄下来的快照，
    文档后来被删或重切也仍然指得准。
    """
    names = {
        record.id: record.name
        for record in services.documents.get_documents_by_ids(
            [item.document_id for item in sources]
        ).values()
    }
    return [
        WikiSourceOut(
            index=item.index,
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            document_name=names.get(item.document_id, item.document_id),
            heading_path=item.heading_path,
            page=item.page,
        )
        for item in sources
    ]


@router.get(
    "/knowledge-bases/{kb_id}/wiki",
    response_model=WikiOverviewOut,
    summary="Wiki 目录与生成状态",
)
def get_wiki(
    kb_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> WikiOverviewOut:
    check_kb_scope(services, caller, [kb_id])
    kb = services.knowledge_bases.get(kb_id)
    pages = services.wiki.pages(kb_id)
    state, last_error = services.wiki.status(kb_id)
    page_count, generated_at = services.wiki.stats(kb_id)
    return WikiOverviewOut(
        kb_id=kb_id,
        enabled=kb.wiki_enabled,
        status=state,
        page_count=page_count,
        generated_at=generated_at,
        model=None,
        last_error=last_error,
        pages=[_page_out(record) for record in pages],
    )


@router.get(
    "/wiki/pages/{page_id}",
    response_model=WikiPageDetailOut,
    summary="读一页 Wiki（正文 + 出处）",
)
def get_wiki_page(
    page_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> WikiPageDetailOut:
    record = services.wiki.page(page_id)
    # 页面归属的知识库决定可见性：先取页面再判范围（同文档端点的顺序）
    check_kb_scope(services, caller, [record.kb_id])
    sources = services.wiki.sources(page_id)
    return WikiPageDetailOut(
        **_page_out(record).model_dump(),
        kb_id=record.kb_id,
        content_md=record.content_md,
        model=record.model,
        updated_at=record.updated_at,
        sources=_sources_out(services, sources),
    )


@router.post(
    "/knowledge-bases/{kb_id}/wiki/generate",
    response_model=WikiGenerateOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重建这个知识库的 Wiki（异步）",
)
def generate_wiki(
    kb_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> WikiGenerateOut:
    """入队一次重建。已在队列里时**返回同一个任务**（幂等，避免连点堆任务）。

    Wiki 形态没开的话由服务层回 409（`ConflictError`）并说明怎么开。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    task = services.wiki.enqueue(kb_id)
    return WikiGenerateOut(task_id=task.id, kb_id=kb_id)


@router.delete(
    "/knowledge-bases/{kb_id}/wiki",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清空这个知识库的 Wiki 页面",
)
def clear_wiki(
    kb_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> None:
    """只删页面，**不动库形态开关**：用户可能只是想重来一次。"""
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    services.wiki.clear(kb_id)
