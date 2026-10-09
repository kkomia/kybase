"""**断 NAS 时的对话全流程**（M2 §6.1 的验收用例，`@pytest.mark.local`）。

这条用例要回答的是 M2 的核心那句话："**断 NAS 的时候，对话还跑得完吗**"，而且
**不需要 PostgreSQL**（`local` 这个 marker 的全部意义，见 `conftest.py` 里那段说明）。

## 假的是什么、真的是什么

| 件 | 这一轮的形态 |
| --- | --- |
| 桌面边车 | **真的**（`app/sidecar.py` 的 `create_app`，本机档）|
| 本机后端面 | **真的**（`/api/v1/*` 就挂在同一个 app 上，`local_router` 那张白名单）|
| 会话 / 消息 / 事件 / 产物 / 笔记 / 设置 | **真的**（本机 SQLite，落在 `tmp_path`）|
| 模型 | 假的（`conftest.FakeChatModel`，按剧本吐工具调用与正文）|
| 知识库提供者 | **连不上**：握手抛 `ConnectError`、检索抛 `RemoteUnavailableError` |
| 出站 HTTP | 一概不许（`_httpx` 被换成会炸的那个）——**"断 NAS"是真的断** ✓ |

## 流程（照 §6.1 那条链，一步不落）

建会话 → 走一轮带工具的流式对话（SSE）→ 列会话 → 读会话详情（消息 / 步骤 / 思考）→
改名 → 回退一轮 → 删会话 → 建笔记 / 列笔记 → 改设置。

**每一步都必须成功** ✓，而且检索失败要**如实出现在步骤里** ✗（不许伪装成"没命中"，
也不许因为 NAS 断了就整轮失败 —— 这一轮照常答话、照常落库 ✓）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import sidecar
from app.core.services import reset_services
from app.core.storage import reset_stores
from app.services import remote_clients
from app.services.knowledge_provider import KnowledgeProviderClient
from app.services.llm import LLMReply
from app.services.remote_clients import RemoteKnowledgeClient, RemoteUnavailableError
from tests.conftest import FakeChatModel, search_tool_call

pytestmark = pytest.mark.local

#: 这一轮的模型剧本：先查资料（NAS 断着，这一步会失败），再作答。
#: 第二步带一段 `reasoning` —— 会话详情里"思考也在"就是靠它验的 ✓。
SCRIPT = [search_tool_call("本机这条链怎么验"), LLMReply(reasoning="先想一下再说。")]
ANSWER = "查不到资料（NAS 断着），我按已知的说：这条链要一步一步验。"


def _boom_httpx():  # type: ignore[no-untyped-def]
    """任何一次出站 HTTP 都直接炸 ✗（"断 NAS"必须是**真的断**，不是"假装断"）。"""
    raise AssertionError("这个用例不该发任何 HTTP 请求（NAS 是断的）")


def _dead_provider():  # type: ignore[no-untyped-def]
    """知识库提供者客户端：**任何请求都连不上**（假传输 ✓，不打真网络 ✓）。

    为什么要显式给一个而不是让它去撞上面那个 `_boom_httpx`：M3 阶段 2 起
    `Clients.tool_specs` 在"这一轮选了库"时会问一次提供者状态，而"怎么知道 NAS 断了"
    的唯一途径就是那次探测**真的失败** —— 让它撞 AssertionError 再被探针吞掉，
    等于把这条断言悄悄关掉（那种"绿着但没在干活"的状态正是 `_boom_httpx` 要防的）。
    这里模拟的是真实形态：连接被拒。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("NAS 断了（这条用例的唯一故障）")

    return KnowledgeProviderClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """边车（本机档）+ 假模型 + 不可达的知识库提供者。"""
    model = FakeChatModel(answer=ANSWER, script=SCRIPT)

    def _build(base: str, token: str, *, workspace, data_dir):  # type: ignore[no-untyped-def]
        # 模型换成假的（不联网）；知识库提供者连不上；其余一律走真装配（本机档的组合根 ✓）
        return sidecar.Clients(
            base,
            token,
            workspace=workspace,
            data_dir=data_dir,
            model=model,
            provider=_dead_provider(),
        )

    def _dead_knowledge(self, **kwargs: Any) -> list[Any]:
        raise RemoteUnavailableError("知识库不可达：NAS 断了（这条用例的唯一故障）")

    monkeypatch.setattr(sidecar, "build_clients", _build)
    monkeypatch.setattr(RemoteKnowledgeClient, "retrieve_sources", _dead_knowledge)
    monkeypatch.setattr(remote_clients, "_httpx", _boom_httpx)
    app = sidecar.create_app("http://nas.test/api/v1", "t", tmp_path / "ws")
    with TestClient(app) as client:
        yield client
    reset_services()
    reset_stores()


def _sse_events(text: str) -> list[dict[str, Any]]:
    """把 SSE 响应拆成事件载荷（**按线上形状解析** ✓，不猜内部对象 ✗）。"""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload:
            out.append(json.loads(payload))
    return out


def test_the_whole_flow_works_with_the_nas_down(client: TestClient) -> None:
    """§6.1 的主判据：**每一步都成功**，且检索失败如实进步骤 ✓。"""
    # ① 建会话（本机后端，不带任何凭据 —— 本机档短路成"本机主人" ✓）
    created = client.post("/api/v1/conversations", json={"kb_ids": ["kb_1"]})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    # ② 走一轮带工具的**流式**对话（同一个 ToolLoop，工具在本机跑）
    stream = client.post(
        "/turn/stream",
        json={
            "message": "这条链怎么验？",
            "conversation_id": conversation_id,
            "kb_ids": ["kb_1"],
        },
    )
    assert stream.status_code == 200, stream.text
    events = _sse_events(stream.text)
    kinds = [event["type"] for event in events]
    assert "delta" in kinds and "done" in kinds, kinds
    # 检索那一步**如实失败**（不是"没命中"，也没有把整轮打崩）✓
    search_steps = [
        event for event in events if event["type"] == "step" and event.get("tool") == "search"
    ]
    assert search_steps, events
    failed = [event for event in search_steps if event.get("outcome") == "failed"]
    assert failed, search_steps
    assert "不可达" in failed[-1]["detail"], failed[-1]
    # 这一轮照常答完
    done = [event for event in events if event["type"] == "done"]
    assert done[-1]["answer"] == ANSWER
    assert not [event for event in events if event["type"] == "error"], events

    # ③ 列会话：它在
    listed = client.get("/api/v1/conversations")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [conversation_id]

    # ④ 会话详情：消息 / 步骤 / 思考都在（落本机库的那一份）
    detail = client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    messages = body["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "这条链怎么验？"
    assert messages[1]["content"] == ANSWER
    # 失败的那一步留在快照里（回看时看得见"当时 NAS 是断的"）
    assert any(step.get("tool") == "search" for step in messages[1]["steps"]), messages[1]
    assert "先想一下" in messages[1]["thinking"], messages[1]

    # ⑤ 事件日志（本机档薄重声明的那一条）读得到
    #
    #    ⚠️ 现况（阶段 3 发现，如实写在这里）：**边车这一轮不写 session_events**，
    #    所以对本机跑出来的轮次它是空列表（服务器那条链由 `api/v1/chat.py` 的
    #    `_TurnSink` 攒草稿，边车没有对应的一环 —— 见 `_record_turn` 的说明与
    #    阶段 3 报告里"留给架构师定"的那一处）。这一条只钉"这个端点在本机档答得上来" ✓，
    #    不钉它现在是空的（补上事件日志之后这里应当能读到 step/tool_call/turn/end）。
    events_out = client.get(f"/api/v1/conversations/{conversation_id}/events")
    assert events_out.status_code == 200, events_out.text
    assert isinstance(events_out.json()["items"], list), events_out.text

    # ⑥ 改名
    renamed = client.patch(
        f"/api/v1/conversations/{conversation_id}", json={"title": "断网也能跑的一轮"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "断网也能跑的一轮"

    # ⑦ 上下文用量（另一条薄重声明）—— 只读、按本机数据现算
    usage = client.get("/api/v1/chat/context-usage", params={"conversation_id": conversation_id})
    assert usage.status_code == 200, usage.text
    assert usage.json()["note"], usage.text

    # ⑧ 回退一轮（那一轮的两条消息都没了）
    rewound = client.post(f"/api/v1/conversations/{conversation_id}/rewind", json={"turns": 1})
    assert rewound.status_code == 200, rewound.text
    after = client.get(f"/api/v1/conversations/{conversation_id}")
    assert after.json()["messages"] == [], after.text

    # ⑨ 笔记：列（空）→ 建 → 列（有）
    assert client.get("/api/v1/notes").json()["items"] == []
    note = client.post("/api/v1/notes", json={"title": "验收记录", "content_md": "- 断网跑通"})
    assert note.status_code == 201, note.text
    notes = client.get("/api/v1/notes").json()["items"]
    assert [item["title"] for item in notes] == ["验收记录"], notes

    # ⑩ 设置：改得动，而且就写在本机库里
    updated = client.patch(
        "/api/v1/settings", json={"values": [{"key": "chat.top_k", "value": "9"}]}
    )
    assert updated.status_code == 200 and updated.json()["updated"] == 1, updated.text
    assert client.get("/api/v1/settings").status_code == 200

    # ⑪ 删会话（连它的消息一起）
    deleted = client.delete(f"/api/v1/conversations/{conversation_id}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/v1/conversations/{conversation_id}").status_code == 404


def test_the_local_files_are_seeded_at_startup(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """边车启动时把**缺的**人设/记忆文件铺上（与 `main.py` 的 lifespan 同一件事）✓。

    为什么要在这一步验：本机档把 `memory.router` 挂在了同一个 app 上（§4.2），
    而边车没有 lifespan 那些步骤 —— 不补这一遍，"新装好的桌面第一次打开记忆页"
    就是一片空白，与服务器那侧的表现不一样 ✗。**只补缺的，绝不覆盖已有的** ✓。

    （这一条自己建 app，不用那个 fixture：它要**建两次** app 才能验"不覆盖"。）
    """

    def _build(base: str, token: str, *, workspace, data_dir):  # type: ignore[no-untyped-def]
        return sidecar.Clients(
            base, token, workspace=workspace, data_dir=data_dir, model=FakeChatModel()
        )

    monkeypatch.setattr(sidecar, "build_clients", _build)
    monkeypatch.setattr(remote_clients, "_httpx", _boom_httpx)
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"

    sidecar.create_app("http://nas.test/api/v1", "t", workspace, data_dir=data_dir)

    # **v0.56 起只种三份**：``MEMORY.md`` 随"记忆档案"退场（§7.2），不再被播种
    # （见 `services/memory.py` 的 `seed_persona`，那份 docstring 里写着这一条）。
    # 老用户盘上已有的 ``MEMORY.md`` 仍在、仍读得到，但那是"旧记忆（只读）"，
    # 与"新装好的桌面第一次打开有什么"是两件事 ✓。
    seeded = {path.name for path in (data_dir / "memory").iterdir()}
    assert seeded >= {"SOUL.md", "PROFILE.md", "AGENTS.md"}, seeded
    soul = data_dir / "memory" / "SOUL.md"
    soul.write_text("# 我的人格\n\n用户自己写的。\n", encoding="utf-8")

    # 同一个数据目录再起一次：**已有的一个字都不动** ✓（那是用户写了几天的东西）
    sidecar.create_app("http://nas.test/api/v1", "t", workspace, data_dir=data_dir)
    assert soul.read_text(encoding="utf-8") == "# 我的人格\n\n用户自己写的。\n"


def test_the_turn_lands_in_the_local_db_file(client: TestClient, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """**物证**：那一轮真的在 `<data_dir>/kylab.db` 里（不是内存里跑完就没了）✓。

    用例直接开 SQLite 读那两张表（测试不适用 L2 那道分层纪律）：
    "账落在本机"这句话必须**在文件里**成立 ✓。

    `session_events` 这一张表**故意不在这里断言**：边车那一轮现在不写事件日志
    （见 `_record_turn` 的说明与上面那条用例里的注），要断言它得先补上那一环。
    """
    from app.core.storage import LOCAL_DB_NAME

    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    client.post(
        "/turn/stream",
        json={"message": "在吗", "conversation_id": conversation_id, "turn_id": "t1"},
    )

    import sqlite3

    database = tmp_path / "data" / LOCAL_DB_NAME
    assert database.is_file(), database
    with sqlite3.connect(database) as conn:
        conversations = conn.execute(
            "select id, title from conversations where id = ?", (conversation_id,)
        ).fetchall()
        messages = conn.execute(
            "select role, content from chat_messages where conversation_id = ?"
            " order by created_at_ms",
            (conversation_id,),
        ).fetchall()
        artifacts = conn.execute("select name from conversation_artifacts").fetchall()
    assert conversations and conversations[0][0] == conversation_id
    assert [row[0] for row in messages] == ["user", "assistant"]
    assert messages[1][1] == ANSWER
    assert artifacts == []  # 这一轮没产出文件（有的话它也该在这张表里）


def test_the_local_face_is_a_whitelist_not_the_whole_api(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`local_router` 是**白名单**（M2 §4.2）：服务器专属的端点**一个都没挂** ✓。

    用 `app.main.create_app()`（本机档）验 —— 它就是边车挂的那个 app 的另一半入口：
    本机后端面（`/api/v1/*`）与桌面壳那条路走的是**同一张路由表**。
    判据两端都要有：**该在的在**（会话 / 笔记 / 设置 / 记忆 / 技能 / 插件 / 沙箱 /
    站点图标 / `/local/status` / 用量那一条）、
    **该不在的不在**（文档 / 知识库 / 检索 / 任务 / 驾驶舱 / 回收站 / 账号 / 前端包 / 模型代理）。

    **2026-10-04 那次"补全"改过这张清单**（当时把技能 / 插件 / 沙箱 / 站点图标 /
    `/stats/usage` 补挂上本机档）：判据是"实现已经在后端里，且它的数据落本机
    （SQLite / data_dir / 本机进程）"，逐域的理由写在 `api/v1/router.py` 的
    `local_router` 那一段。原来那版断言把 `/sandbox` 也列在"该不挂"里——它改了。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    reset_services()
    reset_stores()

    from app.main import create_app

    with TestClient(create_app()) as client:
        status = client.get("/api/v1/local/status")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["deployment"] == "local"
        assert body["database"].endswith("kylab.db")
        # 启动时（lifespan 备库）已经把库文件建出来了 —— 状态页如实说"它在"
        assert body["database_exists"] is True
        assert body["database_bytes"] > 0
        # 本机主人短路：不带凭据也建得出会话 ✓
        assert client.post("/api/v1/conversations", json={}).status_code == 201
        # 该挂的挂着（后五个是 2026-10-04 补上的：数据全在本机那几件）
        for path in (
            "/api/v1/health",
            "/api/v1/notes",
            "/api/v1/settings",
            "/api/v1/memory",
            "/api/v1/skills",
            "/api/v1/plugins",
            "/api/v1/sandbox",
            "/api/v1/stats/usage",
        ):
            assert client.get(path).status_code in (200, 422), (path, client.get(path).text)
        # 该不挂的没挂（数据或能力不在这台机器上，逐域理由见 router.py 那一段）
        for path in (
            "/api/v1/documents/doc_x",
            "/api/v1/knowledge-bases",
            "/api/v1/search",
            "/api/v1/tasks",
            "/api/v1/stats/dashboard",
            "/api/v1/trash",
            "/api/v1/maintenance/storage",
            "/api/v1/webhooks",
            "/api/v1/users",
            "/api/v1/api-keys",
            "/api/v1/app/frontend/manifest",
            "/api/v1/model-proxy/complete",
        ):
            assert client.get(path).status_code == 404, (path, client.get(path).text)
        # `chat.router` 只重声明了那两条只读端点，`/chat/stream` 不在
        assert client.post("/api/v1/chat/stream", json={}).status_code == 404
