"""检索端点（M4）。

**产品边界**：这里只返回检索结果原文与元信息，**不做任何 LLM 预处理**
（架构 §5）——提示注入防护的责任在调用方。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_read
from app.api.v1.schemas import (
    ChannelStatOut,
    SearchHitOut,
    SearchRequest,
    SearchResponse,
)
from app.core.services import Services, get_services
from app.services.api_key import Caller
from app.services.retrieval import MetadataFilter, RetrievalQuery

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse, summary="混合检索")
def search(
    payload: SearchRequest,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> SearchResponse:
    # 请求体里指明了库，所以这里做带范围的判定：只读密钥不能查它没被授权的库
    services.api_keys.check_access(caller, kb_ids=payload.kb_ids)

    filters = None
    if payload.filters is not None:
        filters = MetadataFilter(
            document_ids=payload.filters.document_ids,
            source_kinds=payload.filters.source_kinds,
            created_after=payload.filters.created_after,
            created_before=payload.filters.created_before,
        )

    response = services.retrieval.search(
        RetrievalQuery(
            query=payload.query,
            kb_ids=payload.kb_ids,
            top_k=payload.top_k,
            mode=payload.mode,
            candidate_k=payload.candidate_k,
            score_threshold=payload.score_threshold,
            min_vector_score=payload.min_vector_score,
            min_term_coverage=payload.min_term_coverage,
            rerank=payload.rerank,
            filters=filters,
        )
    )

    return SearchResponse(
        hits=[SearchHitOut.model_validate(hit) for hit in response.hits],
        mode=response.mode,
        reranked=response.reranked,
        filtered_out=response.filtered_out,
        stats=[ChannelStatOut.model_validate(stat) for stat in response.stats],
        embedding_configured=services.runtime.embedding().is_configured,
        embedding_is_development=services.embedder.is_development,
    )
