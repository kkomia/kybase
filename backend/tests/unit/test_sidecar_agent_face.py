"""本机档的**两头接真服务**（2026-10-04 那一刀）：模型搬回本机 + 技能/MCP 接进工具面。

这一份与 `test_sidecar.py` 分开写，是因为它守的是**两件新结论**，而不是边车那些老口径：

1. **模型推理在本机**：本机档的对话链不再经 NAS 的 `/model-proxy`，而是拿**本机**
   注册表里的地址与凭据直连（凭据在本机档由系统钥匙串持有）——没配/配错时的说法
   也要能看（不许变成空回答、也不许冒成 500）；
2. **工具面看得见本机的东西**：技能目录、外部 MCP 服务都从**本机**那份服务图来，
   启停与策略跟着本机的 `app_settings` / MCP 记录走。

三条口径与 `test_sidecar.py` 一致：**假模型**（不打真网络）、**真服务图**（本机
SQLite + 本机目录）、**真工具执行**（走 `build_runner` 那条原路）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import sidecar
from app.services.llm import ChatError, ChatMessage

from .test_sidecar import (
    _client,
    _configure_a_chat_model,
    _in_memory_keychain,
    _ToolCallingModel,
)

pytestmark = pytest.mark.local


def _skill_dir(tmp_path, name: str = "local-demo") -> Any:
    """在本机数据目录里放一个**真的技能**（`<数据目录>/skills/<name>/SKILL.md` ✓）。

    放在数据目录而不是仓库 `skills/`：那才是"用户自己装的技能"落点，也正是
    `SkillService` 三个根里的第二层（见 `services/skills.py::_roots`）。
    """
    directory = tmp_path / "data" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: 本机演示技能：把一件事记下来（用例用，别删）。\n"
        "summary: 本机演示技能\n"
        "---\n\n"
        "# 做法\n\n先读一遍，再动手。\n",
        encoding="utf-8",
    )
    return directory


def _only_local_skills(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """把技能目录收成**只有这一条用例放的那个**（`KYLAB_SKILLS_DIR` 指向空目录 ✓）。

    为什么必须收：仓库自带 `skills/` 有 5 个真技能，`list_skills` 一页的输出会到
    2500+ 字**被截断**（`MAX_MCP_RESULT_CHARS`，那是有意的预算）——截断点在哪儿
    取决于仓库里有几个技能，于是"某一条在不在输出里"这种断言会跟着仓库内容飘。
    把内置那一层指到空目录之后，这台上就只有用例放的那一个技能，
    "agent 看得见本机技能"与"停用之后跟着变"都能一行断言钉死。

    这是**测试夹具**的收窄，不是产品行为 ✗：真机上那三层照旧都扫
    （见 `services/skills.py::_roots`），`/turn` 的 notes 也照旧报真实的条数。
    """
    empty = tmp_path / "no-builtin-skills"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KYLAB_SKILLS_DIR", str(empty))


def _tool_steps(
    payload: dict[str, Any], tool: str, *, status: str = "done"
) -> list[dict[str, Any]]:
    """某件工具的步骤（**默认只要 `done` 那条**）。

    循环对每一步发**两条**事件：先一条 `running`（界面据此显示"正在跑"），跑完再发一条
    `done`（带结果与 outcome ✓）。只要 `done` 那条：`running` 上的 `result` 本来就还是空的，
    拿它去断言只会得到"工具没干活"的假象。
    """
    return [
        step
        for step in payload["steps"]
        if step.get("tool") == tool and step.get("status") == status
    ]


# ============================================================ 模型：本机直连


def test_the_local_model_reads_the_registry_and_the_keychain(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """本机直连用的**就是本机那把 key 与那个地址** ✓（不是 NAS，也不是别处）。

    这条同时是"本机档 `model_providers.api_key` 到底有没有消费路径"的答案：
    走本机后端 `/model-registry` 那三个端点配好之后（与界面同一条路 ✓），
    `_LocalModel` 建出来的客户端手上是**钥匙串里那把 key** ✓ —— 库里那一列是空的 ✗。
    """
    store = _in_memory_keychain(monkeypatch)
    client = _client(tmp_path, monkeypatch, None)  # **不注入假模型**：走运行形态
    provider_id, model_pk = _configure_a_chat_model(client, base_url="http://model.test/v1")

    # 库里不留明文（钥匙串档上 `_for_db` 把这一列写成空 ✓）
    providers = client.get("/api/v1/model-registry/providers").json()["items"]
    row = next(item for item in providers if item["id"] == provider_id)
    assert row["api_key_configured"] is True
    assert "sk-local-test" not in json.dumps(row), row
    assert store.get(f"kylab:model_provider:{provider_id}") == "sk-local-test"

    # 本机档的 `Clients` 现建一次客户端：地址 / key / 模型名三样都来自本机
    from app.core.services import get_services

    clients = sidecar.Clients(
        "http://server.test/api/v1",
        "t",
        workspace=tmp_path / "ws",
        data_dir=tmp_path / "data",
    )
    config = clients.model._chat().config  # type: ignore[attr-defined]
    assert config.base_url == "http://model.test/v1"
    assert config.api_key == "sk-local-test"
    assert config.model_id == "local-test-model"
    assert clients.model_configured() is True
    assert get_services().runtime.llm().api_key == "sk-local-test"
    assert model_pk


def test_a_missing_model_is_reported_readably_not_as_an_empty_answer(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """没配模型 → **一句人话**（不是空回答 ✗、也不是 500 ✗）。

    这是失败路径的第一档，也是最容易在新装好的机器上撞到的一档：本机档既不挂
    `/auth/*` 也不从 `.env` 读模型身份（见 `runtime_config._bootstrap_value`），
    所以"刚装好还没配模型"是**常态化**的起点，而不是异常。
    两处说法都要有：非流式那条的 `error`/`answer`、流式那条的 `error` + `done` 事件。
    """
    client = _client(tmp_path, monkeypatch, None)  # 不注入假模型 = 真的本机直连

    payload = client.post("/turn", json={"message": "在吗"}).json()

    assert "尚未配置对话模型" in payload["error"], payload
    assert "边车报告" in payload["answer"]
    assert "模型" in payload["answer"]

    events = [
        json.loads(line[5:])
        for line in client.post("/turn/stream", json={"message": "在吗"}).text.splitlines()
        if line.startswith("data:")
    ]
    error = next(event for event in events if event["type"] == "error")
    assert "尚未配置对话模型" in error["message"], events


def test_a_rejected_key_says_what_to_do(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """key 无效 → 上游的 401 翻成一句带下一步动作的话（**不是**空回答 ✓）。

    这里换的是**模型那一头那一层**（`llm.shared_client` 那个进程级客户端 →
    打一个假传输的客户端，手法与 `tests/unit/services/test_llm.py` 一致 ✓）：
    真客户端代码照跑（鉴权头、请求体、状态码映射都是它做的 ✓），只是不打真网络。
    """
    import httpx

    _in_memory_keychain(monkeypatch)
    client = _client(tmp_path, monkeypatch, None)
    _configure_a_chat_model(client, base_url="http://model.test/v1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    fake = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.llm.shared_client", lambda: fake)

    clients = sidecar.Clients(
        "http://server.test/api/v1",
        "t",
        workspace=tmp_path / "ws",
        data_dir=tmp_path / "data",
    )

    with pytest.raises(ChatError) as excinfo:
        clients.model.complete([ChatMessage(role="user", content="在吗")])

    message = str(excinfo.value)
    assert "401" in message and "API Key" in message, message


# ============================================================ 技能：接真服务


def test_the_agent_lists_the_local_skills_and_the_switch_follows(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`list_skills` 走**本机技能目录**，而且**停用之后 agent 那一侧跟着变** ✓。

    "跟着变"落在哪：`SkillService.list()` 把用户关掉的技能折成
    `used_by_prompt=False` + 一句原因（`chat.disabled_skills` 存在**本机** settings ✓），
    而 `_render_skills` 就是按它写那一行的 —— 所以模型的下一步看到的是"已被你关掉"。
    """
    _only_local_skills(monkeypatch, tmp_path)
    _skill_dir(tmp_path, "local-demo")
    model = _ToolCallingModel("list_skills", json.dumps({"limit": 200}), "我看到技能了")
    client = _client(tmp_path, monkeypatch, model)

    payload = client.post("/turn", json={"message": "有哪些技能"}).json()
    steps = _tool_steps(payload, "list_skills")
    assert steps, payload["steps"]
    result = steps[0]["result"]
    assert "local-demo" in result, result
    assert "本机演示技能" in result, result
    # `/turn` 的 notes 报的是**真实的条数**（1 个 ✓ —— 上面第一句说的那件事）
    note = next(item for item in payload["notes"] if "技能目录" in item)
    assert "1 个技能" in note, note

    # **关掉它**（走界面那条路：`PUT /skills/{name}/enabled` ✓）
    disabled = client.put("/api/v1/skills/local-demo/enabled", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["used_by_prompt"] is False

    model.called = False
    payload = client.post("/turn", json={"message": "再看看"}).json()
    result = _tool_steps(payload, "list_skills")[0]["result"]
    assert "local-demo" in result and "关掉" in result, result

    # 再打开，agent 那一侧又看得见它了（启停是**双向**的，不是单向开关）
    client.put("/api/v1/skills/local-demo/enabled", json={"enabled": True})
    model.called = False
    payload = client.post("/turn", json={"message": "再再看看"}).json()
    result = _tool_steps(payload, "list_skills")[0]["result"]
    assert "关掉" not in result, result


def test_read_skill_returns_the_real_body(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`read_skill` 读的是**本机那份正文**（不是一句"这侧没有技能目录" ✓）。"""
    _only_local_skills(monkeypatch, tmp_path)
    _skill_dir(tmp_path, "local-demo")
    model = _ToolCallingModel("read_skill", json.dumps({"name": "local-demo"}), "读到了")
    client = _client(tmp_path, monkeypatch, model)

    payload = client.post("/turn", json={"message": "读一下这个技能"}).json()

    result = _tool_steps(payload, "read_skill")[0]["result"]
    assert "先读一遍，再动手" in result, result


# ============================================================ 外部 MCP：接真服务


def _fake_mcp(monkeypatch, *, result: str = "外部服务回的正文"):
    """把 MCP 的**网络那一层**换掉（列出工具 / 调用各一个），其余全是真的。

    与 `test_sidecar._fake_nas` 同一手法：换**接缝**而不是塞一个假服务 ——
    于是"启停、策略、限定名、执行器分发"这些真代码都还在链上（那才是要验的东西）。
    """
    from app.services.mcp_client import MCPClientService, MCPTool

    def _cached_tools(self, record):  # type: ignore[no-untyped-def]
        return (
            [
                MCPTool(
                    name="echo",
                    qualified=f"mcp__{record.name}__echo",
                    description="把参数回显出来（用例用）。",
                    server_id=record.id,
                    server_name=record.name,
                    schema={"type": "object", "properties": {"text": {"type": "string"}}},
                )
            ],
            "",
        )

    monkeypatch.setattr(MCPClientService, "cached_tools", _cached_tools)
    monkeypatch.setattr(MCPClientService, "call", lambda self, *a, **kw: result)


def _register_mcp(client: TestClient, *, policy: str = "allow", name: str = "demo") -> str:
    response = client.post(
        "/api/v1/mcp-servers",
        json={
            "name": name,
            "transport": "stdio",
            "target": "python",
            "args": ["-c", "pass"],
            "policy": policy,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_external_mcp_tools_reach_the_agent_tool_face(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """配好的 MCP 服务 → 工具表里出现 `mcp__<服务>__<工具>`，而且**调得动** ✓。

    2026-10-04 之前这里是一句"边车这一侧没有接 MCP 服务"：`Services.mcp` 被换成了
    一个恒回空的壳，于是"配置页里三台服务好好的、agent 手里一个都没有"。
    这条用例钉住那两半：**服务图里那一个是真的** + **限定名不被白名单筛掉**。
    """
    _fake_mcp(monkeypatch, result="echo 说：你好")
    model = _ToolCallingModel("mcp__demo__echo", json.dumps({"text": "你好"}), "外部工具回来了")
    client = _client(tmp_path, monkeypatch, model)
    _register_mcp(client, policy="allow")

    payload = client.post("/turn", json={"message": "用外部工具回显一下"}).json()

    result = _tool_steps(payload, "mcp__demo__echo")[0]["result"]
    assert "echo 说：你好" in result, result
    # 工具表里也确实摆着它（`Clients.tool_specs` 那条白名单放行 `mcp__*` ✓）
    clients = sidecar.Clients(
        "http://server.test/api/v1",
        "t",
        workspace=tmp_path / "ws",
        data_dir=tmp_path / "data",
    )
    assert "mcp__demo__echo" in {spec.name for spec in clients.tool_specs()}


def test_an_mcp_service_that_is_off_does_not_reach_the_tool_face(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**停用一台 MCP 服务 = 它的工具不再进工具表** ✓（启停跟着本机那一条记录走）。

    这是"看得见"的另一半：不是把配置一股脑倒给模型，而是**停用的就别摆**
    （摆上去模型会照着调，然后撞一句"没有找到启用的 MCP 服务"）。
    """
    _fake_mcp(monkeypatch)
    model = _ToolCallingModel("mcp__demo__echo", "{}", "好的")
    client = _client(tmp_path, monkeypatch, model)
    server_id = _register_mcp(client, policy="allow")

    from app.core.services import get_services

    clients = sidecar.Clients(
        "http://server.test/api/v1",
        "t",
        workspace=tmp_path / "ws",
        data_dir=tmp_path / "data",
    )
    assert "mcp__demo__echo" in {spec.name for spec in clients.tool_specs()}

    off = client.patch(f"/api/v1/mcp-servers/{server_id}", json={"enabled": False})
    assert off.status_code == 200, off.text
    get_services().mcp.invalidate()

    assert "mcp__demo__echo" not in {spec.name for spec in clients.tool_specs()}


def test_the_mcp_policy_gate_still_holds_in_the_sidecar(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**策略闸没被绕** ✓：`ask` 档 + 没有确认通道 → 回"待确认"，绝不静默执行。

    `/turn` 这条非流式路发不出询问（`interactive=False`），所以循环那边按
    `UNAVAILABLE` 处理——回给模型的是"待确认"。这条与服务器档那套语义**逐字一致**
    （见 `services/mcp_client.py::decide`／`agent_tools._call_mcp`）。
    """
    called: list[str] = []

    from app.services.mcp_client import MCPClientService, MCPTool

    def _cached_tools(self, record):  # type: ignore[no-untyped-def]
        return (
            [
                MCPTool(
                    name="echo",
                    qualified=f"mcp__{record.name}__echo",
                    description="回显。",
                    server_id=record.id,
                    server_name=record.name,
                    schema={"type": "object", "properties": {}},
                )
            ],
            "",
        )

    monkeypatch.setattr(MCPClientService, "cached_tools", _cached_tools)
    monkeypatch.setattr(
        MCPClientService, "call", lambda self, *a, **kw: called.append("called") or "不该发生"
    )

    model = _ToolCallingModel("mcp__demo__echo", "{}", "那就算了")
    client = _client(tmp_path, monkeypatch, model)
    _register_mcp(client, policy="ask")

    payload = client.post("/turn", json={"message": "调一下外部工具"}).json()

    assert called == [], "策略是 ask 却没有等确认就调了 —— 闸被绕过了"
    step = _tool_steps(payload, "mcp__demo__echo")[0]
    # 回给模型的是**一句能照着做的事**（这一轮没执行 + 怎么放开它），不是空结果
    assert "这一轮没有执行" in step["result"], step
    assert "策略改成「允许」" in step["result"], step
