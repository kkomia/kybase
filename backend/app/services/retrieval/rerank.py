"""可选 rerank（M3 T3.5）。

架构 §5：rerank 由用户自配 OpenAI 兼容端点（如硅基流动 bge-reranker），**不配置则跳过**。
"跳过"必须是干净地跳过——既不能报错，也不能把结果降级成空列表。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.services.runtime_config import RuntimeConfigService

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_ERROR_BODY = 200

__all__ = [
    "NoopReranker",
    "OpenAICompatReranker",
    "RerankError",
    "RerankProvider",
    "build_reranker",
]


class RerankError(Exception):
    """rerank 失败。调用方应当**降级为不重排**而不是让整次检索失败。"""


class RerankProvider(ABC):
    """重排协议。"""

    model_id: str = "none"
    enabled: bool = False

    @abstractmethod
    def rerank(
        self, *, query: str, documents: Sequence[str], top_n: int
    ) -> list[tuple[int, float]]:
        """返回 ``[(原文档下标, 相关性分)]``，按分数降序。"""


class NoopReranker(RerankProvider):
    """未配置 rerank 时的空实现：原样返回，索引与顺序都不变。"""

    model_id = "none"
    enabled = False

    def rerank(
        self, *, query: str, documents: Sequence[str], top_n: int
    ) -> list[tuple[int, float]]:
        return [(index, 0.0) for index in range(min(top_n, len(documents)))]


class OpenAICompatReranker(RerankProvider):
    """``POST {base_url}/rerank``，请求体遵循主流兼容实现（硅基流动 / Cohere 风格）。"""

    enabled = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    def rerank(
        self, *, query: str, documents: Sequence[str], top_n: int
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload = {
            "model": self.model_id,
            "query": query,
            "documents": list(documents),
            "top_n": min(top_n, len(documents)),
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            # 客户端**共享**（见 app/core/http.py）：检索链路上每次 rerank 都新建客户端，
            # 等于每次都重新握手。超时仍按调用点给。
            client = self._client or shared_client()
            response = client.post(
                f"{self.base_url}/rerank",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise RerankError(f"rerank 端点不可达：{exc}") from exc

        if response.status_code != 200:
            body = response.text[:_MAX_ERROR_BODY]
            raise RerankError(f"rerank 端点返回 {response.status_code}: {body}")

        try:
            items = response.json()["results"]
        except (KeyError, ValueError) as exc:
            raise RerankError(f"rerank 响应格式不符合预期：{exc}") from exc

        ranked: list[tuple[int, float]] = []
        for item in items:
            try:
                ranked.append((int(item["index"]), float(item["relevance_score"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise RerankError(f"rerank 结果缺少 index/relevance_score：{item!r}") from exc
        return sorted(ranked, key=lambda pair: (-pair[1], pair[0]))


def build_reranker(runtime: RuntimeConfigService) -> RerankProvider:
    """配了就启用，没配就干净跳过（架构 §5）。

    与 embedder 同理：每次调用重新读运行期配置，设置页改完立刻生效。
    """
    config = runtime.rerank()
    if config.is_configured:
        return OpenAICompatReranker(
            base_url=config.base_url,
            api_key=config.api_key,
            model_id=config.model_id,
        )
    return NoopReranker()
