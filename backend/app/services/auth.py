"""登录会话与账号引导（v10：名册升级为账号体系）。

**为什么会有这个模块**：产品面向不懂技术的个人用户，"粘贴令牌"这条路对他们
不可用。v0.11 起控制台令牌整条取消，账号是**唯一**的管理员身份来源。这里提供常规的
用户名+密码登录：口令 argon2 慢哈希入库，登录换会话令牌，令牌**哈希存表**
（与 API Key 同一纪律：明文只在响应里出现一次）。

四处刻意的决定：

1. **会话存库而不是无状态 HMAC**。项目里签名 URL 是无状态的，但登录会话有
   "退出登录"与"改密后吊销其他登录"这两个明确需求——无状态令牌做不到吊销。
   代价是每次请求一次查库，本地部署的规模下这无关紧要。

2. **滑动续期**。7 天硬性过期会让"天天用的用户"每周被踢一次；每次用到就
   把过期时间推后 7 天，只有真的搁置一周才需要重新登录。

3. **限流在内存里**。同一用户名连续失败 5 次锁 60 秒。重启清零是可接受的：
   这防的是局域网/家里的口令爆破，不是分布式攻击；引入持久化计数反而
   把简单的事做重。已知的残余风险：按 username 锁定意味着攻击者可以对
   ``admin`` 连错 5 次把本人锁 60 秒（骚扰性 DoS）——威胁模型是防爆破
   不是防 DoS，接受它；真要防得按来源 IP 锁，那是部署层的事。

4. **setup 一次性**。仅当"没有任何带 username 的账号"时开放，首个账号即管理员
   并认领全部无主老数据（v10 之前的库/会话 owner 都是 NULL）。
   设过即关闭：**只在没有任何账号时开放**，之后永久关闭。
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.exceptions import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.security import (
    generate_session_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.enums import UserRole
from app.storage.base import MetaStore, SessionRecord, StoreBundle, UserRecord

__all__ = ["SESSION_TTL", "AuthService", "LoginResult", "ensure_url_signing_secret"]

logger = logging.getLogger(__name__)

#: 会话有效期（滑动）：每次用到都推后这么长。
SESSION_TTL = timedelta(days=7)

#: 同一用户名连续失败这么多次后锁定 ``_LOCKOUT_SECONDS`` 秒。
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60

#: 口令最小长度。对个人用户不设复杂度规则（大小写/数字/符号那套）：
#: 研究表明长度比复杂度更能抗爆破，而复杂度规则只会逼人用便签纸。
MIN_PASSWORD_CHARS = 8

#: 签名密钥在 app_settings 里的键。setup 时生成：下载签名从此不再依赖
#: 下载签名密钥。首次 setup 生成一次并落库，之后长期不变——
#: 凭据会轮换，而签出去的链接不该跟着失效。
URL_SIGNING_SECRET_SETTING = "auth.url_signing_secret"  # noqa: S105


def ensure_url_signing_secret(meta: MetaStore) -> str:
    """把那条下载签名密钥准备好，回它的值（**幂等**：已经有就不动）。

    两个档都要它，但入口不同：

    - **服务器档**：``POST /auth/setup`` 里调它（``AuthService.setup`` 的第一步），
      所以那一档的密钥从"首次初始化"来；
    - **本机档**：**没有任何初始化流程**（它不挂 ``/auth/*``，也就永远跑不到 setup），
      而它的 ``kylab.db`` 是全新的——不在组合根补这一下，那条键就永远是空的，
      于是"下载签名"这条线上的每一个端点都回 401（头像 / 笔记配图 / 文档下载 /
      产物预览），前端再把它当"登录失效"把人踢到登录页。

    **幂等是硬要求**：已有就原样返回，绝不每次启动换一把——换了的话，所有已经发出去
    的签名链接会一起失效（而"凭据轮换、链接不失效"正是这条键独立存在的原因）。
    """
    current = meta.get_setting(URL_SIGNING_SECRET_SETTING)
    if current:
        return str(current)
    generated = secrets.token_urlsafe(32)
    meta.set_setting(URL_SIGNING_SECRET_SETTING, generated)
    logger.info("已生成下载签名密钥并落库（这条键原来没有：本机档由组合根补，服务器档来自初始化）")
    return generated


#: 哑哈希：用户不存在/没有口令时也拿它跑一遍 argon2，把"查无此人"与"口令错误"
#: 拉到同一耗时——否则响应时间会泄露"这个用户名存在"（时序侧信道的用户枚举）。
#: 首次用到时才算（argon2 一次约几十毫秒），免得拖慢 import。
_dummy_hash: str | None = None


def _dummy_verify(password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("dummy-password-for-timing")
    verify_password(password, _dummy_hash)


#: 限流表的上限。每个试过的 username（含不存在的）都留一条记录，
#: spraying 大量随机用户名会缓慢撑内存；到顶就把已过锁定期的清掉。
_MAX_FAILED_ENTRIES = 10_000

#: 滑动续期的最小间隔：last_seen 距今不足这么长就跳过写库。
#: 否则每次请求都搭一次 UPDATE，纯属浪费——续期的语义不变。
_TOUCH_MIN_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """登录/初始化的结果。``token`` 是**唯一一次**出现的会话明文。"""

    token: str
    user: UserRecord


class AuthService:
    """账号引导、登录、会话校验与口令管理。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores
        # 登录限流：username → (连续失败次数, 锁定到这个 monotonic 时间点)
        self._failed: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------ 状态

    def has_accounts(self) -> bool:
        """是否已有任何可登录账号（username 非空）。"""
        return any(user.username for user in self._stores.meta.list_users())

    # ------------------------------------------------------------------ 引导

    def setup(self, *, username: str, password: str, name: str | None = None) -> LoginResult:
        """首次初始化：创建管理员账号并认领无主老数据。

        认领发生在建号之后、发会话之前：老数据（v10 之前的库与会话）owner 是
        NULL，不认领的话管理员自己也看不到它们——"升级后数据全没了"是最伤
        信任的故障。
        """
        if self.has_accounts():
            # 409 而不是 403：这不是权限问题，是状态问题——已经初始化过了
            raise ConflictError("已有账号，初始化入口已关闭。如需新增账号请找管理员")
        cleaned = self._clean_username(username)
        self._check_password(password)

        user = self._stores.meta.create_user(
            UserRecord(
                id=f"user_{uuid.uuid4().hex[:12]}",
                name=(name or cleaned).strip() or cleaned,
                username=cleaned,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
            )
        )
        claimed = self._stores.meta.claim_legacy_ownership(user.id)
        if claimed["knowledge_bases"] or claimed["conversations"]:
            logger.info(
                "老数据已认领给首个管理员：知识库 %d 个、会话 %d 条",
                claimed["knowledge_bases"],
                claimed["conversations"],
            )
        self._ensure_signing_secret()
        logger.warning("已创建管理员账号「%s」：此后 /api/v1 需要登录", cleaned)
        return self._issue_session(user)

    def _ensure_signing_secret(self) -> None:
        """下载签名密钥独立落库：它是密钥，不该复用任何用户凭据。

        生成那一段在 :func:`ensure_url_signing_secret` 里（本机档的组合根调的是**同一个**
        函数）：两处各写一遍 token_urlsafe 就是两个"这把钥匙怎么来"的答案，
        而它们迟早会漂（一处换了长度、另一处没换，签名就再也验不过）。
        """
        ensure_url_signing_secret(self._stores.meta)

    # ------------------------------------------------------------------ 登录

    def login(self, *, username: str, password: str) -> LoginResult:
        cleaned = self._clean_username(username)
        self._check_lockout(cleaned)

        user = self._stores.meta.find_user_by_username(cleaned)
        # 用户不存在、没有口令（纯名册条目）、口令不对：对外都是同一句，
        # 区分开等于告诉攻击者"这个用户名存在"（用户枚举）。
        # 查不到人也要跑一遍慢哈希（_dummy_verify）：否则响应时间会泄露同一件事
        if user is None or user.password_hash is None:
            _dummy_verify(password)
            self._record_failure(cleaned)
            raise UnauthorizedError("用户名或密码不正确")
        if not verify_password(password, user.password_hash):
            self._record_failure(cleaned)
            raise UnauthorizedError("用户名或密码不正确")
        if user.disabled:
            raise UnauthorizedError("该账号已被禁用，请联系管理员")

        self._failed.pop(cleaned, None)
        return self._issue_session(user)

    def _issue_session(self, user: UserRecord) -> LoginResult:
        token = generate_session_token()
        self._stores.meta.create_session(
            SessionRecord(
                id=hash_token(token),
                user_id=user.id,
                expires_at=datetime.now(UTC) + SESSION_TTL,
            )
        )
        return LoginResult(token=token, user=user)

    # ------------------------------------------------------------------ 会话校验

    def authenticate_session(self, token: str) -> tuple[UserRecord, SessionRecord]:
        """把会话令牌换成账号；无效即抛 401。校验通过即滑动续期。"""
        session = self._stores.meta.get_session(hash_token(token.strip()))
        now = datetime.now(UTC)
        if session is None or (session.expires_at and session.expires_at <= now):
            raise UnauthorizedError("登录已失效，请重新登录")

        user = self._stores.meta.get_user(session.user_id)
        if user is None or user.disabled or user.username is None:
            # 账号被删/被禁/被降级成名册条目：会话一并作废，不留"幽灵登录"
            self._stores.meta.delete_session(session.id)
            raise UnauthorizedError("账号不可用，请重新登录")

        # 滑动续期，但距上次写库不足一小时就跳过：每次请求都 UPDATE 纯属浪费
        if session.last_seen_at is None or now - session.last_seen_at >= _TOUCH_MIN_INTERVAL:
            self._stores.meta.touch_session(
                session.id, last_seen_at=now, expires_at=now + SESSION_TTL
            )
        return user, session

    def logout(self, session_id: str) -> None:
        self._stores.meta.delete_session(session_id)

    # ------------------------------------------------------------------ 口令

    def change_password(
        self, user: UserRecord, *, old_password: str, new_password: str, keep_session_id: str
    ) -> int:
        """改口令，并吊销该账号的**其他**会话（当前这条保住）。

        返回吊销了几条——界面据此告诉用户"其他 N 处登录已退出"。
        """
        # 旧口令校验也要限流：否则拿到一条有效会话（共享机器、肩窥）就能
        # 无限爆破旧口令再改密夺号。计数按 user.id，与登录的按 username 分开
        lockout_key = f"pwd:{user.id}"
        self._check_lockout(lockout_key)
        if not user.password_hash or not verify_password(old_password, user.password_hash):
            self._record_failure(lockout_key)
            raise UnauthorizedError("原密码不正确")
        self._check_password(new_password)
        self._stores.meta.update_user_password(user.id, hash_password(new_password))
        return self._stores.meta.delete_sessions_for_user(
            user.id, except_session_id=keep_session_id
        )

    # ------------------------------------------------------------------ 账号管理（管理员）

    def create_account(
        self,
        *,
        name: str,
        username: str,
        password: str,
        role: UserRole = UserRole.MEMBER,
        note: str = "",
    ) -> UserRecord:
        """管理员开通账号。

        初始密码由管理员设定、用户首次登录后自己改（``change_password``）。
        不强制"首次登录必须改密"：那是企业合规需求，家庭/小团队场景里
        只会成为"所有人都用同一个初始密码"的温床。
        """
        cleaned = self._clean_username(username)
        self._check_password(password)
        return self._stores.meta.create_user(
            UserRecord(
                id=f"user_{uuid.uuid4().hex[:12]}",
                name=name.strip() or cleaned,
                note=note.strip(),
                username=cleaned,
                password_hash=hash_password(password),
                role=role,
            )
        )

    def reset_password(self, user_id: str, new_password: str) -> int:
        """管理员重置密码，并吊销该账号的**全部**会话（含正在用的那条）。

        与 ``change_password`` 的区别：那是本人改自己的（保住当前会话），
        这是管理员处置——重置往往因为"原密码可能泄露了"，留着的会话都得死。
        返回吊销了几条。
        """
        user = self._stores.meta.get_user(user_id)
        if user is None or user.username is None:
            raise NotFoundError(f"账号不存在：{user_id}")
        self._check_password(new_password)
        self._stores.meta.update_user_password(user.id, hash_password(new_password))
        return self._stores.meta.delete_sessions_for_user(user.id)

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        """禁用/启用账号。禁用时吊销全部会话——不能等它自然过期。

        **最后一个可用的管理员不能被禁用**：禁完就没有人能进设置页，
        只能靠改库恢复——那是最难向用户解释的一类死锁。
        """
        user = self._stores.meta.get_user(user_id)
        if user is None:
            raise NotFoundError(f"账号不存在：{user_id}")
        if disabled and user.role is UserRole.ADMIN:
            other_admins = [
                item
                for item in self._stores.meta.list_users()
                if item.role is UserRole.ADMIN
                and not item.disabled
                and item.username is not None
                and item.id != user_id
            ]
            if not other_admins:
                raise ConflictError("这是最后一个可用的管理员账号，不能禁用")
        self._stores.meta.set_user_disabled(user_id, disabled)
        if disabled:
            revoked = self._stores.meta.delete_sessions_for_user(user_id)
            logger.warning("账号 %s 已禁用，吊销会话 %d 条", user_id, revoked)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _clean_username(username: str) -> str:
        """登录名归一化：小写 + 压空白。

        大小写不敏感是给不懂技术的用户的：`Admin` 与 `admin` 在他们眼里是
        同一个人，而 SQLite 的唯一索引是字节精确的（存储层保持精确是刻意的，
        见 base.py 的 find_user_by_username）。
        """
        cleaned = username.strip().lower()
        if not cleaned:
            raise InvalidRequestError("用户名不能为空")
        return cleaned

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_CHARS:
            raise InvalidRequestError(f"密码至少 {MIN_PASSWORD_CHARS} 个字符")

    def _check_lockout(self, username: str) -> None:
        fails, locked_until = self._failed.get(username, (0, 0.0))
        if fails >= _MAX_FAILED_ATTEMPTS and time.monotonic() < locked_until:
            wait = int(locked_until - time.monotonic()) + 1
            raise UnauthorizedError(f"尝试次数过多，请 {wait} 秒后再试")

    def _record_failure(self, username: str) -> None:
        # 到顶先清已过锁定期的条目：spraying 随机用户名会把这张表慢慢撑大
        if len(self._failed) >= _MAX_FAILED_ENTRIES:
            now = time.monotonic()
            self._failed = {key: value for key, value in self._failed.items() if value[1] > now}
        fails, _ = self._failed.get(username, (0, 0.0))
        fails += 1
        locked_until = time.monotonic() + _LOCKOUT_SECONDS if fails >= _MAX_FAILED_ATTEMPTS else 0.0
        self._failed[username] = (fails, locked_until)
        if fails >= _MAX_FAILED_ATTEMPTS:
            logger.warning(
                "用户名「%s」连续登录失败 %d 次，锁定 %d 秒", username, fails, _LOCKOUT_SECONDS
            )
