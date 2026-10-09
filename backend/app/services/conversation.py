"""对话留存（《架构设计 v0.2》§3 对话层；开发计划 §11.2）。

**为什么现在做**：这一层原本刻意缺席——`ChatService` 的历史只活在前端内存里，
刷新即失。定位"快速验证知识库里有没有、答得对不对"时那样够用；但一旦用户开始
**回看**（"上周问过什么、当时依据哪几段"），没有留存就等于每次都从头问。

四件事收在这里：

1. **会话的生命周期**：建、列、改名、删；
2. **标题自动生成**：取首轮提问的前若干字。让用户自己起名字的对话工具，
   最后满屏都是"新对话"——而他从标题里想认的是"这是哪一次"；
3. **历史装载**：把库里最近若干轮折成 ``ChatMessage`` 交给对话层，
   于是**前端不再需要自己维护 history**（原来那套在前端内存里的历史可以退休了）；
4. **引用快照**：回答当时依据的原文出处随消息一起存。不是每轮重新检索——
   历史回答当时依据的是哪几段，事后回看必须还是那几段，否则引用编号就对不上。

**边界**：这里只管"存与取"。检索、拼提示词、生成回答仍在 ``ChatService``；
本模块不引入任何对 LLM 的依赖，所以它可以在没有模型配置时照常工作（列表、改名、删）。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.llm import ChatMessage
from app.services.session_events import (
    EVENT_KINDS,
    KIND_TURN_END,
    KIND_TURN_START,
    TURN_DEGRADED,
    TURN_OK,
    EventDraft,
    SessionEvent,
)
from app.storage.base import (
    ChatMessageRecord,
    ConversationRecord,
    SessionEventRecord,
    StoreBundle,
)

__all__ = ["TITLE_MAX_CHARS", "ConversationService"]

logger = logging.getLogger(__name__)

#: 事件日志里"这一轮**真的产出过回答**"的那两个收尾状态（见 `_event_prefix`）。
#:
#: 为什么只认这两个：按 v0.12 起的取舍，出错（``error``）与"一个字都没吐"（``empty``）
#: 的那两轮**不留消息**，可它们的事件照样在日志里。所以"消息里的第 N 轮"与
#: "事件里的第 N 个 ``turn/start``"对不上，得靠这个状态把两边数到同一处。
_MESSAGE_TURN_STATUSES = frozenset({TURN_OK, TURN_DEGRADED})

#: 自动标题长度。够认出"这是哪一次"，又不至于把整个问题塞进左栏。
TITLE_MAX_CHARS = 24

#: 装载历史时的默认轮数上限。与前端原来的 HISTORY_LIMIT 取同一个量级：
#: 无边界地带全部历史，提示词会先被自己挤爆。
DEFAULT_HISTORY_TURNS = 6


@dataclass(frozen=True, slots=True)
class LastTurn:
    """最后那一轮的快照（提问 + 那条回答的几个字段）。

    **刻意摊开字段，而不是把存储层的 `ChatMessageRecord` 交给调用方**：
    协议层只该看到服务层给的东西（《项目工程规范》§3.3 的 L1 就是这么查的——
    它按 import 的模块名判，`api/` 里出现 `app.storage` 直接红）。
    摊开之后续跑那条路拿到的也只是"文本与快照"，与它要做的事正好对上。
    """

    question: str
    answer_id: str
    answer: str
    sources: Sequence[dict[str, object]]
    steps: Sequence[dict[str, object]]
    thinking: str


class ConversationService:
    """会话与消息的读写。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 会话

    def create(
        self,
        *,
        kb_ids: list[str] | None = None,
        title: str = "",
        owner_id: str | None = None,
        model_pk: str | None = None,
        thinking: bool | None = None,
        thinking_effort: str | None = None,
        workspace_id: str | None = None,
    ) -> ConversationRecord:
        """``owner_id``（v10）：登录成员的会话归自己；控制台/API Key 通道无主。

        ``model_pk``（v12）：这条会话选用的对话模型；``None`` = 跟随全局默认。
        ``thinking`` / ``thinking_effort``（v16）：思考开关与强度；``None`` = 跟随全局默认。
        ``workspace_id``（v0.15）：挂到哪个工作区；``None`` = 未归档。
        """
        return self._stores.meta.create_conversation(
            ConversationRecord(
                id=f"conv_{uuid.uuid4().hex[:12]}",
                title=title.strip(),
                kb_ids=tuple(kb_ids or ()),
                owner_id=owner_id,
                model_pk=model_pk,
                thinking=thinking,
                thinking_effort=thinking_effort,
                workspace_id=workspace_id,
            )
        )

    def get(self, conversation_id: str) -> ConversationRecord:
        record = self._stores.meta.get_conversation(conversation_id)
        if record is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        return record

    def get_for_owner(self, conversation_id: str, owner_id: str) -> ConversationRecord:
        """成员视角的取会话：**越主即 404**，不泄露"这条会话存在但不是你的"。

        对话内容是私有数据；用 403 会把别人的会话 id 变成可探测的存在性 oracle。
        """
        record = self.get(conversation_id)
        if record.owner_id != owner_id:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        return record

    def list(
        self,
        *,
        limit: int | None = None,
        owner_id: str | None = None,
        q: str | None = None,
        workspace_id: str | None = None,
        ungrouped: bool = False,
        archived: bool = False,
    ) -> list[ConversationRecord]:
        """置顶优先、其次最近更新（v17 起支持按标题搜索）。

        ``owner_id`` 给成员过滤用（v10 私有隔离）。**搜索与归属过滤都在这里做**，
        而不是在 SQL 里——见下面的取舍说明。

        无过滤时保持 SQL LIMIT 透传；带归属过滤时全表取出再切片——本地部署的会话量
        （几百条）下这点差异无所谓，而"先过滤再 LIMIT"的 SQL 要为一个低频操作
        加一条仓储方法，不值。`q` 走 SQL（标题包含匹配），因为它能把结果集整体缩小，
        与 LIMIT 组合后语义才正确（"搜出来的前 50 条"而不是"前 50 条里搜出来的"）。
        """
        # 工作区过滤与归属过滤同类（都是"这批里要哪一部分"），所以在同一处做：
        # `workspace_id` 点名某个工作区；`ungrouped` 要的是"未归档"那一栏
        # （`workspace_id IS NULL`）——两者互斥，同时给等于没有交集，直接返回空。
        def keep(item: ConversationRecord) -> bool:
            # **归档是一道前置过滤**：默认视图里看不到归档的会话，
            # 要看它们得显式要（`archived=True`）。与归属过滤分开写，
            # 因为它们是两件不同的事（谁的 / 收没收起来）。
            if (item.archived_at is not None) != archived:
                return False
            if owner_id is not None and item.owner_id != owner_id:
                return False
            if ungrouped:
                return item.workspace_id is None
            if workspace_id is not None:
                return item.workspace_id == workspace_id
            return True

        if owner_id is None and workspace_id is None and not ungrouped and not archived:
            # 无过滤的快路径：SQL 里就把归档的排除掉，别拉回来再筛
            records = [
                item
                for item in self._stores.meta.list_conversations(limit=limit, q=q)
                if item.archived_at is None
            ]
            return records
        records = [
            item for item in self._stores.meta.list_conversations(limit=None, q=q) if keep(item)
        ]
        return records[:limit] if limit is not None else records

    def rename(self, conversation_id: str, title: str) -> ConversationRecord:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("会话标题不能为空")
        self.get(conversation_id)
        self._stores.meta.rename_conversation(conversation_id, cleaned[:TITLE_MAX_CHARS])
        return self.get(conversation_id)

    def set_model(self, conversation_id: str, model_pk: str | None) -> ConversationRecord:
        """记录该会话选用的对话模型（``None`` = 回到全局默认）。

        **不校验 model_pk 是否真实存在**：那是 ``ModelRegistryService`` 的事（解析时
        会报明确的错）。这里只管存，避免两处各写一份校验而漂。
        """
        self.get(conversation_id)
        self._stores.meta.set_conversation_model(conversation_id, model_pk)
        return self.get(conversation_id)

    def set_thinking(
        self, conversation_id: str, thinking: bool | None, effort: str | None
    ) -> ConversationRecord:
        """记录该会话的思考偏好（``None`` = 回到全局默认）。与 ``set_model`` 同一套口径。"""
        self.get(conversation_id)
        self._stores.meta.set_conversation_thinking(conversation_id, thinking, effort)
        return self.get(conversation_id)

    def delete(self, conversation_id: str) -> None:
        """删会话（消息由外键级联一并删掉）。"""
        self.get(conversation_id)
        self._stores.meta.delete_conversation(conversation_id)

    def set_archived(self, conversation_id: str, archived: bool) -> ConversationRecord:
        """归档 / 取消归档。**不是删除**：内容与引用都还在。
        不推 ``updated_at``（与置顶/改名同理，见存储层协议）。"""
        self.get(conversation_id)
        self._stores.meta.set_conversation_archived(conversation_id, archived)
        return self.get(conversation_id)

    def previews(self, conversation_ids: list[str]) -> dict[str, str]:
        """``会话 id → 最后一条回答``。历史会话面板的两行预览用它。"""
        return self._stores.meta.last_assistant_previews(conversation_ids)

    def set_pinned(self, conversation_id: str, pinned: bool) -> ConversationRecord:
        """置顶 / 取消置顶。**不推 updated_at**（与改名同一套口径）：置顶是一次整理
        动作，不该把会话顶到"最近活动"的最前面——何况它本来就排最前了。"""
        record = self.get(conversation_id)
        if bool(pinned) == record.pinned:
            return record
        self._stores.meta.set_conversation_pinned(conversation_id, pinned)
        record.pinned = bool(pinned)
        return record

    def rewind(self, conversation_id: str, *, turns: int = 1) -> str:
        """回退最近 ``turns`` 轮问答，返回**被删掉的那句提问**（没有则空串）。

        给「重新生成」用：删掉最后一轮（提问 + 回答），把那句提问还给调用方，
        由它重新发一次——重发走的是正常提问链路，所以**不需要**第二条生成路径。

        为什么是"删一轮"而不是"给回答加个版本"：会话是一份线性记录，
        版本化的代价（每条回答多一张表、回看时还要选版本）远大于它带来的价值；
        用户要的是"这个答案我不满意，重来一次"，旧答案留着反而会让上下文里有两条
        相互矛盾的回复。

        **没有可回退的提问就报错**，不返回空串：静默成功会让调用方以为删了点什么，
        接着发一次空提问。契约只有一个（要么给回问题、要么报错），调用方不必判两种情况。
        """
        if turns <= 0:
            raise InvalidRequestError("回退轮数必须为正整数")
        messages = self._stores.meta.list_messages(conversation_id)
        # 从末尾往前找第 turns 个"提问"，它就是这一轮的开始
        start: int | None = None
        seen = 0
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "user":
                seen += 1
                if seen == turns:
                    start = index
                    break
        if start is None:
            raise InvalidRequestError("这段对话里没有可回退的提问")
        removed = [item.id for item in messages[start:]]
        query = messages[start].content
        self._stores.meta.delete_chat_messages(removed)
        return query

    # ------------------------------------------------------------------ 消息

    def branch(self, conversation_id: str, *, turn: int) -> ConversationRecord:
        """**从这里重开**（D11，2026-09-28 走查）：以第 ``turn`` 轮为界，把到那一轮
        为止的历史复制进一条**新会话**，然后在那条新会话里继续。

        为什么是"复制一段历史"而不是"给消息加 ``parent_message_id``"（走查报告的建议）：
        走查要的是"保留原分支、另起一条"（"用户不敢乱试就是因为一改就回不去"）。
        做对话树要动 ``chat_messages`` 的模型（parent / 版本号）、读侧要选版本、
        回看时还要回答"这是哪条分支"——而用户手上要的只是**另一条能接着聊的会话**。
        复制一份历史是同一件事，代价却小一个量级：原会话**一个字节都不动**，
        两条会话各自可继续，符合"分支"的全部外部行为。

        **第 ``turn`` 轮 = 第 ``turn`` 个提问**（它后面紧跟的那条回答也算这一轮）。
        还没有回答的那一轮（被打断、或续跑正在跑）**照样能当分叉点**：复制出来的
        会话带着那句提问，接着往下聊就是。

        带走的东西（"能带多少带多少"）：

        - 消息（含**出处 / 步骤 / 思考快照**）与它们之间的**事件日志**——
          一次事务写进去，与 ``record_turn`` 同一条纪律（"消息在、事件不在"的窗口
          在这里同样不能有）；
        - 会话档位：``kb_ids`` / 模型 / 思考偏好 / 工作区 / **归属**（``owner_id``
          继承源会话：分叉出来的历史是同一个人的，管理员替成员分叉也一样）；
        - 压缩摘要——**只有它的覆盖标记落在这段历史里**才带（标记在 cut 之外说明
          那份摘要讲的是**后面**那些轮次，带过去等于把未来塞进分支）。

        **不带走文件区**（产物记录与对象存储里的字节一个都不动），所以用户消息上的
        **附件快照也不抄**：那份快照里的 key 指向源会话的对象存储记账，抄过去在
        新会话里点开必然 404（文件抽屉按本会话的产物记录找它）；而共享同一个对象
        key 更糟——删掉源会话会把分叉的文件一起带走，"两条真的独立"就不成立了。

        ``created_at`` **照抄**：消息顺序就是对话顺序，重打一遍时间戳等于让"哪条在前"
        取决于插入的物理位置（``list_messages`` 按 ``(created_at, ctid)`` 排）。
        事件的 ``seq`` 不抄——那是**新会话自己的**编号，由存储层在写事务里从 1 重算。
        """
        source = self.get(conversation_id)
        messages = self._stores.meta.list_messages(conversation_id)
        kept = _turn_prefix(messages, turn)
        if kept is None:
            raise InvalidRequestError(
                f"这段对话只有 {_turn_count(messages)} 轮，没有第 {turn} 轮可作分叉点"
            )

        created = self.create(
            kb_ids=list(source.kb_ids),
            title=_branch_title(source.title, turn),
            owner_id=source.owner_id,
            model_pk=source.model_pk,
            thinking=source.thinking,
            thinking_effort=source.thinking_effort,
            workspace_id=source.workspace_id,
        )

        # 旧 id → 新 id 的对照表：摘要那份标记指向的是一条**消息 id**，
        # 而抄过来的每条消息都是新记录，标记要跟着映射（否则它指向源会话的消息）。
        id_map: dict[str, str] = {}
        copies: list[ChatMessageRecord] = []
        for item in kept:
            new_id = f"msg_{uuid.uuid4().hex[:12]}"
            id_map[item.id] = new_id
            copies.append(
                ChatMessageRecord(
                    id=new_id,
                    conversation_id=created.id,
                    role=item.role,
                    content=item.content,
                    sources=tuple(item.sources),
                    steps=tuple(item.steps),
                    thinking=item.thinking,
                    attachments=(),
                    created_at=item.created_at,
                )
            )
        events = [
            SessionEventRecord(
                conversation_id=created.id,
                kind=item.kind,
                payload=dict(item.payload),
                created_at=item.created_at,
            )
            for item in _event_prefix(
                self._stores.meta.list_session_events(conversation_id),
                turns=sum(1 for item in kept if item.role == "assistant"),
            )
        ]
        try:
            self._stores.meta.append_turn(messages=copies, events=events)
        except Exception:
            # **半条分叉比没分叉更糟**：用户会在列表里看到一条空会话，以为成功了。
            # 消息与事件本来就写在一个事务里，这里只需把这层空壳收掉（级联删消息）。
            try:
                self._stores.meta.delete_conversation(created.id)
            except Exception:  # pragma: no cover - 兜底清理失败不该盖住原始错误
                logger.exception("分叉失败后清理空会话也失败：%s", created.id)
            raise

        summary, upto = self.summary(conversation_id)
        if summary and upto is not None and upto in id_map:
            self.set_summary(created.id, summary, id_map[upto])
        return self.get(created.id)

    def last_turn(self, conversation_id: str) -> LastTurn | None:
        """最后一轮的快照。**给续跑用**，没有就返回 None。

        与 ``rewind`` 的区别：那个删掉整轮（提问 + 回答）把问题还给调用方重发；
        这里只看不删——续跑要的是"同一轮接着做"，提问得留在原地。

        找不到"提问 + 回答"的成对结构就返回 None（会话只有提问、或刚被回退过）：
        能不能续由调用方判断（它还要看那条回答有没有降级标记，见 services/resume.py）。
        """
        messages = self._stores.meta.list_messages(conversation_id)
        for index in range(len(messages) - 1, -1, -1):
            answer = messages[index]
            if answer.role != "assistant":
                continue
            for earlier in range(index - 1, -1, -1):
                if messages[earlier].role == "user":
                    return LastTurn(
                        question=messages[earlier].content,
                        answer_id=answer.id,
                        answer=answer.content,
                        sources=tuple(answer.sources),
                        steps=tuple(answer.steps),
                        thinking=answer.thinking,
                    )
            return None
        return None

    def drop_answer(self, conversation_id: str, *, answer_id: str) -> None:
        """删掉一条回答。**续跑时用**：新的回答会顶替它。

        为什么不让两条回答并存：同一个问题底下挂着两条回答，第二条还在开头写
        "接着上次继续"，回看的人第一件要猜的事就是"上次是哪次"。
        这与「重新生成」删一轮是同一条纪律（见 ``rewind`` 的说明）。
        """
        self.get(conversation_id)
        self._stores.meta.delete_chat_messages([answer_id])

    def messages(self, conversation_id: str) -> list[ChatMessageRecord]:
        self.get(conversation_id)
        return self._stores.meta.list_messages(conversation_id)

    def message_count(self, conversation_id: str) -> int:
        return self._stores.meta.count_messages(conversation_id)

    def append(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str,
        sources: list[dict[str, object]] | None = None,
        steps: list[dict[str, object]] | None = None,
        thinking: str = "",
        attachments: list[dict[str, object]] | None = None,
    ) -> ChatMessageRecord:
        """追加一条消息，并把会话的 ``updated_at`` 推到现在。

        两个动作是**有意分开存但一起做**：消息本身只读不写，而列表要按
        "最近聊过"排序。忘了 touch 的后果是会话永远排在最后，很难被发现。
        """
        self.get(conversation_id)
        record = self._stores.meta.append_message(
            ChatMessageRecord(
                id=f"msg_{uuid.uuid4().hex[:12]}",
                conversation_id=conversation_id,
                role=role,
                content=content,
                sources=tuple(sources or ()),
                steps=tuple(steps or ()),
                thinking=thinking,
                attachments=tuple(attachments or ()),
            )
        )
        self._stores.meta.touch_conversation(conversation_id)
        return record

    # ------------------------------------------------------ 事件日志（P0-2）

    def record_turn(
        self,
        conversation_id: str,
        *,
        question: str,
        answer: str,
        sources: Sequence[dict[str, object]] = (),
        steps: Sequence[dict[str, object]] = (),
        thinking: str = "",
        events: Sequence[EventDraft] = (),
        attachments: Sequence[dict[str, object]] = (),
    ) -> None:
        """把一轮问答（提问 + 回答两条消息）**连同它的事件日志**写进库。

        抄的是 ZCode 那条"会话 = 只追加事件日志"（开发计划 §12.225 的 P0-2）：
        消息与事件在**同一个事务**里落库，于是不存在"消息在、事件不在"的窗口。
        此前（v0.25 起）步骤是流式当时拍下的快照，边流边写会多几十次 UPDATE，
        所以只能结束时一次性写；现在事件是只追加的行，同样一次写、代价不变，
        但**每一轮都留下了一份不可变的过程记录**。

        ``attachments``（v0.55）是这一轮用户消息**随发的附件快照**（文件区里的
        key / 名字 / 类型 / 字节数），只挂用户那一条——用户报的"我发的对话里没有
        文件组件标识"，根因就是原先消息与文件之间**没有任何关联可查**。

        ``steps`` 仍然照旧存：快照的形状一个字没变，老读法（回看、续跑、
        ``degraded`` 判断）全都不受影响。它现在的另一个身份是事件日志的投影
        ——两者应当等价，``tests/unit/services/test_session_events.py`` 与
        端到端用例各钉一遍（见 ``services/session_events.steps_from_events``）。
        """
        self.get(conversation_id)
        messages = [
            # 附件（v0.55）**只挂用户那一条**：它们是"这次带着哪几份文件问的"，
            # 不是回答的一部分（回答里用到的东西由 steps / sources 表达）。
            self._message(
                conversation_id, role="user", content=question, attachments=attachments
            ),
            self._message(
                conversation_id,
                role="assistant",
                content=answer,
                sources=sources,
                steps=steps,
                thinking=thinking,
            ),
        ]
        self._write_turn(conversation_id, messages=messages, events=events)

    def append_answer(
        self,
        conversation_id: str,
        *,
        answer: str,
        sources: Sequence[dict[str, object]] = (),
        steps: Sequence[dict[str, object]] = (),
        thinking: str = "",
        events: Sequence[EventDraft] = (),
    ) -> None:
        """只追加**回答**那一条（续跑用：提问上一轮就在库里，不能再落一遍）。

        与 ``record_turn`` 共用同一条落库路径与同一条"消息 + 事件一起写"的纪律，
        区别只有"写几条消息"——两处各写一份，迟早会出现"续跑那一轮的事件丢了"
        这种只在某一条路上才有的毛病。
        """
        self.get(conversation_id)
        messages = [
            self._message(
                conversation_id,
                role="assistant",
                content=answer,
                sources=sources,
                steps=steps,
                thinking=thinking,
            )
        ]
        self._write_turn(conversation_id, messages=messages, events=events)

    def append_events(
        self, conversation_id: str, events: Sequence[EventDraft]
    ) -> list[SessionEventRecord]:
        """只追加事件、不写消息（中断与失败那两条路用）。

        什么时候会有"事件在、消息不在"：用户中途点了停止（正文没写完，
        答不成一条消息），或者这一轮在流里报了错（按 v0.12 起的取舍，失败的一轮
        不留半截记录）。这两种情况**恰恰最需要日志**——回看时"当时为什么没有回答"
        只有这里答得出来。反过来（消息在、事件不在）才是不能容忍的那种，
        它由 ``record_turn`` 的同一事务挡住。
        """
        self.get(conversation_id)
        if not events:
            return []
        return self._stores.meta.append_session_events(
            [self._event_record(conversation_id, draft) for draft in events]
        )

    def session_events(
        self, conversation_id: str, *, kinds: Sequence[str] | None = None
    ) -> list[SessionEvent]:
        """按 ``seq`` 正序读这条会话的事件（``kinds`` 非空时只取那几种）。

        ``kinds`` 里出现词表之外的取值**当场报错**而不是返回空列表：
        静默返回空会让调用方以为"这条会话没有这类事件"，而真正的原因是拼错了
        ——这个端点是给人和脚本读日志用的，说清楚比宽容有用。
        """
        self.get(conversation_id)
        if kinds:
            unknown = [kind for kind in kinds if kind not in EVENT_KINDS]
            if unknown:
                raise InvalidRequestError(
                    f"不认识的事件类型：{'、'.join(unknown)}；"
                    f"可用的是：{'、'.join(EVENT_KINDS)}"
                )
        records = self._stores.meta.list_session_events(conversation_id, kinds=kinds)
        return [
            SessionEvent(
                id=int(record.id or 0),
                seq=record.seq,
                kind=record.kind,
                payload=dict(record.payload),
                created_at=record.created_at,
            )
            for record in records
        ]

    def _write_turn(self, conversation_id: str, *, messages, events) -> None:  # type: ignore[no-untyped-def]
        """消息 + 事件一次写完，再推 ``updated_at``（与 ``append`` 同一口径）。"""
        self._stores.meta.append_turn(
            messages=messages,
            events=[self._event_record(conversation_id, draft) for draft in events],
        )
        self._stores.meta.touch_conversation(conversation_id)

    @staticmethod
    def _message(
        conversation_id: str,
        *,
        role: str,
        content: str,
        sources: Sequence[dict[str, object]] = (),
        steps: Sequence[dict[str, object]] = (),
        thinking: str = "",
        attachments: Sequence[dict[str, object]] = (),
    ) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            conversation_id=conversation_id,
            role=role,
            content=content,
            sources=tuple(sources),
            steps=tuple(steps),
            thinking=thinking,
            attachments=tuple(attachments),
        )

    @staticmethod
    def _event_record(conversation_id: str, draft: EventDraft) -> SessionEventRecord:
        """草稿 → 存储记录。``seq`` 留 0：那是**写那个事务里**才算得出来的东西。"""
        return SessionEventRecord(
            conversation_id=conversation_id,
            kind=draft.kind,
            payload=dict(draft.payload),
        )

    def ensure_title(self, conversation_id: str, first_question: str) -> None:
        """首轮提问落库后用它生成标题——**只在还没有标题时**。

        用户手动改过名字的会话不该被后续提问覆盖掉。
        """
        record = self.get(conversation_id)
        if record.title.strip():
            return
        title = _title_from(first_question)
        if title:
            self._stores.meta.rename_conversation(conversation_id, title)

    # ------------------------------------------------------------------ 历史

    def history(
        self, conversation_id: str, *, turns: int = DEFAULT_HISTORY_TURNS
    ) -> list[ChatMessage]:
        """把最近 ``turns`` 轮折成交给模型的 history。

        取**最近**若干条而不是最早的：多轮对话里，指代与省略几乎总是指向上一轮，
        最早的几轮对当前问题的帮助最小。

        ``turns`` 数的是"消息条数"而不是"问答对数"：一问一答是两条，调用方给的
        是量级而非精确值——把它当条数更贴近"别把提示词挤爆"这个目的。
        """
        records = self._stores.meta.list_messages(conversation_id)
        recent = records[-turns:] if turns > 0 else []
        return [
            ChatMessage(role=item.role, content=item.content)
            for item in recent
            if item.role in ("user", "assistant") and item.content.strip()
        ]

    # ------------------------------------------------- 上下文摘要（v20.1 压缩）

    def summary(self, conversation_id: str) -> tuple[str, str | None]:
        """（早期对话的摘要，摘要覆盖到的最后一条消息 id）。

        ``("", None)`` = 还没压缩过。取的时候**不校验会话是否存在**——
        调用方通常是"先拿到会话再来问"，这里再查一次只是多一次往返。
        """
        return self._stores.meta.get_conversation_summary(conversation_id)

    def set_summary(
        self, conversation_id: str, summary: str, upto_message_id: str | None
    ) -> None:
        self._stores.meta.set_conversation_summary(conversation_id, summary, upto_message_id)


def _turn_count(messages: Sequence[ChatMessageRecord]) -> int:
    """这段对话有几轮（一轮 = 一次提问）。"""
    return sum(1 for item in messages if item.role == "user")


def _turn_prefix(
    messages: Sequence[ChatMessageRecord], turn: int
) -> list[ChatMessageRecord] | None:
    """到第 ``turn`` 轮为止的消息前缀；没有第 ``turn`` 轮就返回 ``None``。

    一轮从**提问**开始；它后面紧跟的那条回答也算这一轮。被打断、或续跑正在跑的那一轮
    只有提问本身——照样是合法的分叉点（复制出来的会话带着那句提问，接着往下聊就是）。
    """
    if turn < 1:
        return None
    seen = 0
    for index, item in enumerate(messages):
        if item.role != "user":
            continue
        seen += 1
        if seen == turn:
            end = index + 1
            if end < len(messages) and messages[end].role == "assistant":
                end += 1
            return list(messages[:end])
    return None


def _event_prefix(
    events: Sequence[SessionEventRecord], *, turns: int
) -> list[SessionEventRecord]:
    """前 ``turns`` 轮**产出过回答**的历史事件。

    三件事都是刻意的：

    1. **按"产出过回答"的轮次数，不按第 N 个 ``turn/start``**：消息轮次与事件轮次
       并不一一对应——出错（``error``）与"一个字都没吐"（``empty``）的那两轮按 v0.12
       起的取舍**不留消息**（``chat.py::_record_turn`` 只在拿到正文时才写两条），
       而它们的事件照样在日志里。所以要按收尾状态（``ok`` / ``degraded`` = 这一轮
       写出了回答）数轮次；
    2. **没产出回答的那几轮整块丢掉**：新会话的消息里没有它们，事件留着就对不上——
       读侧是按"每条 assistant 消息配一段事件"配对的（``session_events.steps_per_turn``
       + ``fill_missing_thinking``），多出一段会把某一步的推理挂到**错的**那一轮上，
       而那种错不报错、只是回看时内容错位。原会话的日志一个字没动，那些事件仍在原处；
    3. **轮次之间的"无主"事件**（模式切换、命令）只要落在这一段里就带上——它们没有
       配对的消息，但解释了后面那一轮是在什么档下跑的。数满之后的一律不带。

    没有 ``turn/end`` 的那一轮（进程崩过、或还在跑）同样不进结果：它没有消息可对应。
    """
    kept: list[SessionEventRecord] = []
    pending: list[SessionEventRecord] = []
    produced = 0
    for event in events:
        if event.kind == KIND_TURN_START:
            if produced >= turns:
                break
            pending = [event]
            continue
        if not pending:
            if produced >= turns:
                break
            kept.append(event)
            continue
        pending.append(event)
        if event.kind != KIND_TURN_END:
            continue
        if event.payload.get("status") in _MESSAGE_TURN_STATUSES:
            kept.extend(pending)
            produced += 1
        pending = []
    return kept


#: 分叉会话标题里那截标记（见 `_branch_title`）。
_BRANCH_MARK = re.compile(r"（分支 · 第 \d+ 轮）$")


def _branch_title(title: str, turn: int) -> str:
    """分叉会话的标题：源标题 + 「（分支 · 第 N 轮）」。

    为什么必须带标记：照抄源标题会让侧栏出现两条**一模一样**的会话，用户分不清
    哪条是哪条；而"从第几轮分出来"正是这条会话唯一可说的话（也是分叉相对
    "重新生成"的唯一可见差别）。

    **旧的标记要先去掉再接新的**：分叉出的会话再分叉是允许的，一路接下去标题会变成
    一长串括号（侧栏放不下）。这里不另加长度上限——标题列是 ``text``，而多出来的
    只有这截标记本身（源标题本来就已经过接口那道上限）。
    """
    base = _BRANCH_MARK.sub("", title.strip()).rstrip()
    return f"{base or '新对话'}（分支 · 第 {turn} 轮）"


def _title_from(question: str) -> str:
    """首轮提问 → 会话标题。

    压平空白再截断：用户可能粘一整段带换行的文本进来，标题里带换行会撑坏左栏。
    截断处不加省略号——中文标题里"…"占一个字宽，而左栏本来就会用 CSS 省略。

    **比"硬切前 24 字"多做的三件事**（D15，2026-09-28 走查；规则都是从真库里那批标题
    反推出来的——实测 11/14 条标题**就是**首问压平后的前 24 字）：

    1. **去掉开头的礼貌/意图引导词**：「请用 Markdown 表格列出 12 个中国省」这类标题，
       前两个字不承载任何信息，却把真正的意思挤掉一截（去掉之后那一条刚好完整放得下）；
    2. **优先在自然断点收尾**：限长之内、且位置够靠后（≥ 60%）的最后一个句读，
       比硬切好读（实测有一批标题以「。」「，」结尾）；
    3. **去掉结尾悬空的标点与虚词**：中文里「…的」「…，」这样的收尾读起来是被切断了，
       而不是"这就是标题"。

    一件事**没做**：不调模型重新起名。那要多一次模型调用（并且要处理异步、失败、
    计费），而这一条要解决的只是"标题读起来像被切断的"——先把它做扎实。
    """
    flat = " ".join(question.split())
    for lead in _TITLE_LEAD_INS:
        # 只去一次，且别把整句话都削没了（`请记住这个口令：…` 削掉「请」还剩 10 个字以上）
        if flat.startswith(lead) and len(flat) > len(lead) + 4:
            flat = flat[len(lead) :].lstrip("，,：: ")
            break
    if len(flat) <= TITLE_MAX_CHARS:
        return _strip_title_tail(flat)
    window = flat[:TITLE_MAX_CHARS]
    # 够靠后的**句末**标点优先（太靠前的断点会把标题削得太短，还不如硬切）。
    # **只认句末**：「，」是句内停顿，在那里断会把"分 5 个小标题"这种真信息丢掉
    # ——第一版就是那么写的，对照真库数据当场看出来退步了。
    floor = int(TITLE_MAX_CHARS * _TITLE_BREAK_RATIO)
    for mark in ("。", "！", "？", ".", "!", "?"):
        index = window.rfind(mark)
        if index >= floor:
            return _strip_title_tail(window[:index])
    return _strip_title_tail(window)


#: 标题开头那些**礼貌 / 意图**引导词：占着前几个字却不带信息（D15）。
#: 按长度从长到短排，先匹配长的那条（不然「请」会先把「请帮我」削掉一半）。
_TITLE_LEAD_INS = (
    "请帮我",
    "麻烦你",
    "麻烦",
    "帮我",
    "请问",
    "我想",
    "能不能",
    "可以帮我",
    "请",
)

#: 限长之内若在这个比例之后遇到句读，就在那里收尾（见 `_title_from` 第 2 条）。
_TITLE_BREAK_RATIO = 0.6

#: 结尾悬空的标点与虚词（见 `_title_from` 第 3 条）。
_TITLE_TAIL_CHARS = "。，、；：！？!?,;:., "


def _strip_title_tail(text: str) -> str:
    """去掉标题结尾悬空的标点与虚词。

    **逐字往回剥**：实测「用 Markdown 表格列出 12 个中国省份的」这种收尾很常见
    （硬切正好切在「的」前面），剥掉之后才像一句标题。剥到没有可剥的为止，
    但**不为空**——全是标点的标题比"被切断的标题"更糟。
    """
    stripped = text.rstrip(_TITLE_TAIL_CHARS)
    while stripped and stripped[-1] in _TITLE_DANGLING:
        stripped = stripped[:-1].rstrip(_TITLE_TAIL_CHARS)
    return stripped or text.rstrip(_TITLE_TAIL_CHARS)


#: 结尾不该出现的虚词（助词 / 连词 / 介词）。**只剥结尾**：标题中间出现它们很正常。
_TITLE_DANGLING = "的了和与及把被在是对着给让使"
