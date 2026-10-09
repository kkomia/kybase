"""进程级共享的出站 HTTP 客户端。

**为什么要共享**：出站调用（对话模型 / embedding / rerank / 联网）此前各自
``httpx.Client(...)`` 新建——每次都是一轮 TCP + TLS 握手，而且没有 keep-alive。
一轮默认的 Agent 对话要发出十几次出站请求（每步一次模型调用，每一步检索一次 embedding，
可能还有 rerank 与联网），一篇 500 chunk 的文档按 batch=32 摄入是 16 次 embedding 调用。
握手在内网是 5–20ms、公网 100–300ms，这些时间全是白花的固定开销。

**超时仍然按调用点给**（``client.post(..., timeout=...)``）：几个用途的超时本来就不同
（对话 120s / embedding 60s / rerank 30s / 联网 20s），共用一个客户端不该把它们拉平成一个值。
客户端自带的那个默认值只是**兜底**——哪一处漏给也不会掉到 httpx 的 5 秒默认上去。

**不改 follow_redirects**：httpx 默认为假，而联网抓取明确要求不跟随重定向
（每一跳都要重新校验地址，见 ``services/web.py::_get_with_checks``）。
在这里改成跟随，会让那个 SSRF 检查形同虚设。
"""

from __future__ import annotations

import threading

from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）

#: 兜底超时。真正生效的是各调用点显式传的那个，见模块注释。
_FALLBACK_TIMEOUT = 120.0

_client: httpx.Client | None = None
_lock = threading.Lock()


def _limits():  # type: ignore[no-untyped-def]
    """并发上限（**惰性建**：写成模块级原件的话，导入期就会把 httpx 拉进来 ✗）。

    一轮对话最多十几次出站，32 条连接足够几十个并发请求共用；
    keep-alive 留 16 条，免得每次把刚建好的连接挤出去——那等于又回到"每次握手"。
    """
    return httpx.Limits(max_connections=32, max_keepalive_connections=16)


def shared_client() -> httpx.Client:
    """拿进程级的共享客户端（首次调用时创建；线程安全）。

    用双检锁而不是 ``lru_cache``：这个对象要在关停时被**显式关掉**，
    而 ``lru_cache`` 没有这个语义（``cache_clear`` 只是丢引用，
    连接要等 GC——而 GC 的时机不确定，测试里尤其如此）。
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = httpx.Client(timeout=_FALLBACK_TIMEOUT, limits=_limits())
    return _client


def close_shared_client() -> None:
    """关掉共享客户端（进程退出时调；重复调用安全）。

    关掉之后下一次 ``shared_client()`` 会建一个新的——测试因此可以在用例之间
    拿到干净状态，不必去碰私有变量。
    """
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        client.close()
