"""对话留存：会话与消息的读写（§11.2）。

镜像同构：``app/services/conversation.py`` → ``tests/unit/services/test_conversation.py``。

要紧的几条：标题只由首轮提问生成（改过名的不该被覆盖）、历史取最近的若干条、
引用是快照而不是重查、删除要连消息一起删（外键级联在本项目不生效）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.conversation import TITLE_MAX_CHARS, ConversationService
from app.services.session_events import TURN_OK, turn_end_draft, turn_start_draft


@pytest.fixture
def service(bundle) -> ConversationService:  # type: ignore[no-untyped-def]
    return ConversationService(bundle)


def test_create_and_get_roundtrip(service: ConversationService) -> None:
    created = service.create(kb_ids=["kb_1", "kb_2"])

    fetched = service.get(created.id)
    assert fetched.id == created.id
    assert list(fetched.kb_ids) == ["kb_1", "kb_2"]
    assert fetched.title == ""
    assert fetched.created_at is not None


def test_get_missing_conversation_raises(service: ConversationService) -> None:
    with pytest.raises(NotFoundError):
        service.get("conv_不存在")


def test_messages_are_returned_in_order(service: ConversationService) -> None:
    conv = service.create()
    service.append(conv.id, role="user", content="第一个问题")
    service.append(conv.id, role="assistant", content="第一个回答")
    service.append(conv.id, role="user", content="第二个问题")

    contents = [item.content for item in service.messages(conv.id)]
    assert contents == ["第一个问题", "第一个回答", "第二个问题"]


def test_message_count(service: ConversationService) -> None:
    conv = service.create()
    assert service.message_count(conv.id) == 0
    service.append(conv.id, role="user", content="q")
    assert service.message_count(conv.id) == 1


def test_sources_are_stored_as_a_snapshot(service: ConversationService) -> None:
    """引用存快照：事后重查会得到不同结果，引用编号就对不上了。"""
    conv = service.create()
    snapshot = [{"index": 1, "chunk_id": "c1", "document_id": "d1", "preview": "原文"}]

    service.append(conv.id, role="assistant", content="回答", sources=snapshot)

    stored = service.messages(conv.id)[0]
    assert [dict(item) for item in stored.sources] == snapshot


def test_turn_attachments_are_snapshotted_on_the_user_message(
    service: ConversationService,
) -> None:
    """随发的附件**挂在用户那条消息上**（v0.55）。

    用户报的"对话中上传的文件，没有在我发送的对话中有文件组件标识"——根因是消息与
    文件之间原先**没有任何关联**（文件只记到会话），所以这里钉住三件事：
    写进去、读得回来、**回答那条不带**（附件是"带着哪几份文件问的"，不是回答的一部分）。
    """
    conv = service.create()
    snapshot = [
        {"key": "art_1", "name": "指南.pdf", "kind": "pdf", "size_bytes": 448444},
    ]

    service.record_turn(conv.id, question="看看这份", answer="好", attachments=snapshot)

    user, assistant = service.messages(conv.id)
    assert [dict(item) for item in user.attachments] == snapshot
    assert list(assistant.attachments) == []


# --------------------------------------------------------------------- 标题


def test_title_comes_from_the_first_question(service: ConversationService) -> None:
    conv = service.create()
    service.append(conv.id, role="user", content="近视怎么监测眼轴")

    service.ensure_title(conv.id, "近视怎么监测眼轴")

    assert service.get(conv.id).title == "近视怎么监测眼轴"


def test_long_question_is_truncated(service: ConversationService) -> None:
    conv = service.create()
    service.ensure_title(conv.id, "问" * 100)

    assert len(service.get(conv.id).title) == TITLE_MAX_CHARS


def test_title_flattens_newlines(service: ConversationService) -> None:
    """用户可能粘一整段带换行的文本进来，标题里带换行会撑坏左栏。"""
    conv = service.create()
    service.ensure_title(conv.id, "第一行\n\n第二行")

    title = service.get(conv.id).title
    assert "\n" not in title
    assert title == "第一行 第二行"


def test_ensure_title_does_not_overwrite_a_manual_name(service: ConversationService) -> None:
    """**改过名字的会话不该被后续提问覆盖。**

    否则用户整理好的标题会在下一轮对话里被冲掉——而标题正是他用来找回这次对话的东西。
    """
    conv = service.create()
    service.rename(conv.id, "我自己的名字")

    service.ensure_title(conv.id, "新的提问内容")

    assert service.get(conv.id).title == "我自己的名字"


def test_rename_rejects_blank(service: ConversationService) -> None:
    conv = service.create()
    with pytest.raises(ValueError):
        service.rename(conv.id, "   ")


# --------------------------------------------------------------------- 历史


def test_history_returns_recent_messages(service: ConversationService) -> None:
    conv = service.create()
    for index in range(10):
        service.append(conv.id, role="user", content=f"q{index}")

    history = service.history(conv.id, turns=3)

    assert [item.content for item in history] == ["q7", "q8", "q9"]


def test_history_skips_empty_messages(service: ConversationService) -> None:
    """空的助手消息不进历史——模型看到空的上一轮会更离谱。"""
    conv = service.create()
    service.append(conv.id, role="user", content="问题")
    service.append(conv.id, role="assistant", content="   ")

    history = service.history(conv.id)

    assert [item.content for item in history] == ["问题"]


def test_history_of_empty_conversation_is_empty(service: ConversationService) -> None:
    conv = service.create()
    assert service.history(conv.id) == []


# --------------------------------------------------------------------- 删除


def test_delete_removes_messages_too(service: ConversationService) -> None:
    """**外键级联在本项目不生效**（连接没开 PRAGMA foreign_keys），
    所以删除必须显式清消息，否则会留下一堆孤儿行。
    """
    conv = service.create()
    service.append(conv.id, role="user", content="q")
    service.append(conv.id, role="assistant", content="a")

    service.delete(conv.id)

    with pytest.raises(NotFoundError):
        service.get(conv.id)
    # 直接从存储层查：消息应当一条不剩
    assert service._stores.meta.list_messages(conv.id) == []


# --------------------------------------------------------------------- 列表


def test_list_orders_by_recent_activity(service: ConversationService) -> None:
    first = service.create()
    second = service.create()
    # 让第一个成为"最近聊过"的
    service.append(first.id, role="user", content="q")

    ids = [item.id for item in service.list()]
    assert ids.index(first.id) < ids.index(second.id)


def test_rename_does_not_reorder_the_list(service: ConversationService) -> None:
    """改名不推 updated_at。

    否则用户整理一遍标题，排序就按"改标题的时间"而不是"对话发生的时间"——
    而他想按后者找。
    """
    first = service.create()
    second = service.create()
    service.append(second.id, role="user", content="q")  # second 更新

    service.rename(first.id, "改个名字")

    ids = [item.id for item in service.list()]
    assert ids.index(second.id) < ids.index(first.id)


def test_list_limit(service: ConversationService) -> None:
    for _ in range(5):
        service.create()
    assert len(service.list(limit=2)) == 2


# --------------------------------------------------------------------- 会话级对话模型（v12）


def test_conversation_remembers_the_chosen_model(service: ConversationService) -> None:
    conv = service.create(kb_ids=["kb_1"], model_pk="mdl_a")
    assert service.get(conv.id).model_pk == "mdl_a"


def test_conversation_model_defaults_to_none(service: ConversationService) -> None:
    """没显式选就是 None（跟随全局默认），不是某个被猜出来的模型。"""
    conv = service.create()
    assert service.get(conv.id).model_pk is None


def test_set_model_switches_and_clears(service: ConversationService) -> None:
    conv = service.create(model_pk="mdl_a")

    service.set_model(conv.id, "mdl_b")
    assert service.get(conv.id).model_pk == "mdl_b"

    service.set_model(conv.id, None)
    assert service.get(conv.id).model_pk is None


def test_set_model_does_not_touch_updated_at(service: ConversationService) -> None:
    """切模型不算"发生了对话"：不该把会话顶到"最近活动"最前面。"""
    conv = service.create()
    before = service.get(conv.id).updated_at

    service.set_model(conv.id, "mdl_a")

    assert service.get(conv.id).updated_at == before


# ------------------------------------------------- 置顶 / 搜索 / 回退（v17）


def _seed(service: ConversationService, count: int = 3):  # type: ignore[no-untyped-def]
    """造 count 条会话，每条一轮问答。返回按创建顺序的会话。"""
    made = []
    for index in range(count):
        record = service.create(kb_ids=["kb_1"], title=f"会话{index}")
        service.append(record.id, role="user", content=f"问题{index}")
        service.append(record.id, role="assistant", content=f"回答{index}")
        made.append(record)
    return made


def test_pinned_sorts_first_and_keeps_its_rank(service) -> None:  # type: ignore[no-untyped-def]
    """置顶排在列表最前，**且之后聊天不会把它挤下去**——用户置顶正是为了这个。"""
    first, _second, third = _seed(service)

    service.set_pinned(first.id, True)
    # 让后来者"更活跃"：updated_at 更大，但置顶的不该被顶下去
    service.append(third.id, role="user", content="又聊了一句")

    order = [item.id for item in service.list()]

    assert order[0] == first.id
    # 其余按最近更新：third 刚聊过，所以排在 second 之前
    assert order[1] == third.id


def test_unpin_returns_to_normal_order(service) -> None:  # type: ignore[no-untyped-def]
    first, _second, third = _seed(service)
    service.set_pinned(first.id, True)
    service.append(third.id, role="user", content="新的一句")

    service.set_pinned(first.id, False)

    assert next(item.id for item in service.list()) == third.id


def test_search_filters_by_title(service) -> None:  # type: ignore[no-untyped-def]
    """按标题搜索在 SQL 里做，才能与 LIMIT 组合出正确语义。"""
    _seed(service, 3)
    service.create(kb_ids=["kb_1"], title="眼科指南问答")

    hits = service.list(q="眼科")

    assert [item.title for item in hits] == ["眼科指南问答"]


def test_search_treats_wildcards_literally(service) -> None:  # type: ignore[no-untyped-def]
    """搜 `_` 不该变成"任意一个字符"（与文档搜索同一套转义）。"""
    service.create(kb_ids=["kb_1"], title="a_b")
    service.create(kb_ids=["kb_1"], title="axb")

    hits = service.list(q="a_b")

    assert [item.title for item in hits] == ["a_b"]


def test_rewind_removes_last_turn_and_returns_the_question(service) -> None:  # type: ignore[no-untyped-def]
    """「重新生成」的底座：删掉最后一轮并把那句提问还回来。"""
    record = service.create(kb_ids=["kb_1"], title="会话")
    service.append(record.id, role="user", content="第一问")
    service.append(record.id, role="assistant", content="第一答")
    service.append(record.id, role="user", content="第二问")
    service.append(record.id, role="assistant", content="第二答")

    query = service.rewind(record.id)

    assert query == "第二问"
    remaining = [item.content for item in service.messages(record.id)]
    assert remaining == ["第一问", "第一答"]


def test_rewind_without_a_question_is_rejected(service) -> None:  # type: ignore[no-untyped-def]
    """没有可回退的提问时明说，而不是让调用方发一次空提问。"""
    record = service.create(kb_ids=["kb_1"], title="空会话")

    with pytest.raises(InvalidRequestError, match="没有可回退"):
        service.rewind(record.id)


def test_rewind_two_turns(service) -> None:  # type: ignore[no-untyped-def]
    record = service.create(kb_ids=["kb_1"], title="会话")
    service.append(record.id, role="user", content="一")
    service.append(record.id, role="assistant", content="答一")
    service.append(record.id, role="user", content="二")
    service.append(record.id, role="assistant", content="答二")

    query = service.rewind(record.id, turns=2)

    assert query == "一"
    assert service.messages(record.id) == []


def test_search_also_matches_message_content(service) -> None:  # type: ignore[no-untyped-def]
    """D12：搜索要**同时**看标题与消息正文。

    走查实测：同一个词在正文里命中 3 行 / 2 条会话，而列表接口 0 命中——
    "搜一句我记得说过的话"这个最常见的用法直接失效。
    """
    _seed(service, 2)
    target = service.create(kb_ids=["kb_1"], title="随便一个标题")
    service.append(target.id, role="user", content="帮我看看眼轴长度的随访数据")
    service.append(target.id, role="assistant", content="眼轴随访 18 个月")

    hits = service.list(q="眼轴")

    assert [item.id for item in hits] == [target.id]


def test_search_returns_a_conversation_once_even_with_many_hits(service) -> None:  # type: ignore[no-untyped-def]
    """一条会话里命中多条消息时**只出现一次**——这正是那条 SQL 用 `EXISTS` 而不是
    JOIN 的原因（JOIN 会把它复制成多行，而 LIMIT 的语义是"前 N 条会话"）。"""
    record = service.create(kb_ids=["kb_1"], title="标题里没有那个词")
    for index in range(3):
        service.append(record.id, role="user", content=f"第{index}条都提到眼轴")

    hits = service.list(q="眼轴")

    assert [item.id for item in hits] == [record.id]


def test_title_from_question_keeps_a_readable_title() -> None:
    """D15：标题不该是"首问压平后的前 24 字"。

    期望值全是**真库数据跑出来的**（`.cache/compare_d15.py` 拿 12 条真实首问对新旧两版
    逐条对照），不是手编的例子。
    """
    from app.services.conversation import TITLE_MAX_CHARS, _title_from

    # 礼貌引导词去掉（去掉之后这一条刚好把"分 8 个小标题"完整放下）
    assert (
        _title_from("请写一段 600 字左右的说明，分 8 个小标题，中间夹一个表格。不要用工具。")
        == "写一段 600 字左右的说明，分 8 个小标题"
    )
    # 句末标点处收尾，且不留悬空的「。」
    assert (
        _title_from("做一个ppt出来 内容是今日国内新闻。尽量调用工具。")
        == "做一个ppt出来 内容是今日国内新闻"
    )
    # 整句都放得下时原样保留（只去掉结尾那个悬空的标点）
    assert (
        _title_from("请写一段 400 字左右的说明。不要用工具。")
        == "写一段 400 字左右的说明。不要用工具"
    )
    # 短问题原样
    assert _title_from("把这篇文章上传到 测试知识库里面") == "把这篇文章上传到 测试知识库里面"
    # 上限仍然是那个数（改标题规则不该顺手把上限改掉）
    assert len(_title_from("用" * 100)) <= TITLE_MAX_CHARS


def test_title_from_never_ends_with_punctuation_or_particle() -> None:
    """标题结尾不能是标点或悬空虚词——用户看到的就是被切断的样子。"""
    from app.services.conversation import TITLE_MAX_CHARS, _title_from

    samples = [
        "用 Markdown 表格列出 12 个中国省份的省会、常住人口与面积，然后逐条说明其中 6 个",
        "请调用文件列表工具，列出当前文件根的目录内容——不要传 where、也不要传 path",
        "把这篇文章上传到 测试知识库里面",
        "请记住这个口令：ZQ7K-凌云。只回复「记住了」",
        "做一个ppt出来 内容是今日国内新闻。尽量调用工具。",
    ]
    for question in samples:
        title = _title_from(question)
        assert title, question
        assert len(title) <= TITLE_MAX_CHARS, (title, question)
        assert title[-1] not in "。，、；：！？!?,;: ", (title, question)
        assert title[-1] not in "的了和与及把被在是对着给让使", (title, question)


# ------------------------------------------------------- 从这里重开（D11）


def _turn(service: ConversationService, conv_id: str, question: str, answer: str) -> None:
    """落一轮**带事件日志**的问答（与 `/chat` 那条路同一个形状：turn/start … turn/end）。

    分支要抄的东西里有一半在事件日志里，所以造数据时不能只 `append` 两条消息——
    那样抄出来的"事件前缀"永远是空的，用例就测不到它在不在。
    """
    service.record_turn(
        conv_id,
        question=question,
        answer=answer,
        sources=[{"index": 1, "chunk_id": f"c_{question}", "preview": "原文"}],
        steps=[{"phase": "answer", "label": "组织回答", "detail": answer, "status": "done"}],
        thinking=f"{question} 的推理",
        events=[
            turn_start_draft(query=question, model_pk="m1"),
            turn_end_draft(status=TURN_OK, answer_chars=len(answer), steps=1),
        ],
    )


def test_branch_copies_the_history_up_to_that_turn(service: ConversationService) -> None:
    """**从这里重开**：到第 N 轮为止的历史进新会话，原会话一个字节不动。"""
    source = service.create(kb_ids=["kb_1"], title="眼科问答", model_pk="mdl_a", thinking=True)
    _turn(service, source.id, "第一问", "第一答")
    _turn(service, source.id, "第二问", "第二答")
    _turn(service, source.id, "第三问", "第三答")

    branch = service.branch(source.id, turn=2)

    assert branch.id != source.id
    # 新会话只带着前两轮（提问 + 回答都在，第三轮不进来）
    assert [item.content for item in service.messages(branch.id)] == [
        "第一问",
        "第一答",
        "第二问",
        "第二答",
    ]
    # 原会话照旧三轮
    assert service.message_count(source.id) == 6
    # 会话档位跟着走：知识库 / 模型 / 思考偏好
    assert list(branch.kb_ids) == ["kb_1"]
    assert branch.model_pk == "mdl_a"
    assert branch.thinking is True


def test_branch_message_snapshots_are_carried_over(service: ConversationService) -> None:
    """出处 / 步骤 / 思考快照都带走——回看时"当时依据哪几段"不能丢。"""
    source = service.create(kb_ids=["kb_1"], title="会话")
    _turn(service, source.id, "问", "答")

    branch = service.branch(source.id, turn=1)
    answer = service.messages(branch.id)[1]

    assert [dict(item)["chunk_id"] for item in answer.sources] == ["c_问"]
    assert [dict(item)["label"] for item in answer.steps] == ["组织回答"]
    assert answer.thinking == "问 的推理"


def test_branch_copies_the_event_log_with_fresh_seq(service: ConversationService) -> None:
    """事件日志跟着抄，但 ``seq`` 是**新会话自己的**编号（从 1 重算）。"""
    source = service.create(title="会话")
    _turn(service, source.id, "第一问", "第一答")
    _turn(service, source.id, "第二问", "第二答")

    branch = service.branch(source.id, turn=1)
    events = service.session_events(branch.id)

    # 第一轮那两条（turn/start + turn/end）
    assert [item.kind for item in events] == ["turn/start", "turn/end"]
    assert [item.seq for item in events] == [1, 2]
    assert events[0].payload["query"] == "第一问"
    # 原会话两条都在（一个字节没动）
    assert len(service.session_events(source.id)) == 4


def test_branch_event_prefix_ignores_turns_without_messages(
    service: ConversationService,
) -> None:
    """**消息轮次与事件轮次不一一对应**：被打断的那一轮不留消息但事件照记。

    场景：第一轮在流里出错（只有事件、没有消息），第二轮正常。这时"到第 1 轮为止"
    指的是**第二轮**那段历史（消息里唯一的一轮），抄过来的事件也必须是它的——
    按事件里的第 1 个 `turn/start` 切会把第一轮那段失败的过程抄进新会话。
    """
    source = service.create(title="会话")
    # 第一轮：被打断/失败——**只写事件**（这正是 chat.py 那两条路的形状）
    service.append_events(
        source.id,
        [
            turn_start_draft(query="第一问（根本没答）", model_pk=None),
            turn_end_draft(status="error", answer_chars=0, steps=0),
        ],
    )
    _turn(service, source.id, "第二问", "第二答")

    branch = service.branch(source.id, turn=1)

    assert [item.content for item in service.messages(branch.id)] == ["第二问", "第二答"]
    events = service.session_events(branch.id)
    # 抄过来的是**第二问那一轮**的两条事件，被中断那一轮一条都不带
    assert [item.kind for item in events] == ["turn/start", "turn/end"]
    assert [item.payload.get("query") for item in events if item.kind == "turn/start"] == ["第二问"]


def test_branch_inherits_the_owner(service: ConversationService) -> None:
    """**归属继承源会话**：成员分叉自己的会话，新会话还是他的（管理员替成员分叉也一样）。"""
    source = service.create(title="会话", owner_id="user_member")
    _turn(service, source.id, "问", "答")

    branch = service.branch(source.id, turn=1)

    assert branch.owner_id == "user_member"


def test_branch_title_says_where_it_came_from(service: ConversationService) -> None:
    """标题不能被照抄（侧栏会出现两条同名会话，用户分不清）。"""
    source = service.create(title="眼科问答")
    service.append(source.id, role="user", content="问")

    branch = service.branch(source.id, turn=1)

    assert branch.title == "眼科问答（分支 · 第 1 轮）"


def test_branch_title_does_not_pile_up_markers(service: ConversationService) -> None:
    """分叉出的会话再分叉是允许的——但标记不该一路接成一长串括号。"""
    source = service.create(title="眼科问答")
    service.append(source.id, role="user", content="问")

    once = service.branch(source.id, turn=1)
    twice = service.branch(once.id, turn=1)

    assert twice.title == "眼科问答（分支 · 第 1 轮）"


def test_branch_does_not_copy_attachment_snapshots(service: ConversationService) -> None:
    """**附件快照不抄**（`branch` 的说明里那条边界）。

    那份 key 指向**源会话**的文件区记账（对象存储里按会话分前缀），抄到新会话里点开
    必然 404；而共享同一个对象 key 更糟——删掉源会话会把分叉的附件一起带走。
    所以分叉出来的会话"历史正文在、附件片不在"，用户的感受就是这一条。
    """
    source = service.create(title="会话")
    snapshot = [{"key": "art_1", "name": "指南.pdf", "kind": "pdf", "size_bytes": 10}]
    service.record_turn(source.id, question="看看这份", answer="好", attachments=snapshot)

    branch = service.branch(source.id, turn=1)

    assert list(service.messages(branch.id)[0].attachments) == []
    # 原会话那条仍然带着它（一个字节没动）
    assert [dict(item) for item in service.messages(source.id)[0].attachments] == snapshot


def test_branch_copies_the_compaction_summary_only_when_it_covers_the_cut(
    service: ConversationService,
) -> None:
    """压缩摘要**只在它的覆盖标记落在这段历史里**时才带。

    标记在 cut 之外说明那份摘要讲的是**后面**那些轮次——带过去等于把未来塞进分支。
    """
    source = service.create(title="长会话")
    _turn(service, source.id, "第一问", "第一答")
    _turn(service, source.id, "第二问", "第二答")
    first_answer = service.messages(source.id)[1]
    second_answer = service.messages(source.id)[3]

    # 摘要覆盖到第一轮（在 cut 内）：带过去，且标记指向**新会话里**的那条消息
    service.set_summary(source.id, "早期对话的摘要", first_answer.id)
    kept = service.branch(source.id, turn=1)
    assert service.summary(kept.id)[0] == "早期对话的摘要"
    assert service.summary(kept.id)[1] == service.messages(kept.id)[1].id

    # 摘要覆盖到第二轮（在 cut 之外）：不带（它讲的是分支没有的那一段）
    service.set_summary(source.id, "含第二轮的摘要", second_answer.id)
    without = service.branch(source.id, turn=1)
    assert service.summary(without.id) == ("", None)


def test_branch_rejects_a_turn_beyond_the_end(service: ConversationService) -> None:
    source = service.create(title="会话")
    _turn(service, source.id, "问", "答")

    with pytest.raises(InvalidRequestError, match="没有第 3 轮"):
        service.branch(source.id, turn=3)


def test_branch_rejects_a_conversation_without_turns(service: ConversationService) -> None:
    source = service.create(title="空会话")

    with pytest.raises(InvalidRequestError, match="只有 0 轮"):
        service.branch(source.id, turn=1)


def test_branch_rejects_turn_zero(service: ConversationService) -> None:
    """``0`` 不是"从头"，是越界——夹到边界会让用户以为分叉点就是他点的那一处。"""
    source = service.create(title="会话")
    _turn(service, source.id, "问", "答")

    with pytest.raises(InvalidRequestError):
        service.branch(source.id, turn=0)


def test_branch_accepts_a_turn_that_has_no_answer_yet(service: ConversationService) -> None:
    """被打断 / 续跑正在跑的那一轮只有提问——照样能当分叉点（带着那句提问继续聊）。"""
    source = service.create(title="会话")
    _turn(service, source.id, "第一问", "第一答")
    service.append(source.id, role="user", content="第二问（还没答）")

    branch = service.branch(source.id, turn=2)

    assert [item.content for item in service.messages(branch.id)] == [
        "第一问",
        "第一答",
        "第二问（还没答）",
    ]


def test_branch_and_source_are_independent(service: ConversationService) -> None:
    """**两条真的独立**：在分叉出的会话里加一轮，原会话的轮数与事件数一个都不变。"""
    source = service.create(title="会话")
    _turn(service, source.id, "第一问", "第一答")
    _turn(service, source.id, "第二问", "第二答")
    source_count = service.message_count(source.id)
    source_events = len(service.session_events(source.id))

    branch = service.branch(source.id, turn=1)
    _turn(service, branch.id, "分支里的新问题", "分支里的新回答")

    assert service.message_count(source.id) == source_count
    assert len(service.session_events(source.id)) == source_events
    assert [item.content for item in service.messages(source.id)] == [
        "第一问",
        "第一答",
        "第二问",
        "第二答",
    ]
    # 反过来也一样：原会话再加一轮，分支不动
    _turn(service, source.id, "原会话的第三问", "原会话的第三答")
    assert [item.content for item in service.messages(branch.id)] == [
        "第一问",
        "第一答",
        "分支里的新问题",
        "分支里的新回答",
    ]


def test_branch_of_a_branch_is_allowed_and_stays_independent(
    service: ConversationService,
) -> None:
    """分叉出的会话再分叉只是又一次新建——三条互不影响。"""
    source = service.create(title="会话")
    _turn(service, source.id, "第一问", "第一答")

    once = service.branch(source.id, turn=1)
    twice = service.branch(once.id, turn=1)

    assert twice.id not in {source.id, once.id}
    assert service.message_count(twice.id) == 2
    assert service.message_count(once.id) == 2
    # 各自的消息 id 也不重（新记录，不是把行搬过去）
    assert {item.id for item in service.messages(twice.id)}.isdisjoint(
        {item.id for item in service.messages(source.id)}
    )
