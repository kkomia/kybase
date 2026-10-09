"""对话留存的 HTTP 行为（§11.2）。

镜像同构：``app/api/v1/conversations.py`` → 本文件。

这里要证的是"端到端真的存下来了"：会话能建/能列/能改/能删，消息与产物读得回来，
分支与归档各自说得清。

## 为什么这一份打**本机档**（NAS 网页端退役，2026-10-05）

会话面（列表 / 详情 / 消息 / 产物 / 文件区 / 分支 / 归档）**只在本机档存在**：
服务器档那张表里只留了 `/conversations/export`（见 `api/v1/router.py` 的模块头），
NAS 上那份会话数据不迁移、直接丢。所以 `client` 就是 `conftest.local_client`：
**不带凭据**（本机档不设门禁，调用主体由 `api/auth.py::current_caller` 短路成"本机主人"）。

**`/chat` 与 `/chat/stream` 那条落库链**（"提问 → 两条消息落库""历史以库为准"
"标题从第一个提问来"……）已经不在这份用例里：它随会话链路一起退役，本机档的对话走
**边车**的 `/turn*`。逐条的承接者写在下面对应那一节的注释里。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services

pytestmark = pytest.mark.local

#: 会话的库范围：一个**占位 id**。
#:
#: 本机档**没有知识库**（KB 在 NAS 上，`/knowledge-bases` 不挂本机档），而 `kb_ids`
#: 在会话这一层只是**一串 id**——`ConversationService.create` 不校验它存在。这一份要验的
#: 是"范围被记下来、能被回读、能随分支带过去"，不是"这个库在不在"，所以用固定值。
KB_ID = "kb_local"


@pytest.fixture
def client(local_client: TestClient) -> TestClient:
    """本机档客户端（`conftest.local_client`；会话面只在本机档存在）。"""
    return local_client


@pytest.fixture
def kb_id() -> str:
    """会话的库范围（见 `KB_ID` 的说明：占位值，不是真库）。"""
    return KB_ID


def _conversation(client: TestClient, kb_id: str) -> str:
    response = client.post("/api/v1/conversations", json={"kb_ids": [kb_id]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------- CRUD


def test_create_lists_and_deletes(client: TestClient, kb_id: str) -> None:
    conv_id = _conversation(client, kb_id)

    listing = client.get("/api/v1/conversations").json()["items"]
    assert [item["id"] for item in listing] == [conv_id]
    assert listing[0]["message_count"] == 0

    assert client.delete(f"/api/v1/conversations/{conv_id}").status_code == 204
    assert client.get("/api/v1/conversations").json()["items"] == []


def test_rename(client: TestClient, kb_id: str) -> None:
    conv_id = _conversation(client, kb_id)

    response = client.patch(
        f"/api/v1/conversations/{conv_id}", json={"title": "眼科问题汇总"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "眼科问题汇总"
    assert client.get(f"/api/v1/conversations/{conv_id}").json()["title"] == "眼科问题汇总"


def test_rename_rejects_empty_title(client: TestClient, kb_id: str) -> None:
    conv_id = _conversation(client, kb_id)
    response = client.patch(f"/api/v1/conversations/{conv_id}", json={"title": ""})
    assert response.status_code == 422  # 参数校验挡在业务之前


def test_unknown_conversation_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/conversations/conv_不存在").status_code == 404
    assert client.delete("/api/v1/conversations/conv_不存在").status_code == 404


def test_detail_returns_messages(client: TestClient, kb_id: str) -> None:
    conv_id = _conversation(client, kb_id)
    services = get_services()
    services.conversations.append(conv_id, role="user", content="近视怎么监测")
    services.conversations.append(conv_id, role="assistant", content="看眼轴长度")

    detail = client.get(f"/api/v1/conversations/{conv_id}").json()

    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["message_count"] == 2


# ------------------------------------------------- 摘掉的：`/chat*` 那条落库链
#
# **这一整节随"NAS 网页端退役"一起摘掉**（2026-10-05）：`/chat` 与 `/chat/stream`
# 是**会话链路**那一族，两个档里都不再对外（服务器档退役、本机档的对话走**边车**的
# `/turn*`——见 `app/api/v1/chat.py` 与 `app/api/v1/router.py` 的模块头）。
# 下面这 9 条的原判据与承接者逐条写在案：
#
# 1. ``test_chat_persists_the_turn`` / ``test_stream_also_persists``（指定会话之后
#    提问与回答都要落库，流式与非流式两条路都要）——**承接**
#    `tests/integration/api/test_local_backend.py`（本机档整条链：`/turn/stream` 跑完
#    之后会话详情里是 user + assistant 两条）与 `tests/unit/test_sidecar.py`；
# 2. ``test_chat_without_conversation_does_not_persist``（不指定会话就不留垃圾会话）——
#    承接同上：边车 `/turn` 不带 `conversation_id` 时落 `LOCAL_CONVERSATION`，
#    由 `tests/unit/test_sidecar.py` 覆盖；
# 3. ``test_title_is_generated_from_the_first_question``——承接
#    `tests/unit/test_sidecar.py`（边车 `_record_turn` 的 `ensure_title` 那一步）；
# 4. ``test_chat_with_unknown_conversation_is_404``（指了不存在的会话要 404，不做静默新建）
#    ——承接：同一条纪律在边车那条链上由 `tests/unit/test_sidecar.py` 钉；
# 5. ``test_history_comes_from_the_database_not_the_request``（带会话时以库里的历史为准，
#    忽略请求里带的 history）——承接 `tests/unit/services/test_chat.py`
#    （`prepare_context` 那一层：历史从库里读）；
# 6. ``test_conversations_need_credentials``（没有凭据 401）——**只对有账号体系的那一档
#    成立**：会话只在本机档存在，而本机档不挂 `/auth/*`、主体恒为"本机主人"，
#    没有"没有凭据"这个状态（鉴权本身由 `test_auth_api.py` 覆盖）；
# 7. ``test_readonly_key_can_read_but_not_delete``（只读 API Key 能回看、不能删）——
#    同一条：API Key 属于**账号体系**（`api_keys` 表不在本机库里，本机档不挂
#    `/api-keys`），造不出那把钥匙。读写两档的判定在 `api/auth.py`，
#    由 `tests/unit/services/test_api_key.py` 与 `test_auth_api.py` 覆盖；
# 8. ``test_markdown_upload_placeholder``（顺带确认文档接口没被影响）——它打的是
#    `/knowledge-bases/{kb}/documents`，那属于**知识库那一族**（只服务器档有）。
#    承接：`tests/integration/api/test_rest_api.py::test_upload_returns_accepted_with_task`。
#
# 会话面**自己**的 CRUD / 分支 / 文件区（下面那些）一条没摘：它们在本机档仍然活着。


# --------------------------------------------------------------------- 会话级模型（v12）


def test_conversation_remembers_the_chosen_model(client: TestClient, kb_id: str) -> None:
    """建会话时选的模型要能读回来（列表与详情都带上）——界面靠它回填选择器。"""
    created = client.post(
        "/api/v1/conversations", json={"kb_ids": [kb_id], "model_pk": "mdl_pick"}
    ).json()

    assert created["model_pk"] == "mdl_pick"
    assert (
        client.get(f"/api/v1/conversations/{created['id']}").json()["model_pk"] == "mdl_pick"
    )
    listed = client.get("/api/v1/conversations").json()["items"]
    assert [item["model_pk"] for item in listed if item["id"] == created["id"]] == ["mdl_pick"]


def test_conversation_without_model_pk_stays_none(client: TestClient, kb_id: str) -> None:
    """没选就是 None（跟随全局默认），不落一个被猜出来的模型。"""
    created = client.post("/api/v1/conversations", json={"kb_ids": [kb_id]}).json()
    assert created["model_pk"] is None


# ------------------------------- 摘掉的：会话级模型 / 思考偏好那两条 `/chat` 链
#
# 这一节原来有五条，三条是纯会话面的（建会话时带 `model_pk` / 带 `thinking`，
# 读得回来）——**它们留着**（`test_conversation_remembers_the_chosen_model` /
# `test_conversation_without_model_pk_stays_none` /
# `test_conversation_can_be_created_with_thinking_preference`）。
#
# 另外三条**摘掉**（2026-10-05），因为它们判的都是 `/chat` 那条链的"请求级覆盖"
# 语义（`chat.py::_effective_model` / `_effective_thinking` 把请求里的值**回写会话**）：
#
# 1. ``test_chat_records_the_chosen_model_on_the_conversation``（在已有会话上用另一个模型
#    提问 → 该会话记住新选择）。承接：同一件事在本机档由**边车**那一轮做
#    （`app/sidecar.py::_record_turn` 写会话那一栏），HTTP 层由
#    `tests/unit/test_sidecar.py` 与 `tests/unit/test_sidecar_agent_face.py` 覆盖；
# 2. ``test_chat_request_thinking_override_reaches_the_model_and_persists`` /
#    ``test_chat_falls_back_to_the_conversation_thinking`` /
#    ``test_chat_without_conversation_uses_global_default``（请求级思考档覆盖 → 回写会话 →
#    下一次沿用）。承接：档位解析与"哪一档生效"的判定在 `services/chat.py` 与
#    `services/llm.py`，由 `tests/unit/services/test_chat_model_selection.py` 覆盖；
#    会话那一栏的读写由上面留下的两条与 `tests/unit/services/test_conversation.py` 覆盖。
#
# 判据：`/chat` 在两个档里都不再对外（会话链路退役），所以这四条没有一个装机形态
# 能承接它们的 **HTTP 层**——留下的部分是"会话记录里的模型 / 思考栏"，那几条没动。


def test_conversation_can_be_created_with_thinking_preference(
    client: TestClient, kb_id: str
) -> None:
    """建会话时给的思考档要能读回来（上一条留在原处的那三条之一）。"""
    created = client.post(
        "/api/v1/conversations",
        json={"kb_ids": [kb_id], "thinking": True, "thinking_effort": "low"},
    ).json()

    assert created["thinking"] is True
    assert created["thinking_effort"] == "low"


# ------------------------------------------------- 置顶 / 搜索 / 回退（v17）


def _two_turns(client: TestClient) -> str:
    """建一条会话并写进一轮问答（直接落库，不经过模型）。"""
    from app.core.services import get_services

    conversation = client.post("/api/v1/conversations", json={"kb_ids": []}).json()
    services = get_services()
    services.conversations.append(conversation["id"], role="user", content="眼轴怎么测")
    services.conversations.append(conversation["id"], role="assistant", content="用 AL 测量")
    return conversation["id"]


def test_patch_pins_a_conversation(client: TestClient) -> None:
    """置顶是 PATCH 的一个字段：与改名同属"整理这条会话"。"""
    first = _two_turns(client)
    second = client.post("/api/v1/conversations", json={"kb_ids": []}).json()["id"]

    response = client.patch(f"/api/v1/conversations/{first}", json={"pinned": True})

    assert response.status_code == 200, response.text
    assert response.json()["pinned"] is True
    # 列表里置顶的排最前（第二个是刚建的，不置顶）
    items = client.get("/api/v1/conversations").json()["items"]
    assert next(item["id"] for item in items) == first
    assert second in [item["id"] for item in items]


def test_patch_can_rename_and_pin_together(client: TestClient) -> None:
    """两个字段一起传就一起改；只传一个时另一个不动。"""
    conversation_id = _two_turns(client)

    body = client.patch(
        f"/api/v1/conversations/{conversation_id}", json={"title": "改过的标题", "pinned": True}
    ).json()

    assert (body["title"], body["pinned"]) == ("改过的标题", True)


def test_list_searches_by_title(client: TestClient) -> None:
    from app.core.services import get_services

    services = get_services()
    target = client.post("/api/v1/conversations", json={"kb_ids": []}).json()["id"]
    services.conversations.rename(target, "眼科指南问答")
    client.post("/api/v1/conversations", json={"kb_ids": []})

    items = client.get("/api/v1/conversations", params={"q": "眼科"}).json()["items"]

    assert [item["id"] for item in items] == [target]


def test_rewind_returns_the_question_and_removes_the_turn(client: TestClient) -> None:
    """「重新生成」的两步：先回退拿到提问，再由前端重发（重发走正常提问链路）。"""
    conversation_id = _two_turns(client)

    response = client.post(f"/api/v1/conversations/{conversation_id}/rewind", json={"turns": 1})

    assert response.status_code == 200, response.text
    assert response.json() == {"query": "眼轴怎么测", "removed": 2}
    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert detail["messages"] == []


def test_rewind_without_a_question_is_422(client: TestClient) -> None:
    empty = client.post("/api/v1/conversations", json={"kb_ids": []}).json()["id"]

    response = client.post(f"/api/v1/conversations/{empty}/rewind", json={})

    assert response.status_code == 422
    assert "没有可回退" in response.json()["message"]


# --------------------------------------------------------- 从这里重开（D11）

#: 假模型那句固定回答（`conftest.FakeChatModel` 的默认值）。原来它由 `/chat` 那条链
#: 落库，现在由 `_three_turns` 直接写——两边用同一个字面量，免得"分叉带过来的历史"
#: 那几条断言跟着别处改口径。
FAKE_ANSWER = "这是回答。[1]"


def _three_turns(client: TestClient, kb_id: str) -> str:
    """三轮问答的会话。

    **原来走 `/chat` 那条链落库**（提问与回答都经过那一轮的正常路径）——那条链随
    NAS 网页端退役一起摘了（见模块头那个"摘掉的：`/chat*` 那条落库链"）。
    现在改用**服务层**写：`ConversationService.record_turn` 正是那条链落库时调用的
    同一个方法（`chat.py::_record_turn` 逐字调它），所以"库里是三轮问答"这个前提
    一个字没变，分支那几条要的也正是它。
    """
    conv_id = _conversation(client, kb_id)
    services = get_services()
    for index in (1, 2, 3):
        services.conversations.record_turn(
            conv_id, question=f"第{index}问", answer=FAKE_ANSWER
        )
    return conv_id


def test_branch_carries_the_history_and_can_be_continued(
    client: TestClient, kb_id: str
) -> None:
    """**D11 的主验收**：从第 N 轮分叉 → 新会话带着到那一轮为止的历史 → 能在里面继续写。"""
    source = _three_turns(client, kb_id)

    response = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 2})

    assert response.status_code == 201, response.text
    branch = response.json()
    assert branch["id"] != source
    # 标题看得出是从哪儿分出来的（照抄源标题会让侧栏出现两条同名会话）
    assert branch["title"].endswith("（分支 · 第 2 轮）")
    # 只带前两轮（回答是上面那个固定字面量，所以这里能逐条比）
    detail = client.get(f"/api/v1/conversations/{branch['id']}").json()
    assert [m["content"] for m in detail["messages"]] == [
        "第1问",
        FAKE_ANSWER,
        "第2问",
        FAKE_ANSWER,
    ]

    # 在分叉出的会话里继续写一轮：与 `_three_turns` 同一条服务层落库路径
    # （`/chat` 那条链已经退役，见模块头）
    get_services().conversations.record_turn(
        branch["id"], question="换个方向再问", answer="分支里的回答"
    )
    after = client.get(f"/api/v1/conversations/{branch['id']}").json()
    assert [m["content"] for m in after["messages"]][-2] == "换个方向再问"
    assert after["message_count"] == 6


def test_branch_keeps_the_source_untouched_and_both_are_independent(
    client: TestClient, kb_id: str
) -> None:
    """**两条真的独立**：在分叉出的会话里加一轮，原会话的轮数与事件数一个都不变。"""
    source = _three_turns(client, kb_id)
    before = client.get(f"/api/v1/conversations/{source}").json()["message_count"]
    source_events = len(client.get(f"/api/v1/conversations/{source}/events").json()["items"])

    branch = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 1}).json()
    get_services().conversations.record_turn(
        branch["id"], question="分支里的新问题", answer="分支里的回答"
    )

    after = client.get(f"/api/v1/conversations/{source}").json()
    assert after["message_count"] == before
    assert (
        len(client.get(f"/api/v1/conversations/{source}/events").json()["items"]) == source_events
    )


def test_branch_keeps_the_file_area_empty(client: TestClient, kb_id: str) -> None:
    """**分叉不带文件区**（用户能感知到的边界，写进《API 接口规范》§1.9）。

    产物记录与对象存储里的字节都不搬：新会话"历史正文在、文件区是空的"；
    原会话那份一个字节不动。
    """
    source = _two_turns(client)
    upload = client.post(
        f"/api/v1/conversations/{source}/files",
        files={"file": ("报告.md", "# 报告".encode(), "text/markdown")},
    )
    assert upload.status_code == 201, upload.text

    branch = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 1}).json()

    assert client.get(f"/api/v1/conversations/{branch['id']}/files").json()["entries"] == []
    # 原会话那份还在（没有被搬走，也没有被删）
    assert len(client.get(f"/api/v1/conversations/{source}/files").json()["entries"]) == 1


def test_branch_carries_the_knowledge_base_scope(client: TestClient, kb_id: str) -> None:
    """知识库范围跟着走：分叉出来的会话继续用同一批资料。"""
    source = _three_turns(client, kb_id)

    branch = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 1}).json()

    assert branch["kb_ids"] == [kb_id]


def test_branch_of_a_branch_says_the_new_cut(client: TestClient, kb_id: str) -> None:
    """分叉出的会话再分叉是允许的；标题上的标记换成**新的那一处**，不一路接下去。"""
    source = _three_turns(client, kb_id)
    once = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 2}).json()
    assert once["title"].endswith("（分支 · 第 2 轮）")

    # 分叉出来的会话里有 2 轮，所以这里还能再往前分一次
    twice = client.post(f"/api/v1/conversations/{once['id']}/branch", json={"turn": 1})

    assert twice.status_code == 201, twice.text
    assert twice.json()["title"].endswith("（分支 · 第 1 轮）")
    assert twice.json()["title"].count("（分支") == 1


def test_branch_turn_beyond_the_end_is_422(client: TestClient, kb_id: str) -> None:
    source = _two_turns(client)

    response = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 3})

    assert response.status_code == 422
    assert "没有第 3 轮" in response.json()["message"]


def test_branch_turn_zero_is_422(client: TestClient, kb_id: str) -> None:
    """``0`` 不是"从头"——静默夹到边界会让用户以为分叉点就是他点的那一处。"""
    source = _two_turns(client)

    response = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 0})

    assert response.status_code == 422


def test_branch_turn_must_be_an_integer(client: TestClient, kb_id: str) -> None:
    source = _two_turns(client)

    response = client.post(f"/api/v1/conversations/{source}/branch", json={"turn": 1.5})

    assert response.status_code == 422


def test_branch_of_an_unreadable_conversation_is_404(client: TestClient) -> None:
    response = client.post("/api/v1/conversations/conv_不存在/branch", json={"turn": 1})

    assert response.status_code == 404


def test_archived_conversations_leave_the_default_list(client: TestClient) -> None:
    """**归档不是删除**：它从默认列表里消失，但内容还在，能取消归档。

    这条要用接口验，因为它是用户看得见的那半边：归档之后侧栏不该还挂着它。
    """
    first = client.post("/api/v1/conversations", json={"title": "要归档的", "kb_ids": []}).json()
    second = client.post("/api/v1/conversations", json={"title": "留着的", "kb_ids": []}).json()

    archived = client.patch(
        f"/api/v1/conversations/{first['id']}", json={"archived": True}
    ).json()
    assert archived["archived_at"] is not None

    default_ids = [item["id"] for item in client.get("/api/v1/conversations").json()["items"]]
    assert first["id"] not in default_ids
    assert second["id"] in default_ids

    # 归档视图里看得到，而且**内容还在**（能取详情）
    archived_ids = [
        item["id"]
        for item in client.get("/api/v1/conversations?archived=true").json()["items"]
    ]
    assert first["id"] in archived_ids
    assert client.get(f"/api/v1/conversations/{first['id']}").status_code == 200

    # 取消归档 → 回到默认列表
    back = client.patch(
        f"/api/v1/conversations/{first['id']}", json={"archived": False}
    ).json()
    assert back["archived_at"] is None
    default_ids = [item["id"] for item in client.get("/api/v1/conversations").json()["items"]]
    assert first["id"] in default_ids


def test_archiving_does_not_touch_updated_at(client: TestClient) -> None:
    """归档是一次整理动作，**不该把会话顶到"最近活动"的最前面**——
    与改名/置顶同一条纪律（否则整理一遍列表，顺序就按整理时间乱掉了）。"""
    older = client.post("/api/v1/conversations", json={"title": "早的", "kb_ids": []}).json()
    newer = client.post("/api/v1/conversations", json={"title": "晚的", "kb_ids": []}).json()
    before = client.get(f"/api/v1/conversations/{older['id']}").json()["updated_at"]

    client.patch(f"/api/v1/conversations/{older['id']}", json={"archived": False})

    after = client.get(f"/api/v1/conversations/{older['id']}").json()["updated_at"]
    assert after == before
    del newer


def test_preview_returns_the_last_answer(client: TestClient) -> None:
    """历史面板那两行预览给的是**最近一条回答**：回看时想认出的是"这次聊出了什么"，
    而问题常常几条都长得像（"帮我看看这个"）。"""
    conversation = client.post("/api/v1/conversations", json={"title": "带回答的"}).json()
    services = get_services()
    services.conversations.append(conversation["id"], role="user", content="问一句")
    services.conversations.append(
        conversation["id"], role="assistant", content="这是最近的一条回答"
    )

    body = client.get("/api/v1/conversations?with_preview=true").json()

    row = next(item for item in body["items"] if item["id"] == conversation["id"])
    assert row["preview"] == "这是最近的一条回答"


def test_preview_is_opt_in(client: TestClient) -> None:
    """预览要多一次查询，所以默认**不给**：侧栏每次渲染都要那份清单，
    让侧栏为面板的需求付成本不划算。"""
    conversation = client.post("/api/v1/conversations", json={"title": "x"}).json()
    get_services().conversations.append(conversation["id"], role="assistant", content="回答")

    body = client.get("/api/v1/conversations").json()

    row = next(item for item in body["items"] if item["id"] == conversation["id"])
    assert row["preview"] == ""


# ------------------------------------------------------------------ 会话产物（v0.26）


def _artifact(services, conversation_id: str, *, name: str = "短诗.docx"):  # type: ignore[no-untyped-def]
    return services.artifacts.save(
        conversation_id=conversation_id,
        filename=name,
        content=b"poem-bytes",
        kind="docx",
    )


def test_listing_artifacts_says_where_the_file_is(client: TestClient) -> None:
    """卡片的状态以这条接口为准：流式当时那份快照说不清"它后来进没进库"。"""
    services = get_services()
    conversation = client.post("/api/v1/conversations", json={"title": "产物"}).json()
    record = _artifact(services, conversation["id"])

    body = client.get(f"/api/v1/conversations/{conversation['id']}/artifacts").json()

    assert [item["artifact_id"] for item in body["items"]] == [record.id]
    assert body["items"][0]["where"] == "本会话"
    # 没入库就不给 knowledge_base_id——界面据此决定给不给「存进知识库」
    assert body["items"][0]["knowledge_base_id"] is None


def test_artifact_of_another_conversation_is_not_reachable(client: TestClient) -> None:
    """`/conversations/A/artifacts/B` 不能拿到别的会话里的 B。

    权限判定过的是 A，而返回的是 B 的内容——不校验归属就能这样绕过去。
    """
    services = get_services()
    first = client.post("/api/v1/conversations", json={"title": "a"}).json()
    second = client.post("/api/v1/conversations", json={"title": "b"}).json()
    record = _artifact(services, second["id"])

    response = client.get(f"/api/v1/conversations/{first['id']}/artifacts/{record.id}/download-url")

    assert response.status_code == 404


def test_download_url_round_trip(client: TestClient) -> None:
    """签发 → 用签名取内容。**签名端点不带鉴权头**（下载与预览按钮带不了头）。"""
    services = get_services()
    conversation = client.post("/api/v1/conversations", json={"title": "下载"}).json()
    record = _artifact(services, conversation["id"])

    issued = client.get(
        f"/api/v1/conversations/{conversation['id']}/files/download-url",
        params={"key": record.id},
    )
    assert issued.status_code == 200, issued.text
    url = issued.json()["url"]

    fetched = client.get(url)
    assert fetched.status_code == 200
    assert fetched.content == b"poem-bytes"


def test_tampered_signature_is_rejected(client: TestClient) -> None:
    services = get_services()
    conversation = client.post("/api/v1/conversations", json={"title": "改签名"}).json()
    record = _artifact(services, conversation["id"])
    url = client.get(
        f"/api/v1/conversations/{conversation['id']}/files/download-url",
        params={"key": record.id},
    ).json()["url"]

    response = client.get(url.split("&signature=")[0] + "&signature=deadbeef")

    assert response.status_code == 401


# **摘掉一条**（2026-10-05）：``test_ingest_endpoint_is_the_click_that_files_it``
# （原判据：`POST /conversations/{id}/artifacts/{aid}/ingest` 把会话产物送进知识库，
# 库由请求体点明、`services.documents.get(...)` 读得回那份文档）。
# **本机档无法验证**：它落的是知识库那几张表（文档 / 分块在中，那些表都不在本机库里，
# `services.documents` 在本机档整体抛 `KnowledgeBaseUnavailable`）。而 KB 那一族只在
# 服务器档，会话面只在**本机**档——两者的交集是空的，所以这条断言没有一个装机形态
# 能承接。承接者：`tests/unit/services/test_artifacts.py`（产物那一层）与
# `tests/integration/api/test_rest_api.py`（KB 入库那条链）。


def test_deleting_a_conversation_clears_its_temporary_files(client: TestClient) -> None:
    """删会话要连**对象存储里那份临时文件**一起清掉，否则桶会只增不减。"""
    from app.core.storage import get_stores

    services = get_services()
    conversation = client.post("/api/v1/conversations", json={"title": "删掉"}).json()
    record = _artifact(services, conversation["id"])
    assert get_stores().objects.exists(record.location)

    assert client.delete(f"/api/v1/conversations/{conversation['id']}").status_code == 204

    assert not get_stores().objects.exists(record.location)


# ------------------------------------------------------------------ 文件区（v0.26）


def test_file_area_of_a_bare_conversation_lists_its_artifacts(client: TestClient) -> None:
    """没挂工作区的会话也有文件区：临时区里就是这条会话的产物。"""
    services = get_services()
    conversation = client.post("/api/v1/conversations", json={"title": "文件"}).json()
    _artifact(services, conversation["id"])

    body = client.get(f"/api/v1/conversations/{conversation['id']}/files").json()

    assert body["mode"] == "object"
    assert body["label"] == "本会话的文件"
    assert [item["name"] for item in body["entries"]] == ["短诗.docx"]
    assert body["entries"][0]["kind"] == "docx"


def test_upload_then_download_round_trip(client: TestClient) -> None:
    """上传 → 列表里看得到 → 签名链接取回同一份字节。"""
    conversation = client.post("/api/v1/conversations", json={"title": "上传"}).json()
    base = f"/api/v1/conversations/{conversation['id']}/files"

    created = client.post(
        base, files={"file": ("笔记.txt", b"hello file", "text/plain")}
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]

    listing = client.get(base).json()
    assert [item["name"] for item in listing["entries"]] == ["笔记.txt"]

    url = client.get(f"{base}/download-url", params={"key": key}).json()["url"]
    fetched = client.get(url)
    assert fetched.status_code == 200 and fetched.content == b"hello file"


def test_inline_disposition_is_only_honoured_for_safe_kinds(client: TestClient) -> None:
    """``disposition=inline`` 由调用方给，所以**不能**由它决定能不能内联渲染。

    真正决定的是服务端按后缀复核的那张白名单——一份能带 ``<script>`` 的 SVG
    内联在本站 origin 下就是存储型 XSS。
    """
    conversation = client.post("/api/v1/conversations", json={"title": "内联"}).json()
    base = f"/api/v1/conversations/{conversation['id']}/files"
    key = client.post(
        base, files={"file": ("图.svg", b"<svg/>", "image/svg+xml")}
    ).json()["key"]

    url = client.get(
        f"{base}/download-url", params={"key": key, "disposition": "inline"}
    ).json()["url"]

    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


def test_file_listing_refuses_another_conversations_key(client: TestClient) -> None:
    """文件区是按会话划的：拿别的会话的 key 来签链接，签不出来。"""
    services = get_services()
    first = client.post("/api/v1/conversations", json={"title": "a"}).json()
    second = client.post("/api/v1/conversations", json={"title": "b"}).json()
    record = _artifact(services, second["id"])

    response = client.get(
        f"/api/v1/conversations/{first['id']}/files/download-url", params={"key": record.id}
    )

    assert response.status_code == 404


def test_workspace_backed_conversation_browses_the_real_directory(
    client: TestClient, tmp_path
) -> None:
    """挂了工作区多出一档 ``scope=project``（能进子目录），而**默认那档是会话的文件**。

    v0.55 起上传不再写进项目目录（用户报的"同项目里上传的文件分不开"），
    所以这里同时钉住三件事：项目档照旧能浏览、上传落**会话档**、项目目录一个字节都没多。
    """
    services = get_services()
    root = tmp_path / "proj"
    root.mkdir()
    (root / "章节").mkdir()
    (root / "章节" / "一.md").write_text("正文", encoding="utf-8")
    workspace = services.workspaces.create(name="我的项目", root_path=str(root), user_id=None)
    conversation = client.post(
        "/api/v1/conversations", json={"title": "工作区的", "workspace_id": workspace.id}
    ).json()
    base = f"/api/v1/conversations/{conversation['id']}/files"

    # 默认档：这条会话的文件（此刻是空的——项目里的东西不属于它）
    default = client.get(base).json()
    assert default["label"] == "本会话的文件" and default["entries"] == []

    listing = client.get(base, params={"scope": "project"}).json()
    assert listing["mode"] == "workspace" and listing["label"] == "工作区「我的项目」"
    assert [item["name"] for item in listing["entries"]] == ["章节"]

    inner = client.get(base, params={"path": "章节", "scope": "project"}).json()
    assert inner["path"] == "章节" and inner["parent"] == ""
    assert inner["entries"][0]["kind"] == "md"

    client.post(base, files={"file": ("二.md", b"x", "text/markdown")})
    # 上传**不落项目目录**，而是进会话档
    assert not (root / "章节" / "二.md").exists()
    assert [item["name"] for item in client.get(base).json()["entries"]] == ["二.md"]


def test_project_scope_without_a_workspace_says_so(client: TestClient) -> None:
    """没挂工作区时 ``scope=project`` **明确说清**，而不是给一个空列表。"""
    conversation = client.post("/api/v1/conversations", json={"title": "没项目"}).json()

    response = client.get(
        f"/api/v1/conversations/{conversation['id']}/files", params={"scope": "project"}
    )

    assert response.status_code == 422, response.text
    assert "没有挂工作区" in response.json()["message"]


# ----------------------------------------- D20：会话档的目录层级 + 取进本会话


def test_conversation_files_show_directory_layers(client: TestClient) -> None:
    """上传文件夹（名字里带相对路径）→ 会话档也**按目录分层**（D20）。

    走的是真 multipart：前端把 ``图表/一.md`` 当 filename 交过来（`Composer` 那条路）。
    """
    conversation = client.post("/api/v1/conversations", json={"title": "分层"}).json()
    base = f"/api/v1/conversations/{conversation['id']}/files"

    client.post(base, files={"file": ("说明.txt", "根上的".encode(), "text/plain")})
    created = client.post(base, files={"file": ("图表/一.md", b"# x", "text/markdown")})
    assert created.status_code == 201, created.text

    root = client.get(base).json()
    assert root["path"] == "" and root["parent"] is None
    assert [item["name"] for item in root["entries"]] == ["图表", "说明.txt"]
    assert root["entries"][0]["is_dir"] is True and root["entries"][0]["kind"] == "dir"
    # 目录项的 key 是它在这一档里的路径（界面点它就是 `path=key`）
    assert root["entries"][0]["key"] == "图表"

    inner = client.get(base, params={"path": "图表"}).json()
    assert [item["name"] for item in inner["entries"]] == ["一.md"]
    assert inner["path"] == "图表" and inner["parent"] == ""
    # 文件项的 key 仍是产物 id：预览 / 下载那条路与以前一样
    assert inner["entries"][0]["key"] == created.json()["key"]


def test_import_project_file_into_the_conversation(client: TestClient, tmp_path) -> None:
    """「取进本会话」：项目档那一行点一下 → 会话档多一份，项目里那份不动。"""
    services = get_services()
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "报告.md").write_text("正文", encoding="utf-8")
    workspace = services.workspaces.create(name="我的项目", root_path=str(root), user_id=None)
    conversation = client.post(
        "/api/v1/conversations", json={"title": "工作区的", "workspace_id": workspace.id}
    ).json()
    base = f"/api/v1/conversations/{conversation['id']}/files"

    imported = client.post(f"{base}/import", json={"path": "docs/报告.md"})

    assert imported.status_code == 201, imported.text
    entry = imported.json()
    # 回给界面的是**会话文件区里的那一行**：名字保留项目里的相对位置，key 是新的产物 id
    assert entry["name"] == "docs/报告.md" and entry["kind"] == "md" and entry["size_bytes"] == 6
    assert entry["key"].startswith("art_")

    # 会话档的根那层因此多出一个目录，进去就是刚取的那份
    assert [item["name"] for item in client.get(base).json()["entries"]] == ["docs"]
    inner = client.get(base, params={"path": "docs"}).json()
    assert [item["name"] for item in inner["entries"]] == ["报告.md"]
    assert inner["entries"][0]["key"] == entry["key"]

    # 内容取得回来；项目里那份一个字节没动
    url = client.get(f"{base}/download-url", params={"key": entry["key"]}).json()["url"]
    assert client.get(url).content == "正文".encode()
    assert (root / "docs" / "报告.md").read_text(encoding="utf-8") == "正文"


def test_import_project_file_refuses_traversal(client: TestClient, tmp_path) -> None:
    """路径是浏览器回来的：`..` 与绝对路径都拒（工作区那道闸）。"""
    services = get_services()
    root = tmp_path / "proj"
    root.mkdir()
    workspace = services.workspaces.create(name="我的项目", root_path=str(root), user_id=None)
    conversation = client.post(
        "/api/v1/conversations", json={"title": "越界", "workspace_id": workspace.id}
    ).json()

    for bad in ["../外面.txt", "/etc/passwd"]:
        response = client.post(
            f"/api/v1/conversations/{conversation['id']}/files/import", json={"path": bad}
        )
        assert response.status_code == 422, response.text


def test_import_project_file_without_a_workspace_says_so(client: TestClient) -> None:
    """没挂工作区的会话没有「项目文件」可取——明确说清，不给一个空结果。"""
    conversation = client.post("/api/v1/conversations", json={"title": "没项目"}).json()

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/files/import", json={"path": "任意.txt"}
    )

    assert response.status_code == 422, response.text
    assert "没有挂工作区" in response.json()["message"]


def test_import_project_file_refuses_another_conversations_file(
    client: TestClient, tmp_path
) -> None:
    """跨工作区越权：A 会话取不到 B 会话项目里的文件（各看各的根）。"""
    services = get_services()
    first_root = tmp_path / "a"
    first_root.mkdir()
    second_root = tmp_path / "b"
    second_root.mkdir()
    (second_root / "别人的.txt").write_text("b", encoding="utf-8")
    first = client.post(
        "/api/v1/conversations",
        json={
            "title": "a",
            "workspace_id": services.workspaces.create(
                name="A", root_path=str(first_root), user_id=None
            ).id,
        },
    ).json()

    response = client.post(
        f"/api/v1/conversations/{first['id']}/files/import", json={"path": "别人的.txt"}
    )

    assert response.status_code == 404, response.text


# **摘掉一条**（2026-10-05）：``test_import_project_file_needs_a_write_key``
# （原判据：`POST /conversations/{id}/files/import` 取进会话是写动作，只读 API Key 403）
# ——它靠 `/api-keys` 签发一把只读钥匙，而 **API Key 属于账号体系**：`api_keys` /
# `users` / `sessions` 三张表都不在本机库里、`/api-keys` 与 `/auth/*` 都不挂本机档
# （见 `api/v1/router.py` 里"明确不挂"那一段）。本机档主体恒为"本机主人"，
# 造不出"只读的第二个身份"。读写两档的判定在 `api/auth.py::check_access`，
# 由 `tests/unit/services/test_api_key.py` 与 `tests/integration/api/test_auth_api.py` 覆盖。
#
# 其余四条"取进会话"的用例（成 / 穿目录 / 无工作区 / 跨会话）**一条没动**：
# 它们要的是会话面与工作区，两样都在本机档。
