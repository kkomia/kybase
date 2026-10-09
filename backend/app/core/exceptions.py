"""统一领域异常与错误响应映射。

分层约定（工程规范 §3.3）：``api/`` 与 ``mcp_server/`` 不写业务判断；
业务错误一律以本模块的领域异常从 ``services/`` 抛出，由处理器统一转成响应体。
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class KylabError(Exception):
    """领域异常基类。"""

    code = "internal_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "服务内部错误"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.detail = message or self.message


class NotFoundError(KylabError):
    """资源不存在。"""

    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND
    message = "资源不存在"


class ConflictError(KylabError):
    """状态冲突，如重复入库、非法状态迁移。"""

    code = "conflict"
    http_status = status.HTTP_409_CONFLICT
    message = "资源冲突"


class InvalidRequestError(KylabError):
    """请求参数不合法。"""

    code = "invalid_request"
    # 直接写字面量：starlette 在新版本里把 HTTP_422_UNPROCESSABLE_ENTITY 改名了
    http_status = 422
    message = "请求参数不合法"


class BadRequestError(InvalidRequestError):
    """请求本身就不对（**400 那一档**，M5 阶段 1 加的）。

    与父类只差状态码，``code`` 仍是 ``invalid_request``（《API 接口规范》§1.3 那两档
    本来就是同一个码的两种状态：422 是"字段校验没过"，400 是"请求形状对、内容与事实对不上"）。
    用它的是备份上传那两条：**sha256 / bytes 与实收不符**（服务端边收边算，对不上就拒收）、
    以及路径段与清单归属那类"这个请求描述的东西本身不成立"。

    为什么非要 400 而不是 422：422 的语义是"把字段改对再来"，而这两种情况调用方把字段
    改对也没用——它得**重新打包、重新算哈希**（或换一条路径），那是一步不同的动作。
    """

    http_status = status.HTTP_400_BAD_REQUEST


class UnsupportedContentError(KylabError):
    """请求的东西现在拿不到（还没解析完、格式不支持）。

    与 ``NotFoundError`` 分开：文件在、只是"还没到时候"，或者参数不受支持——
    回 404 会让调用方以为文档不见了，进而去重新上传。
    """

    code = "unsupported_content"
    http_status = status.HTTP_409_CONFLICT
    message = "该内容当前不可用"


class PayloadTooLargeError(KylabError):
    """上传内容超过限额。

    单列一类而不是并进 ``InvalidRequestError``：413 对调用方的含义是
    "东西太大，切分/压缩再试"，422 是"请求本身写错了"——下一步动作不同。
    用 ``HTTP_413_CONTENT_TOO_LARGE`` 而不是旧的 ``REQUEST_ENTITY_TOO_LARGE``：
    后者在当前 starlette 里已弃用，会在每次请求时打一条 DeprecationWarning。
    """

    code = "payload_too_large"
    http_status = status.HTTP_413_CONTENT_TOO_LARGE
    message = "上传内容超过限额"


class UpstreamError(KylabError):
    """外部依赖出错（解析节点、embedding、对话模型）。

    单列一类是因为**处置方式不同**：它几乎总是"凭据不对 / 额度用尽 / 服务抖动"，
    用户看到文案就知道该去设置页还是该重试；混进 500 里就只剩一句"服务内部错误"。
    """

    code = "upstream_error"
    http_status = status.HTTP_502_BAD_GATEWAY
    message = "外部服务调用失败"


class EmbeddingError(Exception):
    """向量化失败。带 ``stage`` 便于状态机把失败定位到具体步骤。

    **刻意不是 ``KylabError``**（搬到 core/ 时保持原样）：它由 embedding 提供方抛出、
    被 ingest / retrieval / queue_worker 就地捕获并改写成任务失败状态，
    改基类会同时改掉异常信封与各处 ``except`` 的行为。

    **为什么住在这里而不是 ``services/embedding/base.py``**（2026-10-08 剥离阶段 0）：
    Agent 侧的 `services/failures.py` 要把"向量化失败"翻成用户能读懂的一句话，
    而它是**按类型**分档的（``EmbeddingNotConfiguredError`` 先于 ``EmbeddingError``）——
    类型搬走就翻不了，复制一份又会让 ``isinstance`` 落空。
    故按本模块既有的那条约定（``SecretStoreUnavailable`` 同一个理由）：
    **领域异常一律住这里**，``services/embedding/base.py`` 再导出一次，调用点不用改。
    """

    def __init__(self, message: str, *, stage: str = "embedding") -> None:
        super().__init__(message)
        self.stage = stage


class EmbeddingNotConfiguredError(EmbeddingError):
    """**没有可用的嵌入模型**（是本机配置缺失，不是调用失败）。

    单列一类是因为处置方式完全不同：重试没有意义，正确动作是去设置里选模型。
    调用方据此选择"拒绝建库"或"跳过向量通道"，而不是把它当成上游抖动反复重试，
    更不是退回一个无语义的兜底实现。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, stage="config")


class SecretStoreUnavailable(KylabError):
    """系统钥匙串现在用不上（M5 阶段 6，方案 §4.1 的第二条口径）。

    **503 而不是 500**：这台机器**没有**系统钥匙串（Linux 桌面 / 容器里 / CI），
    这是"这个部署现在没有这个能力"，不是"我们出错了"——500 会把"凭据只能留在本机库里"
    这条出路藏起来。

    它同时是**写不进去**时的失败信号：``SecretStore.set`` 在不可用时抛它，
    而调用方（迁移器、设置写入路径）**绝不因此把秘密写回明文**——写不了就如实报，
    让用户决定（方案 §4.3-5：不清明文 = 不算迁完）。

    为什么住在这里而不是 ``services/secrets.py``：本仓的领域异常一律住这个模块
    （见文件头那条分层约定），而"HTTP 状态码 + code"这套信封也在这里——服务层只抛类，
    协议层不写映射。``services/secrets.py`` 会再导出一次，方案里那个落点仍然成立。
    """

    code = "secret_store_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "系统钥匙串当前不可用"


class SecretTooLarge(InvalidRequestError):
    """一个秘密超过钥匙串单条的容量上限（2560 字节）——**如实拒，绝不截断**。

    截断是最坏的一种处置：它写进去一个"看起来存下来了"的半个钥匙，而用户要等到
    某次调用失败才会发现。422 的语义（"把内容改对再来"）在这里正好：用更短的那种令牌。

    用它的主要是 MCP 那类"整段 env / headers"的值（方案 §4.2 把它们列为"只登记不迁"，
    技术理由之一就是这条上限）。
    """

    code = "secret_too_large"
    message = "这个秘密超过钥匙串单条的容量上限"


class ServiceUnavailableError(KylabError):
    """这个部署现在**没有这个能力**——**不是**"你的凭据不对"，也不是"我们出错了"。

    503 的语义与 401 的语义在这里必须分开（这一条是它存在的全部理由）：

    - **401** 的意思是"你是谁我不知道 / 你的凭据不对"，前端据此**跳登录页**——那是设计；
    - 而"这台机器还没有下载签名密钥"是**我们这边没配好**：用户没做错任何事，把他踢到
      登录页只会让他以为账号出了问题（桌面壳里实测到的那次就是这样：点一张产物卡片的
      预览，人直接被弹到登录页）。
    - 也不是 500：500 会把"缺一件事、补上/重试就好"这条出路藏起来。

    与 ``KnowledgeBaseUnavailable`` / ``SecretStoreUnavailable`` 是同一档、不同起因，
    所以 code 用更笼统的 ``service_unavailable``：调用方按 503 这一档处理（提示 + 重试），
    具体缺什么写在 message 里。
    """

    code = "service_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "这个部署现在没有这个能力"


class UnauthorizedError(KylabError):
    """未提供凭据或凭据无效。

    与 ``ForbiddenError`` 分开：401 表示"你是谁我不知道"（该去拿凭据），
    403 表示"我知道你是谁，但你不能碰这个"（凭据没错，是授权范围不够）。
    这两句给用户的下一步动作完全不同，混成一个状态码就说不清了。
    """

    code = "unauthorized"
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "缺少或无效的凭据"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message)
        # RFC 7235：401 必须带 WWW-Authenticate，否则客户端不知道该用哪种方案
        self.headers = {"WWW-Authenticate": 'Bearer realm="kylab"'}


class ForbiddenError(KylabError):
    """凭据有效，但授权范围不够（权限不足或越界访问其他知识库）。"""

    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN
    message = "凭据权限不足"

    def __init__(
        self, message: str | None = None, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.headers = headers or {}


#: 未走领域异常的旁路（FastAPI 自带的请求校验 422、Starlette 自己的 404/405 等）
#: 也要折成同一形状，否则对接方按 ``code`` 分支会拿到 undefined。
_STATUS_TO_CODE: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "invalid_request",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    502: "upstream_error",
}


def _validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 的第一条校验错误折成一句人话。

    默认响应体是 ``{"detail": [{"loc": [...], "msg": "..."}]}``——形状不对，
    而且前端只读 ``message``。这里取第一条并带上字段路径，够定位即可；
    不把整个数组塞进 message（那会变成一坨 JSON 文本）。
    """
    errors = exc.errors()
    if not errors:
        return InvalidRequestError.message
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", InvalidRequestError.message))
    return f"{location}: {message}" if location else message


def register_exception_handlers(app: FastAPI) -> None:
    """把领域异常注册为统一的 JSON 错误响应。"""

    @app.exception_handler(KylabError)
    async def _handle_kylab_error(_: Request, exc: KylabError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    # 知识库不可用 → 503（M2 §2.2「知识库不可用的如实报错」）。
    #
    # **提供者不可用时也走它**（M3 阶段 3）：本机档的入库整条走提供者客户端，而
    # "没配 / 连不上 / 被拒"在那边一律折成 `KnowledgeBaseUnavailable`（见
    # `services/knowledge_provider.py` 的两条口径）——两个面（HTTP 端点与工具循环）
    # 共用这一个 503 信封，不另开一套错误语义。
    #
    # **惰性 import，不在模块级**：`app.storage.sqlite_impl.meta_store` 反过来 import 本模块
    # 拿 `ConflictError`（"storage → core"这个方向本来就存在），模块级 import 回去会把
    # 两边的依赖拉成双向的，而双向依赖的下一次改动就是循环 import。
    #
    # **503 而不是 500**：本机档没有知识库数据源（在 NAS 上），
    # 这是"这个部署现在没有这个能力"，不是"我们出错了"——500 会把"去设置里看「知识库连接」/
    # 去服务器上做"这条出路藏起来。**也不是空结果**：`Unavailable*Store` 抛异常正是为了不假装查过。
    from app.storage.split_impl import KnowledgeBaseUnavailable

    @app.exception_handler(KnowledgeBaseUnavailable)
    async def _handle_knowledge_base_unavailable(
        _: Request, exc: KnowledgeBaseUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "code": "knowledge_base_unavailable",
                "message": str(exc) or "知识库当前不可用",
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=InvalidRequestError.http_status,
            content={"code": InvalidRequestError.code, "message": _validation_message(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 覆盖 FastAPI 默认的 ``{"detail": ...}``：包括未匹配到路由的 404。
        # 状态码映射不出来的（极少）退回 internal_error，但**保留原始文案**，
        # 免得一句"服务内部错误"把真正的原因吞掉。
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": _STATUS_TO_CODE.get(exc.status_code, "internal_error"),
                "message": str(exc.detail),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """**兜底：没被认领的异常也要留下完整栈**（D16，2026-09-29 实测的洞）。

        之前 500 只回一句 ``Internal Server Error``、日志里**一条 traceback 都没有** ——
        "报错但没有栈"等于没法排障（那次排查只能靠排除法，代价很大）。
        这里只补日志，响应形状与 FastAPI 默认那条保持一致，
        免得下游解析器因为换形状而二次受伤。
        """
        logger.exception("服务端未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500, content={"code": "internal_error", "message": "服务端出错了"}
        )
