"""请求级鉴权依赖（《架构设计 v0.2》§3.2）。

两种凭据，一个入口：

- **登录会话**（``kylab_st_`` 前缀）：Web 控制台用，用户名 + 密码换来；
- **API Key**：给外部程序用，绑定知识库范围 + 只读/读写。

**API Key 还有第三维：工具范围**（v0.60，``allowed_tool_names``）。它只作用于那些
**与某个 MCP 工具 1:1 的端点**（``_TOOL_BY_ROUTE`` 那张表）——其余端点仍然只受
只读/读写档 + 库范围约束，边界与理由写在表头上。判定本身在服务层
（``ApiKeyService.check_tool``），这里只负责查表。

为什么"进控制台"必须是一种比 API Key 更高的身份，而不是同一把钥匙的另一个权限档：
设置页就是 embedding / LLM 密钥的落点，而 ``base_url`` 可改——给了外部 API Key
就等于给了它一条"把你的 Bearer Token 转发到我的服务器"的路。所以管理员身份只能
来自登录会话里的角色。

三处刻意的决定：

1. **鉴权永远生效**（v0.11 取消控制台令牌与"没配凭据就放行"）。
   第一次打开时唯一能调的是 ``GET /auth/status`` 与 ``POST /auth/setup``——
   先建管理员账号，再谈别的；在此之前任何业务请求都 401。
   上一版在"还没配任何凭据"时把请求放行成本机控制台，而"还没配"与"忘了配"
   从代码上无法区分，等于"无账号即裸奔"。
2. **只在 ``/api/v1`` 下生效**。健康探针（``/health``）不鉴权：
   前者是容器编排用来判断存活的，带上鉴权会让 readiness 探针误判。
3. **失败快、文案钝**。缺凭据与凭据无效都回 401，且不区分原因（见 api_key.py）。

**本机档那条短路已随边车一起拆除**（v0.1.0 瘦身）：这个服务只有服务器档一个形态
（它就是知识库本体），所以"缺凭据即 401"全量生效、没有例外。

**签名密钥的两个入口（``signing_secret`` / ``signing_secret_or_raise``）不在这里实现，
只在这里 re-export**（v0.61 搬到 ``services/signing.py``）：服务层的 MCP 工具也要
签发下载链接，而业务层不得反向依赖协议层——共用实现只能住在 ``services/``，
端点这一侧的名字与行为保持不变。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, Request

from app.core.config import API_VERSION
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.services import Services, get_services
from app.models.enums import ApiKeyPermission
from app.services.api_key import READ, WRITE, Caller, resolve_caller
from app.services.signing import signing_secret, signing_secret_or_raise

logger = logging.getLogger(__name__)

__all__ = [
    "AdminDep",
    "CallerDep",
    "ReadDep",
    "WriteDep",
    "check_kb_scope",
    "check_tool_scope",
    "current_caller",
    # 签名密钥的两个入口住在 ``services/signing.py``（服务层也要用，见那里的模块头）。
    # 这两行是**刻意的 re-export**：端点原先就从这里 import，名字不变、行为不变。
    "signing_secret",
    "signing_secret_or_raise",
]


def _bearer(authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer <token>`` 里取令牌。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def current_caller(
    services: Annotated[Services, Depends(get_services)],
    authorization: Annotated[str | None, Header()] = None,
) -> Caller:
    """解析调用主体。**没有凭据就是 401**，不存在"未启用鉴权"这条支路。

    **本机档那条短路已随边车一起拆除**（v0.1.0 瘦身）：这个服务只有服务器档一个形态
    （它就是知识库本体），所以"缺凭据仍然 401"是全量口径，没有例外。
    """
    token = _bearer(authorization)
    if token is None:
        raise UnauthorizedError(
            "缺少凭据：请在请求头带上 Authorization: Bearer <会话令牌或 API Key>"
        )

    # 分流与校验都在服务层 `resolve_caller` 里：**MCP 走同一份判定**。
    # 两处各写一份的话，"同一串令牌在一个入口是管理员、在另一个是匿名"
    # 这类不一致不会有任何测试能同时看到。
    return resolve_caller(services, token)


CallerDep = Annotated[Caller, Depends(current_caller)]


def _check(
    services: Services,
    caller: Caller,
    *,
    need: ApiKeyPermission,
    kb_ids: list[str] | None = None,
) -> None:
    services.api_keys.check_access(caller, need=need, kb_ids=kb_ids)


#: REST 路由（模板）→ MCP 工具名：**与某个工具 1:1 的那几个端点**。
#:
#: **为什么只有这几条**：工具范围管的是**工具面**（``services/tools.py`` 那张表）。
#: 没有对应工具的 REST 端点（改名、移动、任务、统计、分享、回收站……）也就无从谈
#: "工具范围"——它们仍然只受 **只读 / 读写档 + 库范围**约束，行为与从前一字不差。
#: 这条边界是刻意的：工具是发给外部集成的最小面，拿它去管控制台的每一个动作，
#: 只会让这张表长成第二套权限系统——两套判定必然相漂，而漂出来的形态是越权。
#:
#: **键要带 HTTP 方法**：同一路径上往往是两个工具——``/knowledge-bases`` 的 GET 是
#: ``list_knowledge_bases``、POST 是 ``create_knowledge_base``；``/documents/{id}``
#: 的 GET 与 DELETE 同理。只看路径分不出来。
#:
#: **路径按不带 ``/api/v1`` 前缀写**：FastAPI 0.141 起 ``scope["route"]`` 挂的是子
#: 路由自己的 ``APIRoute``（见 ``fastapi.routing`` 的 ``_handle_selected``），
#: ``include_router`` 那一层前缀不在它的 ``path`` 里；``_tool_for`` 两种形状都认。
#:
#: 批量端点（``documents/batch``）归到 ``upload_document``：它一次能做多件事，
#: 工具面上没有对应物，而"能传文档"的那把钥匙才用得上这个入口——
#: 把"一个端点映射成多个工具"那类规则放进这里只会让人读不懂这张表。
_TOOL_BY_ROUTE: dict[tuple[str, str], str] = {
    ("POST", "/search"): "search",
    ("GET", "/knowledge-bases"): "list_knowledge_bases",
    ("POST", "/knowledge-bases"): "create_knowledge_base",
    ("GET", "/knowledge-bases/{kb_id}/documents"): "list_documents",
    ("POST", "/knowledge-bases/{kb_id}/documents"): "upload_document",
    ("POST", "/knowledge-bases/{kb_id}/documents/batch"): "upload_document",
    ("GET", "/documents/{document_id}"): "get_document_status",
    ("DELETE", "/documents/{document_id}"): "delete_document",
    # 取内容的那两条（v0.61）：阅读视角与签发下载链接——它们就是 ``get_document_content``
    # 在 REST 上的两个入口，所以归到同一个工具。**``/documents/{id}/content`` 不在表里**：
    # 它不挂 ``require_read``（只认 URL 里的签名），而这张表只在依赖里生效——
    # 写进来也是一条永远走不到的死条目。
    ("GET", "/documents/{document_id}/preview"): "get_document_content",
    ("GET", "/documents/{document_id}/download-url"): "get_document_content",
    # 按页取原文与强制重解析（2026-10-10）：``get_document_pages`` 与 ``reparse_document``
    # 在 REST 上的入口。**页图那条（``/documents/{id}/pages/{页}/image``）与本表无关**：
    # 与 ``/content`` 同一个理由，它不挂 ``require_read``（只认 URL 里的签名），写进来
    # 也是一条永远走不到的死条目。
    ("GET", "/documents/{document_id}/pages"): "get_document_pages",
    ("POST", "/documents/{document_id}/reparse"): "reparse_document",
    ("POST", "/knowledge-bases/{kb_id}/data-sources"): "add_data_source",
}

_API_PREFIX = f"/api/{API_VERSION}"


def _tool_for(request: Request) -> str | None:
    """这次请求对应哪个工具；没有对应物（或拿不到路由）返回 ``None``。

    **用路由模板而不是原始路径**：路径里带 ``{kb_id}`` 这类参数，拿原始路径就得分
    段正则，而模板是框架已经算好的那一个——参数是什么都不影响查表。
    路由还没匹配上时（``scope["route"]`` 缺失）返回 ``None``：那时无从知道这是哪个
    端点，**宁可不管也不能猜**（猜错的另一个方向就是放行）。
    """
    path = getattr(request.scope.get("route"), "path", None)
    if not isinstance(path, str) or not path:
        return None
    if path.startswith(_API_PREFIX):
        # 老版本 FastAPI 给的是含 include 前缀的那份；这里只在多出来时削掉它
        path = path[len(_API_PREFIX) :] or "/"
    return _TOOL_BY_ROUTE.get((request.method, path))


def check_tool_scope(services: Services, caller: Caller, request: Request) -> None:
    """按"路由 → 工具"那张表判工具范围；表外的端点不受它影响。

    判定本身在服务层（``ApiKeyService.check_tool``），这里只负责**找到工具名**：
    "这次请求算哪个工具"是协议层的事——服务层不认识路由。

    ``visible_tool_names`` 返回 ``None``（管理员 / 登录成员会话 / 空范围的 API Key）
    就直接放行：**"谁不受这一维约束"只在服务层判一次**，这里再判一遍就是第二份副本。
    """
    if services.api_keys.visible_tool_names(caller) is None:
        return
    tool = _tool_for(request)
    if tool is not None:
        services.api_keys.check_tool(caller, tool)


def require_read(
    request: Request,
    services: Annotated[Services, Depends(get_services)],
    caller: CallerDep,
) -> Caller:
    """只读端点。"""
    _check(services, caller, need=READ)
    check_tool_scope(services, caller, request)
    return caller


def require_write(
    request: Request,
    services: Annotated[Services, Depends(get_services)],
    caller: CallerDep,
) -> Caller:
    """写端点（上传、建库、删除、改设置）。"""
    _check(services, caller, need=WRITE)
    check_tool_scope(services, caller, request)
    return caller


def require_admin(caller: CallerDep) -> Caller:
    """管理员专属端点（设置页、模型注册、API Key 管理、用户管理）。

    **必须是管理员会话**，不能只要求"读写权限"：
    这些端点里是 embedding / rerank / LLM 的密钥与外部服务地址，而地址可改——
    给了外部 API Key 就等于给它一条转发凭据的路。
    """
    if not caller.is_admin:
        raise ForbiddenError(
            "该操作需要管理员权限：外部 API Key 不能读写服务端凭据配置"
            "（否则改掉 base_url 就能截获你的 API Key）"
        )
    return caller


ReadDep = Annotated[Caller, Depends(require_read)]
WriteDep = Annotated[Caller, Depends(require_write)]
AdminDep = Annotated[Caller, Depends(require_admin)]


def check_kb_scope(
    services: Services,
    caller: Caller,
    kb_ids: list[str] | None,
    *,
    need: ApiKeyPermission = READ,
) -> None:
    """带库范围的作用域判定。

    不做成依赖，是因为它需要**已解析的请求体或路径参数**，而依赖拿不到；
    硬要拿就得把 body 模型也声明成依赖，签名会重复一遍。

    ``need``：写端点必须传 ``WRITE``——成员拿到的分享可能是只读档，
    只按"看得见"判定会让只读分享变成可写（回归测试钉在 test_visibility_api）。
    """
    services.api_keys.check_access(caller, need=need, kb_ids=kb_ids)
