"""**NAS 网页端退役**之后"哪张表上有什么"的回归钉（2026-10-05）。

这一份是那一轮收尾的**判据本身**：退役只改了"挂哪张表"（形态甲：只摘挂载，
不删代码、不删表、不写 Migration），而"没挂"这种状态**不会被任何一条既有用例发现**
——它表现为 404，而 404 在所有断言里都是"可以接受"的那一类。所以退役这件事
必须自己有一条用例钉着，否则下一次有人顺手把某个 include 加回来，谁都不会发现。

## 退役了什么（逐条的承接者写在这里，被删掉的那批用例的去处也在这里）

被退役的 6 个测试文件（`test_chat_api.py` / `test_chat_commands.py` /
`test_chat_live.py` / `test_chat_resume.py` / `test_chat_step_retry.py` /
`test_chat_approvals.py`，共 107 条）打的是**会话链路**那一族端点，而它们
**在两个档里都不再对外**（服务器档退役、本机档的对话走**边车**的 `/turn*`）：

**逐条说清"谁承接"**（被删掉的那 107 条用例的去处也在这里）：

- `POST /chat/stream`、`POST /chat`、`GET /chat/turns/{id}/live`：原来由那 6 份覆盖。
  承接者是**边车那条链**——`test_local_backend.py`（本机档整条：SSE 事件、落库、回读）
  与 `tests/unit/test_sidecar.py`、`tests/unit/test_sidecar_agent_face.py`（`/turn*` 那一面）；
- `POST /conversations/{id}/resume`、`.../steps/{i}/retry`、`POST /chat/approvals/{id}`：
  承接者同上（边车有它自己的 `/turn/approvals/{id}`）；
- `GET /conversations/{id}/events` 与 `GET /chat/context-usage`：**本机档照旧有**
  （`api/v1/local.py` 的 `chat_reads` 薄重声明）——本文件下面钉的正是这一点；
- `POST /chat/turns/record`（边车写回）：写回那一半早删了，边车直接写本机库
  （`app/sidecar.py::_record_turn`）；
- `POST /sandbox/exec` 的**本机档**那一份：服务器档照旧有它，用例搬去了
  `test_sandbox_api.py`。

**留在服务器档的一条**：`GET /chat/suggested-questions`——它不碰会话面（读的是 NAS 库里
的分段问题），而且**只有这一档有它**（本机档的知识库在别处，问题清单得问提供者）。

**同一轮的收尾（2026-10-05）**：原先与它并列的 `GET /chat/commands` 也走了。上一轮那条
端点被挪进了本机档（`router.py` 的 `local_router`），服务器这一份却还留着 ——
于是同一个端点有了**两份**目录；而它的数据（命令目录 + 技能 + 被禁用的技能）本来只属于
**这台机器**，执行那一轮也在本机（边车）⇒ 服务器档这一条摘掉，只在本机档
（下面 `test_the_local_face_…` 里钉着）。浏览器那一档（没有本机后端）因此没有命令目录，
`listCommands` 拿到 404 就回空列表。

**会话面自己**（列表 / 详情 / 消息 / 产物 / 文件区）也只在本机档了：它的用例
（`test_conversation_api.py`）整份切到了本机档，没有删。

**另外删掉一份**（同一轮）：`tests/unit/api/test_turn_record.py`（6 条，逐条钉
`POST /api/v1/chat/turns/record` 的 HTTP 路径 / 幂等 / 越主 404 / 无令牌 401）。
那条端点**在两个档里都不再对外**（见上表那一行），而"一轮写进本机会话"这件事现在由
边车那一侧做：`tests/unit/test_sidecar.py` 覆盖了同一条链的
`recorded` / `turn_id` / 落库失败不吞回答那几个结论。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client

#: 退役之后**两个档都不该有**的路径（`(方法, 路径)`）。
RETIRED = [
    ("post", "/api/v1/chat/stream"),
    ("post", "/api/v1/chat"),
    ("get", "/api/v1/chat/turns/conv_x/live"),
    ("post", "/api/v1/conversations/conv_x/resume"),
    ("post", "/api/v1/conversations/conv_x/messages/msg_x/steps/0/retry"),
    ("post", "/api/v1/chat/approvals/apr_x"),
    ("post", "/api/v1/chat/turns/record"),
]


def _call(client: TestClient, method: str, path: str):  # type: ignore[no-untyped-def]
    """按方法打一次（POST 带一个空 body；GET 不带——`TestClient.get` 不收 ``json=``）。"""
    if method == "post":
        return client.post(path, json={})
    return client.get(path)


def test_the_server_face_no_longer_serves_the_retired_ones() -> None:
    """服务器档：会话面 + 会话链路那一族**一条都不该挂**（本机档见下面那一条）。"""
    with admin_client() as client:
        # 会话面自己：只剩导出那一条（想再迁就有路的那份保障）
        assert client.get("/api/v1/conversations").status_code == 404
        assert client.post("/api/v1/conversations", json={}).status_code == 404
        assert client.get("/api/v1/conversations/conv_x").status_code == 404
        assert client.get("/api/v1/conversations/export").status_code == 200
        # 会话链路那一族（含 `/chat/*`）
        for method, path in RETIRED:
            response = _call(client, method, path)
            assert response.status_code == 404, f"{method} {path} → {response.status_code}"


def test_the_server_face_still_serves_the_survivor() -> None:
    """留下的那一条照旧在（理由见模块头）：推荐问题。

    `/chat/commands` 2026-10-05 从这一档摘掉（它的数据属于**这台机器**，本机档那一条
    才是它的家，见模块头）——所以这里**反过来钉**：服务器档必须 404 它，
    而本机档照旧有（下面那条用例里）。
    """
    with admin_client() as client:
        # 推荐问题：没有库就回空列表（**不是错误**，见那个端点的说明）
        questions = client.get("/api/v1/chat/suggested-questions", params={"kb_ids": ""})
        assert questions.status_code == 200, questions.text
        # 命令目录回本机了：服务器这一档不该再挂它（同一个端点有两个答案那件事就此收掉）
        assert client.get("/api/v1/chat/commands").status_code == 404


@pytest.mark.local
def test_the_local_face_keeps_the_two_reads_and_drops_the_rest(
    local_client: TestClient,
) -> None:
    """本机档：两条**只读**的会话端点照旧在（薄重声明），命令目录在这一档，
    执行口那条裸 HTTP 不在。"""
    # 命令目录（2026-10-05 起**只**在本机档）：它是"目录与执行同源"那条纪律的落点
    commands = local_client.get("/api/v1/chat/commands")
    assert commands.status_code == 200, commands.text
    assert commands.json()["items"], "命令目录应当是这台机器自己的那批命令"
    # 上下文用量那条只在"会话存在"时答得上来：不存在的会话是 404（与服务器档那份
    # 同一个归属判定），但**路由本身在**——所以这里先建一条真会话再问它。
    conversation = local_client.post("/api/v1/conversations", json={}).json()
    usage = local_client.get(
        "/api/v1/chat/context-usage", params={"conversation_id": conversation["id"]}
    )
    assert usage.status_code == 200, usage.text
    assert usage.json()["note"]
    # 事件日志那条（同样只在会话存在时答得上来）
    events = local_client.get(f"/api/v1/conversations/{conversation['id']}/events")
    assert events.status_code == 200, events.text
    assert isinstance(events.json()["items"], list), events.text
    # 而会话链路那一族的**其余部分**本机档一条都没有
    for method, path in RETIRED:
        response = _call(local_client, method, path)
        assert response.status_code == 404, f"{method} {path} → {response.status_code}"
    # 裸 HTTP 执行口（本机档 2026-10-05 摘掉）
    assert local_client.post("/api/v1/sandbox/exec", json={"argv": ["ls"]}).status_code == 404
    # 只算不跑与看隔离那两条照旧在
    assert local_client.post("/api/v1/sandbox/plan", json={"argv": ["ls"]}).status_code == 200
    assert local_client.get("/api/v1/sandbox").status_code == 200
