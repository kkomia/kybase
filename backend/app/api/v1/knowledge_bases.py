"""知识库端点（M4）。

只做协议适配：校验入参 → 调用 ``KnowledgeBaseService`` → 转成响应模型。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends

from app.api.auth import WRITE, check_kb_scope, require_read, require_write
from app.api.v1.schemas import (
    KBPromptDraftOut,
    KBPromptGenerateIn,
    KnowledgeBaseCreate,
    KnowledgeBaseList,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.core.services import Services, get_services
from app.models.enums import UserRole
from app.services.api_key import Caller

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


def kb_access_flags(record: Any, caller: Caller, services: Services) -> tuple[bool, bool]:
    """``(can_manage, can_write)``——**这两条判定的唯一定义**。

    `record` 标成 `Any` 而不是具体记录类型：协议层不允许 import 存储
    （`scripts/check_layering.py` 的 L1 规则），而那条纪律正是"换存储不用改 api"的保证。

    两条判定都**在后端算**，前端不重复实现：
    - `can_manage` 与 ``services/share.py`` 的 ``_require_owner_or_admin`` 一致；
    - `can_write` 直接复用 ``api_keys.check_access(need=WRITE)``——
      界面据此决定要不要显示「上传文档」「添加数据源」，避免给出一个点了必然 403 的入口。

    **为什么提出来**（M3 阶段 1）：知识库提供者的握手（`api/v1/provider.py`）也要报
    "这把凭据能不能写这个库"。两处各写一份必然漂，而漂的表现是**界面自相矛盾**
    （列表上说能传、点进去 403，或反过来把能用的入口收起来）。所以口径只留这一份，
    两个端点都从这里取。
    """
    managed = caller.is_admin or (
        caller.user is not None
        and (caller.user.role is UserRole.ADMIN or record.owner_id == caller.user.id)
    )
    return managed, managed or services.api_keys.can_write(caller, record.id)


def visible_knowledge_bases(services: Services, caller: Caller) -> list[Any]:
    """调用方**看得见**的知识库记录——范围过滤的唯一定义。

    受限密钥只看到自己范围内的库：**列表也要过滤**，否则光看名字就能探出
    这台机器上有哪些知识库（元信息泄露），而且它点进去必然 403，体验也怪。

    与 :func:`kb_access_flags` 同一条理由提出来共用：握手回的是"这台提供者上有哪些库、
    你能用哪些"，它必须与 ``GET /knowledge-bases`` 给出**同一批**库，
    否则两处对同一把钥匙会有两种答案。
    """
    records = services.knowledge_bases.list_all()
    visible = services.api_keys.visible_kb_ids(caller)
    if visible is None:
        return list(records)
    allowed = set(visible)
    return [record for record in records if record.id in allowed]


def _out(
    record: Any,
    caller: Caller,
    services: Services,
    *,
    document_count: int = 0,
    last_activity: Any = None,
) -> KnowledgeBaseOut:
    """记录 → 响应，并补上"当前主体能不能管 / 能不能写这个库"。

    两条判定走 :func:`kb_access_flags`（**唯一一份口径**，与握手共用）。

    ``document_count`` / ``last_activity`` 由调用方传入：列表接口一次 ``GROUP BY``
    拿到全部库的计数，单个库的接口用 ``document_stats()`` 里对应的一项。
    """
    managed, writable = kb_access_flags(record, caller, services)
    return KnowledgeBaseOut.model_validate(record).model_copy(
        update={
            "can_manage": managed,
            "can_write": writable,
            "document_count": document_count,
            "last_activity": last_activity,
        }
    )


@router.post("", response_model=KnowledgeBaseOut, status_code=201, summary="创建知识库")
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> KnowledgeBaseOut:
    # 建库是写操作。注意**不把新库塞进密钥范围**：密钥能建库不代表它能碰新库，
    # 范围是发钥匙时定死的，运行时不该被调用方自己扩大。
    check_kb_scope(services, caller, None)
    record = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}",
        name=payload.name,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
        # 登录成员建的库归自己（v10 私有隔离）；API Key 通道建的库无主
        owner_id=caller.user.id if caller.user else None,
        # 嵌入模型随库选定并冻结（v11）；留空走服务端默认
        embedding_model_pk=payload.embedding_model_pk,
        # 推荐问题设置（v19）：建库时就能定，之后在「知识库设置 → 推荐问题」里改
        suggested_enabled=payload.suggested_enabled,
        suggested_count=payload.suggested_count,
        suggested_model_pk=payload.suggested_model_pk,
        suggested_prompt=payload.suggested_prompt,
        wiki_enabled=payload.wiki_enabled,
    )
    return _out(record, caller, services)


@router.get("", response_model=KnowledgeBaseList, summary="知识库列表")
def list_knowledge_bases(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> KnowledgeBaseList:
    records = visible_knowledge_bases(services, caller)
    # 计数一次聚合查出来，随列表一起回——前端不必再"逐库拉文档列表只为数数"
    stats = services.knowledge_bases.document_stats()
    return KnowledgeBaseList(
        items=[
            _out(
                record,
                caller,
                services,
                document_count=stats.get(record.id, (0, None))[0],
                last_activity=stats.get(record.id, (0, None))[1],
            )
            for record in records
        ]
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseOut, summary="知识库详情")
def get_knowledge_base(
    kb_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> KnowledgeBaseOut:
    check_kb_scope(services, caller, [kb_id])
    count, last_activity = services.knowledge_bases.document_stats().get(kb_id, (0, None))
    return _out(
        services.knowledge_bases.get(kb_id),
        caller,
        services,
        document_count=count,
        last_activity=last_activity,
    )


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut, summary="修改知识库（名称 / 简介）")
def update_knowledge_base(
    kb_id: str,
    payload: KnowledgeBaseUpdate,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> KnowledgeBaseOut:
    """改名称、简介或切分参数。**与"删除知识库"同一档权限**（WRITE）——都是库级结构动作，
    让改名比删库更严会得到一个说不通的权限阶梯（见 ``lifecycle.py`` 的同款说明）。

    各项都可选，只处理传了的那些；都为空时不动任何东西（幂等）。

    切分参数**只对之后摄入的文档生效**（切块是解析阶段写下的）。这里不假装
    "改完就重切"——重跑由用户显式触发，取舍见 ``services/knowledge_base.py::set_chunking``。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    record = services.knowledge_bases.get(kb_id)
    if payload.name is not None:
        record = services.knowledge_bases.rename(kb_id, payload.name)
    if payload.description is not None:
        record = services.knowledge_bases.set_description(kb_id, payload.description)
    if payload.chunk_size is not None or payload.chunk_overlap is not None:
        record = services.knowledge_bases.set_chunking(
            kb_id, chunk_size=payload.chunk_size, chunk_overlap=payload.chunk_overlap
        )
    if _touches_suggested(payload):
        record = services.knowledge_bases.set_suggested(
            kb_id,
            enabled=(
                payload.suggested_enabled
                if payload.suggested_enabled is not None
                else record.suggested_enabled
            ),
            count=(
                payload.suggested_count
                if payload.suggested_count is not None
                else record.suggested_count
            ),
            # 空串表示"清除"（回到跟随对话模型），与简介空串表示清空同一套约定
            model_pk=(
                payload.suggested_model_pk
                if payload.suggested_model_pk is not None
                else record.suggested_model_pk
            ),
            prompt=(
                payload.suggested_prompt
                if payload.suggested_prompt is not None
                else record.suggested_prompt
            ),
        )
    if payload.wiki_enabled is not None:
        record = services.knowledge_bases.set_wiki(kb_id, payload.wiki_enabled)
    # 库级提示词（v0.19）。空串 = 清除（回到只剩内置提示词），
    # 与简介、出题提示词同一套约定
    if payload.system_prompt is not None:
        record = services.knowledge_bases.set_system_prompt(kb_id, payload.system_prompt)
    count, last_activity = services.knowledge_bases.document_stats().get(kb_id, (0, None))
    return _out(
        record, caller, services, document_count=count, last_activity=last_activity
    )


@router.post(
    "/{kb_id}/prompt/generate",
    response_model=KBPromptDraftOut,
    summary="按文档摘要生成库提示词（草稿）",
)
def generate_kb_prompt(
    kb_id: str,
    payload: KBPromptGenerateIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> KBPromptDraftOut:
    """让对话模型**只依据库里已生成的文档摘要**写一版库提示词。

    **不落库**：这里只产出草稿（连带它依据了哪几篇、有没有引用不存在的文件）。
    写不写、改不改由用户在设置里确认——一次模型调用不该顺手改掉库的配置。

    为什么这个入口是 WRITE 而不是 READ：它**不写数据**，但要花一次模型调用，
    而"能改这个库的配置"和"能为这个库花钱"本来就该是同一批人
    （只读分享的成员连设置面板都看不到）。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    # 先按同一口径判一次存在性：库不存在时给 404，而不是让生成流程报一个
    # "没有文档摘要"（那是另一件事，用户会照着去传文档，然后仍然是 404）
    services.knowledge_bases.get(kb_id)
    draft = services.kb_prompt.generate(kb_id, model_pk=payload.model_pk)
    return KBPromptDraftOut(
        prompt=draft.prompt,
        sources=draft.sources,
        filename_style_citations=draft.filename_style_citations,
    )


def _touches_suggested(payload: KnowledgeBaseUpdate) -> bool:
    """这次 PATCH 有没有碰推荐问题设置。

    四个字段都可选，``None`` 表示"不改"；但只要有一个不是 ``None`` 就整组提交
    （``set_suggested`` 一次写四个值，界面也是一屏提交）。
    """
    return any(
        value is not None
        for value in (
            payload.suggested_enabled,
            payload.suggested_count,
            payload.suggested_model_pk,
            payload.suggested_prompt,
        )
    )
