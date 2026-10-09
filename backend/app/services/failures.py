"""失败原因 → 用户能读懂的一句话（v0.53）。

**为什么单独一层**：对话流内报错的那句话会被前端**原样**显示
（``chat/ui/MessageView`` 的「这一轮没跑起来：{…}」），而异常原文是写给排错的人看的
——它带着端点地址、上游返回体、``httpx.ReadTimeout`` 这类类名（评审原话：
"那一行后面会是 Python 异常串（内部语言）"）。用户看不懂，也拿不到下一步动作。

**做法是"只从写死的句子里挑"，不是"过滤掉异常原文"**：过滤总有漏的（异常文本
可以是任意内容，包括上游返回体里伪装成人话的一句），而这一层一旦漏一次，
用户就又看到内部语言了。所以这里**从不把异常的字符串带进来**——``str(exc)``、
``exc.detail``、``exc.message`` 一次都不出现；异常原文照旧进服务端日志
（``api/v1/chat.py`` 里 ``logger.exception`` / ``logger.warning``）。

分档依据（``services/llm.py`` 的 ``ChatError`` 带这两样）：HTTP 状态码优先，
其次失败原因 ``reason``。两者都没有时给通用句——**宁可笼统，不可漏内部语言**。
"""

from __future__ import annotations

from app.core.exceptions import (
    EmbeddingError,
    EmbeddingNotConfiguredError,
    KylabError,
    UpstreamError,
)
from app.core.lazy_httpx import httpx
from app.services.llm import ChatError

__all__ = ["failure_text"]

#: 失败原因（``ChatError.reason``）→ 一句话。取值表见 ``services/llm.py``。
_BY_REASON: dict[str, str] = {
    "network_error": "连不上对话模型服务：请检查网络，以及「设置 → 模型注册」里的接口地址",
    "timeout": "对话模型响应超时：稍后重试；一直这样请检查接口地址，或换一个模型",
    "stream_idle_timeout": "对话模型中途卡住了（长时间没有返回任何内容）：可以重试，或换一个模型",
    "rate_limited": "对话模型限流或额度已经用尽：稍后重试，或到「设置 → 模型注册」检查账户额度",
    "server_error": "对话模型服务自己出错了：稍后重试；一直这样请报给管理员",
    "not_configured": "还没有配置对话模型：请到「设置 → 模型注册」选一个模型再试",
    "bad_response": (
        "对话模型返回的内容不是预期格式：换一个模型试试（当前这个可能不兼容 OpenAI 协议）"
    ),
    "empty_answer": (
        "对话模型一个字都没有返回：多半是提示词太长、或这个模型不支持当前的请求格式，"
        "请到「设置 → 模型注册」检查后重试"
    ),
    "thinking_only": (
        "对话模型只返回了思考过程、没有正文：把「深度思考」调低或关掉，或换一个非思考型模型"
    ),
}

#: HTTP 状态码 → 一句话。**只对能给出下一步动作的几类分行写**，其余走通用句。
_BY_STATUS: dict[int, str] = {
    401: "对话模型拒绝了这次调用（API Key 无效）：「设置 → 模型注册」里重新填一次密钥",
    403: "对话模型拒绝了这次调用（密钥没有权限）：请检查「设置 → 模型注册」里那把密钥的权限",
    404: "对话模型说找不到这个模型或地址：请检查模型 ID 与接口地址有没有写错",
    400: (
        "对话模型不接受这次请求：提示词可能太长，或这个模型不支持当前参数；"
        "缩短问题或换一个模型再试"
    ),
    413: "这次请求体太大了（对话模型拒收）：缩短问题、少带一些资料，或换一个模型再试",
    422: (
        "对话模型不接受这次请求：提示词可能太长，或这个模型不支持当前参数；"
        "缩短问题或换一个模型再试"
    ),
}

#: 模型调用失败、但没归类（没有状态码也没有原因）时的通用句。
_MODEL_FAILED = "对话模型调用失败：稍后重试；一直这样请到「设置 → 模型注册」检查模型与接口地址"

#: 非对话类的外部依赖（向量化、rerank、解析节点…）失败。
_UPSTREAM_FAILED = "外部服务调用失败（模型或向量化）：稍后重试；一直这样请到设置里检查模型配置"

_NOT_CONFIGURED_EMBEDDING = "还没有配置向量化模型：请到「设置 → 向量化」选定默认嵌入模型"

_EMBEDDING_FAILED = "向量化调用失败（检索这一环用不了）：稍后重试；一直这样请检查「设置 → 向量化」"

#: 领域异常（``KylabError`` 那一族）的 ``code`` → 一句话。
#:
#: **按类挑，不看 ``detail``**：领域异常的类默认文案是我们自己写的（"资源不存在"这类），
#: 但实例文案里有十几处把异常原文拼了进去（``f"读不了这个文件：{exc}"``）——两条在
#: 同一支字符串里，没法分辨，所以这一层一律不用它。而**全部退回通用句也不行**：
#: 那会把"参数不对"说成"服务端出错"，用户照着"稍后重试"试十次也没用。
#: 折中办法是**只按类型给动作**（这一类失败该怎么办），这也是用户真正需要的那半句。
_BY_CODE: dict[str, str] = {
    "not_found": "这条会话或它引用的东西已经不在了：刷新页面再试一次",
    "invalid_request": "这次请求不合法（多半是参数或模型配置不对）：检查后再试",
    "unauthorized": "凭据无效或已过期：重新登录后再试",
    "forbidden": "当前账号没有这个权限：换一个账号，或请联系管理员",
    "payload_too_large": "这次带的内容超出限额：减少资料或缩短问题再试",
    "conflict": "这次操作与当前状态冲突（别处刚改过）：刷新页面再试",
    "unsupported_content": "这个格式当前处理不了：换一种格式再试",
    "upstream_error": _UPSTREAM_FAILED,
}

#: 兜底。**故意笼统**：这里换不来更具体的动作，而任何"更具体"都要靠异常原文。
_GENERIC = "服务端出错了：请稍后重试；一直这样请联系管理员"


def failure_text(exc: BaseException) -> str:
    """``exc`` → 用户能读懂的一句话（**永远不含异常原文**）。

    顺序有讲究：``ChatError`` 先于通用的 ``UpstreamError``（它是子类，带状态码与原因，
    能给出更具体的处置），``EmbeddingNotConfiguredError`` 先于 ``EmbeddingError``
    （没配模型不是调用失败，动作是"去选一个模型"而不是"重试"）。
    """
    if isinstance(exc, ChatError):
        return _chat_text(exc)
    if isinstance(exc, EmbeddingNotConfiguredError):
        return _NOT_CONFIGURED_EMBEDDING
    if isinstance(exc, EmbeddingError):
        return _EMBEDDING_FAILED
    if isinstance(exc, httpx.TimeoutException):
        return _BY_REASON["timeout"]
    if isinstance(exc, httpx.TransportError):
        # 连接层失败里除了超时都是"没通上"：与 ``llm._transport_error`` 同一套分法
        return _BY_REASON["network_error"]
    if isinstance(exc, (UpstreamError, httpx.HTTPError)):
        return _UPSTREAM_FAILED
    if isinstance(exc, KylabError):
        # **不用 ``exc.detail``**：领域异常的文案多数是我们自己写的，但有几处
        # 把异常原文拼了进去（``f"读不了这个文件：{exc}"`` 这类，全仓库十几处）。
        # 逐处去修是另一件事；这一层要能保证"经它出口的话一定没有人话之外的东西"，
        # 所以只认**类型**（``code`` → 这一类该怎么办），类型也没有就给通用句。
        return _BY_CODE.get(exc.code, _GENERIC)
    return _GENERIC


def _chat_text(exc: ChatError) -> str:
    """对话模型失败：状态码优先，其次失败原因，都没有就给通用句。

    **不看 ``exc.message`` 不会丢信息**：我们自己抛出的每一条文案，在 ``_BY_REASON`` /
    ``_BY_STATUS`` 里都有一档对应（``llm.py`` 与 ``chat.py`` 里所有 ``ChatError`` 的
    ``reason`` 都在表里）。所以**新增一个 ``reason`` 时要同时补这里的一档**——
    漏了不会出错，只会让那句失败退成通用句。
    """
    if exc.status is not None:
        known = _BY_STATUS.get(exc.status)
        if known is not None:
            return known
        if 500 <= exc.status < 600:
            return _BY_REASON["server_error"]
    reason = _BY_REASON.get(exc.reason)
    if reason is not None:
        return reason
    return _MODEL_FAILED
