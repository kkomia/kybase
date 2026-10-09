"""开发用确定性嵌入（M2 T2.9）。

**它没有语义。** 实现方式是特征哈希（hashing trick）：把词元哈希到固定维度上累加并做符号散列，
再 L2 归一化。因此：

- 词面重合越多，向量越接近 —— 这让"传进来的 query 与文档用词相同"的场景看起来是work的；
- 换个说法但意思相同的句子**不会**靠近 —— 这不是语义嵌入。

存在意义：让"上传 → 解析 → 切分 → 向量化 → 检索"整条链路在没有 API key、不联网的情况下
也能跑通并被测试，而不是把整条主链路卡在凭据上。启用真实模型后即可替换。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from app.services.embedding.base import EmbeddingProvider, l2_normalize

DEFAULT_DEV_DIM = 256
MAX_TOKENS_PER_TEXT = 512
"""截断上限：开发兜底不追求长文建模，避免超长文本拖慢测试。"""

#: 惰性拿到的 jieba（**故意不是模块级导入** ✗）。
_JIEBA: Any = None


def _jieba_cache_file() -> str | None:
    """jieba 的缓存该落在**数据目录**，而不是进程的 cwd。

    为什么必须显式设：jieba 的 ``cache_file`` 默认是 ``None``，此时它用
    ``os.path.join(tmp_dir or tempfile.gettempdir(), "jieba.cache")`` ——
    而本机沙箱**拒写 %TEMP%**，``tempfile.gettempdir()`` 会静默回退到 **cwd**，
    于是跑一次检索用例，仓库根就冒出一个 ``jieba.cache``（约 9 MB，实测过）。

    **行为不变**：缓存里只是前缀词典的概率表，谁写都一样 —— 切词结果一字不差，
    只是它不再落在 cwd。取不到数据目录时返回 ``None``（退回 jieba 自己的默认）：
    别为了一个缓存路径让切词挂掉。
    """
    try:
        from pathlib import Path

        from app.core.config import get_settings

        data_dir = Path(get_settings().data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / "jieba.cache")
    except Exception:
        return None


def _jieba() -> Any:
    """第一次用到时才导入 jieba（模块级导入会让**客户端运行时**凭空多背 40.9 MB ✗）。

    与 ``app/storage/text.py::_jieba``、``app/services/retrieval/coverage.py::_jieba`` 同一个
    道理（P4-3，2026-09-29 实测）：这个模块挂在 ``app.services.embedding`` 的导入链上 ✓，
    而"切词"只有**服务器**那条向量化链会用 ✓，边车从不切词 ✓。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，jieba 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _JIEBA
    if _JIEBA is None:
        import jieba

        # 缓存落哪儿：**数据目录**，不是 cwd（见 `_jieba_cache_file()` 的说明）
        cache = _jieba_cache_file()
        if cache:
            jieba.dt.cache_file = cache

        _JIEBA = jieba
    return _JIEBA


def tokenize(text: str) -> list[str]:
    """切词：jieba 词元 + 中文单字。

    补单字是因为查询常常很短（"检索"），而 jieba 可能把它切成一整词，
    单字能提高短查询与长文档的碰撞概率，让开发期的召回看起来更合理。
    """
    jieba = _jieba()
    tokens = [word.strip().lower() for word in jieba.cut_for_search(text) if word.strip()]
    tokens.extend(char for char in text if "\u4e00" <= char <= "\u9fff")
    return tokens[:MAX_TOKENS_PER_TEXT]


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """哈希到桶位与符号：blake2b 稳定且跨进程一致（内置 hash() 有随机种子，不能用）。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class DeterministicEmbedder(EmbeddingProvider):
    """特征哈希向量化器（开发兜底）。"""

    model_id = "dev/deterministic-hash"
    is_development = True

    def __init__(self, dim: int = DEFAULT_DEV_DIM, *, max_batch: int = 64) -> None:
        if dim <= 0:
            raise ValueError("维度必须为正整数")
        self.dim = dim
        self.max_batch = max_batch
        jieba = _jieba()
        jieba.initialize()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokenize(text):
            index, sign = _bucket(token, self.dim)
            vector[index] += sign
        return l2_normalize(vector)
