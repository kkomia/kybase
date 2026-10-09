"""签名 URL 的签发与校验（M4 T4.5）。

镜像同构：``app/core/signing.py`` → ``tests/unit/core/test_signing.py``。

这一层是**唯一**守着"原文不被无限期下载"的地方，所以用例只钉安全性质：
篡改、过期、跨资源重放、缺密钥。
"""

from __future__ import annotations

import pytest

from app.core.signing import DEFAULT_TTL_SECONDS, SigningError, sign_resource, verify_resource

SECRET = "test-signing-secret"
RESOURCE = "document:doc_1:original"
NOW = 1_800_000_000


def test_valid_signature_passes() -> None:
    signature, expires = sign_resource(RESOURCE, SECRET, now=NOW)

    # 到期前一刻仍然有效
    verify_resource(RESOURCE, signature, expires, SECRET, now=expires - 1)


def test_signature_expires() -> None:
    """到期即失效——这正是"带过期时间"的意义。"""
    signature, expires = sign_resource(RESOURCE, SECRET, ttl_seconds=60, now=NOW)

    with pytest.raises(SigningError) as excinfo:
        verify_resource(RESOURCE, signature, expires, SECRET, now=expires + 1)

    assert "过期" in str(excinfo.value)


def test_tampered_signature_is_rejected() -> None:
    signature, expires = sign_resource(RESOURCE, SECRET, now=NOW)
    tampered = ("0" if signature[0] != "0" else "1") + signature[1:]

    with pytest.raises(SigningError) as excinfo:
        verify_resource(RESOURCE, tampered, expires, SECRET, now=NOW)

    assert "无效" in str(excinfo.value)


def test_signature_does_not_transfer_to_another_resource() -> None:
    """**一条签名只能换它签的那个东西。**

    否则拿到"下载 Markdown"链接的人，改一下路径就能下原文。
    """
    signature, expires = sign_resource(RESOURCE, SECRET, now=NOW)

    with pytest.raises(SigningError):
        verify_resource("document:doc_2:original", signature, expires, SECRET, now=NOW)


def test_markdown_and_original_are_different_resources() -> None:
    """格式必须参与签名：两者的内容是两回事。"""
    signature, expires = sign_resource("document:doc_1:markdown", SECRET, now=NOW)

    with pytest.raises(SigningError):
        verify_resource("document:doc_1:original", signature, expires, SECRET, now=NOW)


def test_extended_expiry_without_new_signature_is_rejected() -> None:
    """把过期时间往后改必须失败——否则"过期"形同虚设。"""
    signature, expires = sign_resource(RESOURCE, SECRET, ttl_seconds=60, now=NOW)

    with pytest.raises(SigningError):
        verify_resource(RESOURCE, signature, expires + 10_000, SECRET, now=NOW)


def test_wrong_secret_is_rejected() -> None:
    signature, expires = sign_resource(RESOURCE, SECRET, now=NOW)

    with pytest.raises(SigningError):
        verify_resource(RESOURCE, signature, expires, "another-secret", now=NOW)


def test_empty_secret_cannot_verify() -> None:
    """没有密钥时**必须拒绝**，不能退化成"空密钥也能通过"。

    那正是"系统还没配鉴权"时最容易被误当成"没有签名也能下载"的地方。
    """
    signature, expires = sign_resource(RESOURCE, SECRET, now=NOW)

    with pytest.raises(SigningError):
        verify_resource(RESOURCE, signature, expires, "", now=NOW)


def test_signature_is_stable_for_same_inputs() -> None:
    """同样的输入必须给同样的签名（无状态校验的前提）。"""
    a = sign_resource(RESOURCE, SECRET, ttl_seconds=60, now=NOW)
    b = sign_resource(RESOURCE, SECRET, ttl_seconds=60, now=NOW)
    assert a == b


def test_default_ttl_is_short() -> None:
    """默认有效期要短。链接会出现在聊天记录、日志、Referer 里。"""
    assert DEFAULT_TTL_SECONDS <= 900

    _, expires = sign_resource(RESOURCE, SECRET, now=NOW)
    assert expires == NOW + DEFAULT_TTL_SECONDS


def test_resource_parts_are_not_ambiguous() -> None:
    """拼接歧义在这里等于"一条签名能换到另一个资源"。

    ``"a"+"bc"`` 与 ``"ab"+"c"`` 直接拼是一样的，所以 payload 用长度前缀。
    """
    one = sign_resource("document:doc:1", SECRET, now=NOW)[0]
    two = sign_resource("document:doc:1:", SECRET, now=NOW)[0]
    assert one != two
