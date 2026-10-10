"""检索端点（M4）。

**产品边界**：这里只返回检索结果原文与元信息，**不做任何 LLM 预处理**
（架构 §5）——提示注入防护的责任在调用方。

**但判定要排在原文前面**（v0.55）：响应里第一组字段是 `summary` / `distribution` /
`decision`（命中分布是这套服务的特色，详见 ``services/retrieval/distribution.py``
的模块头），`hits` 在它们之后。"先给分布、再给原文"是产品口径而不是排版偏好：
把分布排在原文后面，读的人（模型或界面）就会先被一堆片段带走。
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
            page_from=payload.filters.page_from,
            page_to=payload.filters.page_to,
            heading_contains=payload.filters.heading_contains,
        )

    # 基础阈值与 MCP 那条路**同源**：一处读取（``runtime_config.retrieval_thresholds``），
    # 免得"网页上有分布、MCP 里没有"这种同一份设置两种行为的不一致。
    stats_floor, baseline = services.runtime.retrieval_thresholds()

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
            stats_floor=stats_floor,
            baseline=baseline,
            keep=payload.keep,
            min_score=payload.min_score,
            per_doc=payload.per_doc,
            offset=payload.offset,
            rerank=payload.rerank,
            filters=filters,
        )
    )

    distribution = response.distribution
    decision = response.decision
    decided_by = "" if decision is None else str(decision.get("decided_by") or "")
    return SearchResponse(
        # 判定在前：界面上该显示的是"这批命中是什么状态"，原文是它的下文
        # `kept` 传的是**收敛后剩下的总数**（``response.kept``）而不是这一页给了几条
        # （v0.63）：翻页时这一句必须**逐字不变**——调用方正是靠它决定要不要接着翻；
        # "这一页给了几条"由 ``hits`` 与 ``next_offset`` 说。
        summary=(
            distribution.headline(kept=response.kept, decided_by=decided_by)
            if distribution is not None
            else ""
        ),
        distribution=distribution.as_payload() if distribution is not None else None,
        decision=decision,
        hits=[SearchHitOut.model_validate(hit) for hit in response.hits],
        mode=response.mode,
        reranked=response.reranked,
        filtered_out=response.filtered_out,
        # 翻页的两项紧跟计数：与 MCP 那条路同源（同一个 ``RetrievalResponse``），
        # 免得"工具里能翻、界面上翻不了"这种同一份数据两种能力
        offset=response.offset,
        next_offset=response.next_offset,
        stats=[ChannelStatOut.model_validate(stat) for stat in response.stats],
        embedding_configured=services.runtime.embedding().is_configured,
        embedding_is_development=services.embedder.is_development,
    )
