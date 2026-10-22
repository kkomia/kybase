"""知识库提供者握手：``GET /provider/handshake``。

窄 API 就是既有的知识库 REST（检索 / 入库 / 进度 / 库管理）；握手只补充
三个既有端点没有的信息：连通性、能力集与当前 Key 可见的库清单。

握手必须鉴权：401/403（凭据或权限问题）与连不上（地址问题）是两种不同的结论。
协议版本是整数且只增；客户端遇到不认识的版本应判为不可用，不要硬试。
能力集只在本模块定义，上限值从真实契约读取，不在响应拼装里重复写死。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_read
from app.api.v1.documents import MAX_UPLOAD_BYTES
from app.api.v1.knowledge_bases import kb_access_flags, visible_knowledge_bases
from app.api.v1.schemas import (
    ProviderCallerOut,
    ProviderCapabilitiesOut,
    ProviderEmbeddingCapsOut,
    ProviderHandshakeOut,
    ProviderIngestCapsOut,
    ProviderKbBriefOut,
    ProviderKbCapsOut,
    ProviderRetrievalCapsOut,
    ProviderTrackingCapsOut,
    SearchRequest,
)
from app.core.config import API_VERSION, get_settings
from app.core.services import Services, get_services
from app.models.enums import ApiKeyPermission
from app.services.api_key import Caller
from app.services.retrieval import RetrievalMode

router = APIRouter(prefix="/provider", tags=["provider"])

# ---------------------------------------------------------------- 能力集常量

PROVIDER_NAME = "knowledge"
"""提供者种类。窄 API 今天只服务一种提供者：知识库。"""

PROTOCOL_VERSION = 1
"""握手协议版本（**整数、只增**，见模块头的裁量 3）。"""

INGEST_TRANSPORT = "multipart"
"""上传的编码：与 ``documents.upload_document`` 的 ``UploadFile`` 一致。"""

INGEST_DEDUP = "content_hash"
"""去重口径：内容 hash（``services/ingest.content_key``）。"""

INGEST_IS_ASYNC = True
"""上传即回 202 + ``document_id``，解析 / 切分 / 向量化在任务队列里跑。"""

INGEST_EXTENSIONS: tuple[str, ...] = (
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "csv",
    "md",
    "txt",
    "html",
    "png",
    "jpg",
)
"""界面要提示的格式（不带点号）——**今后唯一的格式口径**（见模块头）。

改这里就是改口径：前端从握手里读它，不再各写一份。
"""

RETRIEVAL_MODES: tuple[str, ...] = tuple(RetrievalMode.ALL)
"""检索模式取自检索服务自己的枚举（``hybrid`` / ``vector`` / ``fulltext``）。"""

RETRIEVAL_DEFAULT_MODE = RetrievalMode.HYBRID
"""不指定 ``mode`` 时的默认档（``SearchRequest.mode`` 的默认值也是它）。"""

RETRIEVAL_RERANK = True
"""支持重排（``SearchRequest.rerank``）。"""

RETRIEVAL_FILTERS = True
"""支持元数据过滤（``SearchRequest.filters``）。"""

TRACKING_DOCUMENT = True
"""支持查单个文档的状态（``GET /documents/{id}``）。"""

TRACKING_TIMELINE = True
"""支持查处理时间线（``GET /documents/{id}/timeline``）。"""

KB_MANAGEMENT_CAPS: dict[str, bool] = {
    "create": True,
    "delete": True,
    "folders": True,
    "shares": True,
    "wiki": True,
}
"""库管理那一族（建 / 删 / 目录 / 分享 / Wiki）：端点都在，页面可以直接使用。
这一位表示"提供者会做"，不表示调用方一定拥有该能力。"""


def _upper_bound(model: type[BaseModel], field: str) -> int:
    """从请求模型的 ``Field(le=...)`` 里读出上界（**不手抄数字**）。

    能力集里的 ``top_k_max`` / ``candidate_k_max`` 就是 ``/search`` 请求体的上限：
    在这里另写一个 ``100`` 必然与它漂（改一处忘一处），而界面会照着这个数做校验。
    """
    for bound in model.model_fields[field].metadata:
        limit = getattr(bound, "le", None)
        if limit is not None:
            return int(limit)
    raise RuntimeError(f"{model.__name__}.{field} 没有上界，握手的能力集取不到这个值")


RETRIEVAL_TOP_K_MAX = _upper_bound(SearchRequest, "top_k")
RETRIEVAL_CANDIDATE_K_MAX = _upper_bound(SearchRequest, "candidate_k")


# ------------------------------------------------------------------ 响应拼装


def provider_capabilities(services: Services) -> ProviderCapabilitiesOut:
    """能力集：**如实报这台机器现在能做什么**，不是"代码里支持什么"。

    两个例外值得记住：``embedding.configured`` 为假时仍然能上传（登记照旧），
    只是检索拿不到向量通道；``is_development`` 为真时检索结果不代表真实效果——
    客户端要把这两件事如实显示，而不是当成正常档。
    """
    embedding = services.runtime.embedding()
    return ProviderCapabilitiesOut(
        retrieval=ProviderRetrievalCapsOut(
            modes=list(RETRIEVAL_MODES),
            default_mode=RETRIEVAL_DEFAULT_MODE,
            rerank=RETRIEVAL_RERANK,
            filters=RETRIEVAL_FILTERS,
            top_k_max=RETRIEVAL_TOP_K_MAX,
            candidate_k_max=RETRIEVAL_CANDIDATE_K_MAX,
        ),
        ingest=ProviderIngestCapsOut(
            transport=INGEST_TRANSPORT,
            async_=INGEST_IS_ASYNC,
            dedup=INGEST_DEDUP,
            # **上传端点自己的常量**，不是"同样取 200MB"
            max_bytes=MAX_UPLOAD_BYTES,
            extensions=list(INGEST_EXTENSIONS),
        ),
        tracking=ProviderTrackingCapsOut(
            document=TRACKING_DOCUMENT,
            timeline=TRACKING_TIMELINE,
        ),
        knowledge_bases=ProviderKbCapsOut(**KB_MANAGEMENT_CAPS),
        embedding=ProviderEmbeddingCapsOut(
            configured=embedding.is_configured,
            # 与 ``/search`` 同一读法：配置从运行期设置取，模型身份从当前嵌入器取
            is_development=services.embedder.is_development,
            model_id=services.embedder.model_id,
            dim=services.embedder.dim,
        ),
    )


def caller_brief(caller: Caller) -> ProviderCallerOut:
    """这把凭据**在提供者这一侧**是什么样（便于核对）。

    ``permission`` 在不受库范围限制的那两档下是 ``None``（管理员会话与登录成员
    都没有密钥记录），那两档**都能写**，所以报 ``readwrite``——报一个空值只会让人
    以为这把凭据只是只读。
    """
    permission = caller.permission
    return ProviderCallerOut(
        kind="api_key" if caller.api_key is not None else "session",
        permission=(permission or ApiKeyPermission.READWRITE).value,
        is_admin=caller.is_admin,
        can_write=(
            caller.is_admin or permission is None or permission is ApiKeyPermission.READWRITE
        ),
        knowledge_base_ids=list(caller.knowledge_base_ids),
    )


def _kb_brief(
    record: Any,
    caller: Caller,
    services: Services,
    *,
    document_count: int = 0,
    last_activity: Any = None,
) -> ProviderKbBriefOut:
    """记录 → 握手里的库摘要。

    ``can_write`` 走 ``knowledge_bases.kb_access_flags``——**与 ``GET /knowledge-bases``
    同一份口径**。两处各写一份的表现是界面自相矛盾：列表上说能传、点进去 403，
    或反过来把能用的入口收起来（能力集需要共用）。
    """
    _, writable = kb_access_flags(record, caller, services)
    return ProviderKbBriefOut.model_validate(record).model_copy(
        update={
            "can_write": writable,
            "document_count": document_count,
            "last_activity": last_activity,
        }
    )


def kb_briefs(services: Services, caller: Caller) -> list[ProviderKbBriefOut]:
    """这次调用**看得见**的库摘要（受限 key 只看到范围内的）。

    计数一次聚合查出来，与列表端点同一个数——握手本来就要少发请求，
    不该让它比列表还贵。
    """
    stats = services.knowledge_bases.document_stats()
    return [
        _kb_brief(
            record,
            caller,
            services,
            document_count=stats.get(record.id, (0, None))[0],
            last_activity=stats.get(record.id, (0, None))[1],
        )
        for record in visible_knowledge_bases(services, caller)
    ]


def build_handshake(services: Services, caller: Caller) -> ProviderHandshakeOut:
    """握手的响应（**纯拼装**：不落库、不调模型、不发网络请求）。

    所以它可以直接被用例喂一份假 ``services`` 调——契约的形状与取值在这条路上就能钉死，
    不必等到跑起一台带 PG 的实例。
    """
    settings = get_settings()
    return ProviderHandshakeOut(
        provider=PROVIDER_NAME,
        protocol_version=PROTOCOL_VERSION,
        app_version=settings.app_version,
        api_version=API_VERSION,
        capabilities=provider_capabilities(services),
        caller=caller_brief(caller),
        knowledge_bases=kb_briefs(services, caller),
        server_time=datetime.now(UTC),
    )


@router.get(
    "/handshake",
    response_model=ProviderHandshakeOut,
    summary="知识库提供者握手（连通性 + 能力集 + 库清单）",
)
def handshake(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> ProviderHandshakeOut:
    """一次调用回答：**凭据有效吗、这台提供者能做什么、我能用哪些库**。

    鉴权在它前面（``require_read``）：没有凭据是 401、凭据无效是 401、
    凭据有效但越权是 403——**都不是这个响应体的一部分**（模块头的裁量 2）。
    """
    return build_handshake(services, caller)
