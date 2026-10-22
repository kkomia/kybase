"""鉴权的 HTTP 层行为（M4 T4.4；v0.11 起账号是唯一的管理员身份）。

镜像同构：``app/api/auth.py`` + ``app/api/v1/api_keys.py`` →
``tests/integration/api/test_auth_api.py``。

**这份用例存在的首要理由**是外部监测报告指出的那条凭据窃取路径：
``PATCH /settings`` 若能由外部密钥调用，局域网内任何人就能把
``embedding.base_url`` 改成自己的服务器，下一次 embed 调用就会把 API Key
以 ``Authorization: Bearer`` 发过去。所以"外部 API Key 进不了设置端点"
必须有一条测试钉着，而不是只靠代码里的一句注释。

v0.11 起还有第二条契约：**鉴权永远生效**。没有控制台令牌，也没有
"还没配凭据所以先放行"——第一次打开时唯一能调的是 ``/auth/status`` 与
``/auth/setup``，其余一律 401。

## NAS 网页端退役之后的改动（2026-10-05）

``/settings`` 那一族**只在旧版存在**了（NAS 上那份设置页随网页端退役），
所以这一份里三处随它改了：

- "哪些端点要 401" 那张参数表里的 ``/settings`` 换成一条同样是服务器档、同样要凭据的
  （2026-10-09 精简后是 ``/api/v1/api-keys``：``/settings`` 与 ``/memory`` 都已不在
  这个服务上了）；
- ``test_external_api_key_cannot_touch_settings`` **摘掉**：那条风险现在**结构性不存在**
  ——能用 API Key 的那一档（服务器）没有 ``/settings``，有 ``/settings`` 的那一档
  （本机）不认 API Key（主体恒为"本机主人"，见 `api/auth.py::current_caller`）。
  理由写在原来那条用例的位置上；
- ``test_setup_then_login_then_me`` 里那处"管理员会话能进设置页"换成能进
  ``/api/v1/users``（同一件事：管理员会话在服务器档管用的那类端点）。

v0.60 起这里还钉**工具范围**：一把 API Key 除了库范围与权限档，还能限"能调哪些工具"
（`allowed_tool_names`，空 = 不受限）。它只作用于**与某个 MCP 工具 1:1 的端点**，
那张映射表与边界写在 ``app/api/auth.py`` 的 ``_TOOL_BY_ROUTE`` 上。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.enums import ApiKeyPermission
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.conftest import admin_client as admin_session


@pytest.fixture
def app_client():
    """**没有任何凭据**的客户端：用于首次初始化与"缺凭据会被拒"两组用例。"""
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def locked():
    """已初始化管理员、请求自带会话凭据的客户端。

    名字沿用旧版（"鉴权锁上之后"）；现在**每个**实例都是锁上的，
    区别只在于有没有凭据。
    """
    with admin_session() as test_client:
        yield test_client


def _setup_admin(client: TestClient, **overrides) -> dict:
    body = {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    body.update(overrides)
    response = client.post("/api/v1/auth/setup", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _issue(client: TestClient, **overrides) -> dict:
    body = {
        "name": "给 Grafana 的只读钥匙",
        "permission": ApiKeyPermission.READONLY.value,
        "knowledge_base_ids": [],
    }
    body.update(overrides)
    response = client.post("/api/v1/api-keys", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- 默认口径



def test_health_stays_open_even_when_locked(locked: TestClient) -> None:
    """健康探针不鉴权：容器编排靠它判断存活，401 会让 readiness 误判。"""
    assert locked.get("/api/v1/health").status_code == 200


# --------------------------------------------------------------------- 缺凭据


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/knowledge-bases"),
        # 这几条都是**服务器档的服务端端点**（同样要求凭据）。
        # 原先这里还有 `/api/v1/settings` 与 `/api/v1/memory`：前者的设置页随 NAS 网页端
        # 退役、后者（记忆）随后端精简整体拆除——两条都不在这张表里了，
        # 换一条同样要凭据的：`/api/v1/wiki/...` 那一族由库路径参数决定，
        # 所以用 `/api/v1/api-keys` 这种无路径参数的清单端点。
        ("get", "/api/v1/api-keys"),
        ("get", "/api/v1/tasks"),
        ("get", "/api/v1/stats/dashboard"),
        ("get", "/api/v1/stats/search-hits"),
    ]
    )
def test_missing_credentials_are_rejected(
    app_client: TestClient, method: str, path: str
) -> None:
    """**没有凭据就是 401，初始化之前也一样**（v0.11 取消了"没配凭据就放行"）。

    用没有任何凭据的客户端：新实例上这些端点也必须先拒绝，
    否则"还没建账号"就等于"谁都能读写"。
    """
    response = getattr(app_client, method)(path)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    # RFC 7235：401 要带 WWW-Authenticate，否则客户端不知道用哪种方式
    assert "WWW-Authenticate" in response.headers


def test_bad_token_is_rejected(locked: TestClient) -> None:
    # 用 ASCII：HTTP 头只能承载 latin-1，中文会先被 httpx 拒掉，测不到服务端行为
    response = locked.get(
        "/api/v1/knowledge-bases", headers={"Authorization": "Bearer not-a-real-key"}
    )
    assert response.status_code == 401


def test_non_bearer_scheme_is_rejected(locked: TestClient) -> None:
    response = locked.get("/api/v1/knowledge-bases", headers={"Authorization": "Basic xyz"})
    assert response.status_code == 401


# --------------------------------------------------------------------- 凭据窃取路径


# **摘掉一条**（2026-10-05）：``test_external_api_key_cannot_touch_settings``
# （原判据：readwrite 的外部 API Key 读 / 写 / 试连 ``/settings`` 全是 403，
# 从而封住"把 ``embedding.base_url`` 指到自己的服务器、下一次 embed 调用就把用户的
# API Key 发过去"那条路）。
#
# **为什么在旧版没有意义、在服务器档也不存在这个面**：那条风险的落点是**服务器档的
# `/settings`**（外部密钥能打进来、而服务器持模型凭据）。NAS 网页端退役之后
# ``/settings`` **只在旧版**（那张 ``app_settings`` 表在本机 SQLite 里），而旧版
# **不认 API Key**——它不挂 `/auth/*` 与 `/api-keys`，调用主体由
# `api/auth.py::current_caller` 短路成"本机主人"，压根没有"外部密钥"这个身份。
# 换句话说这条路径**结构性消失**了：能用 API Key 的那一档没有设置端点，
# 有设置端点的那一档没有 API Key。
#
# 两边的判定都还在代码里（`api/auth.py::check_access` 的"管理员会话"那一档，
# 与 `api/v1/settings.py` 上的 `WriteDep`），覆盖它们的用例：
# `test_api_key_cannot_self_issue`、`test_api_key_cannot_list_users`（本文件，
# 走仍然挂在服务器档的端点）与 `tests/unit/services/test_api_key.py`。


def test_api_key_cannot_self_issue(locked: TestClient) -> None:
    """能签发钥匙的接口如果也能被钥匙打开，一把只读密钥就能给自己发一把读写密钥。"""
    issued = _issue(locked)
    external = {"Authorization": f"Bearer {issued['token']}"}

    assert locked.get("/api/v1/api-keys", headers=external).status_code == 403
    assert (
        locked.post("/api/v1/api-keys", json={"name": "提权"}, headers=external).status_code == 403
    )
    assert locked.delete(f"/api/v1/api-keys/{issued['id']}", headers=external).status_code == 403


# --------------------------------------------------------------------- 权限档位


def test_readonly_key_cannot_create_knowledge_base(locked: TestClient) -> None:
    issued = _issue(locked, permission=ApiKeyPermission.READONLY.value)
    response = locked.post(
        "/api/v1/knowledge-bases",
        json={"name": "偷偷建的库"},
        headers={"Authorization": f"Bearer {issued['token']}"}
    )
    assert response.status_code == 403


def test_readonly_key_can_read(locked: TestClient) -> None:
    issued = _issue(locked)
    response = locked.get(
        "/api/v1/knowledge-bases", headers={"Authorization": f"Bearer {issued['token']}"}
    )
    assert response.status_code == 200


# --------------------------------------------------------------------- 作用域


def test_listing_only_shows_knowledge_bases_in_scope(locked: TestClient) -> None:
    """列表也要过滤：否则光看名字就能探出这台机器上有哪些库。"""
    locked.post("/api/v1/knowledge-bases", json={"name": "公开库"})
    locked.post("/api/v1/knowledge-bases", json={"name": "机密库"})

    all_kbs = locked.get("/api/v1/knowledge-bases").json()["items"]
    assert len(all_kbs) == 2
    target = next(kb for kb in all_kbs if kb["name"] == "公开库")

    issued = _issue(locked, knowledge_base_ids=[target["id"]])
    scoped = {"Authorization": f"Bearer {issued['token']}"}

    visible = locked.get("/api/v1/knowledge-bases", headers=scoped).json()["items"]
    assert [kb["name"] for kb in visible] == ["公开库"]


def test_scoped_key_cannot_read_another_kb(locked: TestClient) -> None:
    locked.post("/api/v1/knowledge-bases", json={"name": "公开库"})
    locked.post("/api/v1/knowledge-bases", json={"name": "机密库"})
    all_kbs = locked.get("/api/v1/knowledge-bases").json()["items"]
    public = next(kb for kb in all_kbs if kb["name"] == "公开库")
    secret = next(kb for kb in all_kbs if kb["name"] == "机密库")

    issued = _issue(locked, knowledge_base_ids=[public["id"]])
    scoped = {"Authorization": f"Bearer {issued['token']}"}

    assert locked.get(f"/api/v1/knowledge-bases/{public['id']}", headers=scoped).status_code == 200
    assert locked.get(f"/api/v1/knowledge-bases/{secret['id']}", headers=scoped).status_code == 403


def test_scoped_key_cannot_search_another_kb(locked: TestClient) -> None:
    """越界检索要 403。

    两个库都是**真实创建**的，不用编造的 id：若拿一个不存在的库去测，
    "被拒"可能是因为库不存在而不是因为越界——那样即使范围判定坏掉了，
    用例照样是绿的（我第一版就是这么写的，属于会骗人的测试）。
    """
    locked.post("/api/v1/knowledge-bases", json={"name": "公开库"})
    locked.post("/api/v1/knowledge-bases", json={"name": "机密库"})
    all_kbs = locked.get("/api/v1/knowledge-bases").json()["items"]
    public = next(kb["id"] for kb in all_kbs if kb["name"] == "公开库")
    secret = next(kb["id"] for kb in all_kbs if kb["name"] == "机密库")

    issued = _issue(locked, knowledge_base_ids=[public])
    scoped = {"Authorization": f"Bearer {issued['token']}"}

    allowed = locked.post(
        "/api/v1/search", json={"query": "任意", "kb_ids": [public]}, headers=scoped
    )
    assert allowed.status_code == 200

    blocked = locked.post(
        "/api/v1/search", json={"query": "任意", "kb_ids": [secret]}, headers=scoped
    )
    assert blocked.status_code == 403
    assert secret in blocked.json()["message"], "报错要点出是哪个库，用户才配得回来"


# --------------------------------------------------------------------- 明文只出现一次


def test_plaintext_token_only_in_creation_response(locked: TestClient) -> None:
    issued = _issue(locked)
    token = issued["token"]
    assert token.startswith("kylab_sk_")

    listing = locked.get("/api/v1/api-keys").json()["items"]
    assert len(listing) == 1

    # 列表里既没有明文也没有摘要，只有展示前缀
    assert token not in str(listing)
    assert "key_hash" not in listing[0]
    assert listing[0]["prefix"].startswith("kylab_sk_")
    assert len(listing[0]["prefix"]) < len(token)


def test_revoked_key_stops_working(locked: TestClient) -> None:
    issued = _issue(locked)
    scoped = {"Authorization": f"Bearer {issued['token']}"}
    assert locked.get("/api/v1/knowledge-bases", headers=scoped).status_code == 200

    assert locked.delete(f"/api/v1/api-keys/{issued['id']}").status_code == 204
    assert locked.get("/api/v1/knowledge-bases", headers=scoped).status_code == 401









# --------------------------------------------------------------------- 账号体系（v10）


def _setup_admin(client: TestClient, **overrides) -> dict:
    body = {"username": "admin", "password": "correct horse battery"}
    body.update(overrides)
    response = client.post("/api/v1/auth/setup", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_status_reports_needs_setup(app_client: TestClient) -> None:
    """前端据此决定显示"首次设置管理员"还是"登录"。"""
    assert app_client.get("/api/v1/auth/status").json()["needs_setup"] is True

    _setup_admin(app_client)

    assert app_client.get("/api/v1/auth/status").json()["needs_setup"] is False


def test_setup_then_login_then_me(app_client: TestClient) -> None:
    setup = _setup_admin(app_client, username="Admin", name="测试用户")
    assert setup["token"].startswith("kylab_st_")
    assert setup["user"]["role"] == "admin"
    # 用户名归一化为小写；显示名保留原样
    assert (setup["user"]["username"], setup["user"]["name"]) == ("admin", "测试用户")

    # 会话令牌立即可用，且管理员会话能进**管理员专属**的那些端点
    # （原来是 `/api/v1/settings`——它随 NAS 网页端退役只在旧版了，换 `/api/v1/users`：
    #  同一类"只有管理员会话打得开"的服务器端点）
    session = {"Authorization": f"Bearer {setup['token']}"}
    assert app_client.get("/api/v1/users", headers=session).status_code == 200
    assert app_client.get("/api/v1/auth/me", headers=session).json()["username"] == "admin"

    # 退出后令牌作废
    assert app_client.post("/api/v1/auth/logout", headers=session).status_code == 204
    assert app_client.get("/api/v1/auth/me", headers=session).status_code == 401

    # 重新登录（大小写不敏感）
    login = app_client.post(
        "/api/v1/auth/login", json={"username": "ADMIN", "password": "correct horse battery"}
    )
    assert login.status_code == 200, login.text


def test_setup_is_one_shot_over_http(app_client: TestClient) -> None:
    _setup_admin(app_client)
    again = app_client.post(
        "/api/v1/auth/setup", json={"username": "second", "password": "another good password"}
    )
    assert again.status_code == 409


def test_login_rejects_wrong_password_with_uniform_message(app_client: TestClient) -> None:
    _setup_admin(app_client)

    wrong = app_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    ghost = app_client.post(
        "/api/v1/auth/login", json={"username": "ghost", "password": "wrong-password"}
    )
    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json()["message"] == ghost.json()["message"]



def test_change_password_over_http(app_client: TestClient) -> None:
    setup = _setup_admin(app_client)
    session = {"Authorization": f"Bearer {setup['token']}"}

    changed = app_client.post(
        "/api/v1/auth/password",
        json={"old_password": "correct horse battery", "new_password": "new-horse-battery"},
        headers=session
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revoked_sessions"] == 0  # 只有这一条会话，没有别的可吊销

    # 当前会话仍然有效；旧口令已不能登录
    assert app_client.get("/api/v1/auth/me", headers=session).status_code == 200
    assert (
        app_client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "correct horse battery"}
        ).status_code
        == 401
    )


# --------------------------------------------------------------------- 工具范围（v0.60）


def _scoped(issued: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued['token']}"}


def _upload(client: TestClient, kb_id: str, name: str) -> dict:
    """管理员传一份文档（用来造出"有真实 id 的文档"给按 id 的端点用）。

    ``start=false``：不把摄入任务真的跑起来——这条用例验的是鉴权，不是流水线。
    """
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, b"# " + name.encode(), "text/markdown")},
        params={"start": "false"},
    )
    assert response.status_code == 202, response.text
    return response.json()["document"]


def test_tool_scope_restricts_a_key_to_its_tools(locked: TestClient) -> None:
    """限了工具范围的钥匙，范围外的那一类端点必须 403，范围内的照常。

    **正反两面都要查**：只查"被拒"的话，它可能只是被别的判定顺手挡住了——
    那样这条用例在工具范围整个失效之后仍然是绿的（会骗人的测试）。
    """
    kb = locked.post("/api/v1/knowledge-bases", json={"name": "工具范围库"}).json()
    issued = _issue(locked, permission="readwrite", allowed_tool_names=["search"])
    scoped = _scoped(issued)

    allowed = locked.post(
        "/api/v1/search", json={"query": "任意", "kb_ids": [kb["id"]]}, headers=scoped
    )
    assert allowed.status_code == 200, allowed.text

    blocked = locked.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("偷渡.md", b"# hello", "text/markdown")},
        headers=scoped,
    )
    assert blocked.status_code == 403
    assert "upload_document" in blocked.json()["message"]


def test_every_mapped_route_resolves_to_its_tool(locked: TestClient) -> None:
    """**"路由 → 工具"那张映射表要真的被走到**：逐条打一遍，一条都不能漏。

    为什么值得一条条打：映射靠**路由模板**（``request.scope["route"].path``）匹配，
    而它的形状随框架版本变过（含不含 ``include_router`` 的那层前缀）。哪天它变了，
    只测一两条用例可能仍然绿着——那张表会退化成"只有一两条有效"。

    这条同时是"表里的模板写得对不对"的检查：路径写错一条，那一条就会放行（红）。
    """
    kb = locked.post("/api/v1/knowledge-bases", json={"name": "映射表库"}).json()
    document = _upload(locked, kb["id"], "映射表文档.md")
    # 范围只留 search，其余映射全部落在范围外
    scoped = _scoped(_issue(locked, permission="readwrite", allowed_tool_names=["search"]))

    cases: list[tuple[str, str, str, dict]] = [
        (
            "create_knowledge_base",
            "post",
            "/api/v1/knowledge-bases",
            {"json": {"name": "偷偷建的库"}},
        ),
        ("list_knowledge_bases", "get", "/api/v1/knowledge-bases", {}),
        ("list_documents", "get", f"/api/v1/knowledge-bases/{kb['id']}/documents", {}),
        (
            "upload_document",
            "post",
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            {"files": {"file": ("再传一份.md", b"# x", "text/markdown")}},
        ),
        (
            "upload_document",
            "post",
            f"/api/v1/knowledge-bases/{kb['id']}/documents/batch",
            {"json": {"action": "disable", "document_ids": [document["id"]]}},
        ),
        ("get_document_status", "get", f"/api/v1/documents/{document['id']}", {}),
        ("delete_document", "delete", f"/api/v1/documents/{document['id']}", {}),
        # 取内容那两条（v0.61）：只限了 search 的钥匙**不该能把文档正文拉走**——
        # REST 上它们是 ``get_document_content`` 的入口，与 MCP 那边同一个闸门
        ("get_document_content", "get", f"/api/v1/documents/{document['id']}/preview", {}),
        (
            "get_document_content",
            "get",
            f"/api/v1/documents/{document['id']}/download-url",
            {},
        ),
        # 按页取原文与强制重解析（2026-10-10）：同样是"某个工具在 REST 上的入口"
        (
            "get_document_pages",
            "get",
            f"/api/v1/documents/{document['id']}/pages",
            {"params": {"from_page": 1, "to_page": 1}},
        ),
        ("reparse_document", "post", f"/api/v1/documents/{document['id']}/reparse", {}),
        (
            "add_data_source",
            "post",
            f"/api/v1/knowledge-bases/{kb['id']}/data-sources",
            {"json": {"kind": "rss", "url": "https://example.com/feed.xml"}},
        ),
    ]

    for tool, method, path, kwargs in cases:
        response = getattr(locked, method)(path, headers=scoped, **kwargs)
        assert response.status_code == 403, f"{method.upper()} {path} 没被工具范围挡住"
        assert tool in response.json()["message"], f"{path} 被拒的理由不是工具范围"


def test_tool_scope_covers_the_page_and_reparse_routes(locked: TestClient) -> None:
    """按页取原文与强制重新解析（2026-10-10）也受工具范围约束。

    **两条都是某个工具在 REST 上的入口**：``GET /documents/{id}/pages`` 是
    ``get_document_pages``（只读），``POST /documents/{id}/reparse`` 是
    ``reparse_document``（写）。它们都挂了 ``require_read`` / ``require_write``，
    所以 ``_TOOL_BY_ROUTE`` 那张表在这里真的走得到——**不登记就是一个绕过口**。

    页图那条（``GET /documents/{id}/pages/{页}/image``）刻意不查：它只认 URL 里的
    签名（同 ``/documents/{id}/content``），压根不在这张表里，登记了也走不到。

    正反两面都查（同上面那条的理由）：只查"被拒"的话，它可能只是被别的判定顺手
    挡住了——映射整个失效之后这条用例仍然是绿的。
    """
    kb = locked.post("/api/v1/knowledge-bases", json={"name": "新能力库"}).json()
    document = _upload(locked, kb["id"], "新能力文档.md")
    pages_params = {"from_page": 1, "to_page": 1}
    pages_path = f"/api/v1/documents/{document['id']}/pages"
    reparse_path = f"/api/v1/documents/{document['id']}/reparse"

    # 反面：范围只留 search，这两条都落在范围外
    blocked = _scoped(_issue(locked, permission="readwrite", allowed_tool_names=["search"]))
    reparse = locked.post(reparse_path, headers=blocked)
    assert reparse.status_code == 403, reparse.text
    assert "reparse_document" in reparse.json()["message"]

    pages = locked.get(pages_path, params=pages_params, headers=blocked)
    assert pages.status_code == 403, pages.text
    assert "get_document_pages" in pages.json()["message"]

    # 正面：把工具真放开，两条都要走得通（否则"被拒"可能只是被别的判定挡住了）
    write_opened = _scoped(
        _issue(locked, permission="readwrite", allowed_tool_names=["reparse_document"])
    )
    requeued = locked.post(reparse_path, headers=write_opened)
    assert requeued.status_code == 202, requeued.text
    assert requeued.json()["document"]["stage"] == "uploaded", "重解析要真的复位并重新排队"

    read_opened = _scoped(_issue(locked, allowed_tool_names=["get_document_pages"]))
    assert locked.get(pages_path, params=pages_params, headers=read_opened).status_code == 200


def test_unscoped_key_is_unaffected_by_the_tool_scope(locked: TestClient) -> None:
    """**空工具范围 = 不受限**：老钥匙与"只限了库范围"的钥匙行为与从前一字不差。"""
    kb = locked.post("/api/v1/knowledge-bases", json={"name": "照常的库"}).json()
    issued = _issue(locked, permission="readwrite")
    scoped = _scoped(issued)

    assert issued["allowed_tool_names"] == []
    assert locked.get("/api/v1/knowledge-bases", headers=scoped).status_code == 200
    created = locked.post(
        "/api/v1/knowledge-bases", json={"name": "再建一个"}, headers=scoped
    )
    assert created.status_code == 201
    assert (
        locked.post(
            "/api/v1/search", json={"query": "任意", "kb_ids": [kb["id"]]}, headers=scoped
        ).status_code
        == 200
    )


def test_tool_scope_does_not_touch_unmapped_endpoints(locked: TestClient) -> None:
    """**表外的端点不受工具范围影响**——这条边界是刻意的。

    ``GET /knowledge-bases/{id}`` 没有对应的工具，所以只限了 ``search`` 的钥匙照样
    读得到它。工具范围管的是"工具面"（发给外部集成的最小面），不是控制台的每个动作；
    真拿它去管全部端点，它就长成了第二套权限系统——两套判定必然相漂。
    """
    kb = locked.post("/api/v1/knowledge-bases", json={"name": "详情库"}).json()
    issued = _issue(locked, allowed_tool_names=["search"])

    detail = locked.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=_scoped(issued))
    assert detail.status_code == 200, detail.text


def test_unknown_tool_name_is_rejected_at_creation(locked: TestClient) -> None:
    """非法工具名要**在发钥匙的时候就拒**。

    存下来的后果不是"这把钥匙少一个工具"，而是"用户以为授权成功了、实际一次都调不动"
    ——报错要到使用时才出现，中间还隔着一次配置。2026 年另一处同类问题是
    "名单只有一处"那条纪律的反面：这里宁可当场报错，也不猜他想要哪个。
    """
    response = locked.post(
        "/api/v1/api-keys",
        json={
            "name": "拼错的钥匙",
            "permission": "readonly",
            "allowed_tool_names": ["search", "搜一下"],
        },
    )

    assert response.status_code == 422, response.text
    message = response.json()["message"]
    assert "搜一下" in message, "要点出到底哪个名字不认识"
    assert "search" in message, "要把可用的名字列出来，用户才改得对"
    # 拒绝就是**一把都没发**，不会留下"看起来发成了"的半截状态
    assert locked.get("/api/v1/api-keys").json()["items"] == []


def test_tool_scope_is_echoed_in_the_listing(locked: TestClient) -> None:
    """列表要回显工具范围：不回显的话，用户看不出"这把钥匙为什么调不动某个工具"，
    而这类问题只会表现为"某个客户端偶尔报错"，最难查。
    """
    issued = _issue(locked, allowed_tool_names=["search", "list_documents"])

    assert issued["allowed_tool_names"] == ["search", "list_documents"]
    listed = locked.get("/api/v1/api-keys").json()["items"][0]
    assert listed["allowed_tool_names"] == ["search", "list_documents"]



