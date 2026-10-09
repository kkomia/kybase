"""对话留存端点（开发计划 §11.2）。

**为什么单独一组端点而不是塞进 `/chat`**：会话的建、列、改名、删是**管理动作**，
与"问一个问题"是两件事。混在一起的话，前端每次提问都要顺便处理一堆会话状态；
分开之后 `/chat` 只负责回答，会话列表自己刷新。

**要不要鉴权**：要。这些端点读写的是用户与知识库的历史对话，属于内容本身；
用 ``ReadDep`` / ``WriteDep``，外部只读 API Key 也能回看历史（合理），
但删会话需要读写权限。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse

from app.api.auth import require_read, require_write, signing_secret_or_raise
from app.api.v1.schemas import (
    ChatAttachmentOut,
    ChatMessageOut,
    ChatSourceOut,
    ConversationArtifactListOut,
    ConversationArtifactOut,
    ConversationBranchIn,
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationFileImportIn,
    ConversationListOut,
    ConversationOut,
    ConversationRewindIn,
    ConversationRewindOut,
    ConversationUpdateIn,
    FileDownloadUrlOut,
    FileEntryOut,
    FileListingOut,
    IngestArtifactIn,
)
from app.api.v1.workspaces import Device, device_from_headers
from app.core.caller import WRITE, Caller
from app.core.config import Settings, get_settings
from app.core.downloads import content_disposition, media_type_of
from app.core.exceptions import NotFoundError, PayloadTooLargeError, UnauthorizedError
from app.core.services import Services, get_services
from app.core.signing import SigningError, verify_resource
from app.services.artifacts import (
    ARTIFACT_SCOPE_CONVERSATION,
    file_signature_resource,
    split_filename,
)
from app.services.conversation_export import MEDIA_TYPE, PAGE_SIZE
from app.services.session_events import fill_missing_thinking, steps_per_turn

router = APIRouter(prefix="/conversations", tags=["conversations"])

#: 单个文件的上传上限。与文档上传同一个数：两者都是"用户往我们的存储里放东西"，
#: 两个不同的上限只会让人猜"为什么这里能传、那里不能"。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _summary(services: Services, record, *, preview: str = "") -> ConversationOut:  # type: ignore[no-untyped-def]
    return ConversationOut(
        id=record.id,
        title=record.title,
        kb_ids=list(record.kb_ids),
        model_pk=record.model_pk,
        thinking=record.thinking,
        thinking_effort=record.thinking_effort,
        pinned=record.pinned,
        workspace_id=record.workspace_id,
        archived_at=record.archived_at,
        preview=preview,
        created_at=record.created_at,
        updated_at=record.updated_at,
        message_count=services.conversations.message_count(record.id),
    )


def _get_visible(services: Services, caller: Caller, conversation_id: str):  # type: ignore[no-untyped-def]
    """成员只能碰自己的会话（404 而不是 403：不暴露存在性）；其余通道照旧。"""
    owner = _caller_owner(caller)
    if owner is None:
        return services.conversations.get(conversation_id)
    return services.conversations.get_for_owner(conversation_id, owner)


def _caller_owner(caller: Caller) -> str | None:
    """只有**普通成员**会话才有归属过滤；管理员会话（is_admin）与
    API Key 通道都没有——否则管理员用网页会话看不到 API Key 建的无主会话（回归踩过）。
    """
    if caller.user is not None and not caller.is_admin:
        return caller.user.id
    return None


@router.get("", response_model=ConversationListOut, summary="会话列表（置顶优先，其次最近更新）")
def list_conversations(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(
        default=None, description="按标题**或消息正文**搜索（包含匹配，忽略大小写）"
    ),
    workspace_id: str | None = Query(default=None, description="只看这个工作区下的会话（v0.15）"),
    ungrouped: bool = Query(default=False, description="只看**未归档**的会话（不属于任何工作区）"),
    archived: bool = Query(
        default=False, description="看**已归档**的会话（历史会话面板的归档视图）"
    ),
    with_preview: bool = Query(
        default=False, description="是否带上最近一条回答的开头（历史会话面板的两行预览）"
    ),
) -> ConversationListOut:
    # 成员只看到自己的会话（v10 私有隔离）：对话内容是私有数据，
    # 列表不按归属过滤就等于把别人的问题全部摊开
    if workspace_id is not None:
        # 越权的工作区 id 直接 404：否则可以拿它当探针，试出别人有哪些工作区。
        # **设备那一维同样要过**（v0.59）：拿另一台机器的项目 id 来筛，
        # 与拿别人的项目 id 来筛是同一件事——会话本身不按设备隔离，
        # 但"这个筛选项指向的工作区"仍受设备闸管
        services.workspaces.get(
            workspace_id,
            user_id=_caller_owner(caller),
            device_id=request_device.id if request_device is not None else None,
        )
    records = services.conversations.list(
        limit=limit,
        owner_id=_caller_owner(caller),
        q=q,
        workspace_id=workspace_id,
        ungrouped=ungrouped,
        archived=archived,
    )
    # 预览**一次查完**（一条 `DISTINCT ON`），不是逐个会话去查——
    # 列表最多几十条，逐个查就是几十次往返。只有面板要它，所以做成了可选参数。
    previews = (
        services.conversations.previews([item.id for item in records]) if with_preview else {}
    )
    return ConversationListOut(
        items=[_summary(services, item, preview=previews.get(item.id, "")) for item in records]
    )


@router.get(
    "/export",
    summary="导出会话（NDJSON 流；给本机导入器用）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {MEDIA_TYPE: {"schema": {"type": "string"}}},
            "description": (
                "一行一个 JSON 的流：首行 header、每会话 conversation/message/event/artifact、"
                "末行 footer。形状与取舍见《API 接口规范》§1.11 与 "
                "`services/conversation_export.py` 的模块头。"
            ),
        }
    },
)
def export_conversations(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    limit: int = Query(default=PAGE_SIZE, ge=1, le=PAGE_SIZE, description="这一页最多扫几条会话"),
    offset: int = Query(default=0, ge=0, description="跳过前几条（翻页；含归档那一批）"),
    since: datetime | None = Query(
        default=None,
        description="只导**严格晚于**这个时刻更新过的会话（ISO 8601）。增量导入用",
    ),
) -> StreamingResponse:
    """把一个部署里的会话导成**自包含**的一份流（方案 §3.1：本机不直连服务器库）。

    为什么要有这条端点（而不是让本机去拼 ``GET /conversations/{id}`` + ``/events`` +
    ``/artifacts`` 三条）：那三条拼不出完整的会话（**丢 ``context_summary``**——
    它不在任何 API 响应里），也没有版本化的契约；而"导入"这件事必须有一个
    **有版本、可断言**的线格式（见 ``services/conversation_export.py``）。

    - **归属**：与列表同一个判据（``_caller_owner``）——成员只导自己的，
      管理员/本机主人导自己可见的全部。所以这条端点**不新增任何存储方法**，
      per-conversation 复用现有的 ``list_*`` 与 ``get_conversation_summary``；
    - **归档的会话也导**（归档不是删除）；
    - **流式**：逐条会话现读现发，客户端可以边收边写（本机导入器就是这么做的），
      内存上界是一条会话而不是一页；
    - 本机档**也挂着**这条端点（``local_router`` include 了同一个 router）：
      导自己的本机会话，格式与契约一致。两个档位一份实现。

    ``limit`` / ``offset`` / ``since`` 的语义写在各自的参数说明里；分页游标是
    "扫过的会话数"，不是"这一页返回了几条"（``since`` 会让两者不同，所以末行的
    footer 报的是实际发出去的数）。
    """
    return StreamingResponse(
        # 生成器在请求处理返回**之后**才被消费（Starlette 在线程池里迭代同步生成器），
        # 所以这里不能省掉 `defer` 之外的任何准备：会抛的校验都发生在返回响应之前
        # （`visible` 的第一次查询也在首次迭代时才跑——那时库里读的是只读快照，无锁）
        services.conversation_export.stream(
            owner_id=_caller_owner(caller), limit=limit, offset=offset, since=since
        ),
        media_type=MEDIA_TYPE,
    )


@router.post(
    "",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
def create_conversation(
    payload: ConversationCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> ConversationOut:
    """新建会话。

    标题允许留空：真正的标题由**第一轮提问**生成（见 ``ConversationService``）。
    这里能传标题是为了"复制一次旧会话"这类将来可能有的用法。
    """
    kb_ids = list(payload.kb_ids)
    if payload.workspace_id is not None:
        # 校验可见性（越权 404），并在调用方没指定库时**继承工作区的库**：
        # 这就是"知识库与 Agent 天生融合"落到行为上的样子——进入项目，
        # 资料范围就定了（见 docs/设计/Agent-工作区与能力层设计-v0.1.md §5）。
        # **设备闸也在这里过**（v0.59）：会话不按设备隔离，但"挂进哪个项目"
        # 是工作区的事——把会话挂进另一台机器的项目，与挂进别人的项目同一类越界
        workspace = services.workspaces.get(
            payload.workspace_id,
            user_id=_caller_owner(caller),
            device_id=request_device.id if request_device is not None else None,
        )
        if not kb_ids:
            kb_ids = list(workspace.kb_ids)
    record = services.conversations.create(
        kb_ids=kb_ids,
        title=payload.title,
        owner_id=_caller_owner(caller),
        model_pk=payload.model_pk,
        thinking=payload.thinking,
        thinking_effort=payload.thinking_effort,
        workspace_id=payload.workspace_id,
    )
    return _summary(services, record)


@router.get("/{conversation_id}", response_model=ConversationDetailOut, summary="会话详情")
def get_conversation(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ConversationDetailOut:
    """会话 + 全部消息。

    一次给全而不是分页：一次对话通常几十轮，比"翻页找上文"的体验好得多；
    真到了几百轮再谈分页。
    """
    record = _get_visible(services, caller, conversation_id)
    records = services.conversations.messages(conversation_id)
    # 老会话的"哪段推理属于哪一步"（v0.54）：这一版之前落库的步骤里没有 `thinking`
    # （那时整轮只存一串，回看时思考仍是一团）。用户在回看时**正是要点开它们**，
    # 所以读的时候按事件日志现算一次补上——规则与写侧同一份
    # （`session_events.steps_from_events` 用同一个 `StepThinking`）。
    #
    # 只有确实缺这个键的会话才去读日志：新会话每步自带推理，白读一遍是多余的一次查询。
    # 条数对不上（`fill_missing_thinking` 的判据）时什么都不补——把推理挂到错的那一轮
    # 比不补更难发现。
    projected = (
        steps_per_turn(services.conversations.session_events(conversation_id))
        if any(
            item.role == "assistant" and item.steps and not any("thinking" in s for s in item.steps)
            for item in records
        )
        else []
    )
    assistant_index = 0
    messages: list[ChatMessageOut] = []
    for item in records:
        steps = [dict(step) for step in item.steps]
        if item.role == "assistant" and steps:
            if assistant_index < len(projected):
                steps = fill_missing_thinking(steps, projected[assistant_index])
            assistant_index += 1
        messages.append(
            ChatMessageOut(
                id=item.id,
                role=item.role,
                content=item.content,
                sources=[ChatSourceOut.model_validate(src) for src in item.sources],
                steps=steps,
                thinking=item.thinking,
                # 随发的附件快照（v0.55）：老消息是 '[]'，返回空列表
                attachments=[ChatAttachmentOut.model_validate(f) for f in item.attachments],
                created_at=item.created_at,
            )
        )
    return ConversationDetailOut(**_summary(services, record).model_dump(), messages=messages)


@router.patch(
    "/{conversation_id}",
    response_model=ConversationOut,
    summary="修改会话（标题 / 置顶）",
)
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> ConversationOut:
    """标题 / 置顶 / 归档 / 归属都可选，只处理传了的那些；都为空时幂等。

    **归属用 ``model_fields_set`` 判断是否传了**，不能只看 ``is not None``：
    "退回未归档"要传 ``workspace_id: null``，而那与"这个字段没传"在值上完全一样。
    Pydantic v2 的 ``model_fields_set`` 正好区分这两者，比自定义哨兵干净。

    ``workspace_id`` 带上设备（v0.59）：挂进的那条工作区必须在**这台设备**
    （或不带设备头的服务器端）里看得见，否则与挂进别人的项目一样回 404。
    退回未归档（``null``）不看设备——那是取消归属，与"属于哪台机器"无关。
    """
    _get_visible(services, caller, conversation_id)
    record = services.conversations.get(conversation_id)
    if payload.title is not None:
        record = services.conversations.rename(conversation_id, payload.title)
    if payload.pinned is not None:
        record = services.conversations.set_pinned(conversation_id, payload.pinned)
    if payload.archived is not None:
        record = services.conversations.set_archived(conversation_id, payload.archived)
    if "workspace_id" in payload.model_fields_set:
        services.workspaces.bind_conversation(
            conversation_id,
            payload.workspace_id,
            user_id=_caller_owner(caller),
            device_id=request_device.id if request_device is not None else None,
        )
        record = services.conversations.get(conversation_id)
    return _summary(services, record)


@router.post(
    "/{conversation_id}/rewind",
    response_model=ConversationRewindOut,
    summary="回退最近 N 轮问答（「重新生成」用）",
)
def rewind_conversation(
    conversation_id: str,
    payload: ConversationRewindIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ConversationRewindOut:
    """删掉最近 ``turns`` 轮（提问 + 回答），返回被删掉的那句提问。

    **不在这里重新生成**：回答是流式产出的，重发必须走 `/chat/stream`——
    在这里再调一次模型会让"怎么重试、怎么中断"出现第二条实现。
    前端拿到 ``query`` 后原样重发一次即可。
    """
    _get_visible(services, caller, conversation_id)
    before = services.conversations.message_count(conversation_id)
    query = services.conversations.rewind(conversation_id, turns=payload.turns)
    after = services.conversations.message_count(conversation_id)
    return ConversationRewindOut(query=query, removed=max(0, before - after))


@router.post(
    "/{conversation_id}/branch",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="从第 N 轮分叉出一条新会话（「从这里重开」）",
)
def branch_conversation(
    conversation_id: str,
    payload: ConversationBranchIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ConversationOut:
    """把到第 ``payload.turn`` 轮为止的历史复制进一条**新会话**；原会话一个字节不动。

    与 ``rewind`` 的分工：那个是"删掉尾巴、把那句提问还给界面重发"（**改原会话**），
    这个是"另起一条"（**原会话不动**）。用户不敢乱试的正是后者——一改就回不去了。

    带走消息（含出处 / 步骤 / 思考快照）与它的事件日志，带走知识库范围 / 模型 /
    思考偏好 / 工作区与**归属**；**不带走文件区**（产物记录与对象存储里的字节都不搬），
    所以消息上的附件快照也不抄——那份 key 指向源会话的记账，抄过去点开必然 404。
    这条边界写进《API 接口规范》§1.9。
    """
    _get_visible(services, caller, conversation_id)
    record = services.conversations.branch(conversation_id, turn=payload.turn)
    return _summary(services, record)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（连同全部消息）",
)
def delete_conversation(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    _get_visible(services, caller, conversation_id)
    # **先清临时产物，再删会话**：清的时候要知道这条会话有哪些产物（记录随会话一起
    # 删掉之后就查不到了）。工作区里的那些一份都不动——它们在用户的目录里。
    services.artifacts.discard_for_conversation(conversation_id)
    services.conversations.delete(conversation_id)


# ---------------------------------------------------------------- 会话产物（v0.26）


def _get_artifact(services: Services, caller: Caller, conversation_id: str, artifact_id: str):  # type: ignore[no-untyped-def]
    """取一份产物，**先确认它属于这条会话**。

    不确认的话，``/conversations/A/artifacts/B`` 能拿到别的会话里的 B——
    权限判定（``_get_visible``）过的是 A，而返回的是 B 的内容。
    """
    _get_visible(services, caller, conversation_id)
    record = services.artifacts.get(artifact_id)
    if record.conversation_id != conversation_id:
        # 404 而不是 403：不暴露"这份产物在别处存在"
        raise NotFoundError(f"产物不存在：{artifact_id}")
    return record


@router.get(
    "/{conversation_id}/artifacts",
    response_model=ConversationArtifactListOut,
    summary="这条会话产出的文件",
)
def list_artifacts(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ConversationArtifactListOut:
    """界面上那几张文件卡片的**当前状态**。

    步骤里那份快照是流式当时的样子（"刚导出"），这里的是现在的样子（可能已经入库）。
    回看历史会话时以这一份为准，否则刷新一下卡片就退回"未入库"了。
    """
    _get_visible(services, caller, conversation_id)
    return ConversationArtifactListOut(
        items=[
            ConversationArtifactOut.model_validate(services.artifacts.describe(record))
            for record in services.artifacts.list_for_conversation(conversation_id)
        ]
    )


@router.get(
    "/{conversation_id}/files",
    response_model=FileListingOut,
    summary="这条会话的文件区（会话文件 / 项目目录）",
)
def list_files(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    path: str = Query(default="", description="要列的那一层子目录（两档都认）"),
    scope: str = Query(
        default=ARTIFACT_SCOPE_CONVERSATION,
        description=(
            "conversation（默认）= 这条会话的文件（上传 + 产出，可进子目录）；"
            "project = 会话挂着的项目目录（可进子目录）"
        ),
    ),
) -> FileListingOut:
    """文件面板的内容。``scope`` 两档（v0.55）：

    - ``conversation``：**这条会话的文件**——上传的与产出的都在这儿。上传一律落这一档
      并按会话记账，所以同一项目下不同会话的文件**分得开**（改之前挂了工作区就把上传
      写进项目目录，于是整个项目共用一个池子）。``path`` 从 D20 起也认：上传文件夹时
      名字里带着相对路径（``图表/第二季度.png``），这一档因此与项目档一样能进子目录；
    - ``project``：会话挂着的**项目目录**（能进子目录）——那是用户自己的项目文件，
      只有挂了工作区才有这一档，没挂时服务层会明确说清。

    哪一份落在哪儿由服务层算，界面不需要知道（``ArtifactService``）。
    """
    _get_visible(services, caller, conversation_id)
    listing = services.artifacts.list_files(conversation_id, path, scope=scope)
    return FileListingOut(
        mode=listing.mode,
        label=listing.label,
        path=listing.path,
        parent=listing.parent,
        truncated=listing.truncated,
        entries=[
            FileEntryOut(
                key=item.key,
                name=item.name,
                is_dir=item.is_dir,
                size_bytes=item.size_bytes,
                modified_at=item.modified_at,
                kind=item.kind,
            )
            for item in listing.entries
        ],
    )


@router.post(
    "/{conversation_id}/files",
    response_model=FileEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="往文件区里放一份文件",
)
async def upload_file(
    conversation_id: str,
    file: Annotated[UploadFile, File(...)],
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    path: str = Query(default="", description="旧参数，已不用（上传恒落会话文件区，平铺）"),
) -> FileEntryOut:
    """界面上的"上传"。

    **一律落这条会话的文件区**（v0.55）：不再写进项目目录——那条路会让同一项目下
    所有会话共用一堆文件，且"这份是谁传的"没有记录（用户报的"上传的文件分不开"）。
    文件名**可以带相对路径**（``图表/a.png``）：上传文件夹时用它保留目录结构。

    ``path`` 是旧接口留下的参数，收下但不用（见服务层说明）。
    """
    _get_visible(services, caller, conversation_id)
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise PayloadTooLargeError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    entry = services.artifacts.write_file(
        conversation_id,
        path=path,
        filename=file.filename or "未命名",
        content=content,
    )
    return FileEntryOut(
        key=entry.key,
        name=entry.name,
        is_dir=entry.is_dir,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
        kind=entry.kind,
    )


@router.post(
    "/{conversation_id}/files/import",
    response_model=FileEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="把项目目录里的一份文件取进本会话",
)
def import_project_file(
    conversation_id: str,
    payload: ConversationFileImportIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> FileEntryOut:
    """「取进本会话」（D20）：把**这条会话自己的工作区**里的一份文件复制进文件区。

    与「加入知识库」是两个目的地，别混：这一步进的是**这条会话的文件区**
    （别的会话看不到、删会话一起清），进知识库那条走 ``artifacts/…/ingest``。
    源路径只走工作区那道闸（绝对路径 / ``..`` / 符号链接出界都拒）；
    返回的是**会话文件区里的那一行**（key 是新的产物 id），界面据此说清"现在它在会话里"。
    """
    _get_visible(services, caller, conversation_id)
    entry = services.artifacts.import_from_project(conversation_id, payload.path)
    return FileEntryOut(
        key=entry.key,
        name=entry.name,
        is_dir=entry.is_dir,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
        kind=entry.kind,
    )


@router.get(
    "/{conversation_id}/files/download-url",
    response_model=FileDownloadUrlOut,
    summary="签发文件链接（预览 / 下载共用）",
)
def file_download_url(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    settings: Annotated[Settings, Depends(get_settings)],
    caller: Annotated[Caller, Depends(require_read)],
    key: str = Query(..., description="文件区里的 key（工作区是相对路径，临时区是产物 id）"),
    disposition: str = Query(
        default="attachment",
        pattern="^(attachment|inline)$",
        description="inline 供页面内预览（PDF / 图片 / Office）；其余类型服务端强制 attachment",
    ),
) -> FileDownloadUrlOut:
    """签发一条短期链接。**与文档下载同一套签名**，理由也一样：预览与下载按钮带不了头。"""
    # 取一次内容只为确认"这份文件真的读得到"：读不到就别签发一条注定 404 的链接
    _, name = services.artifacts.read_file(conversation_id, key)
    secret = signing_secret_or_raise(settings, services)
    url, expires_at = services.artifacts.file_url(
        conversation_id, key, secret=secret, inline=disposition == "inline"
    )
    return FileDownloadUrlOut(url=url, expires_at=expires_at, name=name)


#: 可以 ``inline`` 呈现的种类。**按后缀判，不按上传方声明的类型判**——
#: 后者是用户可以随便写的，拿它当开关等于让上传者决定"能不能在我们站点的
#: origin 下渲染它"（一份 SVG 能带 ``<script>``，那就是存储型 XSS）。
INLINE_SAFE_KINDS = frozenset({"pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp", "avif"})


@router.get(
    "/{conversation_id}/files/content",
    summary="按签名取文件内容（预览 / 下载共用）",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "文件内容"}},
)
def download_file_content(
    conversation_id: str,
    services: Annotated[Services, Depends(get_services)],
    settings: Annotated[Settings, Depends(get_settings)],
    key: str = Query(..., description="文件区里的 key"),
    expires: int = Query(..., description="签发时给出的到期时间戳"),
    signature: str = Query(..., description="签发时给出的签名"),
    disposition: str = Query(
        default="attachment",
        pattern="^(attachment|inline)$",
        description="inline 供页面内预览；只有白名单里的类型才会真的内联",
    ),
) -> Response:
    """**刻意不挂鉴权依赖**：这个 URL 要能直接在浏览器里打开（``<iframe>`` / ``<img>``）。

    它的授权凭据是 URL 里的签名，而签名绑定了"哪条会话、哪份文件、什么时候过期"——
    比一个长期令牌更窄。缺了签名或签名对不上都取不到内容。
    """
    secret = signing_secret_or_raise(settings, services)
    try:
        verify_resource(
            file_signature_resource(conversation_id, key),
            signature,
            expires,
            secret,
        )
    except SigningError as exc:
        raise UnauthorizedError(f"文件链接无效：{exc}") from exc

    content, name = services.artifacts.read_file(conversation_id, key)
    kind = split_filename(name)[1]
    inline = disposition == "inline" and kind in INLINE_SAFE_KINDS
    return Response(
        content=content,
        media_type=media_type_of(name, None),
        headers={
            "Content-Disposition": content_disposition(
                name, disposition="inline" if inline else "attachment"
            ),
            # 内容类型是按后缀推的，不让浏览器再嗅探一遍（与文档下载同一条）
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(content)),
        },
    )


@router.post(
    "/{conversation_id}/artifacts/{artifact_id}/ingest",
    response_model=ConversationArtifactOut,
    summary="把一份产物存进知识库（显式动作）",
)
def ingest_artifact(
    conversation_id: str,
    artifact_id: str,
    payload: IngestArtifactIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ConversationArtifactOut:
    """用户点了卡片上那个「存进知识库」时走的路径。

    与模型那把 ``ingest_artifact`` 工具同一个服务方法——**两条入口，一个动作**：
    分开实现的话，"点按钮入的库"与"跟它说一句入的库"迟早会有两套行为。
    """
    record = _get_artifact(services, caller, conversation_id, artifact_id)
    # 入库要**写**权限，且落在这个库的范围内（与工具那条入口同一句判定）
    services.kb.api_keys.check_access(caller, need=WRITE, kb_ids=[payload.knowledge_base_id])
    services.artifacts.ingest(
        record,
        knowledge_base_id=payload.knowledge_base_id,
        uploaded_by=caller.user.id if caller.user is not None else None,
    )
    return ConversationArtifactOut.model_validate(services.artifacts.describe(record))
