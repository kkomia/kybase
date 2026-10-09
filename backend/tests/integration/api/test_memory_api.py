"""记忆端点的集成测试（v0.14 三期；v0.56 起是**一份档案**，见设计文档 §6.3、§7.4）。

镜像同构：``app/api/v1/memory.py`` → ``tests/integration/api/test_memory_api.py``。

覆盖四件事：

1. **文件读取是走本地目录的**，所以 ``memory.enabled=false`` 时也该能用
   ——记忆关着的时候，用户依然该能打开自己的文件看看写了什么；
2. **查证要跑在真实工作区上**：从这里进去的请求要能命中磁盘上的变更流、
   带回文件与行号，噪声查询返回空；关着时**明确报错**（不是返回空结果，见 §2.3）；
3. **路径越界要挡住**：路径由前端传进来，这是读端点唯一的安全边界；
4. **两个端点确实没了**（档案制 §6.3、§7.4）：``PUT``/``DELETE /memory/files/{path}``
   （绕过预算与变更流的后门）与 ``GET /memory/graph``（图谱退场）——
   它们不该以任何形态存在，所以断言 404/405 而不是"返回了错误码"。

**没有"测试连接"这类端点了**（v0.46 删）：记忆跑在我们自己的进程里，
没有第二个进程可连。所以这一份里也不再有任何 ``monkeypatch`` 打桩的 HTTP——
本地实现不需要假装别的东西活着，这本身就是那次改动的价值。

## 为什么这一份打**本机档**（NAS 网页端退役，2026-10-05）

这一份里有两族端点，它们的档在今天**只可能是本机档**：

- ``/memory*``：记忆本体在 ``<data_dir>/memory``（本机目录），**两档都有**；
- ``/settings``：开关走运行期配置，而那张表（``app_settings``）**只在本机档**
  （NAS 上那份设置页随网页端一起退役）。

所以用 `conftest.local_client`：**不带凭据**（本机档不设门禁，主体短路成"本机主人"）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services

pytestmark = pytest.mark.local


@pytest.fixture
def client(local_client: TestClient) -> TestClient:
    """本机档客户端（记忆 + 它的开关都只在本机档成立）。"""
    return local_client


_ARCHIVE = """---
updated: 2026-10-03
---

# 用户档案

## 身份与称呼

- 用户叫小又，称呼「小又」即可。

## 长期偏好与风格

- 用户要求回答先给结论，再列依据。

## 进行中的项目

### 内网部署

- 项目目标是不出公网。

## 工具与环境

- 用户的内网有一台 L20。
"""

_CHANGES = """- 2026-10-01 09:20 · 新增 · 长期偏好与风格 · 来源：显式
  - 新：用户偏好先给结论
- 2026-10-02 14:05 · 顶替 · 长期偏好与风格 · 来源：显式
  - 旧：用户偏好先给结论
  - 新：用户要求回答先给结论，再列依据。
"""


@pytest.fixture
def workspace(client: TestClient):
    """往真实的工作区目录里放一份像样的档案，返回那个目录。

    用 settings 算出来的路径（而不是自己拼一个）——不然测试和产品会在
    "工作区到底在哪"这件事上各说各话。
    """
    root = get_services().memory.workspace
    (root / "daily" / "2026-09-16").mkdir(parents=True)
    (root / "digest" / "personal").mkdir(parents=True)
    (root / "session" / "dialog").mkdir(parents=True)
    (root / "PROFILE.md").write_bytes(_ARCHIVE.encode())
    (root / "changes.md").write_bytes(_CHANGES.encode())
    # 旧记忆（已退场）：文件留盘、界面显示成只读，后端不再消费它
    (root / "MEMORY.md").write_bytes(
        "---\nsummary: 核心\n---\n\n## 核心长期记忆\n\n- 用户偏好先给结论\n".encode()
    )
    (root / "SOUL.md").write_bytes("# 我是 KYLAB\n".encode())
    (root / "daily" / "2026-09-16" / "会话一.md").write_bytes(
        "# 今天的现场\n\n结论见 [[digest/personal/锂价.md]]\n".encode()
    )
    (root / "digest" / "personal" / "锂价.md").write_bytes(
        "---\ntags: [锂价]\n---\n\n# 锂价敏感性\n\n来源 [[会话一]]\n"
        "\n锂价下跌 10% 会让电池业务毛利下降约 0.8 个百分点。\n".encode()
    )
    (root / "session" / "dialog" / "conv_x.md").write_bytes("# 原始对话\n".encode())
    return root


def _enable(client: TestClient, **values: str) -> None:
    """把记忆打开（以及需要时改别的项）。走设置端点，与用户的操作同一条路。

    设置端点的请求体是 ``{"values": [{"key": …, "value": …}]}``——**是列表不是字典**，
    因为一个键可能被重复提交，而且这样服务端能按序报出"哪个键不认识"。
    """
    payload = {"memory.enabled": "true", **values}
    response = client.patch(
        "/api/v1/settings",
        json={"values": [{"key": key, "value": value} for key, value in payload.items()]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rejected"] == [], response.text


# --------------------------------------------------------------- 状态与列表


def test_overview_lists_files_and_counts(client: TestClient, workspace) -> None:
    body = client.get("/api/v1/memory").json()
    paths = [item["path"] for item in body["files"]]

    # **默认开**（v0.56 改，§7.3）：注入一次模型调用都不产生，所以"默认关"
    # 只等于"这个功能默认不存在"
    assert body["status"]["enabled"] is True
    # 夹具里留着旧的 ``MEMORY.md``（"旧记忆（只读）"，§7.2）：它还在，但已经不注入了
    assert body["status"]["core_file_exists"] is True
    assert body["status"]["file_count"] == len(paths)
    # 派生物目录不进列表：原始对话不是记忆
    assert "session/dialog/conv_x.md" not in paths
    assert {
        "PROFILE.md",
        "changes.md",
        "MEMORY.md",
        "SOUL.md",
        "daily/2026-09-16/会话一.md",
    } <= set(paths)
    assert body["truncated"] is False


def test_overview_reports_which_files_are_injected(client: TestClient, workspace) -> None:
    """``injected`` 报的是**那三份设定文件**（两份人设 + 档案）——
    ``MEMORY.md`` **不在里面**：它已经退场（§7.2），界面上显示成"旧记忆（只读）"。"""
    by_path = {item["path"]: item for item in client.get("/api/v1/memory").json()["files"]}

    assert by_path["MEMORY.md"]["injected"] is False
    assert by_path["PROFILE.md"]["injected"] is True
    assert by_path["SOUL.md"]["injected"] is True
    assert by_path["daily/2026-09-16/会话一.md"]["injected"] is False


def test_overview_reports_local_counts(client: TestClient, workspace) -> None:
    """状态是**纯本地的数字**：几份文件、上次更新。

    原先还有"几份可召回、可召回几条"那两个读数——它们数的是旧召回池
    （``daily/`` / ``digest/``）切出来的块，而检索那一路已经退场、代码不再消费
    那些文件，所以两个读数连同它们的口径一起删了。
    """
    status = client.get("/api/v1/memory").json()["status"]

    assert status["file_count"] > 0
    assert status["last_changed_at"], "有文件就该有'上次更新'时间"
    # **没有连通性字段了**：没有第二个进程可连，也就没有"连没连上"这回事
    assert "reachable" not in status
    assert "base_url" not in status
    assert "retrievable_count" not in status and "entry_count" not in status


# ------------------------------------------------------------------- 已删除的端点


def test_the_graph_endpoint_is_gone(client: TestClient, workspace) -> None:
    """图谱退场（§6.3）：端点**不该以任何形态存在**。

    留一个"返回错误码"的路由也算留了一条实现路径（它会被 OpenAPI 列出来、
    被客户端当成"有这个东西但坏了"），所以断言 404。
    """
    assert client.get("/api/v1/memory/graph").status_code == 404


def test_file_level_write_endpoints_are_gone(client: TestClient, workspace) -> None:
    """``PUT`` / ``DELETE /memory/files/{path}`` 是**绕过预算与变更流的后门**（§6.3）。

    整份覆盖能一次撑爆预算、也能删掉一条而不留痕，所以这两个方法整个删掉：
    档案的写入只有 ``POST /memory/remember``（外加界面上的条目级编辑）。
    断言 405（路由在、方法不在）或 404（路由也没了）都算"到不了处理器"。
    """
    path = "digest/personal/锂价.md"
    put = client.put(f"/api/v1/memory/files/{path}", json={"content": "# 我改了\n"})
    delete = client.delete(f"/api/v1/memory/files/{path}")

    assert put.status_code in (404, 405)
    assert delete.status_code in (404, 405)
    # 文件一个字都没变
    assert "电池业务毛利" in (workspace / path).read_text(encoding="utf-8")


# ------------------------------------------------------------------- 读文件


def test_read_file_roundtrip(client: TestClient, workspace) -> None:
    """读原文（含 frontmatter）——界面上的「原文」视图与迁移草稿都靠它。"""
    body = client.get("/api/v1/memory/files/digest/personal/锂价.md").json()

    assert body["content"].startswith("---")
    assert body["meta"] == {"tags": ["锂价"]}
    assert body["title"] == "锂价敏感性"


def test_reading_works_even_when_memory_is_disabled(client: TestClient, workspace) -> None:
    """**关着记忆也能读自己的文件**：要求"先打开一个开关才能读自己的文本文件"
    是没道理的（这条界线写在 services/memory_files.py 的模块头）。

    对照：``recall`` 关着时 422（见下一条），而档案的读写不看那个开关。
    """
    _disable(client)

    assert client.get("/api/v1/memory").json()["status"]["enabled"] is False
    assert client.get("/api/v1/memory/files/PROFILE.md").status_code == 200


@pytest.mark.parametrize(
    "bad",
    ["../backend.env", "daily/../../x.md", "C:foo.md", "notes.txt", "a.md:stream"],
)
def test_path_escape_never_reaches_a_file(client: TestClient, workspace, bad: str) -> None:
    """越界/非法路径一律拒。**这是读端点唯一的安全边界**：路径由前端给出。

    这里断言的是"**没有 200**"而不是某个具体码，因为两条防线各自生效：

    - ``../backend.env`` 这类会被 HTTP 客户端**先归一化**（httpx 把它折成
      ``/api/v1/memory/backend.env``），于是根本没匹配上路由 → 404。
      真实的浏览器也一样，所以这条路径上"到不了处理器"是可接受的结局；
    - 但**裸客户端**（``curl --path-as-is``）能把未归一化的路径送到处理器，
      那时 ``safe_path`` 必须拦住它——那一条在
      ``tests/unit/services/test_memory_files.py`` 里逐条钉着（含 ``..``、
      盘符、ADS 数据流、控制字符）。协议层的用例只负责证明"文件读不出来"。
    """
    response = client.get(f"/api/v1/memory/files/{bad}")
    assert response.status_code != 200, response.text
    assert response.status_code in (404, 422), response.text


# --------------------------------------------------------------- 查证与记住


def _disable(client: TestClient) -> None:
    response = client.patch(
        "/api/v1/settings",
        json={"values": [{"key": "memory.enabled", "value": "false"}]},
    )
    assert response.status_code == 200, response.text


def test_recall_requires_memory_enabled(client: TestClient, workspace) -> None:
    """关着时**明确报错**，不返回空结果——返回空会让模型以为"没有相关记忆"，
    然后基于错误前提继续推理（§2.3）。

    断言 ``code`` 与那句话（而不是只断言状态码）：这一层的价值全在"说清为什么"，
    只对状态码的话，把文案改成"参数不合法"也能过。
    """
    _disable(client)

    response = client.post("/api/v1/memory/recall", json={"query": "锂价"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "未启用长期记忆" in response.json()["message"]


def test_recall_finds_a_real_change_with_source(client: TestClient, workspace) -> None:
    """查证跑在真实工作区的**变更流**上：命中要带回文件 + 行号 + 分数。

    行号是界面"点一下跳到那一条"的依据；``path`` 恒为 ``changes.md``
    （档案本身每轮已经注入，不在池子里，§5.3）。
    """
    _enable(client)

    body = client.post("/api/v1/memory/recall", json={"query": "用户要求回答先给结论"}).json()

    assert body["hits"], f"这条改动就在磁盘上，必须能查到：{body}"
    top = body["hits"][0]
    assert top["path"] == "changes.md"
    assert top["start_line"] == 3, "第二条记录（第一条占 2 行）"
    assert "先给结论" in top["text"]
    assert top["score"] > 0
    # 那句话必须提醒这是变更流、不是知识库原文（与 MCP 同一句）
    assert "变更流" in body["note"]


def test_recall_returns_empty_for_a_noise_query(client: TestClient, workspace) -> None:
    """**开着时返回空是诚实的答案**（检索确实跑过了），与"关着时返回空"不是一回事。

    本地检索没有"连不上"这种中间态，所以空只有一个含义：
    变更流里确实没有相关的话。
    """
    _enable(client)

    body = client.post("/api/v1/memory/recall", json={"query": "合唱团的排练时间安排"}).json()

    assert body["hits"] == []


def test_recall_never_returns_document_pool_content(client: TestClient, workspace) -> None:
    """两池不混（§2.1，判据 §9.2 第 8 条）在这一层的证据。

    召回只读这个工作区的**变更流**：既不碰任何文档片段（这一路根本不 import
    检索服务），也不返回已经注入的档案条目，更不碰 ``daily/``/``digest/``
    那一层（它已经不在池子里了，§5.3）。

    三个查询各有它证明的东西：档案条目（已注入）、digest 里的原文、
    知识库那种说法——**一个都不该出现在结果里**。
    """
    _enable(client)

    for query in ("用户叫小又", "锂价下跌对毛利的影响", "知识库里的那段原文"):
        body = client.post("/api/v1/memory/recall", json={"query": query}).json()
        assert body["hits"] == [], query
        assert all(hit["path"] == "changes.md" for hit in body["hits"])


def test_recall_limit_is_clamped_by_the_schema(client: TestClient, workspace) -> None:
    _enable(client)
    assert (
        client.post("/api/v1/memory/recall", json={"query": "x", "limit": 999}).status_code
        == 422
    )


def test_remember_returns_the_action_and_the_receipt(client: TestClient, workspace) -> None:
    """写入返回 ``action`` 与 ``receipt``（§4.4）：界面与模型用的是**同一句话**。

    四种动作在这一条里一次走完：added → existing（同一条再记一次）→
    replaced（带 ``replaces`` 更正）→ rejected（超单条上限）。
    """
    _enable(client)
    fact = "用户要求在周五先复盘"

    added = client.post("/api/v1/memory/remember", json={"content": fact}).json()
    assert added["action"] == "added"
    assert fact in added["receipt"]
    assert added["section"] == "长期偏好与风格"

    again = client.post("/api/v1/memory/remember", json={"content": fact}).json()
    assert again["action"] == "existing"
    assert "已经有了" in again["receipt"]

    corrected = client.post(
        "/api/v1/memory/remember",
        json={"content": "用户要求在周五先复盘，再定下周计划", "replaces": fact},
    ).json()
    assert corrected["action"] == "replaced"
    assert "改成" in corrected["receipt"]
    assert corrected["replaced"] == fact

    rejected = client.post("/api/v1/memory/remember", json={"content": "长" * 121}).json()
    assert rejected["action"] == "rejected"
    assert "拆成两条" in rejected["receipt"]

    # 档案里是最后那一条，被顶替的进了变更流
    content = client.get("/api/v1/memory/files/PROFILE.md").json()["content"]
    assert "再定下周计划" in content
    changes = client.get("/api/v1/memory/files/changes.md").json()["content"]
    assert fact in changes


def test_remember_does_not_require_the_switch(client: TestClient, workspace) -> None:
    """**「记住」与 ``recall`` 不是同口径**：档案的读写不看那道闸（§7.3），
    它只管注入与 recall。

    对照：``recall`` 关着时仍然 422（见上面那条）。
    """
    _disable(client)

    response = client.post("/api/v1/memory/remember", json={"content": "项目代号叫 kylab"})

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "added"
    content = client.get("/api/v1/memory/files/PROFILE.md").json()["content"]
    assert "项目代号叫 kylab" in content


def test_remember_rejects_an_overlong_payload_at_the_transport_layer(
    client: TestClient, workspace
) -> None:
    """500 字以上是**协议层**挡的（与 service 的常量同源）。

    它是让请求别白读一遍的粗护栏；真正的单条上限是 120 字，由服务层以**回执**
    拒绝（见上一条）。
    """
    _enable(client)
    response = client.post("/api/v1/memory/remember", json={"content": "字" * 501})
    assert response.status_code == 422


def test_remember_rejects_sensitive_content(client: TestClient, workspace) -> None:
    """敏感信息**绝不进档案**（§4.2 的否决项）：档案每轮都进上下文。"""
    _enable(client)

    body = client.post(
        "/api/v1/memory/remember", json={"content": "用户的令牌是 sk-abcdefghijklmno"}
    ).json()

    assert body["action"] == "rejected"
    assert "不进档案" in body["receipt"]
    assert "sk-abc" not in (workspace / "PROFILE.md").read_text(encoding="utf-8")


# --------------------------------------------------------------- 档案卡与变更流


def test_archive_endpoint_reports_sections_budget_and_source(
    client: TestClient, workspace
) -> None:
    """档案卡一回给全：分区（含读数）、条目、来源、草稿、迁移入口（§6.1）。

    来源小字不是档案文件里有的东西——它取自变更流里**最近把它写进来的那条记录**，
    所以这一条同时钉住"档案条目 ↔ 变更流记录"能对上（否则界面上那一行永远是空的）。
    """
    body = client.get("/api/v1/memory/archive").json()

    assert body["path"] == "PROFILE.md"
    assert body["updated"] == "2026-10-03"
    assert body["budget"] == {
        "entries": 4,
        "chars": sum(
            len(line[2:])
            for line in _ARCHIVE.splitlines()
            if line.startswith("- ")
        ),
        "entry_limit": 60,
        "char_limit": 4000,
    }

    names = [item["name"] for item in body["sections"]]
    assert names == ["身份与称呼", "长期偏好与风格", "进行中的项目", "工具与环境"]
    assert all(item["known"] for item in body["sections"])

    projects = next(item for item in body["sections"] if item["name"] == "进行中的项目")
    assert [group["name"] for group in projects["groups"]] == ["内网部署"]
    assert projects["group_limit"] == 8

    preference = next(item for item in body["sections"] if item["name"] == "长期偏好与风格")
    fact = "用户要求回答先给结论，再列依据。"
    entry = next(item for item in preference["items"] if item["text"] == fact)
    assert entry["source"] == "显式"
    assert entry["change_at"] == "2026-10-02 14:05"
    # 变更流倒序给，但 index 是文件顺序：这条来自第 2 条记录（下标 1）
    assert entry["change_index"] == 1

    assert body["draft"]["path"] == "import-draft.md"
    # 有可折叠的旧数据（MEMORY.md / digest）且还没迁过 → 入口出现
    assert body["migration_available"] is True


def test_archive_endpoint_reports_unknown_section(client: TestClient, workspace) -> None:
    """未知分区照常返回、计入读数、``known=false``（§3.5 第 4 条）。"""
    (workspace / "PROFILE.md").write_bytes(
        (_ARCHIVE + "\n## 朋友与家人\n\n- 用户有个弟弟在读书。\n").encode()
    )

    body = client.get("/api/v1/memory/archive").json()

    unknown = next(item for item in body["sections"] if item["name"] == "朋友与家人")
    assert unknown["known"] is False
    assert [item["text"] for item in unknown["items"]] == ["用户有个弟弟在读书。"]
    assert body["budget"]["entries"] == 5


def test_changes_endpoint_is_newest_first(client: TestClient, workspace) -> None:
    """变更流倒序给，每条带 ``index``（文件顺序）与 ``restorable``（§6.2）。"""
    body = client.get("/api/v1/memory/changes").json()

    changes = body["changes"]
    assert [item["action"] for item in changes] == ["顶替", "新增"]
    assert [item["index"] for item in changes] == [1, 0]
    assert changes[0]["old"] == "用户偏好先给结论"
    assert changes[0]["new"] == "用户要求回答先给结论，再列依据。"
    # 顶替有旧值可写回 → 可还原；新增没有 → 不可还原
    assert changes[0]["restorable"] is True
    assert changes[1]["restorable"] is False


def test_forget_then_restore_round_trips(client: TestClient, workspace) -> None:
    """忘掉 = 删除 + 留痕 + 可还原（§9.2 第 6 条），REST 与工具同一套语义。"""
    fact = "用户要求回答先给结论，再列依据。"

    forgotten = client.post("/api/v1/memory/forget", json={"topic": fact}).json()
    assert forgotten["action"] == "forgotten"
    assert fact not in (workspace / "PROFILE.md").read_text(encoding="utf-8")

    restored = client.post("/api/v1/memory/restore", json={"text": fact}).json()
    assert restored["action"] == "restored"
    assert fact in (workspace / "PROFILE.md").read_text(encoding="utf-8")


def test_group_rename_rewrites_the_group_and_logs_one_change(
    client: TestClient, workspace
) -> None:
    """组改名 = 一次顶替，旧名进变更流（§3.1 第 2 条）：正文一个字不改。"""
    body = client.post(
        "/api/v1/memory/group",
        json={"section": "进行中的项目", "old": "内网部署", "new": "内网知识库"},
    ).json()

    assert body["action"] == "replaced"
    content = (workspace / "PROFILE.md").read_text(encoding="utf-8")
    assert "### 内网知识库" in content
    assert "### 内网部署" not in content
    assert "项目目标是不出公网。" in content

    changes = (workspace / "changes.md").read_text(encoding="utf-8")
    assert "旧：内网部署" in changes
    assert "新：内网知识库" in changes

    # 还原把组名改回去
    client.post("/api/v1/memory/restore", json={"text": "内网部署"})
    assert "### 内网部署" in (workspace / "PROFILE.md").read_text(encoding="utf-8")


def test_migrate_folds_old_files_and_never_touches_them(client: TestClient, workspace) -> None:
    """机械折叠：折进档案、**旧文件一个字节都不动**、重跑净改动为零（§8.4）。"""
    memory_before = (workspace / "MEMORY.md").read_bytes()
    digest_path = workspace / "digest" / "personal" / "锂价.md"
    digest_before = digest_path.read_bytes()

    report = client.post("/api/v1/memory/migrate").json()
    assert report["skipped"] is False
    assert report["added"] >= 1
    assert (workspace / "MEMORY.md").read_bytes() == memory_before
    assert digest_path.read_bytes() == digest_before

    archive_text = (workspace / "PROFILE.md").read_text(encoding="utf-8")
    assert "用户偏好先给结论" in archive_text
    # 折叠进变更流（来源：迁移），所以界面看得见它怎么来的
    changes = (workspace / "changes.md").read_text(encoding="utf-8")
    assert "来源：迁移" in changes

    # 迁完入口消失（指纹与水位一致）
    assert client.get("/api/v1/memory/archive").json()["migration_available"] is False
    # 再跑一次：跳过、净改动为零
    again = client.post("/api/v1/memory/migrate").json()
    assert again["skipped"] is True


# ------------------------------------------------------------------- 别的


def test_there_is_no_probe_endpoint(client: TestClient, workspace) -> None:
    """**「测试连接」这个端点删掉了**（v0.46）：记忆跑在我们自己的进程里，
    没有第二个进程可连——留着它只会在界面上引出一串用户不该读的实现细节
    （地址、端口、异常原文）。

    断言 404 而不是 403/405：它不该以任何形态存在。
    """
    assert client.post("/api/v1/memory/probe").status_code == 404


def test_the_settings_group_has_no_service_address(client: TestClient, workspace) -> None:
    """设置里「长期记忆」那一组**不再有服务地址**（那是 ReMe 的遗物）。

    这条钉的是配置面：字段名一旦漏出去，界面会自动把它渲染成一个输入框
    （设置页的字段是后端给的，见 ``SettingsModal`` 的 featureGroups），
    于是用户会看到一个填了也没用的地址栏。
    """
    groups = client.get("/api/v1/settings").json()["groups"]
    memory_group = next(item for item in groups if item["key"] == "memory")
    keys = [field["key"] for field in memory_group["fields"]]

    assert keys == [
        "memory.enabled",
        "memory.capture",
        "memory.capture_model",
        "memory.workspace",
        "memory.persona_files",
    ]
    assert all("base_url" not in key and "service_scope" not in key for key in keys)
    # 旧的四项（定时捕获 / 整理 / 向量）连字段都不该在——留着会让设置页显示几个
    # "改了没有任何效果"的开关（期五清理）
    assert not any("capture_every" in key or "dream" in key or "vector" in key for key in keys)
