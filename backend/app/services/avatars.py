"""用户头像（v0.29）。

一件小东西，两条边界要写清楚：

1. **文件存在对象存储里，不在数据库里**。头像是一张图（几十到几百 KB），
   塞进 PG 的 bytea 会让每次读账号都拖着一份二进制——而账号是**每个页面
   都要读一次**的东西。库里只留一个 key，图片本体按 key 取。
2. **key 是内容寻址的**（``avatars/<用户>/<内容 hash 前 16 位>.<后缀>``）。
   于是换头像就是换 key：``<img src>`` 天然拿到新图，不需要 ``?v=`` 那类
   缓存击穿参数（而"忘了加 v，用户看到的还是旧头像"是这类功能最常见的 bug）。
   旧的那张在换的时候**显式删掉**——不然换十次就在桶里留十张。

**格式按魔数认，不按上传时声明的 content-type**：后者是客户端说了算的字符串，
声明成 ``image/png`` 的可以是一段脚本。认出来的那几种之外一律拒绝。

**为什么不在这里缩放**：缩放要一个成像库（Pillow），而这个项目到目前
一张图都不处理。前端用 canvas 缩到 256×256 再传（见 `AppAvatar` 那一侧），
这里只守住"是不是图、有多大"这两条——**服务端的职责是不被撑爆，不是修图**。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from urllib.parse import quote

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.storage.base import StoreBundle, UserRecord

__all__ = ["AVATAR_PREFIX", "MAX_AVATAR_BYTES", "AvatarService", "sniff_image"]

logger = logging.getLogger(__name__)

AVATAR_PREFIX = "avatars"
"""对象存储里的前缀。与内容寻址的 ``originals/``、会话产物的 ``conversations/`` 分开。"""

MAX_AVATAR_BYTES = 2 * 1024 * 1024
"""单张上限 2MB。前端会先缩到 256px（通常 10–40KB），这个上限挡的是"绕过界面直接传"。"""

#: 认这几种（魔数, 后缀, media type）。**顺序有关系**：前缀更长的排前面没坏处，
#: 但这里几组互不为前缀，随便排。
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)

#: WebP 的魔数不在开头（``RIFF????WEBP``），单独判。
_RIFF = b"RIFF"
_WEBP = b"WEBP"
_WEBP_OFFSET = 8


def sniff_image(blob: bytes) -> tuple[str, str] | None:
    """看头几个字节认格式，返回 ``(后缀, media_type)``；认不出返回 ``None``。"""
    for magic, suffix, media in _SIGNATURES:
        if blob.startswith(magic):
            return suffix, media
    if blob[:4] == _RIFF and blob[_WEBP_OFFSET : _WEBP_OFFSET + 4] == _WEBP:
        return "webp", "image/webp"
    return None


class AvatarService:
    """头像的存取与签名链接。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 写

    def save(self, user_id: str, blob: bytes) -> str:
        """存一张新头像，返回新的 key（并登记到用户记录上）。"""
        if not blob:
            raise InvalidRequestError("上传的内容是空的")
        if len(blob) > MAX_AVATAR_BYTES:
            raise InvalidRequestError(
                f"图片太大（{len(blob) // 1024} KB，上限 {MAX_AVATAR_BYTES // 1024} KB）"
            )
        sniffed = sniff_image(blob)
        if sniffed is None:
            raise InvalidRequestError("这不像一张图片（只认 PNG / JPEG / GIF / WebP）")
        suffix, _media = sniffed

        digest = hashlib.sha256(blob).hexdigest()[:16]
        key = f"{AVATAR_PREFIX}/{user_id}/{digest}.{suffix}"
        previous = self._set_key(user_id, self._stores.objects.write(key, blob))
        self._drop(previous, keep=key)
        logger.info("头像已更新：%s", user_id)
        return key

    def clear(self, user_id: str) -> None:
        """删掉头像（回到"用名字生成的默认头像"）。没有头像时也成功——它要的是结果。"""
        self._drop(self._set_key(user_id, ""), keep="")

    def _set_key(self, user_id: str, key: str) -> str:
        """把 key 写到用户记录上，返回**改之前**那个。"""
        record = self._stores.meta.get_user(user_id)
        if record is None:
            raise NotFoundError(f"没有这个使用者：{user_id}")
        previous = record.avatar_key
        self._stores.meta.set_user_avatar(user_id, key)
        return previous

    def _drop(self, key: str, *, keep: str) -> None:
        """删掉旧图。**删不掉不影响这次操作**：留一张孤儿图比让"换头像"失败轻得多。"""
        if not key or key == keep or not key.startswith(f"{AVATAR_PREFIX}/"):
            return
        try:
            self._stores.objects.delete(key)
        except Exception:
            logger.warning("旧头像没删掉（留成孤儿）：%s", key, exc_info=True)

    # ------------------------------------------------------------------ 读

    def read(self, user_id: str) -> tuple[bytes, str]:
        """取头像本体：``(字节, media_type)``。没有头像就 404。"""
        record = self._stores.meta.get_user(user_id)
        if record is None or not record.avatar_key:
            raise NotFoundError("这个使用者还没有头像")
        try:
            blob = self._stores.objects.read(record.avatar_key)
        except (NotFoundError, OSError) as exc:
            # 记录与对象对不上（换了桶、手删了文件）：**说清楚**，界面会退回默认头像，
            # 而不是把一次 500 甩给用户。
            #
            # 两个异常都要接：本地文件那份实现抛的是内置的 ``FileNotFoundError``
            # （``OSError`` 的子类），对象存储那份抛的是域内的 ``NotFoundError``
            # ——只接后者的话，开发环境（本地实现）会漏成 500。
            raise NotFoundError("头像文件已经不在存储里了，重新上传一次即可") from exc
        media = sniff_image(blob)
        return blob, (media[1] if media else "application/octet-stream")

    def url_for(
        self,
        user: UserRecord,
        *,
        secret: str | None,
        ttl_seconds: int | None = None,
        now: int | None = None,
    ) -> tuple[str, int]:
        """给一张头像签发链接：``(url, 到期时间戳)``；没有头像或签不出时返回 ``("", 0)``。

        **为什么头像也要签名**：``<img src>`` 带不了 Authorization 头，而账号列表
        读得到、不代表谁都能取图。公开放开的话，一个用户 id 就能把别人的脸拉下来
        （id 是 ``user_xxx`` 这种可枚举的串）。签名的代价只是一行查询。
        """
        if not user.avatar_key or not secret:
            return "", 0
        from app.core.signing import DEFAULT_TTL_SECONDS, sign_resource

        signature, expires_at = sign_resource(
            resource(user.id, user.avatar_key),
            secret,
            ttl_seconds=ttl_seconds or DEFAULT_TTL_SECONDS,
            now=now,
        )
        return (
            f"/api/v1/avatars/{user.id}?expires={expires_at}&signature={signature}",
            expires_at,
        )


def resource(user_id: str, key: str) -> str:
    """被签名的资源标识：``avatar:<用户>:<key>``。

    与文件那条同一个道理（见 ``services/artifacts.py::file_signature_resource``）：
    URL 里同时有用户与签名，**两者都要绑进签名**，否则换一个用户 id 去请求
    同一份签名，端点会先查"这个人有没有头像"——而这层校验本该由签名保证。
    ``key`` 里有斜杠，转义后再拼：不转的话 ``a/b`` 与 ``a:b`` 会撞成同一个标识。
    """
    return f"avatar:{user_id}:{quote(key, safe='')}"


#: 组合根注入口（与 ``Translator`` 同一手法：服务不认识 api 层）。
SecretGetter = Callable[[], str | None]
