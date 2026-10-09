"""检索服务（M3）。

对外只暴露：检索入口、查询/结果类型、rerank 构造器，以及**相关度下限的口径**
（v0.53：接口层要把标定值与默认写进参数说明，必须与判定用的是同一份）。
"""

from __future__ import annotations

from app.services.retrieval.coverage import content_terms, term_coverage
from app.services.retrieval.fusion import DEFAULT_RRF_K, rrf_fuse
from app.services.retrieval.rerank import (
    NoopReranker,
    OpenAICompatReranker,
    RerankError,
    RerankProvider,
    build_reranker,
)
from app.services.retrieval.service import (
    DEFAULT_MIN_TERM_COVERAGE,
    MIN_VECTOR_SCORE_BY_MODEL,
    RetrievalService,
    calibrated_vector_floor,
    similarity_from_distance,
)
from app.services.retrieval.types import (
    ChannelStat,
    MetadataFilter,
    RetrievalHit,
    RetrievalMode,
    RetrievalQuery,
    RetrievalResponse,
)

__all__ = [
    "DEFAULT_MIN_TERM_COVERAGE",
    "DEFAULT_RRF_K",
    "MIN_VECTOR_SCORE_BY_MODEL",
    "ChannelStat",
    "MetadataFilter",
    "NoopReranker",
    "OpenAICompatReranker",
    "RerankError",
    "RerankProvider",
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalQuery",
    "RetrievalResponse",
    "RetrievalService",
    "build_reranker",
    "calibrated_vector_floor",
    "content_terms",
    "rrf_fuse",
    "similarity_from_distance",
    "term_coverage",
]
