"""沙箱**执行口**的集成测试（服务器档；v0.16）。

镜像同构：``app/api/v1/sandbox.py`` 的 ``POST /sandbox/exec`` 与准入规则那一段 →
本文件。（同一个模块里那两条**只读**端点——``GET /sandbox`` 与 ``POST /sandbox/plan``
——留在 `test_agent_api.py`：它们打的是本机档，见下面。）

## 为什么这一份打**服务器档**（NAS 网页端退役，2026-10-05）

``POST /sandbox/exec`` 是一条**裸 HTTP 执行口**（一个 body 里写命令就执行）。
2026-10-05 起它**从本机档白名单里摘掉了**（`api/v1/router.py` 的 `_sandbox_local`）：

- 它今天**没有任何前端 / 客户端调用**——界面那条链是 ``GET /sandbox``（看这台机器有
  什么隔离）+ ``POST /sandbox/plan``（只算不跑），而 Agent 真正执行时走的是**进程内**的
  `services/agent_exec.py`（工具调用那条路，带模式闸 / 权限档 / 计划门闸 + 审批）；
- 它同时缺三样一般做法都要求的东西：**Origin / Host 校验**（MCP 规范对本地服务是
  ``MUST``）、**本地令牌**、以及 ``approved`` 由**请求方自填**。

服务器档照旧有它（管理员在 NAS 上执行），所以这一份留在服务器档，用
``server_client``（`conftest.admin_client`，管理员会话）。

## 配置那一半为什么走服务层

``_set_rules`` 原来用 ``PATCH /settings`` 写 ``chat.permission`` 与那三张清单，
而 **`/settings` 只在本机档**（运行期配置表在本机 SQLite 里），这一条又只在服务器档
——两种端点要同时用，所以直接调 ``runtime.set``（那个端点内部调的就是它）。
``PATCH /settings`` 自己的契约（未知键回 ``rejected``）由
`tests/integration/api/test_settings_api.py` 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import admin_client as admin_session


@pytest.fixture
def server_client() -> TestClient:
    """**服务器档**的管理员客户端（`POST /sandbox/exec` 只在那张表上）。"""
    with admin_session() as test_client:
        yield test_client


def _set_rules(**values: str) -> None:
    """把这几项运行期配置写进去（服务层），并**回读确认真的写进去了**。

    白名单校验（未知键回 ``rejected``）不在这里验：那是 ``PATCH /settings`` 自己的契约，
    由 `tests/integration/api/test_settings_api.py` 覆盖。
    """
    runtime = get_services().runtime
    runtime.set(dict(values))
    for key, value in values.items():
        assert runtime.get(key) == value, (key, runtime.get(key))


def test_sandbox_exec_requires_approval_under_manual_policy(server_client: TestClient) -> None:
    """「手动批准」那一档：未确认回 **409**（不是 403）——不是"你不能做"，
    是"要先确认"。界面据此弹确认框，确认后带 approved 重调。

    ⚠️ 2026-09-29 四档化：原来这条钉的是旧值 ``workspace``（如今映射到「默认（智能）」），
    而智能档按"工作区内不问"判，`python -c print(1)` 不会问 ✗ —— 所以**钉住会问的那一档**
    （手动批准）。断言强度不变：**没确认就必须 409，且说的是"要确认"**。
    """
    _set_rules(**{"chat.permission": "manual"})

    response = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["python", "-c", "print(1)"]}
    )

    assert response.status_code == 409, response.text
    assert "确认" in response.json()["message"]


def test_sandbox_exec_refuses_when_isolation_is_required(server_client: TestClient) -> None:
    """**严格模式**（设置里打开「无隔离时拒绝执行」）下，没有内核级隔离就拒绝，不回退裸跑。

    这条钉的是那一项设置的行为；**出厂值是"关"= 降级为直接执行**（见下一条，
    以及 `api/v1/sandbox.py` 模块头里"第 3 条"那一段）。
    """
    from app.services import isolation

    found = isolation.detect()
    if found.available:  # pragma: no cover - 有隔离的机器上这条不适用
        pytest.skip("这台机器有可用的隔离后端，拒绝路径不适用")

    _set_rules(**{"chat.permission": "full", "sandbox.require_isolation": "true"})
    response = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["python", "-c", "print(1)"], "approved": True}
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "unsupported_content"
    assert "拒绝执行" in response.json()["message"]


def test_sandbox_exec_degrades_to_direct_without_isolation(
    server_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**默认降级为直接执行**（v0.55）：没有 bwrap / docker 的机器也要能跑命令。

    与上一条是一对：默认那一档要让"本地源码启动与容器部署"两边都能执行工具
    （用户报的"明明指定了工作区，还是不能执行工具"）。降级时后端如实报 ``direct``。
    """
    from app.services import isolation

    found = isolation.detect()
    if found.available:  # pragma: no cover - 有隔离的机器上这条不适用
        pytest.skip("这台机器有可用的隔离后端，降级路径不适用")

    monkeypatch.setattr(
        isolation,
        "run_isolated",
        lambda argv, **kwargs: isolation.ExecutionResult(
            exit_code=0, stdout="1\n", stderr="", truncated=False, backend="direct"
        ),
    )
    _set_rules(**{"chat.permission": "full", "sandbox.require_isolation": "false"})
    response = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["python", "-c", "print(1)"], "approved": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["backend"] == "direct"


def test_sandbox_exec_respects_view_permission(server_client: TestClient) -> None:
    """「仅查看」这一档：命令**不跑**，而且回的话要说清是权限档拦的。

    2026-09-27：这一档原来是设置里的「命令执行策略 = 拒绝」（`sandbox.exec_policy`），
    那一项已折进权限轴，所以现在推到的是 `chat.permission = view`。
    """
    _set_rules(**{"chat.permission": "view"})

    response = server_client.post("/api/v1/sandbox/exec", json={"argv": ["ls"], "approved": True})

    assert response.status_code == 403
    assert "仅查看" in response.json()["message"]


def test_sandbox_exec_rejects_an_empty_command(server_client: TestClient) -> None:
    assert server_client.post("/api/v1/sandbox/exec", json={"argv": []}).status_code == 422


# ------------------------------------------------------------- 准入规则（v0.17）


def test_exec_deny_rule_wins_over_a_broader_allow(server_client: TestClient) -> None:
    """**deny 永远优先**，而且要能通过接口看到这个结论。

    少了这一条，用户"我加了一条 deny"会被一条更宽的 allow 静默盖掉——
    而用户以为自己已经禁掉了。
    """
    _set_rules(
        **{
            "chat.permission": "full",
            "sandbox.rules_allow": "Bash(git push:*)",
            "sandbox.rules_deny": "Bash(git push --force:*)",
        }
    )

    allowed = server_client.post(
        "/api/v1/sandbox/plan", json={"argv": ["git", "push", "origin", "main"]}
    )
    assert allowed.status_code == 200

    blocked = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["git", "push", "--force", "origin", "main"]}
    )
    assert blocked.status_code == 403, blocked.text
    assert "拒绝规则" in blocked.json()["message"]


def test_allow_rule_skips_the_confirmation(server_client: TestClient) -> None:
    """放行清单里的命令**不再问**——这正是规则存在的意义（同一个动作问一遍就够）。"""
    _set_rules(
        **{
            "chat.permission": "workspace",
            "sandbox.rules_allow": "Bash(git status:*)",
            # **把隔离模式显式钉成严格**（出厂值是"没有真隔离就用降级档 direct 直接跑"，
            # 见 `test_sandbox_exec_degrades_to_direct_without_isolation`）。不钉的话，
            # 这条用例的期望取决于**跑它的机器有没有内核隔离**：有隔离时 409、
            # 没有时 200 且命令真的跑掉——同一条用例两种结果，那不是在测行为，是在测环境。
            "sandbox.require_isolation": "true",
        }
    )

    # 命中放行规则 → **不再问确认**；严格模式下没有内核隔离就由隔离层拒绝
    # （code=unsupported_content）——用这个区分两件事：
    # "准入已通过、卡在隔离" 与 "准入没过、卡在确认"。
    response = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["git", "status", "--short"]}
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "unsupported_content"


def test_command_outside_the_rules_still_asks(server_client: TestClient) -> None:
    """没命中任何规则时回到默认档（ask）——**默认放行等于规则表形同虚设**。"""
    _set_rules(**{"chat.permission": "workspace", "sandbox.rules_allow": "Bash(ls)"})

    response = server_client.post(
        "/api/v1/sandbox/exec", json={"argv": ["curl", "https://x.test"]}
    )

    assert response.status_code == 409
    assert "确认" in response.json()["message"]


def test_remember_writes_a_word_prefix_rule(server_client: TestClient) -> None:
    """「以后都允许」写进放行清单的是**词前缀**，不是完整命令——
    记住完整命令等于没记住（下次参数就不同了）。

    读回走**服务层**（``runtime.get``）：``GET /settings`` 只在本机档，见模块头。
    """
    _set_rules(**{"chat.permission": "workspace", "sandbox.rules_allow": ""})

    server_client.post(
        "/api/v1/sandbox/exec",
        json={"argv": ["git", "status", "--short"], "approved": True, "remember": True},
    )

    assert get_services().runtime.get("sandbox.rules_allow").strip() == "Bash(git:*)"


def test_remember_does_not_duplicate(server_client: TestClient) -> None:
    _set_rules(**{"chat.permission": "workspace", "sandbox.rules_allow": "Bash(git:*)"})

    server_client.post(
        "/api/v1/sandbox/exec",
        json={"argv": ["git", "commit"], "approved": True, "remember": True},
    )

    assert get_services().runtime.get("sandbox.rules_allow").count("Bash(git:*)") == 1
