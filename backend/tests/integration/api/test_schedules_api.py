"""定时任务端点（v0.33）。

镜像同构：``app/api/v1/schedules.py`` → 本文件。

这一组钉住的是**这个产品面能不能被正常使用**：

1. **建得出、列得出、改得动、删得掉**（含"改时间要重算下次"——
   不重算的话"把每天 9 点改成 18 点"会等到下一个 9 点才生效，而用户以为改好了）；
2. **时间说得清**：响应里带人话描述与服务器时区（cron 按服务器时区解释，
   不把时区回给界面的话，"每天 9 点"是哪个 9 点就成了猜）；
3. **立即跑一次真的排上了**（它不是装饰：新建之后最想确认的就是"它会跑成什么样"）；
4. **越权一律 404**（与知识库/工作区同一口径：403 会暴露"这个 id 存在"）。

## 为什么这一份打**本机档**（NAS 网页端退役，2026-10-05）

``/scheduled-tasks`` 那一族**只在本机档存在**：定时任务的产物是**会话**，而会话落本机
（见 `api/v1/router.py` 的 `local_router` 那一段）。所以 `client` 是
`conftest.local_client`：**不带凭据**（本机档不设门禁，主体短路成"本机主人"）。

两处随档位改了口径（都在原处留了说明）：**"立即跑一次"在本机档是就地跑**
（`services/schedules.run_now` 走 `runner` 接缝，不进队列表——本机没有那张表），
所以判据改成"回来的运行标识是真的"；**"成员只看自己的"**只对有账号体系的那一档成立
（本机档不挂 `/auth/*`、`users` 表不在本机库里），已摘掉。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services

pytestmark = pytest.mark.local


@pytest.fixture
def client(local_client: TestClient) -> TestClient:
    """本机档客户端（`conftest.local_client`；定时任务只在本机档存在）。"""
    return local_client


def _create(client: TestClient, **overrides: object) -> dict:  # type: ignore[type-arg]
    payload = {
        "name": "每日早报",
        "prompt": "把昨天的构建日志汇总成三条结论",
        "kind": "cron",
        "cron": "0 9 * * *",
    }
    payload.update(overrides)
    response = client.post("/api/v1/scheduled-tasks", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 建 / 列


def test_create_returns_the_next_run_and_a_readable_schedule(client: TestClient) -> None:
    item = _create(client)
    assert item["kind"] == "cron"
    assert item["enabled"] is True
    assert item["next_run_at"]
    assert item["schedule_text"] == "每天 09:00"
    assert item["run_count"] == 0
    assert item["conversation_id"] is None  # 没跑过就不该先占一条会话


def test_list_carries_the_server_timezone(client: TestClient) -> None:
    """cron 按**服务器本地时间**解释，所以界面必须能把这件事说出来。"""
    _create(client)
    payload = client.get("/api/v1/scheduled-tasks").json()
    assert payload["timezone"]
    assert len(payload["items"]) == 1


def test_create_once_task_with_a_future_time(client: TestClient) -> None:
    when = datetime.now().astimezone() + timedelta(hours=3)
    item = _create(client, kind="once", cron="", run_at=when.isoformat())
    assert item["kind"] == "once"
    assert "跑一次" in item["schedule_text"]


# ------------------------------------------------------------------ 校验


def test_a_time_in_the_past_is_rejected(client: TestClient) -> None:
    """不建"永远不会跑"的记录：那种记录在界面上表现为"建好了但一直没动"。"""
    when = datetime.now().astimezone() - timedelta(hours=1)
    response = client.post(
        "/api/v1/scheduled-tasks",
        json={"name": "过期", "prompt": "跑", "kind": "once", "run_at": when.isoformat()},
    )
    assert response.status_code == 422
    assert "已经过去" in response.json()["message"]


def test_bad_cron_is_rejected_with_a_readable_reason(client: TestClient) -> None:
    response = client.post(
        "/api/v1/scheduled-tasks",
        json={"name": "坏的", "prompt": "跑", "kind": "cron", "cron": "0 25 * * *"},
    )
    assert response.status_code == 422
    assert "小时" in response.json()["message"]


def test_empty_prompt_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/scheduled-tasks", json={"name": "空的", "prompt": "  ", "cron": "0 9 * * *"}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ 改


def test_changing_the_time_recomputes_the_next_run(client: TestClient) -> None:
    item = _create(client)
    before = item["next_run_at"]
    updated = client.patch(
        f"/api/v1/scheduled-tasks/{item['id']}", json={"cron": "0 18 * * *"}
    ).json()
    assert updated["schedule_text"] == "每天 18:00"
    assert updated["next_run_at"] != before


def test_disabling_clears_the_next_run(client: TestClient) -> None:
    """停用还挂着下次时间，会让人以为它还会跑（而它不会）。"""
    item = _create(client)
    disabled = client.patch(f"/api/v1/scheduled-tasks/{item['id']}", json={"enabled": False}).json()
    assert disabled["enabled"] is False
    assert disabled["next_run_at"] is None
    reenabled = client.patch(f"/api/v1/scheduled-tasks/{item['id']}", json={"enabled": True}).json()
    assert reenabled["next_run_at"]


def test_delete_removes_the_schedule(client: TestClient) -> None:
    item = _create(client)
    assert client.delete(f"/api/v1/scheduled-tasks/{item['id']}").status_code == 204
    assert client.get("/api/v1/scheduled-tasks").json()["items"] == []


def test_unknown_id_is_404(client: TestClient) -> None:
    assert client.patch("/api/v1/scheduled-tasks/sched_none", json={}).status_code == 404
    assert client.delete("/api/v1/scheduled-tasks/sched_none").status_code == 404


# ------------------------------------------------------------------ 立即跑一次


@pytest.fixture
def runner(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """本机档那条**执行接缝**（`services/schedules.run_now` 里的 ``_runner``）。

    为什么要手动装一个：测试夹具一律 ``KYLAB_RUN_WORKER=false``（测试要手动驱动
    worker、对时序下断言），于是本机档不绑调度器，`run_now` 会走"入队"那条路——
    而本机没有队列表，端点是 503。装上这个之后，这两条用例才是在验
    "它把这次运行交给了谁"（而不是在验"端点回了 503"这种假绿）。

    返回的是被提交的 scheduled_id 列表（用例据此断言真的交给了执行那条路）。
    """
    submitted: list[str] = []

    def submit(scheduled_id: str) -> str:
        submitted.append(scheduled_id)
        return "task_manual_run"

    # `run_now` 用的是 `self._runner.submit(id)`（不是"可调对象"），所以给一个有那个方法的
    # 小对象——接口形状照 `workers/local_worker.py` 那个真调度器，只把实际执行换掉。
    monkeypatch.setattr(get_services().schedules, "_runner", SimpleNamespace(submit=submit))
    return submitted


def test_run_now_enqueues_a_real_task(client: TestClient, runner: list[str]) -> None:
    """「立即跑一次」真的把这一条交给了执行那条路（不是只回了一句人话）。

    **两档的口径不同、落点也不同**（见 `services/schedules.run_now`）：服务器档是**入队**
    （那一行能在 `GET /tasks` 里看到）；本机档是**就地跑**——交给 ``runner`` 接缝，
    本机没有那张队列表。所以本机档的判据是"交出去的那一条就是它"。
    """
    item = _create(client)

    payload = client.post(f"/api/v1/scheduled-tasks/{item['id']}/run").json()

    assert payload["task_id"], payload
    assert payload["detail"], payload
    assert runner == [item["id"]], "端点没有把它交给执行那条路"


def test_run_now_does_not_move_the_schedule(client: TestClient, runner: list[str]) -> None:
    """手动跑一次**不改变周期**——否则点一下"立即跑"就把每天 9 点挪到了别处。"""
    item = _create(client)
    client.post(f"/api/v1/scheduled-tasks/{item['id']}/run")
    assert runner == [item["id"]], "这一条要先真的跑起来，下面的断言才有意义"
    again = client.get("/api/v1/scheduled-tasks").json()["items"][0]
    # 比**时刻**而不是比字符串：同一时刻可以写成 +08:00 也可以写成 Z
    assert datetime.fromisoformat(again["next_run_at"]) == datetime.fromisoformat(
        item["next_run_at"]
    )


# ------------------------------------------------------------------ 归属


# **摘掉一条**（2026-10-05）：``test_members_only_see_their_own_schedules``
# （原判据：成员看不见、也改不动别人的定时任务，404 而不是 403）——
# "成员"这件事在本机档不存在：`/auth/*` 不挂本机档、`users` / `sessions` 两张表
# 都不在本机库里（见 `api/v1/router.py` 里"明确不挂"那一段），主体恒为"本机主人"，
# 造不出第二个身份（原用例的 `services.users.create(...)` 在本机档直接不可用）。
# 而定时任务只在本机档存在 ⇒ 这条断言没有一个装机形态能承接。
# 归属判定本身（`owner_id` 那一条）留在 `services/schedules` 里没动，
# 由 `tests/unit/services/` 那一份按服务层覆盖。
