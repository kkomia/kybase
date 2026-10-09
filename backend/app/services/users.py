"""使用者名册（调研报告 G6）。

**这是名册，不是账号体系。** 没有密码、没有邮箱、没有角色、没有团队。
使用场景是"局域网内几个人共用一台机器"，他们需要的是**知道是谁传的**，
而不是登录与权限。后者会带来组织架构、邀请、配额、审计一整套东西，
远超本项目要解决的问题（架构 §1 的定位）。

与凭据体系的关系：**两者刻意分开**
- 凭据（管理员会话 + 两档 API Key，见 §11.4）决定"能做什么"；
- 名册（本模块）记录"是谁做的"。

所以名册**不参与鉴权**：伪造一个名字不会让人获得任何权限，
只会让归属记错——而局域网内共用一台机器的几个人，本来也没有互相防范的需求。
这一点必须写清楚，否则后来的人会误以为这里是安全边界。
"""

from __future__ import annotations

import logging
import uuid

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.storage.base import StoreBundle, UserRecord

__all__ = ["OPERATOR_HEADER", "UserService"]

logger = logging.getLogger(__name__)

#: 前端用来声明"我是谁"的请求头。
#:
#: 用请求头而不是请求体字段：它要出现在**所有**写操作上（上传、建库、删块……），
#: 逐个接口加字段既啰嗦又容易漏；请求头加一次就全覆盖。
OPERATOR_HEADER = "X-Kylab-Operator"

MAX_NAME_CHARS = 32


class UserService:
    """名册的读写，以及"把请求头里的名字解析成使用者"。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 名册

    def create(self, *, name: str, note: str = "") -> UserRecord:
        cleaned = _clean_name(name)
        if not cleaned:
            raise InvalidRequestError("使用者名字不能为空")
        return self._stores.meta.create_user(
            UserRecord(id=f"user_{uuid.uuid4().hex[:12]}", name=cleaned, note=note.strip())
        )

    def get(self, user_id: str) -> UserRecord:
        record = self._stores.meta.get_user(user_id)
        if record is None:
            raise NotFoundError(f"使用者不存在：{user_id}")
        return record

    def list(self) -> list[UserRecord]:
        return self._stores.meta.list_users()

    def count_documents(self, user_id: str) -> int:
        return self._stores.meta.count_documents_by_user(user_id)

    def delete(self, user_id: str) -> None:
        """删使用者。

        **他传过的文档保留**，只把归属置空：文档已经进了知识库、已经向量化、
        可能已被引用——把使用者删掉就顺手删掉他的文档，那是数据丢失而不是
        权限撤销。界面上会显示"未记录"。
        """
        self.get(user_id)
        self._stores.meta.delete_user(user_id)

    # ------------------------------------------------------------------ 当前操作者

    def resolve_operator(self, token: str | None) -> UserRecord | None:
        """把请求头解析成使用者；解析不到就返回 ``None``。

        ``token`` 优先当 **id** 解析，解析不到再当名字试一次。这个顺序是必要的：

        **HTTP 头只能是 ASCII**，而使用者名字是中文。浏览器发
        ``X-Kylab-Operator: 小王`` 会直接抛错（实测 curl 侧报
        ``UnicodeEncodeError: 'ascii' codec can't encode``）——所以界面必须发
        ``user_xxx`` 这样的 id。但手写 curl 时发名字更自然，所以两种都收。

        不自动创建使用者：拼错一个字就会静默多出一个，名册很快就脏了。
        """
        if not token:
            return None
        raw = token.strip()
        if not raw:
            return None

        found = self._stores.meta.get_user(raw)
        if found is None:
            # 退回按名字解析（手写 curl 的场景）
            found = self._stores.meta.find_user_by_name(_clean_name(raw))
        if found is None:
            logger.info("请求头里的操作者「%s」不在名册里，本次不记归属", raw)
        return found


def _clean_name(name: str) -> str:
    """压平空白并截断。

    换行会让归属在列表里撑破行；过长则截断——名字是给人看的标签，
    不是需要精确保留的数据。
    """
    return " ".join(name.split())[:MAX_NAME_CHARS]
