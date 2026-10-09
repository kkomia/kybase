"""用户头像（v0.29）。

镜像同构：``app/services/avatars.py`` → 本文件。

四件要钉住的事：

1. **按魔数认格式**：声明成 ``image/png`` 的一段脚本不该被存下来；
2. **key 是内容寻址的**：换一张图 = 换一个 key（``<img>`` 的缓存天然失效），
   旧的那张**显式删掉**（不然换十次留十张）；
3. **只存 key、图在对象存储**：库里那一列不放二进制；
4. 签名链接绑定了"谁的哪张图"——换一个 user id 用同一份签名取不到。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.avatars import AvatarService, resource, sniff_image
from app.storage.base import UserRecord

#: 一张 1×1 的 PNG（真实字节，前 8 位是 PNG 魔数）
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16


@pytest.fixture
def service(bundle) -> AvatarService:  # type: ignore[no-untyped-def]
    return AvatarService(bundle)


def _user(store, name: str = "小又") -> UserRecord:  # type: ignore[no-untyped-def]
    return store.create_user(UserRecord(id=f"user_{name}", name=name))


def test_format_is_recognised_by_magic_bytes() -> None:
    """认格式看**头几个字节**，不看上传时声明的 content-type。

    后者是客户端说了算的字符串：声明 ``image/png`` 的可以是一段脚本。
    """
    assert sniff_image(PNG) == ("png", "image/png")
    assert sniff_image(JPEG) == ("jpg", "image/jpeg")
    assert sniff_image(WEBP) == ("webp", "image/webp")
    assert sniff_image(b"#!/bin/sh\nrm -rf /") is None


def test_saving_an_avatar_keeps_the_bytes_out_of_the_database(service, bundle, store) -> None:  # type: ignore[no-untyped-def]
    """库里只留 key，图片本体在对象存储里。

    账号是每个页面都要读一次的东西；把一张几十 KB 的图塞进那张表，
    每次读账号都拖着一份二进制。
    """
    user = _user(store)

    key = service.save(user.id, PNG)

    assert key.startswith(f"avatars/{user.id}/") and key.endswith(".png")
    saved = store.get_user(user.id)
    assert saved is not None and saved.avatar_key == key
    # 本体取得到，类型是认出来的那个
    blob, media = service.read(user.id)
    assert blob == PNG
    assert media == "image/png"


def test_replacing_an_avatar_drops_the_previous_one(service, bundle, store) -> None:  # type: ignore[no-untyped-def]
    """换头像：新的 key 不同（内容寻址），**旧的那张删掉**。

    不删的话，换十次就在桶里留十张——而 key 里带着内容 hash，
    旧的永远不会再被引用到。
    """
    user = _user(store)

    first = service.save(user.id, PNG)
    second = service.save(user.id, JPEG)

    assert first != second
    # 旧的那张真的没了（本地实现抛 FileNotFoundError、对象存储抛 NotFoundError，
    # 两个都算"没了"——这条断言只关心结果）
    with pytest.raises((NotFoundError, FileNotFoundError)):
        bundle.objects.read(first)
    assert bundle.objects.read(second) == JPEG


def test_a_non_image_is_refused(service, store) -> None:  # type: ignore[no-untyped-def]
    user = _user(store)

    with pytest.raises(InvalidRequestError, match="不像一张图片"):
        service.save(user.id, b"<script>alert(1)</script>")


def test_an_oversized_image_is_refused(service, store) -> None:  # type: ignore[no-untyped-def]
    """上限挡的是"绕过界面直接传"：前端会先缩到 256px（通常几十 KB）。"""
    from app.services.avatars import MAX_AVATAR_BYTES

    user = _user(store)

    with pytest.raises(InvalidRequestError, match="太大"):
        service.save(user.id, PNG + b"\x00" * MAX_AVATAR_BYTES)


def test_clearing_goes_back_to_the_generated_avatar(service, bundle, store) -> None:  # type: ignore[no-untyped-def]
    """去掉头像：记录清空 + 文件删掉。**没有头像时也成功**——它要的是结果。"""
    user = _user(store)
    key = service.save(user.id, PNG)

    service.clear(user.id)

    saved = store.get_user(user.id)
    assert saved is not None and saved.avatar_key == ""
    with pytest.raises((NotFoundError, FileNotFoundError)):
        bundle.objects.read(key)
    with pytest.raises(NotFoundError):
        service.read(user.id)
    service.clear(user.id)  # 再来一次也不炸


def test_the_signed_url_binds_both_the_user_and_the_image(service, store) -> None:  # type: ignore[no-untyped-def]
    """签名绑的是"谁的哪张图"：换个 user id 用同一份签名取不到。

    不绑用户的话，端点会先去查"这个人有没有头像"——而那层校验本该由签名保证。
    """
    user = _user(store)
    key = service.save(user.id, PNG)
    # **重新读一遍**：save 改的是库里的那一行，手上这个 record 还是旧的
    # （生产那条路上，调用方拿到的本来就是刚读出来的记录）
    fresh = store.get_user(user.id)

    url, expires = service.url_for(fresh, secret="s3cret")

    assert url.startswith(f"/api/v1/avatars/{user.id}?") and expires > 0
    assert resource(user.id, key) != resource("user_other", key)


def test_without_a_signing_secret_there_is_no_url(service, store) -> None:  # type: ignore[no-untyped-def]
    """签不出就**不给链接**（返回空串），由调用方决定怎么降级——
    发一条永远有效的链接比"这次没头像"糟得多。"""
    user = _user(store)
    service.save(user.id, PNG)

    assert service.url_for(user, secret=None) == ("", 0)


def test_a_user_without_an_avatar_has_no_url(service, store) -> None:  # type: ignore[no-untyped-def]
    user = _user(store)

    assert service.url_for(user, secret="s") == ("", 0)
