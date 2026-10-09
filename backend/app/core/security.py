"""凭据的生成与校验原语（《架构设计 v0.2》§3.2）。

只放"纯函数"级别的密码学动作，不碰存储、不碰请求——密钥怎么存、谁能用哪把钥匙，
那是 ``services/api_key.py`` 与 ``api/deps.py`` 的事（分层纪律 §3.3）。

三个刻意决定：

1. **只存哈希，明文永不落库**。发出去的明文只在创建响应里出现一次，
   之后库里只有摘要。这是凭据类数据的底线。

2. **用 SHA-256 而不是 bcrypt/argon2**。慢哈希是为了对抗"低熵口令被离线爆破"；
   API Key 是 32 字节随机数（256 位熵），爆破空间大到慢哈希没有意义，
   反而每次请求都要付几十毫秒。主流做法也是快速哈希（Dify / RAGFlow 同）。
   **不得**把本模块的 ``hash_token`` 用到用户口令上。

3. **比较用 ``compare_digest``**。虽然我们是"哈希后查库"、并不逐字节比密钥，
   但校验旧格式/调试路径时若出现直接比较，也必须定时安全，免得留下计时侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = [
    "API_KEY_PREFIX",
    "SESSION_TOKEN_PREFIX",
    "api_key_prefix",
    "display_prefix",
    "generate_session_token",
    "generate_token",
    "hash_password",
    "hash_token",
    "tokens_equal",
    "verify_password",
]

#: 明文前缀。**带前缀不是为了好看**：一串无特征的高熵字符串在日志、截图、
#: 粘贴板里没人认得出来，有了前缀才有可能被密钥扫描工具（gitleaks 之类）识别，
#: 也方便用户一眼看出"这是 KYLAB 的钥匙、不是别的服务的"。
API_KEY_PREFIX = "kylab_sk_"

#: 随机部分长度（字节）。32 字节 = 256 位熵。
_TOKEN_BYTES = 32


def generate_token() -> str:
    """生成一把新的 API Key 明文（只在创建时出现一次）。"""
    return API_KEY_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """凭据摘要。返回值进库，明文不落盘。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """定时安全比较（需要直接比明文时用，例如校验 API Key 的摘要）。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def display_prefix(token: str, *, keep: int = 6) -> str:
    """取用于界面展示的前缀（``ab12cd``）。

    这一段会被存进库，好让列表里能分辨"哪把是哪把"。它取自 32 字节随机串，
    留在外面的位数远不足以缩小搜索空间，**不是秘密泄露**。
    """
    body = token[len(API_KEY_PREFIX) :] if token.startswith(API_KEY_PREFIX) else token
    return body[:keep]


def api_key_prefix(token: str) -> str:
    """把明文渲染成给人看的样子（``kylab_sk_ab12…``）。

    只在创建响应里用得到——列表走的是库里存的 ``key_prefix``。
    """
    return f"{API_KEY_PREFIX}{display_prefix(token)}…"


# --------------------------------------------------------------------------- 口令与会话（v10）

#: 登录会话令牌的前缀。**按前缀路由凭据类型**（api/auth.py 的 current_caller）：
#: 会话与 API Key 是两种东西，各走各的校验路径。
#: noqa 的理由：这是**前缀**不是口令，S105 按变量名含 token 误报。
SESSION_TOKEN_PREFIX = "kylab_st_"  # noqa: S105

#: 模块级单例：PasswordHasher 的参数在构造时定死，实例本身无状态，反复 new 没意义。
_password_hasher = PasswordHasher()


def generate_session_token() -> str:
    """生成一条新的登录会话令牌明文（只在登录响应里出现一次，库里只存它的哈希）。"""
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def hash_password(password: str) -> str:
    """口令哈希（argon2id）。

    **口令与凭据是两种东西**：API Key / 会话令牌是 256 位随机数，快速哈希足够；
    口令是人选的、熵低，必须用慢哈希抗离线爆破——所以这里不用本模块的
    ``hash_token``（SHA-256），那个函数只许用于高熵凭据。
    """
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。**任何不匹配都返回 False**：格式错、哈希串损坏、口令不对，
    对调用方都是同一件事——"不能登录"。区分开等于给攻击者布尔预言机。
    """
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
