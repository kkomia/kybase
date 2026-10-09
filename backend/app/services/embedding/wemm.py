"""局域网 WeMM-Embedding-2B（llama.cpp + GGUF）的嵌入实现。

原始接入文档：``docs/归档/调研/WeMM-Embedding-2B-接入文档-v0.1.md``（用户给的原文落档，
改动它要连同实现一起改）。那台服务给出的是 **2048 维、已 L2 归一化**的向量，
文本 / 图片 / 视频**共用同一个向量空间**——所以"用一句文字搜一张图"在这一层就成立，
不需要第二套索引。

三个端口都按"一个实现"接住：

1. 文本：``POST /v1/embeddings``（OpenAI 兼容）。**批量走数组**：文档 §7 实测总吞吐封顶
   约 33 请求/秒，加并发只增加排队延迟、不提高吞吐，所以这里的批**串行发**、一行线程都不建；
2. 图片 / 视频：``POST /embedding``（原生）。三步——``GET /props`` 取 ``media_marker``
   → 文件字节 base64 → 提交 ``{"content": {"prompt_string": marker, "multimodal_data": [...]}}``；
3. ``GET /props`` 本身（取 marker）。

## 两个必须写下来的坑

- **媒体绝不能用旧式 ``image_data`` + ``[img-N]``**：当前 llama.cpp 会把它当**纯文本静默
  忽略**——不报错、也不生效，于是"图根本没进向量"而调用方以为成功了。本实现只发
  ``content.prompt_string`` + ``content.multimodal_data``，并且用例里明文断言请求体里
  **没有** ``image_data`` 这个键（这种坑靠读代码看不出来，只能靠用例钉住）。
- **marker 每次请求现取**：它随**服务重启（含空闲 30 分钟后的冷启动）**变化，
  硬编码或缓存下来的值会在那一刻开始一直失败。``GET /props`` 的实测开销是 ~10ms
  （见报告里的真机数字），现取的代价可以忽略。

## 只碰 embedding，不碰 /completion

这台服务上的模型是 embedding 专用的（文档 §2 第 4 条）。生成接口一个字节都不发：
发错了不会报错，只会得到一段毫无意义的文本，而排查要从"向量为什么不对"倒退很久。
"""

from __future__ import annotations

import base64
import logging
import math
import time
from collections.abc import Callable, Sequence

from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.services.embedding.base import EmbeddingError, EmbeddingProvider, fit_dimension

__all__ = ["WeMMEmbedder"]

logger = logging.getLogger(__name__)

#: 原生维度（文档 §1：固定 2048，更小维度用 Matryoshka 截断）。
NATIVE_DIM = 2048

TEXT_PATH = "/v1/embeddings"
MEDIA_PATH = "/embedding"
PROPS_PATH = "/props"

#: 超时下限来自文档 §2 第 1 条：冷启动约 10s，文本建议 ≥30s、视频建议 ≥120s。
#: 这里取的是**比下限更宽**的值（文本 60s、媒体 300s）：这条链路上失败一次的代价是
#: 整个文档摄入重跑，而多等一会儿的代价只是慢——宁可等。
TEXT_TIMEOUT_SECONDS = 60.0
MEDIA_TIMEOUT_SECONDS = 300.0
PROPS_TIMEOUT_SECONDS = 30.0

#: 502 = 暂时不可用（文档 §6）；503/504 同理。**embedding 无副作用，重试是安全的**，
#: 所以这几档直接退避重试，而不是把失败甩给上层去重跑整个文档。
RETRYABLE_STATUS = frozenset({502, 503, 504})
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0

#: 允许的范数偏差。文档 §1 写着"已 L2 归一化"，但"服务保证过"**不等于**"不必校验"：
#: 没归一化的向量不会让任何一层报错，只会让余弦相似度与点积不再等价——排序悄悄变差，
#: 排查时从"检索结果不对"倒退很久才想到这一层。1e-3 是浮点传输的余量（实测 1.000000）。
NORM_TOLERANCE = 1e-3

_MAX_ERROR_BODY = 300


class _StaleMarker(Exception):
    """内部信号：这次媒体请求失败**看起来**是 marker 过期（服务重启过）。

    单列一个内部异常而不是就地重试，是为了让"哪一次失败要换 marker"这件事只写在
    `embed_media` 一处；`_embed_media_once` 只管把服务的话翻译成这个信号。
    """


class WeMMEmbedder(EmbeddingProvider):
    """``WeMM-Embedding-2B`` 的客户端（文本 + 图片 + 视频）。"""

    supports_media = True

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        dim: int,
        api_key: str = "",
        max_batch: int = 32,
        text_timeout: float = TEXT_TIMEOUT_SECONDS,
        media_timeout: float = MEDIA_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if dim <= 0:
            raise ValueError("维度必须为正整数")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        #: **目标**维度（知识库记的就是它）：小于原生 2048 时按 Matryoshka 截断 + 重归一化。
        self.dim = dim
        self.max_batch = max(1, int(max_batch))
        self._api_key = api_key
        self._text_timeout = text_timeout
        self._media_timeout = media_timeout
        self._client = client
        #: 退避用的睡眠函数**可注入**：用例不该真的等 1s + 2s（那会让"重试"这件事
        #: 在单测里变成一个慢动作，久而久之就没人愿意测它）。
        self._sleep = sleep

    # ------------------------------------------------------------------ 文本

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量文本 → 向量（顺序与入参一致）。

        **一批一个请求、批与批串行**：见模块头第 1 条。空输入不发请求。
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch):
            batch = list(texts[start : start + self.max_batch])
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model_id, "input": batch}
        response = self._request(TEXT_PATH, payload=payload, timeout=self._text_timeout)
        self._raise_for_status(response, what="文本向量化")

        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError(f"文本向量化响应不是 JSON：{exc}") from exc

        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise EmbeddingError("文本向量化响应里没有 data 数组（不符合 OpenAI 规范）")
        ordered = sorted(
            (item for item in items if isinstance(item, dict)),
            key=lambda item: item.get("index", 0),
        )
        if len(ordered) != len(batch):
            raise EmbeddingError(
                f"返回向量数 {len(ordered)} 与请求文本数 {len(batch)} 不一致"
                "（错位会让向量与文本对不上，且极难察觉）"
            )
        return [
            fit_dimension(
                self._native_vector(item.get("embedding"), what="文本向量化"),
                self.dim,
                model_id=self.model_id,
            )
            for item in ordered
        ]

    # ------------------------------------------------------------------ 媒体

    def media_marker(self) -> str:
        """取当前 ``media_marker``。

        **每次媒体请求前现取**（见模块头）：它是一个随进程启动生成的随机标记，
        服务重启 / 冷启动之后就换了，硬编码或缓存的值会从那一刻起一直不生效。
        """
        response = self._request(PROPS_PATH, method="GET", timeout=PROPS_TIMEOUT_SECONDS)
        self._raise_for_status(response, what="读取 /props")
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError(f"/props 响应不是 JSON：{exc}") from exc
        marker = body.get("media_marker") if isinstance(body, dict) else None
        if not isinstance(marker, str) or not marker:
            raise EmbeddingError("/props 响应里没有 media_marker（服务版本不对？）")
        return marker

    def embed_media(self, data: bytes) -> list[float]:
        """一份图片 / 视频的原始字节 → 向量（顺序：先取 marker，再提交）。"""
        if not data:
            raise EmbeddingError("媒体内容为空：没有字节可嵌入")
        marker = self.media_marker()
        try:
            return self._embed_media_once(data, marker)
        except _StaleMarker:
            # marker 失效是**预期的**（服务重启 / 冷启动之后换了），处置只有一个：
            # 重取一个再试一次。重试一次就够——再失败说明不是 marker 的事。
            fresh = self.media_marker()
            logger.info("WeMM media_marker 已失效，重取后重试一次（旧 %s，新 %s）", marker, fresh)
            return self._embed_media_once(data, fresh)

    def _embed_media_once(self, data: bytes, marker: str) -> list[float]:
        payload = {
            "content": {
                # **只发这两个字段**：旧式 image_data + [img-N] 会被 llama.cpp 当纯文本
                # 静默忽略（见模块头），所以这里连一个多余的键都不带。
                "prompt_string": marker,
                "multimodal_data": [base64.b64encode(data).decode("ascii")],
            }
        }
        response = self._request(MEDIA_PATH, payload=payload, timeout=self._media_timeout)
        if response.status_code != 200:
            if self._looks_like_stale_marker(response):
                raise _StaleMarker()
            self._raise_for_status(response, what="媒体向量化")

        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError(f"媒体向量化响应不是 JSON：{exc}") from exc
        return fit_dimension(
            self._native_vector(self._media_vector_of(body), what="媒体向量化"),
            self.dim,
            model_id=self.model_id,
        )

    @staticmethod
    def _looks_like_stale_marker(response: httpx.Response) -> bool:
        """这次失败是不是"marker 过期"。

        判据两条，都由服务端的话决定：
        - 响应体里提到 marker（服务把原因说出来了，最可靠）；
        - **400**：媒体这条路上 payload 的其余部分（prompt_string 之外只有我们自己
          生成的 base64）不可能畸形，所以 400 几乎只有一种解释——prompt_string 里那个
          marker 不是它当前的。宁可多取一次 /props（~10ms），也不要让一份文档白失败。
          其余 4xx（403/404/413…）不在此列：那些不是换 marker 能解决的。
        """
        if "marker" in response.text[:_MAX_ERROR_BODY].lower():
            return True
        return response.status_code == 400

    @staticmethod
    def _media_vector_of(body: object) -> list[float]:
        """媒体响应的向量。

        原生端点回的是 ``[{"embedding": [[...]]}]``（**多一层嵌套**，官方客户端里
        那句 ``embedding[0] if isinstance(embedding[0], list)`` 就是在剥它）；
        OpenAI 兼容端点回的是 ``{"data": [{"embedding": [...]}]}``。两种都认，
        因为文档 §4 两条路都写着可用，而"换端点就静默拿到错的东西"不值当。
        """
        if isinstance(body, dict):
            items = body.get("data")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return WeMMEmbedder._vector_of(items[0].get("embedding"))
        if isinstance(body, list) and body and isinstance(body[0], dict):
            embedding = body[0].get("embedding")
            if isinstance(embedding, list) and embedding and isinstance(embedding[0], list):
                embedding = embedding[0]
            return WeMMEmbedder._vector_of(embedding)
        raise EmbeddingError("媒体向量化响应结构不认识（既不是原生数组也不是 OpenAI 格式）")

    @staticmethod
    def _vector_of(value: object) -> list[float]:
        """把一个 embedding 值校验成"非空的数字数组"。

        这一层必须严：静默接受 `None` / 空数组 / 字符串会让坏向量写进库，
        而它只在**检索结果莫名其妙**的时候才暴露出来。
        """
        if not isinstance(value, list) or not value:
            raise EmbeddingError("响应里的 embedding 不是非空数组")
        out: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise EmbeddingError("响应里的 embedding 含非数字项")
            out.append(float(item))
        return out

    def _native_vector(self, value: object, *, what: str) -> list[float]:
        """校验服务给的原生向量：**2048 维、已 L2 归一化**（文档 §1 的两条承诺）。

        为什么要校验"服务保证过的事"：这两条一旦不成立，后果**大多不是报错，而是静默变差**。
        维度不对还能被仓储层拦住（`VectorDimensionMismatch`），而没归一化完全无声——
        余弦相似度与点积不再等价，召回顺序跟着歪。校验放在这里，"服务被换过 / 换了启动
        参数 / 代理接错了后端"这几种情况当场就能看出来，而不是等用户说"搜出来的东西不对"。
        """
        vector = self._vector_of(value)
        if len(vector) != NATIVE_DIM:
            raise EmbeddingError(
                f"{what}返回 {len(vector)} 维，WeMM-Embedding-2B 应为 {NATIVE_DIM} 维"
                "（服务被换过？或启动参数带了维度截断？截断请改模型登记里的维度，由我们这一侧做）"
            )
        norm = math.sqrt(sum(item * item for item in vector))
        if abs(norm - 1.0) > NORM_TOLERANCE:
            raise EmbeddingError(
                f"{what}返回的向量 L2 范数是 {norm:.6f}（应≈1）：未归一化的向量会让"
                "余弦相似度不再等于点积，检索排序会静默变差"
            )
        return vector

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        """带鉴权头**仅当配了 key**。

        局域网那台代理没有鉴权（文档 §5 的客户端一个 Authorization 都不发），
        硬塞一个 ``Bearer``（哪怕是空串）只会让代理多一条无意义的判断。
        """
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        method: str = "POST",
        timeout: float,
    ) -> httpx.Response:
        """发一次请求，**502/503/504 与连接层错误按指数退避重试**。

        退避间隔 1s → 2s（``RETRY_BASE_SECONDS * 2**attempt``），一共 3 次尝试。
        超时与"服务不可用"在这里不区分对待：对调用方来说两者都是"这一趟没拿到"，
        而 embedding 无副作用，重试永远安全（文档 §6）。
        非重试档的状态码**原样返回**给调用方解释：403/404 与 marker 过期是两件事。
        """
        url = f"{self.base_url}{path}"
        client = self._client or shared_client()
        last_error = ""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                if method == "GET":
                    response = client.get(url, headers=self._headers(), timeout=timeout)
                else:
                    response = client.post(
                        url, json=payload, headers=self._headers(), timeout=timeout
                    )
            except httpx.TransportError as exc:
                # 连不上 / 读超时：服务可能在冷启动（~10s）或刚重启，值得等一次
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = f"HTTP {response.status_code}: {response.text[:_MAX_ERROR_BODY]}"
            if attempt < RETRY_ATTEMPTS - 1:
                self._sleep(RETRY_BASE_SECONDS * (2**attempt))
        raise EmbeddingError(
            f"WeMM 嵌入服务不可用（{RETRY_ATTEMPTS} 次尝试都失败，"
            f"{self.base_url}{path}）：{last_error}。"
            "若刚空闲过 30 分钟，首个请求要等模型重新加载（约 10s）；"
            "也可能是代理没起来（systemctl --user status wemm-proxy）"
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, what: str) -> None:
        if response.status_code == 200:
            return
        body = response.text[:_MAX_ERROR_BODY]
        hint = ""
        if response.status_code == 404:
            hint = "（地址或路径不对：文本是 /v1/embeddings，媒体是 /embedding）"
        raise EmbeddingError(f"{what}失败：HTTP {response.status_code}: {body}{hint}")
