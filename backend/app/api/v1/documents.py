"""文档端点：上传、列表、详情、子文件树、重跑（M4）。

上传接口的处理顺序是有讲究的：**先落盘建记录（uploaded）→ 再入队**。
反过来会出现在"文件还没存好"时任务已被 worker 领走的竞态。
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.auth import (
    READ,
    WRITE,
    check_kb_scope,
    require_read,
    require_write,
    signing_secret,
    signing_secret_or_raise,
)
from app.api.v1.schemas import (
    ChunkList,
    ChunkOut,
    DocumentBatchIn,
    DocumentBatchItemOut,
    DocumentBatchOut,
    DocumentDisabledIn,
    DocumentList,
    DocumentOut,
    DocumentPartList,
    DocumentPartOut,
    DocumentProgressOut,
    DocumentRenameIn,
    DocumentTimelineOut,
    UploadAccepted,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import PayloadTooLargeError, UnauthorizedError
from app.core.services import KbServices, get_kb_services
from app.core.signing import SigningError, verify_resource
from app.models.enums import ApiKeyPermission, DataSourceKind, DocumentStage
from app.services.api_key import Caller
from app.services.documents import content_kind, signature_resource
from app.services.idempotency import fingerprint
from app.services.ingest import content_disposition, normalize_filename

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
"""单文件上限 200MB：与 MinerU 云端解析的单文件上限对齐（架构 §15），
超限要在入口挡住，而不是等跑到云端才失败。"""

MAX_CHUNK_PREVIEW = 200
"""单次最多返回多少块：详情页只预览开头几段，但接口不能没有上限。"""


def _to_out(
    record,  # type: ignore[no-untyped-def]
    *,
    chunk_count: int = 0,
    uploader: str = "",
    question_stats: tuple[int, int] = (0, 0),
    questions_pending: bool = False,
    progress=None,  # type: ignore[no-untyped-def]
) -> DocumentOut:
    """记录 → 响应模型。用 ``model_validate`` 而不是手抄字段：

    协议层不该 import ``app.storage`` 的记录类型（工程规范 §3.3 L1），
    字段名对不上时 pydantic 会直接报错，不用等到线上发现"某个字段忘了同步"。
    """
    out = DocumentOut.model_validate(record)
    questioned_chunks, question_total = question_stats
    return out.model_copy(
        update={
            "chunk_count": chunk_count,
            "uploaded_by_name": uploader,
            # 原件类型：界面据此决定首页先取「原文版式」还是「解析文本」
            "original_kind": content_kind(record.name, has_markdown=False),
            "question_count": question_total,
            "questioned_chunk_count": questioned_chunks,
            "questions_pending": questions_pending,
            # 分段进度（§12.115）。**必须在这里校验一次**：``model_copy(update=...)``
            # 不做校验，直接塞 dataclass 会得到"序列化出来的形状对、但 pydantic
            # 每次都警告 UnexpectedValue"的结果（用例的 warning 抓到的）
            "progress": DocumentProgressOut.model_validate(progress, from_attributes=True)
            if progress is not None
            else None,
        }
    )


def document_out(services: KbServices, record) -> DocumentOut:  # type: ignore[no-untyped-def]
    """单个文档的完整响应（切块数、上传者名字、出题情况都由后端补）。

    公开出来给别的路由复用（如"移动到目录"要回一份文档）——两处各拼一遍
    必然漂（一处忘了补 chunk_count，界面就少一列数字）。
    """
    counts = services.documents.chunk_counts([record.id])
    names = _uploader_names(services, [record])
    return _to_out(
        record,
        chunk_count=counts.get(record.id, 0),
        uploader=names.get(record.uploaded_by or "", ""),
        question_stats=services.documents.question_stats([record.id]).get(record.id, (0, 0)),
        questions_pending=record.id in services.documents.active_question_documents([record.id]),
        progress=services.documents.progress_by_documents([record]).get(record.id),
    )


def _uploader_names(services: KbServices, records) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """把一批文档的 ``uploaded_by`` 一次解析成名字（G6）。

    **批量解析而不是逐条查**：文档列表一页几十条，逐条查就是 N+1；
    而名册本身很小，整个读出来更划算。
    """
    wanted = {record.uploaded_by for record in records if record.uploaded_by}
    if not wanted:
        return {}
    return {item.id: item.name for item in services.users.list() if item.id in wanted}


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步摄入）",
)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    start: bool = Query(default=True, description="是否立即入队摄入；false 表示仅登记"),
    folder_id: str | None = Query(default=None, description="放进哪个目录（v13）；留空=根目录"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="可选。带上它则同一键的重试不会产生第二份文档（架构 §3.2）",
    ),
    operator_token: str | None = Header(
        default=None,
        alias="X-Kylab-Operator",
        description=(
            "可选。使用者名册里的 id（形如 user_xxx），记录是谁传的（G6）。"
            "不参与鉴权。**用 id 而不是名字**：HTTP 头只能是 ASCII，"
            "而名字可能是中文（实测浏览器与 curl 都会报编码错）"
        ),
    ),
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> UploadAccepted:
    """上传文档。

    **幂等键是可选的，而不是强制的**——这里与架构 §3.2 的字面要求有一处偏差，
    理由是我们已经有一条更强的兜底：内容 hash 去重。同一份文件重复上传，
    ``ingest.submit`` 会认出来并回 ``is_duplicate=True``，本来就不会入库两次。

    幂等键补的是 hash 覆盖不到的那一段：**同一个键配不同内容**时的判定，
    以及"客户端连自己上次传没传成功都不知道"的场景（回放上次的响应，
    而不是让它重新走一遍去重）。

    做成强制会立刻打断既有前端与所有集成方（401 之后又来一次全员 400），
    而收益只是把已有的保护换一种表达。所以：**提供则生效，不提供仍受 hash 去重保护**。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    # 归属标注（G6）：前端从名册里选一个人放进请求头。
    # **不参与鉴权**——伪造一个名字只会让归属记错，不会获得任何权限
    operator = services.users.resolve_operator(operator_token)
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        # 走领域异常而不是 HTTPException：后者会绕过统一错误信封，返回 {"detail": ...}
        raise PayloadTooLargeError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")

    filename = file.filename or "未命名"

    # 幂等键的处理必须在读文件之后：指纹要覆盖内容，否则"同一个键换文件"
    # 会被当成重放，直接回上次的 document_id——那是最坏的一种错（用户以为传了新的）
    request_hash = (
        fingerprint(kb_id, filename, start, hashlib.sha256(content).hexdigest())
        if idempotency_key
        else ""
    )
    if idempotency_key:
        claim = services.idempotency.begin(idempotency_key, request_hash)
        if claim.is_replay:
            logger.info("幂等键 %s 命中重放，不再重复入库", idempotency_key)
            return UploadAccepted.model_validate(claim.replay)

    try:
        response = _do_upload(
            services,
            kb_id=kb_id,
            filename=filename,
            content=content,
            mime_type=file.content_type,
            start=start,
            uploaded_by=operator.id if operator else None,
            uploader_name=operator.name if operator else "",
            folder_id=folder_id,
        )
    except Exception:
        # 业务没跑成：把键放掉，让客户端能真正重试。
        # 不放的话键留着而 response 为空，重试永远拿到"正在处理中"——
        # 比直接报错更糟，因为它看起来像"再等等就好"。
        if idempotency_key:
            services.idempotency.release(idempotency_key)
        raise

    if idempotency_key:
        # mode="json" 是必须的：`model_dump()` 会留下 datetime 对象，而幂等响应要落库
        # 成 JSON——实测这里会抛 "Object of type datetime is not JSON serializable"，
        # 而**抛出点在上传成功之后**，于是"文件已入库但接口 500"，客户端重试又被
        # 幂等层拦成 409。一个序列化细节能把成功路径变成不可重试的失败。
        services.idempotency.complete(idempotency_key, response.model_dump(mode="json"))

    return response


def _do_upload(
    services: KbServices,
    *,
    kb_id: str,
    filename: str,
    content: bytes,
    mime_type: str | None,
    start: bool,
    uploaded_by: str | None = None,
    uploader_name: str = "",
    folder_id: str | None = None,
) -> UploadAccepted:
    """真正的入库动作。抽出来是为了让幂等层的"占键 → 执行 → 挂响应"读起来是直的。"""
    outcome = services.ingest.submit(
        knowledge_base_id=kb_id,
        filename=filename,
        content=content,
        mime_type=mime_type,
        uploaded_by=uploaded_by,
        folder_id=folder_id,
    )
    if not start or outcome.is_duplicate:
        return UploadAccepted(
            document=_to_out(
                outcome.document,
                chunk_count=outcome.chunk_count,
                uploader=uploader_name,
            ),
            is_duplicate=outcome.is_duplicate,
            task_id=None,
        )

    task = services.documents.enqueue_ingest(outcome.document.id)
    return UploadAccepted(
        document=_to_out(outcome.document, uploader=uploader_name),
        is_duplicate=False,
        task_id=task.id,
    )


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentList,
    summary="知识库下的文档列表",
)
def list_documents(
    kb_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
    folder_id: str | None = Query(default=None, description="只看这个目录里的文档"),
    root: bool = Query(default=False, description="只看未归档（根目录）的文档"),
    q: str | None = Query(
        default=None, max_length=200, description="按文件名模糊搜（大小写不敏感）"
    ),
    stage: DocumentStage | None = Query(default=None, description="只保留这个流水线阶段"),
    source_kind: DataSourceKind | None = Query(default=None, description="只保留这个来源类型"),
    limit: int = Query(default=50, ge=1, le=200, description="这一页取几篇"),
    offset: int = Query(default=0, ge=0, description="从第几篇开始取（跳过多少篇）"),
) -> DocumentList:
    """知识库下的文档列表，支持目录 / 文件名 / 状态 / 来源四个维度的收窄。

    ``stage`` 与 ``source_kind`` 用枚举而不是裸字符串：传一个拼错的值时
    框架直接回 422，而不是被当成"合法但匹配不到"而静默返回空列表——
    后者会让用户以为"这个库真的没有失败文档"。

    **分页在 SQL 里做**（``limit``/``offset``），并另起一次 ``COUNT(*)`` 回 ``total``：
    一个库上万篇时，"把全量读出来再在 Python 里切页"会把响应体和耗时
    都随库大小放大。``total`` 与 ``items`` 用的是同一套过滤条件（同一个构造器）。
    """
    check_kb_scope(services, caller, [kb_id])
    records = services.documents.list_documents(
        kb_id,
        folder_id=folder_id,
        root_only=root,
        q=q,
        stage=stage.value if stage else None,
        source_kind=source_kind.value if source_kind else None,
        limit=limit,
        offset=offset,
    )
    counts = services.documents.chunk_counts([record.id for record in records])
    names = _uploader_names(services, records)
    ids = [record.id for record in records]
    question_stats = services.documents.question_stats(ids)
    questions_pending = services.documents.active_question_documents(ids)
    # 分段进度（§12.115）：整页一次算完——每行画进度条，逐篇查就是 40 次往返
    progress = services.documents.progress_by_documents(records)
    total = services.documents.count_documents(
        kb_id,
        folder_id=folder_id,
        root_only=root,
        q=q,
        stage=stage.value if stage else None,
        source_kind=source_kind.value if source_kind else None,
    )
    return DocumentList(
        items=[
            _to_out(
                record,
                chunk_count=counts.get(record.id, 0),
                uploader=names.get(record.uploaded_by or "", ""),
                question_stats=question_stats.get(record.id, (0, 0)),
                questions_pending=record.id in questions_pending,
                progress=progress.get(record.id),
            )
            for record in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/knowledge-bases/{kb_id}/documents/batch",
    response_model=DocumentBatchOut,
    summary="批量删除 / 重新摄入 / 移动 / 停用启用 / 生成问题",
)
def batch_documents(
    kb_id: str,
    payload: DocumentBatchIn,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> DocumentBatchOut:
    """对选中的一批文档执行同一个动作。

    **逐条返回成败**，接口本身不因个别失败而报错——批量操作里"10 篇删掉 9 篇"
    是正常结果，界面要能指出剩下那一篇为什么没成。请求里的 id 若不属于这个库，
    记为该条失败，不会被执行。
    """
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    items = services.batch.run(
        kb_id,
        payload.action,
        payload.document_ids,
        folder_id=payload.folder_id,
        all_documents=payload.all,
    )
    return DocumentBatchOut(
        action=payload.action,
        succeeded=sum(1 for item in items if item.ok),
        failed=sum(1 for item in items if not item.ok),
        items=[
            DocumentBatchItemOut(document_id=item.document_id, ok=item.ok, error=item.error)
            for item in items
        ],
    )


def _guard_document(
    services: KbServices, caller: Caller, document_id: str, *, need: ApiKeyPermission = READ
) -> object:
    """按文档归属的知识库做范围判定，**并把它已经读出来的那条文档返回**。

    这几个端点只拿到 ``document_id``，而密钥范围是绑在知识库上的，
    所以必须先把文档读出来、取出它属于哪个库再判。

    注意**先取文档再判范围**的顺序：反过来（先判后取）在文档不存在时
    会给出 403 而不是 404，等于告诉调用方"这个 id 在本机上存在但你无权看"——
    越权探测者最想要的就是这种区分。

    返回类型写成 ``object``：这条记录是 ``app.storage`` 的类型，而协议层不得
    依赖它（工程规范 §3.3 L1）。返回值只是**省掉一次多余的查询**，
    需要用的端点自己 ``model_validate`` 或交给服务层。
    """
    record = services.documents.get(document_id)
    check_kb_scope(services, caller, [record.knowledge_base_id], need=need)
    return record


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="文档详情")
def get_document(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
) -> DocumentOut:
    _guard_document(services, caller, document_id)
    record = services.documents.get(document_id)
    # 走共享的 document_out 而不是就地拼 _to_out：出题统计这类"后端补的派生字段"
    # 一处漏传就是"列表有、详情没有"的不一致（v24 实测踩到：详情一直显示 0 条问题）。
    return document_out(services, record)


@router.get(
    "/documents/{document_id}/timeline",
    response_model=DocumentTimelineOut,
    summary="处理进度时间线（共几步 / 现在第几步 / 每步耗时）",
)
def document_timeline(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
) -> DocumentTimelineOut:
    """给列表行的分段进度条与右侧抽屉的明细喂数据。

    **跑着时最后一步的耗时是"到现在为止"**，所以前端轮询时它会一直在长——
    这是"还在动"的证据，比一个转圈图标可信。

    ``stalled`` 走与列表同一个入口（``progress_by_documents``）拿：抽屉里那个
    "疑似卡住"与列表行上那个必须是同一个结论，两处各判一次迟早说不到一块儿去。
    """
    record = _guard_document(services, caller, document_id)
    timeline = services.documents.timeline(document_id)
    out = DocumentTimelineOut.model_validate(timeline, from_attributes=True)
    # 停滞只在"还在跑"时有意义：跑完的文档没有租约，标它等于误报
    out.stalled = services.documents.progress_by_documents([record])[document_id].stalled
    return out


@router.patch("/documents/{document_id}", response_model=DocumentOut, summary="重命名文档")
def rename_document(
    document_id: str,
    payload: DocumentRenameIn,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> DocumentOut:
    """改显示名。只读分享的成员改不了——那是 owner 的库。"""
    _guard_document(services, caller, document_id, need=WRITE)
    record = services.documents.rename(document_id, payload.name)
    return document_out(services, record)


@router.patch(
    "/documents/{document_id}/disabled",
    response_model=DocumentOut,
    summary="停用 / 恢复检索",
)
def set_document_disabled(
    document_id: str,
    payload: DocumentDisabledIn,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> DocumentOut:
    """停用后文档**不参与检索**（全文与向量两条通道都过滤），其余一切保留：

    原文、切块、向量、上传记录都在，恢复是零成本。与删除的区别是
    删除会把原文移入回收站并立即清掉切块与向量。
    """
    _guard_document(services, caller, document_id, need=WRITE)
    record = services.documents.set_disabled(document_id, payload.disabled)
    return document_out(services, record)


@router.post(
    "/documents/{document_id}/cancel",
    response_model=DocumentOut,
    summary="取消解析（叫停还在跑的摄入）",
)
def cancel_document(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> DocumentOut:
    """用户主动叫停。**不是删除**：已产出的东西留着，随时可以重新摄入。

    语义是协作式的（见 ``IngestService.ingest``）：正在云端跑的那一次请求没法
    中途掐断，但它返回后不会再往下推进。响应里回的已经是 ``canceled`` 态。
    """
    _guard_document(services, caller, document_id, need=WRITE)
    record = services.documents.cancel(document_id)
    return document_out(services, record)


@router.get(
    "/documents/{document_id}/parts",
    response_model=DocumentPartList,
    summary="子文件树（大文件切分）",
)
def list_document_parts(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
) -> DocumentPartList:
    _guard_document(services, caller, document_id)
    parts = services.documents.list_parts(document_id)
    return DocumentPartList(items=[DocumentPartOut.model_validate(part) for part in parts])


@router.get(
    "/documents/{document_id}/chunks",
    response_model=ChunkList,
    summary="切块列表（文档详情页的正文预览）",
)
def list_document_chunks(
    document_id: str,
    limit: int = Query(default=20, ge=1, le=MAX_CHUNK_PREVIEW, description="最多返回多少块"),
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
) -> ChunkList:
    """按 ``ordinal`` 升序返回切块。

    同时给出 ``total``：前端要能说清"这是前 5 块，共 137 块"，
    否则用户会把预览当成全文。
    """
    _guard_document(services, caller, document_id)
    chunks = services.documents.list_chunks(document_id, limit=limit)
    return ChunkList(
        items=[ChunkOut.model_validate(chunk) for chunk in chunks],
        total=services.documents.chunk_count(document_id),
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重新摄入（失败重跑）",
)
def reprocess_document(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_write),
) -> UploadAccepted:
    _guard_document(services, caller, document_id, need=WRITE)
    task = services.documents.enqueue_ingest(document_id, force=True)
    # **重新取一次**：force 会把阶段推回 CHUNKING（见 enqueue_ingest），
    # 拿入队前那份快照回给前端会显示成"还是 indexed"，用户会以为点了没反应
    document = services.documents.get(document_id)
    return UploadAccepted(
        document=document_out(services, document),
        is_duplicate=False,
        task_id=task.id,
    )


# --------------------------------------------------------------------- 下载与阅读（T4.5 / G2）


class DownloadUrlOut(BaseModel):
    """一条下载链接。**相对路径**：对外域名只有部署时才知道。"""

    url: str
    expires_at: int
    format: str


class PreviewOut(BaseModel):
    """「阅读」视角的内容。

    文本类直接内联返回（Markdown / 纯文本），非文本类只回一条签名 URL 让浏览器自己渲染
    （PDF、图片）。**不把二进制塞进 JSON**：那要 base64，体积涨三分之一，
    而且浏览器拿到 base64 还得再解回来才能渲染。
    """

    kind: str
    """``markdown`` / ``pdf`` / ``image`` / ``docx`` / ``pptx`` / ``excel`` / ``binary``。"""
    filename: str
    text: str | None = None
    url: str | None = None
    expires_at: int | None = None
    original_kind: str | None = None
    """原始文件的类型（按扩展名判断），**与本次返回的 kind 无关**。

    界面据此决定要不要给「原文版式 / 解析文本」这个切换：``binary`` 或 ``None``
    表示原件没有可渲染的版式（老式 .doc、压缩包等），就不给用户一个点开是空的入口。
    """


#: 需要一个签名链接、由浏览器或前端库自己画出来的类型。
_RENDERABLE_KINDS = frozenset({"pdf", "image", "docx", "pptx", "excel"})


@router.get(
    "/documents/{document_id}/preview",
    response_model=PreviewOut,
    summary="阅读视角（解析文本内联 / 原件版式给签名链接）",
)
def preview_document(
    document_id: str,
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
    source: str = Query(
        default="auto",
        pattern="^(auto|original)$",
        description="auto=有解析产物就给解析文本；original=强制看原件版式",
    ),
) -> PreviewOut:
    """文档的「阅读」视角，两种来源：

    - ``auto``（默认）：有解析产物就给归一化后的 Markdown，否则给原件版式；
    - ``original``：**不管有没有产物**都看原件（PDF / 图片 / Office 原版式）。

    与「切块预览」是两个视角、刻意并存：切块回答"解析成了什么"（调试用，
    等宽文本带块号），阅读回答"原文长什么样"（日常用，渲染件）。
    """
    _guard_document(services, caller, document_id)

    content = (
        services.documents.original_view(document_id)
        if source == "original"
        else services.documents.reading_view(document_id)
    )
    # 原件本身是什么类型：界面据此决定要不要给「原文版式 / 解析文本」这个切换
    original_kind = content_kind(services.documents.get(document_id).name, has_markdown=False)

    if content.kind == "markdown":
        return PreviewOut(
            kind="markdown",
            filename=content.filename,
            text=content.data.decode("utf-8", errors="replace"),
            original_kind=original_kind,
        )

    if content.kind in _RENDERABLE_KINDS:
        secret = signing_secret(get_settings(), services)
        if not secret:
            # 没有签名密钥时发不了链接（同 download-url 的说明）。但这里**不报错**：
            # 回一个 binary 让前端退化成"只能下载"，比整个阅读面板报红字好
            return PreviewOut(kind="binary", filename=content.filename, original_kind=original_kind)
        # inline：这些内容要**在页面里直接渲染**（PDF 进 iframe、图片进 img、
        # Office 由前端 fetch 后自己画）。带 attachment 的话浏览器会把 iframe
        # 里的 PDF 变成下载——那正是"点开文档就下载"的原因。
        url, expires_at = services.documents.download_url(
            document_id, fmt="original", secret=secret, disposition="inline"
        )
        return PreviewOut(
            kind=content.kind,
            filename=content.filename,
            url=url,
            expires_at=expires_at,
            original_kind=original_kind,
        )

    return PreviewOut(kind="binary", filename=content.filename, original_kind=original_kind)


@router.get(
    "/documents/{document_id}/download-url",
    response_model=DownloadUrlOut,
    summary="签发下载链接（带过期时间）",
)
def document_download_url(
    document_id: str,
    fmt: str = Query(
        default="original",
        alias="format",
        pattern="^(original|markdown)$",
        description="original=原文件（默认），markdown=解析产物",
    ),
    services: KbServices = Depends(get_kb_services),
    caller: Caller = Depends(require_read),
) -> DownloadUrlOut:
    """签发一条短期下载链接。

    **需要鉴权**：拿链接要带凭据，链接本身则可以在浏览器里直接打开（无需头）。
    这正是签名 URL 想解决的矛盾——``<img>`` 与下载按钮带不了 Authorization 头。
    """
    _guard_document(services, caller, document_id)

    secret = signing_secret_or_raise(get_settings(), services)
    url, expires_at = services.documents.download_url(document_id, fmt=fmt, secret=secret)
    return DownloadUrlOut(url=url, expires_at=expires_at, format=fmt)


#: 可以 ``inline`` 呈现的种类。判定用 ``content_kind``（**按文件名后缀**得出，
#: 服务端自己算的），而不是数据库里那个 mime——上传时声明的 mime 是客户端给的，
#: 有的上传器干脆留空（存成 ``application/octet-stream``），拿它当开关会让这些
#: 文件白白退化成下载。
INLINE_KINDS = frozenset({"pdf", "image", "docx", "pptx", "excel"})

#: 即使种类允许也**必须**走 attachment 的后缀。按后缀判而不是按媒体类型判：
#: 媒体类型可能来自上传方声明（不可靠，甚至可以声明成 image/png 来蒙混），
#: 而后缀是唯一稳定的身份。
#:
#: 这一条是**安全边界**，不是显示偏好：``inline`` 意味着浏览器按返回的
#: Content-Type 在本站 origin 下渲染它——一份上传的 SVG 能带 ``<script>``，
#: 内联渲染就等于在**我们的 origin 下执行上传者的脚本**（存储型 XSS）。
#: 其余非 PDF/位图的类型（HTML、XML…）根本不在 :data:`INLINE_KINDS` 里，够不着这条路径。
INLINE_BLOCKED_SUFFIXES = (".svg", ".svgz")


@router.get(
    "/documents/{document_id}/content",
    summary="按签名取内容（下载 / 页面内渲染）",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "文件内容"}},
)
def download_document_content(
    document_id: str,
    request: Request,
    fmt: str = Query(default="original", alias="format", pattern="^(original|markdown)$"),
    expires: int = Query(..., description="签发时给出的到期时间戳"),
    signature: str = Query(..., description="签发时给出的签名"),
    disposition: str = Query(
        default="attachment",
        pattern="^(attachment|inline)$",
        description="inline 供页面内直接渲染（PDF / 位图）；其余类型服务端强制 attachment",
    ),
    services: KbServices = Depends(get_kb_services),
    settings: Settings = Depends(get_settings),
) -> Response:
    """签名内容端点（下载 / 页面内渲染共用）。

    **刻意不挂 ``require_read`` 依赖**：这个 URL 要能直接在浏览器里打开
    （图片标签、下载按钮都带不了自定义头）。它的授权凭据是 URL 里的签名，
    而签名已经绑定了"哪个文档、哪种格式、什么时候过期"——比一个长期令牌更窄。

    用 ``Response`` 而不是 ``FileResponse``：内容是从对象存储读进内存的字节，
    没有磁盘路径可给。``FileResponse`` 只接受路径，传 BytesIO 会在
    ``os.stat`` 上抛 TypeError（实测踩到）。

    **``disposition=inline`` 必须由服务端按媒体类型复核**：它不在签名里，
    调用方可以自己改这个参数，所以它绝不能成为"能不能在内联渲染"的开关——
    真正的开关是 :data:`_INLINE_SAFE_MEDIA_TYPES`。这正是 iframe 里预览 PDF
    必须的：带着 ``attachment`` 的响应在 iframe 里会被浏览器**变成下载**
    （踩过：详情页一打开就下载 PDF）。
    """
    secret = signing_secret_or_raise(settings, services)

    try:
        verify_resource(signature_resource(document_id, fmt), signature, expires, secret)
    except SigningError as exc:
        # 401：链接无效或过期，正确动作是重新签发一条
        raise UnauthorizedError(f"下载链接无效：{exc}") from exc

    content = services.documents.content(document_id, fmt=fmt)
    filename = normalize_filename(content.filename)
    # inline 只对**原件**有意义。种类的判定必须用 `has_markdown=False`：
    # `content()` 对"有解析产物的原件"会返回 kind=markdown（它按"给用户看什么"判），
    # 拿它当开关会让解析过的 PDF 全部拿不到 inline —— 而"解析过的 PDF"恰恰是预览的常态。
    kind = content_kind(content.filename, has_markdown=False) if fmt == "original" else ""
    inline = (
        disposition == "inline"
        and kind in INLINE_KINDS
        and not content.filename.lower().endswith(INLINE_BLOCKED_SUFFIXES)
    )
    headers = {
        "Content-Disposition": content_disposition(
            filename, disposition="inline" if inline else "attachment"
        ),
        # 内联渲染时禁止浏览器嗅探内容类型：上传时声明的 mime 是用户可控的，
        # 没有 nosniff 就能把一个"声明成图片"的 HTML 嗅探成网页并执行脚本
        "X-Content-Type-Options": "nosniff",
        # 告诉浏览器这个端点支持分段取。**PDF 内联预览必须要它**：
        # Chrome 的 PDF 查看器按 Range 渐进加载，服务端只回整包（200、无 Accept-Ranges）时
        # 它会退化成"下载这个文件"——表现为点开 PDF 就下载（实测踩到）。
        "Accept-Ranges": "bytes",
    }

    total = len(content.data)
    range_header = request.headers.get("range")
    if range_header is not None:
        span = _parse_range(range_header, total)
        if span is None:
            # 416：Range 不合法或越界。**不能**默默回整包——那会让客户端把 200 当成
            # "服务端不支持分段"，进而放弃内联渲染
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
            )
        start, end = span
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return Response(
            content=content.data[start : end + 1],
            status_code=206,
            media_type=content.media_type,
            headers=headers,
        )

    return Response(content=content.data, media_type=content.media_type, headers=headers)


def _parse_range(value: str, total: int) -> tuple[int, int] | None:
    """解析单段 ``Range``，返回闭区间 ``(start, end)``；不合法返回 ``None``。

    只支持单段：多段（``bytes=0-1,5-6``）要发 multipart/byteranges，浏览器看 PDF
    从来不用它，支持它是纯负担。三种写法都要认，PDF 查看器三种都会用到：
    ``bytes=0-1023``（定长）、``bytes=1000-``（到结尾）、``bytes=-500``（尾部 N 字节）。
    """
    if not value.startswith("bytes=") or "," in value:
        return None
    start_text, _, end_text = value[len("bytes=") :].strip().partition("-")
    try:
        if not start_text:
            # 后缀写法：最后 N 字节
            length = int(end_text)
            if length <= 0:
                return None
            return max(0, total - length), total - 1
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
    except ValueError:
        return None
    if start > end or start >= total:
        return None
    return start, min(end, total - 1)
