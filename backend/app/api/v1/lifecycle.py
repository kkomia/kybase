"""数据生命周期端点：影响清单、级联删除、回收站（M6 / T6.3、T6.4）。

**这组端点是计划里欠得最久的一块**：存储层从 M1 就有回收站，但接口层连一个
``DELETE /documents/{id}`` 都没有——用户传错一份文档只能看着它留着。

三个刻意的接口设计：

1. **影响清单与删除分成两个端点**（``GET .../impact`` 与 ``DELETE``）。
   合成一个"带确认参数的 DELETE"看着简洁，但界面就没法在动手**之前**
   把"会删掉 3 份文档、412 个切块"显示出来——而那正是二次确认的意义。
2. **删除文档进回收站，删除知识库不进。** 前者可恢复，后者不可
   （涉及多份文档、多个对象与一个向量分区）。两种的 ``restorable`` 不同，
   界面据此显示不同的警示强度。
3. **恢复要说明它恢复的是"骨架 + 原文"**：切块与向量在删除时就清掉了，
   恢复出来的文档需要重新摄入才有检索能力。不说清的话用户会以为恢复完就能搜。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import READ, WRITE, check_kb_scope, require_read, require_write
from app.api.v1.schemas import ImpactOut, TrashEntryOut, TrashListOut
from app.core.exceptions import ForbiddenError
from app.core.services import Services, get_services
from app.models.enums import ApiKeyPermission
from app.services.api_key import Caller

router = APIRouter(tags=["lifecycle"])


def _guard_document(
    services: Services, caller: Caller, document_id: str, *, need: ApiKeyPermission = READ
) -> None:
    """先取文档再判范围（顺序不能反，反了 404 会变成 403，等于泄露"这个 id 存在"）。"""
    document = services.documents.get(document_id)
    check_kb_scope(services, caller, [document.knowledge_base_id], need=need)


def _require_admin_for_trash(caller: Caller) -> None:
    """回收站只认管理员（v0.11 起管理员身份只来自会话角色）。

    回收站里是**所有人**删掉的文档（含别人的）。成员拦掉好理解；**API Key
    也拦**是因为：哪怕只读档的 key 也能从列表里看到所有人删过什么（跨库
    元信息泄露），读写档还能恢复/彻底删除任意条目。它是运维职能，
    不属于任何集成场景。
    """
    if not caller.is_admin:
        raise ForbiddenError("回收站需要管理员身份")


def _impact_out(report) -> ImpactOut:  # type: ignore[no-untyped-def]
    return ImpactOut(
        kind=report.kind,
        id=report.id,
        name=report.name,
        documents=report.documents,
        chunks=report.chunks,
        parts=report.parts,
        size_bytes=report.size_bytes,
        running_tasks=report.running_tasks,
        document_names=list(report.document_names),
        restorable=report.restorable,
    )


# --------------------------------------------------------------------- 影响清单


@router.get(
    "/documents/{document_id}/impact",
    response_model=ImpactOut,
    summary="删除这份文档会波及什么",
)
def document_impact(
    document_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ImpactOut:
    _guard_document(services, caller, document_id)
    return _impact_out(services.lifecycle.impact_of_document(document_id))


@router.get(
    "/knowledge-bases/{kb_id}/impact",
    response_model=ImpactOut,
    summary="删除这个知识库会波及什么",
)
def knowledge_base_impact(
    kb_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ImpactOut:
    check_kb_scope(services, caller, [kb_id])
    return _impact_out(services.lifecycle.impact_of_knowledge_base(kb_id))


# --------------------------------------------------------------------- 删除


@router.delete(
    "/documents/{document_id}",
    response_model=TrashEntryOut,
    summary="删除文档（原文进回收站，索引立即清除）",
)
def delete_document(
    document_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> TrashEntryOut:
    """删除文档。

    **返回** ``200`` 而不是 ``204``：正文里给出回收站条目 id 与到期时间，
    界面据此提示"7 天内可从回收站恢复"——这正是用户最需要知道的一句话。
    """
    _guard_document(services, caller, document_id, need=WRITE)
    entry = services.lifecycle.delete_document(document_id)
    return TrashEntryOut.model_validate(entry)


@router.delete(
    "/knowledge-bases/{kb_id}",
    response_model=ImpactOut,
    summary="删除知识库（不可恢复）",
)
def delete_knowledge_base(
    kb_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ImpactOut:
    """删除整个知识库。

    **返回影响清单**：界面拿它拼"已删除 3 份文档、412 个切块"的回执。
    比只回 204 有用——用户删完会想知道"到底删掉了多少"。

    注意（v10）：write 档的被分享者也能删——"写"包含内容生命周期，
    这与删库不可恢复（不进回收站）是同一个刻意选择的两端。
    想要"能传不能删"的档位，得加第三档权限，暂不做。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    return _impact_out(services.lifecycle.delete_knowledge_base(kb_id))


# --------------------------------------------------------------------- 回收站


@router.get("/trash", response_model=TrashListOut, summary="回收站")
def list_trash(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> TrashListOut:
    _require_admin_for_trash(caller)
    return TrashListOut(
        items=[TrashEntryOut.model_validate(item) for item in services.lifecycle.list_trash()]
    )


@router.post(
    "/trash/{trash_id}/restore",
    status_code=status.HTTP_202_ACCEPTED,
    summary="从回收站恢复（需要重新摄入）",
)
def restore_from_trash(
    trash_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> dict[str, str]:
    """恢复文档骨架与原文，并**重新入队摄入**。

    为什么返回 ``202``：恢复不是"点完就好"——文档回到「已上传」，
    要再跑一遍解析与向量化才有检索能力。返回 202 并带上任务 id，
    界面可以引导用户去任务中心看进度。
    """
    _require_admin_for_trash(caller)
    document_id, task_id = services.lifecycle.restore(trash_id)
    return {"document_id": document_id, "task_id": task_id or ""}


@router.delete("/trash/{trash_id}", status_code=status.HTTP_204_NO_CONTENT, summary="彻底删除")
def drop_trash(
    trash_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    _require_admin_for_trash(caller)
    services.lifecycle.drop_trash(trash_id)
