"""签名 URL 的签发与校验（《架构设计 v0.2》§3.2：图片与原文下载返回带过期时间的签名 URL）。

**为什么不能直接给路径**：原文与图片是知识库的内容本体，一个裸的
``/documents/doc_x/content`` 只要拿到 id 就能无限期下载。而浏览器里
``<img>`` 与下载链接**没法带 Authorization 头**——这正是签名 URL 存在的唯一理由：
把"你有权访问"这件事编码进 URL 本身，并给它一个到期时间。

设计上刻意选了**无状态**：

- 签名覆盖 ``资源标识 + 过期时刻``，不落库、不需要会话；
- 校验只需要一个密钥，因此重启进程、多进程部署都不影响已发出的链接；
- 到期即失效，不需要清理任务。

密钥来源：``KYLAB_URL_SIGNING_SECRET``（或首次初始化时落库的那一条）；都没有则签不出。
**三个都没有时不允许签发**：那时系统处于"无鉴权"状态，与其发一个永远有效的链接，
不如让调用方走需要鉴权的常规接口（见 api 层的处理）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

__all__ = ["DEFAULT_TTL_SECONDS", "SigningError", "sign_resource", "verify_resource"]

#: 默认有效期。下载是"点一下马上就开始"的动作，几分钟足够；
#: 太长会让一条链接在聊天记录/日志里长期可用，而它的内容是知识库原文。
DEFAULT_TTL_SECONDS = 600


class SigningError(Exception):
    """签名不合法或已过期。调用方负责翻译成 401/403。"""


def _payload(resource: str, expires_at: int) -> bytes:
    """被签名的内容：资源与到期时间，用与 fingerprint 同样的"长度前缀"写法。

    直接拼接会有歧义（``"a"+"bc"`` 与 ``"ab"+"c"`` 一样），而歧义在这里意味着
    **一条签名能换到另一个资源**。
    """
    parts = [resource.encode("utf-8"), str(expires_at).encode("ascii")]
    out = bytearray()
    for part in parts:
        out += f"{len(part)}:".encode("ascii") + part
    return bytes(out)


def _sign(resource: str, expires_at: int, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), _payload(resource, expires_at), hashlib.sha256
    ).hexdigest()


def sign_resource(
    resource: str,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, int]:
    """签名一个资源，返回 ``(签名, 到期时间戳)``。"""
    expires_at = (now if now is not None else int(time.time())) + ttl_seconds
    return _sign(resource, expires_at, secret), expires_at


def verify_resource(
    resource: str, signature: str, expires_at: int, secret: str, *, now: int | None = None
) -> None:
    """校验签名；不通过就抛 ``SigningError``。

    先比签名再看到期：**顺序不能反**。反过来的话，一个随手改的过期时间会得到
    "已过期"的提示，而"签名被篡改"也是同一句——调用方据此无法区分这两件事，
    而它们该给的动作不同（前者该警觉，后者只该重新要一条链接）。
    """
    if not signature or not secret:
        raise SigningError("签名缺失")

    if not hmac.compare_digest(_sign(resource, expires_at, secret), signature):
        raise SigningError("签名无效")

    current = now if now is not None else int(time.time())
    if current > expires_at:
        raise SigningError("签名已过期，请重新获取下载链接")
