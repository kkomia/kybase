"""凭据与口令原语（`core/security.py`）。

**为什么值得单测**：这里是"只存哈希、明文永不落库"的落点，而口令校验有一条
安全性质——**任何不匹配都返回 False**（格式错、哈希串损坏、口令不对，对调用方
是同一件事）。区分开等于给攻击者一个布尔预言机。交接文档把 core 层缺单测列为
质量缺口，本文件补上最要紧的几条。
"""

from __future__ import annotations

from app.core import security


def test_generated_api_keys_carry_the_prefix_and_enough_entropy() -> None:
    first = security.generate_token()
    second = security.generate_token()

    assert first.startswith(security.API_KEY_PREFIX)
    assert first != second
    # 32 字节随机 → base64url 后远长于 40；太短就说明熵不够
    assert len(first) > 40


def test_hash_token_is_deterministic_sha256_hex() -> None:
    token = security.generate_token()
    digest = security.hash_token(token)

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
    assert digest == security.hash_token(token)
    assert token not in digest


def test_tokens_equal_is_true_only_for_identical_strings() -> None:
    assert security.tokens_equal("abc123", "abc123") is True
    assert security.tokens_equal("abc123", "abc124") is False


def test_display_prefix_strips_the_key_prefix() -> None:
    token = f"{security.API_KEY_PREFIX}abcdefghij"

    assert security.display_prefix(token) == "abcdef"
    assert security.display_prefix(token, keep=3) == "abc"
    # 不带前缀的令牌（会话令牌等）也能取前缀
    assert security.display_prefix("zzzzzz") == "zzzzzz"


def test_api_key_prefix_is_rendered_for_humans() -> None:
    assert security.api_key_prefix(f"{security.API_KEY_PREFIX}abcdefghij") == "kylab_sk_abcdef…"


def test_session_tokens_have_their_own_prefix() -> None:
    token = security.generate_session_token()

    assert token.startswith(security.SESSION_TOKEN_PREFIX)
    assert not token.startswith(security.API_KEY_PREFIX)


def test_password_hashing_is_argon2_not_fast_hash() -> None:
    """口令必须慢哈希（本模块的 SHA-256 只许用于高熵凭据）。"""
    digest = security.hash_password("correct horse battery")

    assert digest.startswith("$argon2id$")
    assert digest != security.hash_token("correct horse battery")


def test_verify_password_accepts_only_the_right_one_and_never_raises() -> None:
    digest = security.hash_password("s3cret-pw")

    assert security.verify_password("s3cret-pw", digest) is True
    assert security.verify_password("wrong-pw", digest) is False
    # **不抛异常**：损坏的哈希串也只回 False——抛错等于告诉攻击者"这个账号的哈希坏了"
    assert security.verify_password("s3cret-pw", "not-a-hash") is False
    assert security.verify_password("s3cret-pw", "") is False
