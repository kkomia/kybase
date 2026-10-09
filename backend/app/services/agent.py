"""对话链路的事件类型（服务层内部契约）。

本模块只放**纯逻辑**：事件数据结构。检索与模型调用留在 ``services/chat.py``
（那里才拿得到检索服务与配置），这样本模块不依赖任何服务，也就不会与 chat.py
形成循环导入。

**历史（v0.2 清理）**：这里曾经装着"意图识别 → 检索词改写 → 多轮检索工具"那一整套
——``PLAN_PROMPT`` / ``DECIDE_PROMPT`` / ``parse_plan`` / ``parse_decision`` /
``AgentPlan`` / ``AgentDecision`` / ``intent_label`` 与它们的解析辅助函数。
它的前提是"模型不支持原生工具调用，所以把检索描述成提示词里的一个工具，
让模型输出 JSON 表态"（见 v0.1 的架构说明）。对话主流程换成原生 ``tools``
参数的工具循环（``services/tool_loop.py``）之后，那一整条链路**没有任何调用者**：
旧函数还占着"主 Agent 逻辑"的位置，连设置页都挂着一个只有它在读的选项
（``chat.agent_max_rounds``，改了不生效）。整套删除，只留事件类型——
它们仍是 API 层与前端共用的 wire contract。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型标注；运行时不需要，避免与 chat.py 循环导入
    from app.services.chat import SourceRef

__all__ = [
    "ApprovalEvent",
    "DeltaEvent",
    "DoneEvent",
    "SourcesEvent",
    "StepEvent",
    "StepThinking",
    "ThinkingEvent",
    "step_snapshot",
]


# --------------------------------------------------------------------- 事件
# 服务层内部的进度事件，由 api/v1/chat.py 翻成 SSE 的 type 字段。
# 用 dataclass 而不是裸 dict：事件形状是这条链路的对外契约，写死类型才能在改动时被
# 类型检查与测试拦住（协议层负责序列化，服务层不碰 wire format）。


@dataclass(frozen=True, slots=True)
class StepEvent:
    """工作流里的一个步骤（第 N 次检索、组织回答……）。"""

    phase: str
    label: str
    detail: str = ""
    status: str = "done"  # "running" | "done"
    degraded: bool = False
    """这一步**没按设计跑成**，走了降级路径。

    现在的生产者是工具循环的两道闸（``tool_loop.py``）：步数用尽与整轮墙钟用尽
    都会发一条 ``degraded=True`` 的步骤，用户据此看到"这次没跑完"，
    也据此拿到「继续」（接着做，见 ``services/resume.py``）。
    界面那条横幅的措辞取自 ``detail``（不写死），所以两种原因的文案各说各的。
    """
    added: int | None = None
    """本步带来的**新增**资料条数（只有检索类步骤有）。

    由检索工具自己算（相对"这一轮已经给过的那些"），界面靠它把那一步显示成
    「这轮没找到新资料」——**"又查了一次但什么都没多出来"与"查到了新东西"
    对用户是两件不同的事**，而只看「命中 8 段」看不出来（见 ``agent_tools._absorb``）。
    """
    outcome: str = ""
    """这一步的**结果类别**（D22，2026-09-28 走查）：``""`` 正常 /
    ``"blocked"`` 被拦下 / ``"awaiting"`` 在等用户确认 / ``"failed"`` 这一步没做成。

    原先只有 ``status``（``running``/``done``）——被拦下与跑完都是 ``done``，
    界面就只能**匹配句式**（「没有执行」「等待确认」「拒绝执行」…）来判断
    "这一行说的不是成功，是拦截"，而那一行是**默认展开**的
    （见 ``TraceStepRow.isRefusalDetail``）：措辞一改，用户就会以为它做了。
    由执行器给结构化事实（``ToolOutcome.outcome``），这里原样带上去。
    """

    args: str = ""
    """模型给这个工具的**原始入参**（JSON 字符串，v0.25）。

    给界面"点开看这一步到底调了什么"用——只给 ``detail`` 那一行人话摘要的话，
    用户没法判断"检索知识库"这次查的是什么词、为什么没命中。

    保留**原始字符串**而不是解析后的 dict：模型可能给出不完整或不合法的 JSON,
    而这里只是要给用户看，不该因为解析失败就把整条步骤丢掉。
    """

    result: str = ""
    """工具返回的正文（v0.25，已按 ``MAX_STEP_RESULT_CHARS`` 截断）。

    与 ``detail`` 的分工：``detail`` 是**结论**（「命中 8 段」），
    它是**原文**。界面默认只显示结论，用户点开才看原文。
    """

    tool: str = ""
    """这一步调的是**哪个工具**（``web_search`` / ``fetch_url`` / ``search``…，v0.26）。

    为什么不能从 ``label`` 推：label 是给人看的中文（「联网搜索」），
    它会被改写、会为了顺口而合并；而界面要拿它做两件事——**挑图标**与**把同类调用并成一组**，
    两件都要求它是个稳定标识。``phase`` 也不行：所有工具调用的 phase 都是 ``tool``。

    非工具步骤（理解问题、组织回答）是空串。
    """

    artifacts: tuple[dict[str, object], ...] = ()
    """这一步**产出的文件**（v0.25）：导出类工具生成的那份东西。

    为什么要单独带出来：那些工具原本只在 `detail` 里说一句"已存进知识库"，
    于是界面上**没有可点的东西**——用户要下载还得自己去文档列表里找。
    带上 id 与名字之后，界面能在这一步下面挂一张文件卡片，点开就是下载。

    ``sources`` 是"这一步读到了什么"，``artifacts`` 是"这一步产出了什么"，
    两个方向不同，所以是两个字段而不是一个。
    """

    kind: str = ""
    """这一步的**语义种类**（P2-1，照 ZCode 的工具卡四元组）。

    取值是固定枚举（``read`` / ``search`` / ``write`` / ``delete`` / ``exec`` /
    ``skill`` / ``session`` / ``message`` / ``tool``，定义在
    ``services/tool_meta.py`` 一处），**从工具元数据的三个字段推出来**：
    界面按它选图标与配色，于是"加一个工具"不再需要动界面
    （改之前前端的 `TOOL_ICONS` 是一张工具名→图标的表，加工具的人不记得改它，
    那一行就退化成中性方块）。

    为什么不让界面自己按 ``tool`` 分类：``tool`` 是**显示名**（会改名、会被外部
    MCP 造出任意名字），而种类是语义；两者放在一起，改名的代价就是"图标悄悄变错"。

    非工具步骤（理解问题、组织回答）是空串。
    """


@dataclass(frozen=True, slots=True)
class SourcesEvent:
    """当前累计的资料出处（多次检索会多次发出，始终是累计列表）。"""

    sources: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ThinkingEvent:
    """思考过程增量（推理模型的 reasoning_content）。"""

    text: str


@dataclass(frozen=True, slots=True)
class DeltaEvent:
    """正文增量。"""

    text: str


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    """一次工具调用**在等用户点头**（v0.41，``ask`` 档）。

    它不是"一个步骤"，而是问题：界面据此弹一条确认条，用户点的那一下走
    ``POST /api/v1/chat/approvals/{approval_id}``。发出去之后这一轮**会停在那里**
    （见 ``tool_loop._perform``）——**先发再等**是这条协议的全部要点。

    服务层与界面之间只传这七个字段：界面不解析参数、不查策略，
    要展示什么、同意之后会写下哪条规则，都由执行器在这里说清。
    """

    approval_id: str
    tool: str
    """原始工具名（界面按它选图标，与 ``StepEvent.tool`` 同一套）。"""
    label: str
    """标题（「执行命令」）——与过程面板那一行同一句话。"""
    args: str
    """要执行什么：就是用户看到的那一行命令。"""
    detail: str = ""
    """补一句上下文（在什么隔离里跑、断没断网）。"""
    rule: str = ""
    """「这类都允许」会写进放行清单的那行规则：不先给用户看，那个按钮就是盲签。"""
    timeout_seconds: float = 0.0
    """等多久算没有回应（界面据此说清"再不来就按拒绝处理"）。"""


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """回答结束，带后端拼装好的全文。"""

    answer: str


class StepThinking:
    """把模型推理**按步骤快照切成段**的累加器：这一段属于哪一步，只有这一处判。

    **要它解决什么**：一轮里的 ``ThinkingEvent`` 是一串增量，而落库的 ``thinking``
    是整轮拼起来的一整串——于是回看那条消息时，二十一步的推理全连在一起，
    看不出哪一段是"产生这次工具调用"的那一轮推理（用户报的原话：「思考就应该
    对应到他调用的工具里面去」）。而"产生这一步的那一轮推理"在流里**有位置**：
    它紧挨在这一步的步骤事件之前，所以累加起来、在这一步的快照上取走即可。

    **两条链路各自持有一个实例**（``api/v1/chat.py`` 的 ``_TurnSink`` 与
    ``services/schedule_runner.py``）：取段这一步写在 ``step_snapshot`` 里，
    两处不可能各写一份规则（那个函数存在的理由就是"两处各写一份必然分叉"）。

    **它与整轮那份 ``thinking`` 是两个累加器，不能合并**：整轮那份要跨轮一直拼、
    最后整串落库（既有行为，别动，见 ``services/conversation``）；这一份每挂上
    一处快照就**取空一次**——"增量"的定义就是"自上一处快照以来"。
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: list[str] = []

    def note(self, text: str) -> None:
        """收一段推理增量（空串不收：它既不是段的开始也不是段的内容）。"""
        if text:
            self._pending.append(text)

    def take(self) -> str:
        """取走"自上一处快照以来"的那段推理，并**取空**；没有就返回空串。

        用列表攒着最后 ``join``，不做字符串逐个相加：一次长思考有几百个增量，
        逐次相加是 O(n²)（``api/v1/chat.py`` 里那份日志草稿也攒着不拼，但理由不同
        ——它是为了少写行数，这里是为了少复制字符）。
        """
        if not self._pending:
            return ""
        text = "".join(self._pending)
        self._pending.clear()
        return text


def step_snapshot(
    event: StepEvent, *, thinking: StepThinking | None = None
) -> dict[str, object] | None:
    """把一条步骤事件收成**落库的快照**；``running`` 的那条返回 ``None``（不入库）。

    这段映射原先写在 ``api/v1/chat.py`` 的 ``_TurnSink`` 里。搬到服务层是因为
    **定时任务那条链路也要落同一份快照**（它没有 SSE，但会话里的过程面板要看的东西
    一模一样），而复制一份必然分叉——分叉的表现是"定时任务的会话里步骤显示不全"，
    那种不一致极难被注意到。

    两处刻意的取舍：

    - **``running`` 不入库**，只有「组织回答」例外：其余步骤都会跟着一条 ``done``，
      存下 running 只会在回看时多出一行没有结论的步骤；而「组织回答」的完成由
      ``DoneEvent`` 表达，不带上它就少了最后那一行；
    - **空字段不写键**（而不是写空值）：快照是每一轮都存一遍的东西，
      省下的键在长对话里是真金白银，而"缺省即空"在读取侧是一句话的事。

    ``thinking``：调用的那两条链路把**自己那个** ``StepThinking``
    传进来，于是这一步会带上"产生它的那一轮推理"（增量，见本函数的实现处）。
    不传（``None``）时快照里不会有这个键——**读日志投影那条路就是这样**：
    ``services/session_events.step_event_draft`` 落进日志的那份 payload
    与消息里那份是**同一个 dict**（调用方算过一次就传进来，见它那个 ``snapshot``
    参数），所以投影与快照仍然逐字相等，读取侧不必再判一次"这段属于哪一步"。
    """
    if event.status == "running" and event.phase != "answer":
        return None
    snapshot: dict[str, object] = {
        "phase": event.phase,
        "label": event.label,
        "detail": event.detail,
        "status": event.status,
        # 降级标记要落库（v0.32）：它原来只发给流，于是刷新一次页面，
        # "这次没跑完"的提示与「继续」按钮就一起消失了
        **({"degraded": True} if event.degraded else {}),
        # 工具名落在快照里：回看历史时同样要按它选图标、把同类调用并成一组
        **({"tool": event.tool} if event.tool else {}),
        # 种类（P2-1）：回看历史时同样是**界面选图标与配色的依据**。
        # 不落库的话，刷一次页面之后那一列就退回"按工具名猜"——正是这次要拆掉的东西
        **({"kind": event.kind} if event.kind else {}),
        **({"added": event.added} if event.added is not None else {}),
        # 结果类别要落库（D22）：与 `degraded` 同一个理由——只发给流的话，
        # 刷新一次页面，"这一步是被拦下的"就退回去靠匹配句式猜了
        **({"outcome": event.outcome} if event.outcome else {}),
        **({"args": event.args} if event.args else {}),
        **({"result": event.result} if event.result else {}),
        **({"artifacts": [dict(item) for item in event.artifacts]} if event.artifacts else {}),
    }
    if thinking is not None:
        # 挂的是**增量**（自上一处快照以来那一段），不是整串：二十一步各挂一份整串
        # 的话，那条消息的 JSON 会大二十倍，界面上同一段话也会被念二十遍。
        #
        # 一轮里并行的多个调用只有**第一个**快照带得上它——``running`` 在那两处
        # tool_loop 里是先按调用顺序全部发出去的（见 ``tool_loop._perform``），
        # 而它们在这里就返回了（上面那条 return），所以"这一轮的第一处快照"
        # 恰好是第一个 ``done``。
        segment = thinking.take()
        # 空段**不写这个键**（而不是写空串）：界面靠"有没有这个键"决定要不要渲染
        # 那一块，空串会让每一步底下都多出一块空白
        if segment.strip():
            snapshot["thinking"] = segment
    return snapshot
