"""工具循环：一轮对话的**主流程**（P0）。

这是把"知识库检索框架"换成"个人 Agent 框架"的那一处改动。与它对照的旧链路
（`ChatService.answer_agent_stream`：先判定意图 → 改写检索词 → 检索若干轮 →
把资料塞进提示词 → 作答，资料是**预设进提示词**的，模型唯一能做的动作是"再检索一次"）
在 v0.2 已整体删除，那次对照留在 git 历史与《开发计划》§12.172 里。

- **这条链路**：给模型一批工具（原生 tool calling），它自己决定查什么、做什么、
  要不要查第二次；**资料是它取回来的工具结果**，不是我们塞给它的上下文。

四个设计取舍：

1. **工具集与内置 MCP 服务共用一份实现**（`app/services/tools.py`）。13 个内置工具
   对外走 MCP、对内由这里调用——一处实现两个门。所以"知识库降级成工具"几乎是免费的：
   `search` 早就是其中一个工具了，只是以前对话循环没走它。
2. **每一步都是一次流式调用，且都带着工具表**（v0.40 合并；v0.34 先给收尾带上）。
   从前是"非流式选工具 + 流式作答"两次：实测（2026-09-21，一轮三步）
   4.44 秒那次只为了决定还要不要调工具，紧接着 4.09 秒又把同一份上下文重想一遍。
   合成一次之后每轮少一跳。代价是工具参数也可能来自**流式拼装**——
   碎片由 ``llm.assemble_tool_calls`` 按 ``index`` 拼回完整字符串再交给执行那一步，
   拼坏了照样按"不是合法 JSON"回给模型（那条路本来就有）。
   另一处变化：**思考现在每一步都会流给界面**（以前只有作答那一步的），
   过程面板因此显示的是"整轮的想法"，而不只是收尾那一次。
3. **事件形状与旧链路一致**（StepEvent / SourcesEvent / ThinkingEvent / DeltaEvent /
   DoneEvent）：协议层与界面不用为这次换框架改动，出处（SourcesEvent）也照旧发——
   用了资料就该给出处，这一点不因框架变化而丢。**旧链路删除后，"一致"的对象没有了，
   但这五个事件类型本身是现行契约**（`services/agent.py`），由本模块产出、
   由 `api/v1/chat.py` 翻成 SSE。
4. **工具报错如实回到循环**：不吞、不伪造结果。模型看到错误才能改路子；
   把失败包装成"空结果"会让它以为查过了没有，然后基于错误前提继续推理。
5. **一批里的几件事并发跑**（v0.27）：模型会把互不依赖的调用放在同一条消息里
   （见 ``services/chat.py`` 的系统提示词第 2 条），等它们的常常是同一个网络。
   串行执行时一批三页网页就是三页之和。事件与消息的顺序仍然确定：
   **running 全发 → 并发执行 → 结果按调用顺序回灌**（见 ``_execute_batch``）。
6. **思考一路开着**（v0.27 试过关掉，撤了）：选工具那一步是这条链路上次数最多的模型
   调用，实测关掉能让单次往返从 1.18s 降到 0.68s——但多步循环里真正决定快慢与好坏的是
   "下一步做什么、能不能几件事一起发、这条路走不通换哪条"，那些判断都出在思考里。
   厂商的协议也是这个意思：思考模式下带工具调用的助手消息要带着推理往后传
   （见 ``llm.ChatMessage.reasoning``），关掉等于每轮把它的计划擦一次。
7. **执行前过一道模式闸**（v0.43，P1-1，照抄 ZCode 的四档 + QwenPaw 的计划门闸）：
   当前档由 ``services/modes`` 判定、`plan` 档的门闸状态由 ``services/plan_gate`` 持有。
   **被拦的调用不执行**，而是把"为什么被拦 + 怎么办"当成工具结果回灌给模型——
   与 ``agent_exec`` 里"策略拦下"完全同一个形状（连步骤总结的措辞都对齐：
   「没有执行（…拦下）」，但**括号里写明是哪一道闸**——模式写「Agent 模式「x」」，
   策略写「拒绝规则」「命令执行策略…」——用户才知道该去动哪个设置），
   所以界面、快照、事件流都不必为它新增分支。
   **工具表不变**：四档下交给模型的工具一模一样，变的只是"这一步允不允许执行"
   （见 ``modes`` 模块头的第 1 条规矩）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:  # 只为标注：chat.py 反过来要用这个模块（工具循环是它的主流程）
    from app.services.chat import SourceRef

from app.core.exceptions import KylabError
from app.core.logging import sanitize_log_value
from app.services import modes, plan_gate
from app.services.agent import (
    ApprovalEvent,
    DeltaEvent,
    DoneEvent,
    SourcesEvent,
    StepEvent,
    ThinkingEvent,
)
from app.services.approvals import (
    ALLOW_ONCE,
    DENY,
    UNAVAILABLE,
    ApprovalDecision,
    ApprovalRegistry,
    ApprovalRequest,
)
from app.services.llm import (
    ChatError,
    ChatMessage,
    LLMReply,
    TextMarkerFilter,
    ToolCall,
    ToolCallDelta,
    ToolSpec,
    assemble_tool_calls,
)

# 「该收尾了」与「收尾必须交代什么」那两段话在 `prompt`（写给模型的措辞归它管），
# 这里只负责**什么时候**把它们送进去（§12.338 方案 3）
from app.services.prompt import WRAP_UP_NOTE, converge_note
from app.services.tool_meta import kind_of, meta_of, parallel_groups

__all__ = [
    "CONVERGE_RATIO",
    "DEFAULT_MAX_STEPS",
    "MARKER_ONLY_ANSWER",
    "MARKER_STEP_LABEL",
    "STOP_ANSWERED",
    "STOP_CONTEXT",
    "STOP_STEPS",
    "STOP_TIME",
    "StopReason",
    "ToolLoop",
    "text_marker_step",
    "tool_label",
]

logger = logging.getLogger(__name__)

#: 一轮里最多几步工具调用。
#:
#: **v0.25 从 6 提到 30**。原注释的理由是"常见的一轮是 2–3 步，给 6 足够"——
#: 那是在只有检索一个工具的时候估的。现在一轮里可能有联网搜索、抓网页、读笔记、
#: 查文档状态、派子 Agent 的组合，实测**正常提问就会撞到 6**，
#: 撞上之后界面会说"工具步数用尽，按现有资料作答"——用户看到的是一个突然变差的回答。
#:
#: 30 是"实际用不到、但也不会失控"的量级：真正的一轮长这样也不会超过十几步，
#: 而它仍然挡得住"模型反复调同一个工具"那种死循环（每一步都是一次真实请求，
#: 没有上限就会一直烧下去）。**上限本身不能取消**，这是钱的问题，不是洁癖。
#: 需要更高就把 `ToolLoop(max_steps=...)` 传大——组合根在 `services/chat.py`。
DEFAULT_MAX_STEPS = 30

#: 一轮的**墙钟上限**（秒）。与步数上限是两道独立的闸（子 Agent 里那两道同源，见
#: `services/subagent.py` 的 `MAX_SECONDS`）：
#:
#: - **步数**挡的是"来回很多次"——每一次都真实花钱；
#: - **时间**挡的是"某一步卡很久"——一次工具调用慢下来（抓一个不响应的网页、
#:   外部 MCP 卡在网络上、模型端排队），步数一动不动地耗着，而用户那边只能看着转圈。
#:   没有这道闸时，最坏情况是 `步数 × 单次超时`：30 步 × 120 秒不是理论值，
#:   而是"每次调用都接近超时"时真会发生的事。
#:
#: 300 秒是"认真查一轮够用、不正常的一轮会被拦住"的量级：正常一轮（联网搜几次 +
#: 抓两三个网页 + 派一个子 Agent）实测在 1~2 分钟内；子 Agent 自己有 90 秒的上限，
#: 所以这道闸主要是兜"主循环一步一步慢慢挪"和"工具卡住"。
#: **它不替代工具的自身超时**（那是每个工具自己的事，见 `llm.DEFAULT_TIMEOUT_SECONDS`）。
DEFAULT_MAX_SECONDS = 300.0

#: 单个工具结果的字符上限。超了截断并**明确告诉模型被截了**：
#: 悄悄截断会让它以为"这就是全部"，而截断常常正好丢在它要的那一段之后。
MAX_RESULT_CHARS = 12000

#: 这一轮的**结局**（照搬清单第 3 条：止损结局要显式）。
#:
#: 为什么要它：Kimi 对"超出上下文长度"的任务**直接计为失败**并如实说
#: （`docs/归档/调研/Kimi-Resources-能力与实现-照搬清单.md` 第 3 条，机制级），
#: 而我们从前只说一句"按现有信息作答"——"没查完"与"查完了"于是分不出来。
#: `subagent.py` 早有同形状的 `stopped_reason`（answered/budget/timeout/error），
#: 这里把它推广到主循环：**结局是枚举**，一句话只是它的渲染。
STOP_ANSWERED = "answered"
"""模型自己判断做完了（正常结局）。"""
STOP_STEPS = "steps"
"""工具步数用尽（阈值沿用 `DEFAULT_MAX_STEPS`，不新造）。"""
STOP_TIME = "time"
"""墙钟用尽（阈值沿用 `DEFAULT_MAX_SECONDS`）。"""
STOP_CONTEXT = "context"
"""上下文用尽——**留位**：Kimi 那边是"超限直接失败"，而我们有两级压缩，
不以此收尾，所以这一档目前没有产生者（写出来是为了枚举本身完整）。"""


@dataclass(frozen=True, slots=True)
class StopReason:
    """这一轮为什么停下——枚举取值 + 两句给人看的话。

    ``kind`` 是**机器读的**（落库 / 前端文案按它分档），``label`` / ``detail``
    是**人读的**（过程面板那一步），``note`` 是**回给模型**的那一句
    （最后一步还想调工具时用它拒绝，见 `_execute` 的 ``stop``）。
    """

    kind: str
    label: str
    detail: str
    note: str = ""

    @property
    def incomplete(self) -> bool:
        """这一轮**没按设计走完**吗（界面据此摆重试入口）。"""
        return self.kind != STOP_ANSWERED


#: 预算用到几成时给模型发那条「该收尾了」的提示（§12.338 方案 3）。
#: 为什么是 0.8：实测里两次把预算打满的回合（G-03 的 80 步 / K-03 的 60 步）都是
#: **打满之后**才收尾，最后那几步全花在"再试一种装法"上。留两成余量，模型才有机会
#: 把已经拿到的东西整理成回答（而不是在最后一步仓促作答）。
#:
#: **步数与时间共用这一个比例**：它们是同一个"这一轮还剩多少余量"的两种量法，
#: 分别定一个数只会让两条闸的松紧不一致（见 `DEFAULT_MAX_SECONDS` 那段的分工）。
CONVERGE_RATIO = 0.8

#: 一轮里最多重试几次**流式失败**（P2-2，见 ``_answer``）。
#:
#: 抄的是 ZCode 那条"可重试错误白名单"（调研报告 §2.1 第 3 条）：网络抖一下不该
#: 把整轮毁掉。但**上限是 1**，而且要满足"还没往界面吐任何正文"（见 ``_answer``）：
#:
#: - 重试是"抖一下"的补救，不是"端点一直坏着"的续命。那种情况如实失败更好——
#:   界面据此给出重试入口，而不是我们在这里悄悄烧调用（每一步都是真金白银）；
#: - 已经吐了正文再重试，用户会看到回答被写两遍（或者前半段凭空消失）。
#:   那道判断在 ``_answer`` 里，不在这里。
MAX_STREAM_RETRIES = 1

#: 一次工具调用**发给界面的**原文上限（入参与结果各一份）。
#:
#: 远小于 `MAX_RESULT_CHARS`（给模型的 12000）：那个是模型的上下文预算，
#: 而这个只是"用户点开看一眼这一步调了什么"。12000 字塞进 SSE 会让长会话的
#: 事件流大出一个量级，而没人会读完它——截断处如实标注。
MAX_STEP_PREVIEW_CHARS = 2000

#: 工具名 → 中文步骤名（过程面板显示的就是它）。
#:
#: 不做成"从工具定义里取 description"：那是**写给模型看的**（说清何时该用），
#: 比给用户看的标签长得多。这两个受众要的东西不一样。
_LABELS = {
    "search": "检索知识库",
    "list_knowledge_bases": "查看知识库",
    "create_knowledge_base": "新建知识库",
    "upload_document": "上传文档",
    "add_data_source": "添加数据源",
    "list_documents": "查看文档列表",
    "get_document_status": "查询文档状态",
    "delete_document": "删除文档",
    "create_note": "写笔记",
    "attach_note_to_kb": "把笔记加入知识库",
    "list_notes": "查看笔记",
    "recall": "查档案变更",
    "remember": "记住",
    "forget": "忘掉",
    "export_document": "导出文档",
    "export_table": "导出表格",
    "export_deck": "导出幻灯",
    # 交付**沙箱里已经做出来的**那份文件（v0.56）：标签要说清"它是把东西交出来"，
    # 而不是再说一次"导出"——否则面板上「导出图表」与「交付文件」看起来是同一件事，
    # 而前者是"我描述内容你造文件"、后者是"我把刚才那个文件给你"
    "export_file": "交付文件",
    "ingest_artifact": "存进知识库",
    "web_search": "联网搜索",
    "web_fetch": "抓取网页",
    "list_skills": "查看技能目录",
    "read_skill": "读技能",
    "spawn_subagent": "派子 Agent",
    # 这台机器上的能力（v0.33）：文件、执行、表格、定时任务。
    # 名字要说清**它替我做了什么**（"读文件"而不是 "read_file"）：过程面板是给用户看的，
    # 而他对这几个动作的第一反应是"它在我电脑上干什么了"
    "list_files": "查看文件",
    "read_file": "读文件",
    "search_files": "在文件里搜",
    "run_command": "执行命令",
    "list_tables": "查看表格",
    "query_table": "查表格",
    "schedule_task": "挂定时任务",
    "list_scheduled_tasks": "查看定时任务",
    # 会话文件区（v0.55）：名字照旧说清"它替我做了什么"——"上传的文件"是用户此刻
    # 脑子里那个说法（他说"我传的那个文件"），比"会话文件区"更直接
    "list_conversation_files": "查看上传的文件",
    "read_conversation_file": "读上传的文件",
    "ingest_file": "把文件加入知识库",
}


#: 同一批里最多同时跑几个工具。
#:
#: 与 ``services/tools.py`` 里"``web_fetch`` 一次最多 5 个网址"同一个量级——
#: 那个数是提示词里写给模型看的，批次也就这么大。**不是无限**：抓网页那个工具
#: 自己还会并发（最多 5），两层乘起来就是 25 个同时飞的请求，而它们背后是同一台
#: 机器、同一个出口。超过这个数的批次分两波跑，慢一点，但不会把网络打满。
MAX_PARALLEL_TOOLS = 5

#: 整段正文就是一块工具标记（剥完什么都不剩）时**顶上回答的那句话**。
#:
#: 为什么不能交空回答：界面上那是个空气泡，而落库那一步的判据是"``answer`` 非空"
#: （见 ``api/v1/chat._events``）——空回答会让**整轮**从会话里消失，用户回头看不到
#: "我问过、它没答"这件事。所以留一句人话，把"这次为什么没有回答"说在明处；
#: 「继续」那个出口由 :func:`text_marker_step` 的 ``degraded`` 带出来。
#:
#: 定在这里而不是协议层：它是**回答的措辞**，与下面那条步骤是同一件事的两半。
MARKER_ONLY_ANSWER = (
    "（这一轮没有可读的回答：模型把工具调用写进了正文，而这一轮已经没有可用的工具表"
    "——可以用下面的「继续」把它接着做完。）"
)

#: 那条说明步骤的标签。协议层按它认出"这一轮已经说过标记的事了"（见 ``chat._clean_answer``），
#: 免得两条链路各说一遍。
MARKER_STEP_LABEL = "模型把工具调用写进了正文"


def text_marker_step(names: Sequence[str], *, had_tools: bool = False) -> StepEvent:
    """正文里剥出工具调用标记时补的那一步（见 ``llm.split_text_tool_calls``）。

    **``degraded=True``**：这一轮没按设计走完——它还想查，只是没有可用的调用路
    （步数/时间已用尽、这条链路本来就没有工具，或者它有工具表却没走接口）。
    界面据此把「继续 / 重试」两个出口摆出来（见 ``ChatView`` 的 ``.reply-degraded``），
    而 ``detail`` 就是那里显示的原因（一句话由服务端给，前端不在本地写死，
    见 ``useChatTurns.degradedReason``）。

    ``had_tools`` 只影响措辞：**不能对用户说错话**——带了工具表的那一步说
    "没有可用的工具表"是假的，而这句话正是他要据以判断"再点一次有没有用"的东西。
    """
    wanted = "、".join(names) if names else "某个工具"
    why = (
        "它想调用{wanted}，却没有给出结构化调用；那段标记已从回答里去掉"
        if had_tools
        else "它想调用{wanted}，而这一轮已经没有可用的工具表；那段标记已从回答里去掉"
    )
    return StepEvent(
        phase="answer",
        label=MARKER_STEP_LABEL,
        detail=why.format(wanted=wanted),
        degraded=True,
    )


def _clip(text: str, limit: int) -> str:
    """按字符截断并**如实标注**——不说的话，用户会以为工具只返回了这么多。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（共 {len(text)} 字，已截断）"


def tool_label(name: str) -> str:
    """工具名 → 界面上的中文步骤名。

    两类回退：

    - **外部 MCP 工具**（``mcp__<服务>__<工具>``）→ ``外部工具：服务 · 工具``。
      不显示成 ``mcp__tavily__search``：过程面板是给用户看的，限定名是协议层的东西，
      而"哪个服务的哪个工具"才是他想知道的；
    - 其余未知工具**回退到原名**（不隐藏它）：看得见一个陌生名字，
      也比看不见它强——那意味着"有件事发生了但界面没说"。
    """
    known = _LABELS.get(name)
    if known:
        return known
    parts = (name or "").split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp" and parts[1] and parts[2]:
        return f"外部工具：{parts[1]} · {parts[2]}"
    return name


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """一次工具调用的结果。"""

    content: str
    """回给模型的文本（已经是字符串：工具返回结构体时由这里序列化）。"""

    sources: list[SourceRef] = field(default_factory=list)
    """检索类工具带回的出处。只有它会让界面多出一排引用。"""

    artifacts: list[dict[str, object]] = field(default_factory=list)
    """这一步**产出的文件**（导出类工具）。空 = 这一步没产出文件。

    与 ``sources`` 对称：一个是读到了什么，一个是产出了什么。
    界面拿它挂文件卡片（可点、可下载），而不是只在步骤说明里写一句
    "已存进知识库"——那句话没有落点，用户还得自己去列表里找。
    """

    summary: str = ""
    """过程面板上的那一行说明（人话）。

    **与 `content` 分开**：`content` 是给模型的结构化结果（常常是 JSON），
    直接显示会把界面变成一屏转义字符。工具自己最清楚"这次拿到了什么"，
    所以摘要由它给（如「23 篇文档」「命中 8 段」）。
    """

    outcome: str = ""
    """这一步的**结果类别**（D22，2026-09-28 走查）：``""`` 正常 / ``"blocked"`` 被拦下 /
    ``"awaiting"`` 在等用户确认 / ``"failed"`` 这一步没做成（工具内部错误、业务规则拦下）。

    **为什么要有它**：`StepEvent` 原先只有 ``status``（``running``/``done``）——
    被拦下与跑完都是 ``done``。界面因此只能**匹配句式**（「没有执行」「等待确认」
    「拒绝执行」…）判断"这一行说的不是成功，是拦截"，而那一行是**默认展开**的
    （见 `TraceStepRow.isRefusalDetail`）：措辞一改，用户看到的就是"它好像做了"。
    由执行器给结构化事实（``agent_tools`` 从 ``ExecOutcome`` 的 ``approval`` / ``ran`` 推），
    界面不再猜。
    """

    added: int | None = None
    """这一步带回的**新增**资料条数（``None`` = 这不是检索类调用）。

    **"新增"是相对这一轮已经给过的那些算的**，所以"又查了一次但什么都没多出来"
    是 ``0`` 而不是别的数——界面靠它把那一步显示成"这轮没找到新资料"。
    由执行器算而不是循环自己比：只有它知道这一批里哪些是刚去重过的
    （见 ``app/services/agent_tools.py::_absorb``）。
    """

    approval: ApprovalRequest | None = None
    """这条调用**在等用户点头**（``ask`` 档，v0.41）。

    执行器给的（见 ``agent_exec._awaiting``）：非空表示"还没有执行，先问一下"。
    循环拿它发一条 ``ApprovalEvent``、停下来等人回答，拿到决定之后带着
    ``approval=…`` 把这条**重跑一遍**——执行与"没批准时怎么回话"都还是执行器说了算，
    循环只负责"把问题送到界面上、把答案带回来"。
    """

    def step_detail(self, limit: int = 120) -> str:
        """过程面板的那一行：优先用工具给的摘要，没有才回退到结果开头。"""
        text = self.summary or " ".join(self.content.split())
        text = " ".join(text.split())
        return text[:limit] + ("…" if len(text) > limit else "")


#: 执行一个工具：``(工具名, 参数字典[, approval=…]) -> ToolOutcome``。
#: 由组合根绑定（见 ``api``/``core.services``），循环自己不认识 Services。
#:
#: ``approval`` **只在"这一批里有调用在等用户点头"时传**（见 ``_perform``）：
#: 它是那条调用的审批结论。这一层不把它做成必填参数，是为了让"不管审批"的执行器
#: （子 Agent、测试里的假执行器）保持原样——它们根本不会遇到需要审批的调用。
ToolRunner = Callable[..., ToolOutcome]


class ToolLoop:
    """把"模型 → 工具 → 模型"跑到出正文为止。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        tools: Sequence[ToolSpec],
        runner: ToolRunner,
        approvals: ApprovalRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        mode: str | None = None,
        permission: str | None = None,
        gate: plan_gate.PlanGate | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory
        self._tools = list(tools)
        self._runner = runner
        # ``approvals`` 非空 = 这条链路**有界面可以问**（对话页）。
        # 为空 = 没有人可以问（定时任务、脚本、单测）：那种链路上"停下来问"
        # 只会白等一个 120 秒，所以按"没批准"当场回给模型（见 ``_resolve_approvals``）。
        self._approvals = approvals
        self._max_steps = max(1, max_steps)
        # 至少给 1 秒：0 或负数会让每一轮一进来就"时间已用尽"，
        # 那不是"关掉这道闸"，而是"把工具整个关掉"——想关就传一个大数
        self._max_seconds = max(1.0, max_seconds)
        # 时钟可注入：用例不必真的 sleep 到超时（也就能测"跨过上限的那一步")
        self._clock = clock
        #: 这一轮的**模式档**（任务行为：要不要先给计划）。不传 = 默认档（``goal``）。
        self._mode = modes.coerce(mode) if mode is not None else modes.DEFAULT_MODE
        #: 这一轮的**权限档**（能碰多少，2026-09-27 从四档模式里拆出来的那根轴）。
        #: 不传 = 默认档（``workspace``）：子 Agent、脚本、单测这些调用点不必知道有权限轴，
        #: 而默认档与拆分之前的行为差别是"工作区里的写不再逐条问"（见 modes 模块头）。
        self._permission = (
            modes.coerce_permission(permission)
            if permission is not None
            else modes.DEFAULT_PERMISSION
        )
        #: ``plan`` 档的计划门闸。为空 = 按"还没有计划"算（fail-closed，
        #: 与 ``tool_meta`` 那条"未声明一律独占"同一口径）：计划档下宁可不写。
        self._gate = gate
        #: 这一轮还剩几次"流中断重试"（见 ``MAX_STREAM_RETRIES``）。
        #: 在 ``run`` 里每一轮重新给满，不是构造时定死——对象可能被复用几轮。
        self._stream_retries_left = MAX_STREAM_RETRIES
        #: 这一轮的**结局**（`StopReason`）；`None` = 这一轮还没跑（或正在跑）。
        #: 给调用方读的：落库/前端要按 `kind` 分"未查完"与"已作答"，
        #: 而事件流里只有渲染出来的 `label`/`detail`（见 `last_stop`）。
        self._last_stop: StopReason | None = None

    @property
    def last_stop(self) -> StopReason | None:
        """这一轮怎么结束的（`run` 跑完之后读；跑之前是 `None`）。

        为什么要有这个读数：事件流里那一步只有**给人看的两句话**，
        而"未查完"这件事要能机器分档（落库、前端文案、统计各要各的）。
        阈值一律沿用现成的两道闸，不在这里另定（照搬清单第 3 条）。
        """
        return self._last_stop

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def run(
        self,
        *,
        messages: list[ChatMessage],
        emit_prefix: Iterator[object] | None = None,
    ) -> Iterator[object]:
        """跑完一轮，产出事件流。

        ``messages`` 是**已经拼好的**（system + 历史 + 本轮问题）；
        循环只往它后面追加助手与工具消息。
        """
        if emit_prefix is not None:
            yield from emit_prefix

        if not self._tools:
            # 一个工具都没有（例如没绑 runner 的单测环境）：直接作答，
            # 不要装作"考虑过用工具"
            yield from self._answer(messages)
            return

        # 这一轮开始：把当前档告诉计划门闸（不在 ``plan`` 档就把上一段的"已给计划"清掉，
        # 见 ``plan_gate.PlanGate.sync_mode``）。放在这里而不是构造时：`ToolLoop`
        # 可能被复用几轮，而档是每轮都可能变的
        if self._gate is not None:
            self._gate.sync_mode(self._mode)

        # 墙钟从**这一轮开始**算，不从对象构造算：`ToolLoop` 可能被复用，
        # 而"这一轮用了多久"才是用户能感知的那个量
        started_at = self._clock()
        # 重试预算同理，**每一轮重新给满**（见 ``MAX_STREAM_RETRIES``）
        self._stream_retries_left = MAX_STREAM_RETRIES
        # 「预算快到八成，该收尾了」那条提示一轮只送一次（见 `_near_budget`）
        warned = False
        # 这一轮的结局：先按"还没结束"放着，三条出口各自写它（见 `last_stop`）
        self._last_stop = None

        for step in range(self._max_steps):
            if self._expired(started_at):
                # 时间到：不再开新的一轮 LLM 调用（它自己也要时间），
                # 直接收尾作答。**与步数用尽走同一条降级路径**——用户看到的东西一样，
                # 只是原因不同（结局枚举见 `StopReason`）。
                reason = self._time_stop(started_at)
                self._last_stop = reason
                yield self._stop_step(reason)
                # 收尾前把「必须交代什么」写给它：这一轮的预算确实用尽了（§12.338 方案 3）
                self._request_wrap_up(messages)
                yield from self._answer(messages)
                return
            # 预算快到八成：**提醒它该收尾了**（还剩几步如实告诉它），一轮只送一次。
            # 放在这一步的模型调用**之前**：那一次调用本身就是它决定"继续做还是收口"的地方
            if not warned and self._near_budget(step, started_at):
                warned = True
                messages.append(
                    ChatMessage(
                        role="user",
                        content=converge_note(
                            steps_left=self._steps_left(step),
                            seconds_left=self._seconds_left(started_at),
                        ),
                    )
                )
                # 记一行日志：这两条提示是"看不见的"（不进步骤事件），
                # 而"这一轮到底提醒没提醒"是事后最常问的那件事（含真跑对照）
                logger.info(
                    "预算到 %d%%：提醒模型收尾（还剩 %d 步 / %d 秒）",
                    int(CONVERGE_RATIO * 100),
                    self._steps_left(step),
                    int(self._seconds_left(started_at)),
                )
            # **每一步就是一次流式调用**（v0.40）：它要么给出一批工具调用、
            # 要么给出正文，一次说清。以前分成"非流式选工具 + 流式作答"两次——
            # 实测（2026-09-21，一轮三步）那两次做的是同一件事：
            # 4.44 秒那次只为了"决定还要不要调工具"（思考 1258 字），
            # 紧接着 4.09 秒又把同一份上下文重想一遍才作答。
            # 合成一次之后每轮少一跳，且模型每次都能在同一口气里"边想边说"。
            #
            # 代价是"工具参数也可能来自流式拼装"——那正是 v0.38 铺好的路
            # （`llm.assemble_tool_calls`），拼坏了由 `_parse_arguments`
            # 按"不是合法 JSON"回给模型，那条路本来就有。
            outcome = yield from self._answer(messages, tools=self._tools)
            if not outcome.wants_tools:
                # **这一轮以正文收尾**：``plan`` 档下就算"计划已经给了"
                # （见 `plan_gate` 模块头"什么时候算已给出计划"）。
                # 这是它**自己**判断做完了——不套收尾格式（见 `_request_wrap_up` 的说明）
                self._last_stop = StopReason(
                    kind=STOP_ANSWERED,
                    label="已作答",
                    detail="模型自己判断做完了",
                )
                self._note_plan(outcome.text)
                return
            yield from self._perform(
                messages,
                text=outcome.text,
                reasoning=outcome.reasoning,
                calls=outcome.tool_calls,
                stop=self._stop_reason(step, started_at),
            )

        # 步数用完还没收口：**如实说**，让模型基于已有信息作答，
        # 而不是把"没跑完"包装成"跑完了"。
        #
        # `degraded=True` 就是这件事：这一轮**没按设计走完**（它还想继续查，
        # 但没机会了）。界面据此给出重试入口——"这次答得浅"与"链路退化了，
        # 你可以再要一次"对用户是两件事，不说清楚他只会觉得模型不行。
        #
        # 从照搬清单第 3 条起，这一步的**结局是枚举**（`StopReason`）：label 里那句
        # 「未查完」就是要让"没查完"与"查完了"在界面上分得开（Kimi 对超限任务
        # 直接计为失败并如实说；我们从前只说"按现有信息作答"，两者读起来一样）。
        reason = self._steps_stop()
        self._last_stop = reason
        yield self._stop_step(reason)
        # 收尾前把「必须交代什么」写给它（§12.338 方案 3 的第二半）：实测两次把预算
        # 打满的回合（G-03 / K-03）都以"我再跑一次补完"收尾，而那一轮已经结束了
        self._request_wrap_up(messages)
        yield from self._answer(messages)

    def _steps_left(self, step: int) -> int:
        """还能再执行几批工具。

        最后一步（``step == max_steps - 1``）是**留给收尾作答**的（见 `_stop_reason`），
        所以此刻还剩 ``max_steps - 1 - step`` 批。报给模型的数与真正能被执行的批数一致，
        它才换算得准——报大了它会以为还能再试两次。
        """
        return max(0, self._max_steps - 1 - step)

    def _seconds_left(self, started_at: float) -> float:
        """墙钟还剩多少秒（可能为 0：刚好卡在闸上）。"""
        return max(0.0, self._max_seconds - (self._clock() - started_at))

    def _near_budget(self, step: int, started_at: float) -> bool:
        """这一步是否已经进入"预算快用完"那一段（见 `CONVERGE_RATIO`）。

        两道闸谁先到算谁：步数与时间**是同一个预算的两种量法**，任一进入那一段就该提醒
        （只按步数算的话，"某一步卡了很久"的那一轮永远收不到提醒）。
        """
        if (step + 1) >= self._max_steps * CONVERGE_RATIO:
            return True
        return (self._clock() - started_at) >= self._max_seconds * CONVERGE_RATIO

    def _request_wrap_up(self, messages: list[ChatMessage]) -> None:
        """收尾前把「必须交代哪四件事」作为一条用户消息写进对话（§12.338 方案 3）。

        为什么塞进 `messages` 而不是只写在系统提示词里：系统提示词**每一轮都在**，
        模型读到它时离收尾还很远；这一条只在预算真的用尽的那一刻出现，紧挨着
        "刚刚被闸拦住"的上下文，它才会照着格式写。

        为什么只在**用尽**时强制（"接近耗尽"那一条只提醒、不强制）：模型自己判断
        做完了而收口的那一轮是正常回答，硬套"哪部分没做成"这种格式反而会让它
        没事找事地写一段道歉（§12.338 的第三条要求：预算充足时不误伤正常回答）。

        只往 `messages` **追加**，不改它的语义：那是这一轮现拼的模型输入
        （见 `api/v1/chat.agent_messages`）；落库用的是事件流里那条回答，不是这份 list。
        """
        logger.info("收尾：把「必须交代哪四件事」写进对话（预算已用尽）")
        messages.append(ChatMessage(role="user", content=WRAP_UP_NOTE))

    def retry_step(
        self,
        *,
        messages: list[ChatMessage],
        tool: str,
        arguments: str,
        reasoning: str = "",
    ) -> Iterator[object]:
        """**只重跑一步**（D24，2026-09-28 走查）：用存下来的入参再跑一次那次调用，然后接着答完。

        为什么"接着答"是这条语义的一半：只把工具重跑一遍、回答还是旧的，用户看到的
        仍是那个建立在失败结果上的答案；这一步的全部价值在于"结果换掉之后，它把话接着说下去"。

        为什么整条都走既有循环、而不是另写一条"执行工具"的路：**闸全在循环里**。
        模式闸 / 权限档（``_blocked_by_mode``）、计划门闸、审批（``_execute_batch`` 里
        按调用发出去的那条 `approval`）都判定在 ``_execute`` / ``_resolve_approvals``
        这两处——绕过去重跑一次工具，等于开了一条"从历史里再执行一次"的后门。
        所以这里只做三件事：把这条调用**当这一批里唯一的一条**跑掉（事件顺序与
        ``_perform`` 逐条对齐）、把它接回 ``messages``、``run`` 接着跑。

        接回 ``messages`` 的那两条（assistant 的 ``tool_calls`` + ``tool`` 结果）是
        "这一次调用真的执行了"的全部凭据：模型看到的是"这一次调用成功了"，而不是一句转述。
        ``reasoning`` 由调用方给（存下来的那一步的推理）：端点（DeepSeek 实测）要求带
        工具调用的助手消息把 ``reasoning_content`` 传回来，缺它这一轮请求直接 400。

        ``stop=None``：**重跑不认上一轮那两道闸**。步数/墙钟是"这一轮还能不能继续做"
        的预算，而这是一次用户点名的动作——预算已经在"接着答"那一段里由 ``run`` 重新给。
        真正与安全有关的闸（模式 / 权限 / 审批）一个都不跳过，见上。

        **失败如实**：``_execute`` 早就定死——工具失败就是把错误当结果回灌
        （``outcome="failed"`` 那套），重跑还是失败就照旧是失败，这里一个字节都不粉饰。
        """
        call = ToolCall(id=f"call_{uuid4().hex[:12]}", name=tool, arguments=arguments)
        # 顺序与 ``_perform`` 一致：running 先发、再执行、done 带结果（界面按它把两行并成一条）
        yield StepEvent(
            phase="tool",
            label=tool_label(tool),
            tool=tool,
            kind=kind_of(tool),
            status="running",
        )
        outcomes = self._execute_batch([call], stop=None)
        merged = _merge_sources(outcomes)
        if merged:
            # 与 ``_perform`` 同一条：一批（这里是"一条"）只发一次累计出处
            yield SourcesEvent(sources=merged)
        # 审批照旧问出来、停在那儿等回答：`ask` 档的工具不允许因为"这次是从历史里重跑"
        # 就免问——这条路上同样没有界面之外的旁路
        outcomes = yield from self._resolve_approvals([call], outcomes)
        outcome = outcomes[0]
        yield StepEvent(
            phase="tool",
            label=tool_label(tool),
            tool=tool,
            kind=kind_of(tool),
            detail=outcome.step_detail(),
            added=outcome.added,
            outcome=outcome.outcome,
            args=_clip(arguments, MAX_STEP_PREVIEW_CHARS),
            result=_clip(outcome.content, MAX_STEP_PREVIEW_CHARS),
            artifacts=tuple(outcome.artifacts),
        )
        messages.append(
            ChatMessage(role="assistant", content="", tool_calls=(call,), reasoning=reasoning)
        )
        messages.append(ChatMessage(role="tool", content=outcome.content, tool_call_id=call.id))
        # 从这里接着把这一轮跑完：``run`` 的第一步就是又一次模型调用（带着工具表），
        # 与"这一次调用刚刚返回"完全同形——所以"接着答"不必另写一份
        yield from self.run(messages=messages)

    # ------------------------------------------------------------------ 内部

    def _note_plan(self, text: str) -> None:
        """计划档下"它把计划说出来了"这一步（见 ``plan_gate`` 模块头）。

        三个条件缺一不可：有门闸、当前是 ``plan`` 档、**正文非空**。
        空正文不算计划——那是端点抽风，不是它给了个"什么都不做"的计划。
        """
        if self._gate is None or self._mode != modes.MODE_PLAN or not text.strip():
            return
        self._gate.note_plan(text)

    def _plan_given(self) -> bool:
        """这一轮计划阶段里，计划给了没有。

        **没有门闸按"没给"算**（fail-closed）：没有会话上下文时（脚本、外部 MCP、
        子 Agent）宁可多拦一次，也不能默认放行——放行的方向是不可逆的那一边。
        """
        if self._gate is None:
            return False
        return self._gate.plan_given

    def _blocked_by_mode(self, call: ToolCall) -> ToolOutcome | None:
        """模式闸：这一步允不允许执行；不允许就给出**要回灌给模型的那段话**。

        形状与 ``agent_exec._refused`` 完全一致（``content`` = 理由，``summary`` =
        「没有执行（…拦下）」），所以事件、快照、界面都不必为它新增分支。

        摘要里的括号**必须点明是"Agent 模式"这一道闸**：输入框旁边并排摆着
        「模式·x」与「命令·x」两个胶囊，"被拦下"那一行只写档名的话，用户会去
        改执行策略而不是模式（或反过来），而这两处的下一步完全不同。

        ``plan`` 档之外的档**不会**走到这里返回非空（``modes.allows`` 一律放行）：
        它们的差别在"要不要问一句"，那件事在 ``_execute`` 里用审批表达。
        """
        meta = meta_of(call.name)
        allowed, reason = modes.decide(
            meta,
            mode=self._mode,
            permission=self._permission,
            plan_given=self._plan_given(),
            tool=call.name,
            tool_label=tool_label(call.name),
        )
        if allowed:
            return None
        # 摘要里必须点明是**哪一道闸**拦下的：输入框那一排并排摆着「权限」与「模式」两颗，
        # 只写档名的话，用户会去改另一颗，而两处的下一步完全不同（见 `modes` 模块头
        # 第 3 条的顺序说明）。
        permission_ok, _ = modes.permission_allows(meta, self._permission, tool=call.name)
        if permission_ok:
            summary = f"没有执行（模式「{modes.label_of(self._mode)}」拦下）"
        else:
            summary = f"没有执行（权限「{modes.permission_label_of(self._permission)}」拦下）"
        return ToolOutcome(content=reason, summary=summary, outcome="blocked")

    def _expired(self, started_at: float) -> bool:
        """这一轮是否已经用满墙钟（见 `DEFAULT_MAX_SECONDS`）。"""
        return self._clock() - started_at >= self._max_seconds

    # ------------------------------------------------------------ 结局（`StopReason`）

    def _steps_stop(self) -> StopReason:
        """步数用尽的结局。"""
        return StopReason(
            kind=STOP_STEPS,
            label="未查完（工具步数用尽）",
            detail=(
                f"本轮最多 {self._max_steps} 步，到这里停下——按现有信息作答，"
                "并说清还差什么"
            ),
            note="本轮工具步数已用完",
        )

    def _time_stop(self, started_at: float) -> StopReason:
        """时间用尽的结局。措辞与步数用尽**分开**：用户看到"慢"和"多"要能区分——
        前者是这次的网络/服务慢，后者是这题要查的东西太多，下一步该做的事不一样。"""
        used = round(self._clock() - started_at)
        return StopReason(
            kind=STOP_TIME,
            label="未查完（本轮时间已用尽）",
            detail=(
                f"本轮最多 {int(self._max_seconds)} 秒，已用 {used} 秒，到这里停下——"
                "按现有信息作答，并说清还差什么"
            ),
            note="本轮时间已用尽",
        )

    def _stop_step(self, reason: StopReason) -> StepEvent:
        """把结局渲染成过程面板那一步。

        **`degraded=True` 跟着 `incomplete` 走**：这一轮没按设计走完才摆重试入口，
        而"已作答"那一档没有这一步（它不发事件）。
        """
        return StepEvent(
            phase="tool",
            label=reason.label,
            detail=reason.detail,
            degraded=reason.incomplete,
        )

    def _execute(
        self, call: ToolCall, *, stop: StopReason | None, approval: str | None = None
    ) -> ToolOutcome:
        """执行一次调用。**所有失败都变成回给模型的文本**，不往上抛。

        抛出去会让整轮失败；而工具失败（参数不对、库里没有、服务连不上）
        通常是**模型能自己纠正**的——把它当结果回给它，它下一轮换个法子。

        ``approval`` 非空时那次调用带的是**审批结论**（见 ``_perform`` 的第二遍）。

        两处顺序是有讲究的：

        1. **模式闸排在参数解析之前**：被模式拦下时，"参数是不是合法 JSON"根本不影响
           结论，而这句理由比"参数不合法"重要得多——先说重要的那句。
        2. **模式闸也排在 ``stop`` 之前**：``stop``（步数/时间用尽）那句是"没机会了，
           直接作答"，而模式拦下那句要的是"换个做法：先给计划"。两句的下一步不同，
           谁在更前面就以谁为准——模式是**这一轮能不能做**，比"还来不来得及"更根本。
        """
        blocked = self._blocked_by_mode(call)
        if blocked is not None:
            return blocked
        if stop is not None:
            # 最后一步还调工具（或时间已经用完）：不执行了，直接告诉它没机会了，
            # 省下一次真实调用（它通常只是想再确认一遍）
            return ToolOutcome(content=f"（{stop.note}，请直接给出回答）")
        try:
            args = _parse_arguments(call.arguments)
        except ValueError as exc:
            return ToolOutcome(content=f"工具参数不是合法 JSON：{exc}")
        decision = self._approval_for(call, approval)
        try:
            # ``approval`` 只在**有结论**时传（见 ``ToolRunner`` 的说明）：
            # 不管审批的执行器（子 Agent、测试里的假执行器）签名里没有它，
            # 多发一个 None 会让它们 TypeError
            outcome = (
                self._runner(call.name, args, approval=decision)
                if decision is not None
                else self._runner(call.name, args)
            )
        except KylabError as exc:
            # 这一类是**我们自己写给用户看**的话（"这条会话没有挂工作区…"、
            # "没有这个目录：x"）：原样交给模型，它据此换一步走。
            logger.info("工具 %s 被业务规则拦下：%s", call.name, sanitize_log_value(exc))
            # 也要**结构化**标成 failed：这一类是"这一步没做成"（会话没挂工作区、没有这个目录…），
            # 界面靠 `forceExpand` 把它默认摊开——只给模型一句话、界面上却像正常跑完，
            # 正是 §12.325 那个"用户会以为它做了"的坑。
            return ToolOutcome(content=f"工具执行失败：{exc}", outcome="failed")
        except Exception as exc:  # 工具是外部世界，什么都能抛
            # 其余的是**我们的 bug**（AttributeError / KeyError 之类）：日志里留全文与
            # 栈，**交给模型的只给人话**。原先这里把 `{exc}` 原样写进内容——2026-09-28
            # 走查 D33 实测到 `'Sandbox' object has no attribute 'exists'` 就这么进了上下文：
            # 用户看到天书，模型还会顺着这句废话继续猜（它读到的不是"失败"，是一段源码）。
            #
            # 异常文本里可能带着模型给的参数（多行 JSON）：日志那边仍然要转义，否则一条
            # 日志会被伪装成好几条，而多出来的那几行看起来像我们自己打的。
            logger.warning(
                "工具 %s 内部错误（全文只进日志，不进上下文）：%s",
                call.name,
                sanitize_log_value(exc),
                exc_info=True,
            )
            return ToolOutcome(
                content=(
                    "这个工具这次没能跑起来（内部错误，细节已记进日志）。"
                    "换一步再试，或者把这件事拆开做。"
                ),
                # **过程面板那一行仍然给真原因**（`step_detail()` 优先用 `summary`）。
                # 两个诉求各有落点，别把它们揉成一条：
                # - `content` 是**模型读的** → 只给人话（走查 D33：异常原文进上下文，
                #   模型会顺着那段源码继续猜，用户也在回答里看到天书）；
                # - `summary` 是**上屏幕的** → 给真原因（仓库既有契约："工具的失败要出现
                #   在过程面板里，不能悄悄吞掉"——`tests/integration/api/test_chat_api.py`
                #   那条集成用例钉的就是它，改成只给人话会把这条判据弄丢）。
                summary=f"工具内部错误：{_clip(str(exc), 120)}",
                # 结果类别（D22）：这一幕是**这一步自己出错了**，不是"跑完了"。
                # 界面据此把这一行默认展开（见前端 `TraceStepRow.forceExpand`）——
                # `summary` 给的是真原因，可它原先折在下面，用户看到的只是一行成功的工具名。
                outcome="failed",
            )
        return _truncate(outcome)

    def _approval_for(self, call: ToolCall, approval: str | None) -> str | None:
        """这一条调用带不带审批结论。

        除了"上一遍问回来的那个决定"（``_resolve_approvals``），**模式**也会给结论：
        ``edit`` / ``yolo`` 档下需要点头的调用直接带上"同意一次"，
        于是它不必停下来等人（见 ``modes.auto_approves``）。

        **一律传 ``allow_once``**，不传"以后都允许"：后者会往放行清单里写一条规则
        （见 ``agent_exec``），而"我顺手切了一档"不该悄悄改掉那张清单——
        想写规则就在确认条上点那个按钮，那是明确的一次动作。

        **免问不等于越过拒绝**：显式的拒绝规则与「沙箱执行 → 拒绝」总开关在
        ``agent_exec`` 里排在审批之前，模式改不动它们。
        """
        if approval is not None:
            return approval
        meta = meta_of(call.name)
        if meta.needs_approval and modes.auto_approves(meta, self._permission):
            logger.info("权限 %s 免去了 %s 的确认", self._permission, call.name)
            return ALLOW_ONCE
        return None

    def _execute_batch(
        self,
        calls: Sequence[ToolCall],
        *,
        stop: StopReason | None,
        approvals: Sequence[str | None] | None = None,
    ) -> list[ToolOutcome]:
        """执行**同一批**调用：能并发的并发，返回顺序与传入一致。

        为什么并发（v0.27 实测的账）：模型会在一批里同时要三页网页、两个方向的检索
        ——它自己说了这几件事互不依赖。串行执行时，一批三页网页的墙钟时间就是三页之和
        （实测抓页 0.4–1.2s/页），而它们之间**没有共享状态**，等的是同一个网络。

        **但"一批"不等于"都安全"**（v0.42 改）：原先这里把整批一律并发，
        于是"导出文档 + 写笔记 + 上传文档"这种组合也在赌它们之间没有共享状态——
        而那个赌注是隐式的，加写类工具的人根本不知道自己被并发调用。
        现在按 ``tool_meta.parallel_groups`` 分组：

        - 只读且声明可并发的（检索、抓网页、读文件……）连成一组并发跑；
        - **独占调用各成一组**，天然是屏障：它前后的两组不会跨过它并发
          （"它自己会改文件"，两边的读结果不可能互相作数）——照 DSH 的 exclusive 语义；
        - 没声明元数据的工具（外部 MCP、以后新加的）**按独占算**（fail-closed）：
          最坏是慢一点，不是坏数据。

        三件事仍然成立：**顺序在 ``run`` 里定死**（running 全发 → 执行 → done 按序发）；
        **共享状态各自加锁**（来源账本、DuckDB 连接）；**每条调用各自开 session**。

        ``approvals`` 与 ``calls`` **一一对应**（第二遍重跑待确认的那几条时才传，
        见 ``_perform``）。**等待绝不在这里发生**：这个池里的线程一旦阻塞在"等人回答"上，
        生成器就再也吐不出那条询问事件了——那条死锁的形状记在 ``agent_tools._call_mcp``。
        """
        decisions = list(approvals) if approvals is not None else [None] * len(calls)
        if stop is not None or len(calls) <= 1:
            return [
                self._execute(call, stop=stop, approval=decisions[index])
                for index, call in enumerate(calls)
            ]
        outcomes: list[ToolOutcome] = []
        for group in parallel_groups([call.name for call in calls]):
            if len(group) == 1:
                # 独占（或本来就只有一个）：不走线程池——为一条调用建池是白付开销，
                # 而且它紧接着的那一组要等它跑完（屏障语义）
                index = group[0]
                outcomes.append(self._execute(calls[index], stop=None, approval=decisions[index]))
                continue
            with ThreadPoolExecutor(max_workers=min(len(group), MAX_PARALLEL_TOOLS)) as pool:
                # `map` **保序返回**：谁先跑完不影响结果顺序，也就影响不到事件与消息的顺序
                outcomes.extend(
                    pool.map(
                        lambda index: self._execute(
                            calls[index], stop=None, approval=decisions[index]
                        ),
                        group,
                    )
                )
        return outcomes

    def _answer(
        self, messages: list[ChatMessage], *, tools: Sequence[ToolSpec] | None = None
    ) -> Iterator[object]:
        """流式跑**一步**：产出思考与正文，**返回值**是这一步的结果（``LLMReply``）。

        ``tools`` 非空时这次请求带着工具表（见 ``run`` 里那段说明）：模型想调工具
        就给得出结构化调用，而不是把标记写进正文。碎片拼装用 ``assemble_tool_calls``，
        与执行之间没有别的加工——参数照样原样交给 ``_parse_arguments``。

        **"组织回答"这一步必须发出来**：它是真实发生的动作；少了它，一轮
        "没调工具、直接回答"的消息在界面上会变成**零步骤**——而界面在零步骤时会
        退回一条兜底（历史上那套"检索 + 生成"两步），于是凭空画出一条"检索知识库"，
        用户看到的现象就是"我明明没开知识库，它为什么去检索了"（实测报过来的就是这个）。

        **什么时候发它：第一段正文到达时**。光有思考不算——每一步都会流式吐思考
        （v0.40 起），而下一步往往是"我先查一下"然后再调工具：那种步骤不是回答，
        不该在过程面板里留下一条"组织回答"。只想调工具的那一轮也不发 `DoneEvent`，
        它被 ``run`` 当工具步骤接过去继续跑。

        **流中断时在这一轮里重试一次**（P2-2，见 ``MAX_STREAM_RETRIES``）。两个前提
        缺一不可，它们是这段代码存在的全部理由：

        - 这次失败**是可重试的那一类**（``ChatError.retryable``：空闲超时、限流、
          5xx、超时、连接错误）。401、参数错误这些重试一百次也一样，当场失败；
        - **还没往界面吐过任何正文**。吐过了就不能重试——重试会把正文再写一遍
          （用户看到同一句话出现两次），而"把已经发出的字收回来"是做不到的。
          只吐了思考不算"吐过正文"：思考是过程，重试时把它丢掉重来，界面上多
          一小段思考，代价远小于整轮失败。

        重试要**发一条步骤让用户看得见**：他不知道流断了，只会以为模型卡住了；
        那条步骤也是"这一轮为什么慢了一点"的唯一痕迹（它同样进会话事件日志）。

        **正文过一个标记过滤器**（§12.228，见 ``llm.TextMarkerFilter``）：模型把工具调用
        写进正文时，那段不该出现在用户眼前——增量是边到边发的，收尾处再剥只是
        "库里干净、屏幕上脏过"。过滤器扣住的正是"还可能是标记"的那几块。
        """
        while True:
            parts: list[str] = []
            reasoning_parts: list[str] = []
            fragments: list[ToolCallDelta] = []
            started = False
            # **每趟重试都新建一个**：这一趟扣下的东西随失败一起丢掉（上面那段说明），
            # 带着上一趟的缓冲重来，拼出来的正文就多了半截标记
            marker = TextMarkerFilter()
            try:
                for delta in self._client_factory().stream_events(messages, tools):
                    if delta.tool_calls:
                        fragments.extend(delta.tool_calls)
                    if delta.reasoning:
                        reasoning_parts.append(delta.reasoning)
                    # 正文先过过滤器：`shown` 才是该发出去的那部分（可能为空——
                    # 那说明这一块还扣着，或者它就是一段标记）
                    shown = marker.feed(delta.text) if delta.text else ""
                    if shown:
                        parts.append(shown)
                        if not started:
                            yield StepEvent(phase="answer", label="组织回答", status="running")
                            started = True
                        yield DeltaEvent(text=shown)
                    if delta.reasoning:
                        yield ThinkingEvent(text=delta.reasoning)
                break
            except ChatError as exc:
                if started or not exc.retryable or self._stream_retries_left <= 0:
                    raise
                self._stream_retries_left -= 1
                # 这一趟攒下的东西**全部丢掉再重来**：正文一个字都还没发（上一条判断），
                # 而拼到一半的工具调用碎片绝不能接着拼——把两趟拼起来会得到一段
                # 谁也不认识的 JSON，模型据此执行的是个错东西
                yield StepEvent(
                    phase="tool",
                    label="流中断，正在重试",
                    detail=_clip(str(exc), 120),
                )

        # 流结束了：把还扣着的那段定下来（截断的标记在这里才认得出）
        tail = marker.flush()
        if tail:
            parts.append(tail)
            if not started:
                yield StepEvent(phase="answer", label="组织回答", status="running")
                started = True
            yield DeltaEvent(text=tail)
        calls = assemble_tool_calls(fragments) if fragments else ()
        if not calls:
            text = "".join(parts)
            if marker.dropped:
                # 这一轮剥掉过标记，**如实说一句**（见 `text_marker_step`）：
                # 用户得知道这段回答为什么短、以及「继续」能把这一轮接着做完
                yield text_marker_step(marker.names, had_tools=bool(tools))
                if not text:
                    # **剥空**的（不是"模型本来一个字都没吐"——那种要原样留空，
                    # 由上层判成失败，见 ``schedule_runner`` 里"没有产出正文"那条）：
                    # 不能交空回答——界面上是个空气泡，落库那条判断还会让整轮消失。
                    # 顶上的这句人话**也要发成增量**：收尾那条的全文与落库用的字符串
                    # 必须是同一个（Agent 关掉那条链路就是按增量拼回答的）
                    text = MARKER_ONLY_ANSWER
                    if not started:
                        yield StepEvent(phase="answer", label="组织回答", status="running")
                        started = True
                    yield DeltaEvent(text=text)
                    parts.append(text)
            yield DoneEvent(answer=text)
        return LLMReply(
            text="".join(parts),
            tool_calls=calls,
            # 思考原样带回去：下一步要把这条助手消息重新发出去，而端点
            # （DeepSeek 实测）要求带工具调用的助手消息带着 reasoning_content
            # ——流式那一步的思考只有这里留了一份（``ThinkingEvent`` 发出去就没了）
            reasoning="".join(reasoning_parts),
        )

    def _stop_reason(self, step: int, started_at: float) -> StopReason | None:
        """这一步的调用该不该真的执行（见 ``_execute`` 的 ``stop``）。

        两道闸共用"这一批不执行"这条路，但**理由要分开告诉模型**：
        它下一轮得知道是"步数没了"还是"时间没了"（两者的应对不一样）。
        回给模型的那句话在 `StopReason.note` 里，这里只负责选哪一档。
        """
        if step == self._max_steps - 1:
            return self._steps_stop()
        if self._expired(started_at):
            return self._time_stop(started_at)
        return None

    def _perform(
        self,
        messages: list[ChatMessage],
        *,
        text: str,
        reasoning: str,
        calls: Sequence[ToolCall],
        stop: StopReason | None,
    ) -> Iterator[object]:
        """把一批调用跑掉：助手消息入队 → 执行 → 结果按序回灌。

        **两种来路共用这一份**（v0.34）：选工具那一步给的调用，与收尾那一步
        流式拼出来的调用。它们的区别只在"怎么拿到调用"，之后的账（消息顺序、
        出处合并、事件顺序）必须一模一样——各写一份必然会漂。
        """
        messages.append(
            ChatMessage(
                role="assistant",
                content=text,
                tool_calls=tuple(calls),
                # **思考要跟着这条消息回去**：端点（DeepSeek 实测）在思考模式下
                # 要求带工具调用的助手消息把 reasoning_content 传回来，
                # 缺这个字段的那一轮请求直接 400——那时工具都调完了，
                # 用户只看到一句失败（见 llm.ChatMessage.reasoning）
                reasoning=reasoning,
            )
        )
        # 先把这一批的 `running` **全发出去**，再执行。
        #
        # 并发之后"一条 running 紧跟一条 done"不再成立（谁先跑完谁先回），
        # 而界面把 `done` 合进"同名的第一条 running"（见前端 `mergeStep`）——
        # 事件顺序乱了就会出现错位的步骤行。所以顺序在这里定死：
        # **running 按调用顺序发全 → 执行 → done 也按调用顺序发**。
        # 用户看到的是"这几件事同时在跑"，而不是几行闪来闪去的占位。
        for call in calls:
            # `tool` 是**原始工具名**（不是人话标签）：界面按它选图标、把同类调用并成
            # 一组。放在这里而不是让界面猜 label——label 是给人看的，会被改写
            #
            # `kind` 是**语义种类**（P2-1，照 ZCode 的四元组）：图标与配色按它选，
            # 工具名只用来显示。两处都给：`tool` 是"这是什么工具"（分组、回看对账），
            # `kind` 是"它属于哪一类"（长什么样、什么颜色）。
            yield StepEvent(
                phase="tool",
                label=tool_label(call.name),
                tool=call.name,
                kind=kind_of(call.name),
                status="running",
            )
        outcomes = self._execute_batch(calls, stop=stop)
        merged = _merge_sources(outcomes)
        if merged:
            # **一批只发一条累计的出处**，而不是每条调用各发一条。
            # 发多条时"最后发的那条"未必是最全的那条（并发下先跑完的可能先发），
            # 界面只认最后一次，于是后发的那条会把先查到的资料盖掉——
            # 正是账本当初要解决的问题（见 agent_tools.build_runner）。
            #
            # 放在"问用户"**之前**发：等待可能持续到超时，而这一批里别的调用
            # （检索、抓网页）已经跑完的东西没有理由跟着一起等。
            yield SourcesEvent(sources=merged)
        outcomes = yield from self._resolve_approvals(calls, outcomes)
        for call, outcome in zip(calls, outcomes, strict=True):
            # 顺序必须与 `tool_calls` 一致：OpenAI 兼容端点要求每条调用都有结果，
            # 而"结果与调用怎么配对"靠的是 tool_call_id，不是顺序——但保持同序
            # 仍然是对端最容易处理的那种形状（也便于人读日志）。
            messages.append(ChatMessage(role="tool", content=outcome.content, tool_call_id=call.id))
            yield StepEvent(
                phase="tool",
                label=tool_label(call.name),
                tool=call.name,
                kind=kind_of(call.name),
                detail=outcome.step_detail(),
                added=outcome.added,
                # 结果类别（D22）：界面据此知道"这一行不是成功，是拦截"，不再匹配句式
                outcome=outcome.outcome,
                # 入参与原文：界面默认只看 `detail` 那一行结论，
                # 点开才看这两个（v0.25，照 Kimi 的"可以看每个工具调用的内容"）
                args=_clip(call.arguments, MAX_STEP_PREVIEW_CHARS),
                result=_clip(outcome.content, MAX_STEP_PREVIEW_CHARS),
                artifacts=tuple(outcome.artifacts),
            )

    def _resolve_approvals(
        self, calls: Sequence[ToolCall], outcomes: list[ToolOutcome]
    ) -> Iterator[object]:
        """把这一批里**在等用户点头**的那几条问出来、等回答案，再重跑一遍。

        **"先发再等"是这段代码的全部要点**（v0.41）：``yield ApprovalEvent(...)``
        先把询问交给上层（生成器在这里让出控制权，SSE 那一层把它写进响应流），
        等**下一次被恢复**时才阻塞等人回答。反过来（先阻塞、再 yield）界面根本收不到
        那个询问，两边一起等死——那条死锁的形状记在 ``agent_tools._call_mcp`` 里，
        也正是这一档长期以来只能"拒绝并说清"的原因。

        一次只问一条：确认条对应**一个动作**，同时摆三条要用户点三次的东西，
        界面与判断都会复杂一截，而"一批里同时要跑两条命令"本来就少见。

        **拒绝时可以捎带一句给模型的话**（P2-1，照 ZCode 的"拒绝理由输入框"）：
        那句话拼进回灌文本（见 `_with_reason`），下一轮模型据此改路子——
        否则它只知道"被拒了"，多半会把同一条命令原样再试一次。

        问完之后的执行走 `_execute_batch`（几条都已拿到决定，可以照旧并发）——
        顺序仍然是"按调用顺序回灌结果"，与没有审批时一模一样。
        """
        pending: list[tuple[int, ApprovalRequest]] = []
        for index, outcome in enumerate(outcomes):
            if outcome.approval is not None:
                pending.append((index, outcome.approval))
        if not pending:
            return outcomes
        if self._approvals is None:
            # **没有人可以问**（定时任务、脚本、单测）：不登记、也不发事件，
            # 直接按"没批准"重跑一遍——回给模型的仍是那句"要先确认 + 怎么放开"。
            # 在这里等满 120 秒是白等：那条链路上没有界面。
            resolved = list(outcomes)
            for index, _ in pending:
                resolved[index] = self._execute(calls[index], stop=None, approval=UNAVAILABLE)
            return resolved

        # 先按顺序**一条条问**：每一条都是"发出询问 → 停住等回答"，
        # 所以下面这个循环会在 `wait` 里真的阻塞（生成器的线程，不是工具线程池）
        answers: dict[int, ApprovalDecision] = {}
        for index, request in pending:
            yield _approval_event(request)
            answers[index] = self._approvals.wait_decision(request.approval_id)
        resolved = list(outcomes)
        second = self._execute_batch(
            [calls[index] for index, _ in pending],
            stop=None,
            approvals=[answers[index].decision for index, _ in pending],
        )
        for (index, _), outcome in zip(pending, second, strict=True):
            resolved[index] = _with_reason(outcome, answers[index])
        return resolved


def _approval_event(request: ApprovalRequest) -> ApprovalEvent:
    """把一条待确认翻成发给界面的事件。

    **不做任何加工**：要执行什么、同意之后会写下哪条规则，都是执行器说清的
    （见 ``agent_exec._awaiting``）——界面只负责显示与回传那个 id。
    """
    return ApprovalEvent(
        approval_id=request.approval_id,
        tool=request.tool,
        label=request.label,
        args=request.args,
        detail=request.detail,
        rule=request.rule,
        timeout_seconds=request.timeout_seconds,
    )


def _with_reason(outcome: ToolOutcome, answer: ApprovalDecision) -> ToolOutcome:
    """拒绝时把用户填的那句话**拼进回灌给模型的文本**（P2-1，照 ZCode）。

    为什么在循环这一层拼、而不是在执行器里：执行器（``agent_exec._not_approved``）
    只认识"三个取值之一"这件事实，而**这句话是用户说的**，与"为什么没执行"的
    政策口径无关。放在这里，任何"要问用户的工具"都自动具备这条能力，
    而不必各自再写一遍。

    两处刻意的限定：

    - **只在拒绝档拼**。用户在"允许"上捎一句话，意思是"顺便提一句"，
      把它写成"对方拒绝了这次执行"是假的；而放行的理由对模型也没有可操作的下一步；
    - **空理由一个字节都不加**：这是"不填理由"那条路上的验收——
      与加这个输入框之前完全一样（用例钉住这一条）。

    措辞照 ZCode 的确认弹窗：先说"对方拒绝了这次执行"，再给理由——
    模型看到的第一句必须是**结论**，它才知道下一步不能再走这条。
    """
    if answer.decision != DENY or not answer.reason:
        return outcome
    return ToolOutcome(
        content=f"{outcome.content}\n（对方拒绝了这次执行，理由是：{answer.reason}）",
        sources=outcome.sources,
        artifacts=outcome.artifacts,
        summary=outcome.summary,
        added=outcome.added,
        # 类别要带过去：被拒绝**本身就是**拦截，别在这一步把它丢掉
        outcome=outcome.outcome or "blocked",
    )


def _merge_sources(outcomes: Sequence[ToolOutcome]) -> list[SourceRef]:
    """把一批调用各自的出处快照并成**一份**（按 ``chunk_id`` 去重，按编号排序）。

    每一条快照都是"那一刻的账本"（``agent_tools.build_runner`` 的 ``book``，
    它是累计的），所以并集就是"这批跑完之后账本里的全部"。为什么不直接用最后
    一条快照：并发下"最后一条"是按**调用顺序**排的，而它对应的那次执行未必是最晚
    跑完的——按调用顺序取最后一条，会把最晚跑完那次带回来的资料丢掉。

    编号（``index``）在账本里是唯一的（去重 + 顺延编号都在锁里做），
    所以排序就是"编号升序"，与渲染给模型的 [n] 一一对应。
    """
    merged: dict[str, SourceRef] = {}
    for outcome in outcomes:
        for ref in outcome.sources:
            merged.setdefault(ref.chunk_id, ref)
    return sorted(merged.values(), key=lambda ref: ref.index)


def _parse_arguments(raw: str) -> dict[str, Any]:
    """把模型给的参数解析成 dict。

    容忍三种现实里常见的偏差：空串（无参工具）、``null``、以及被包了一层的
    字符串化 JSON——其余一律当错误回给模型，让它自己改。
    """
    text = (raw or "").strip()
    if not text or text == "null":
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, str):
        # 有些模型会把参数对象再序列化一层
        try:
            inner = json.loads(parsed)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if isinstance(inner, dict):
            return inner
    raise ValueError(f"参数应当是对象，收到 {type(parsed).__name__}")


def _truncate(outcome: ToolOutcome) -> ToolOutcome:
    """超长结果截断，并**明确标注**截断（见 ``MAX_RESULT_CHARS``）。"""
    if len(outcome.content) <= MAX_RESULT_CHARS:
        return outcome
    keep = outcome.content[:MAX_RESULT_CHARS]
    return ToolOutcome(
        content=f"{keep}\n\n（结果过长已截断，以上是前 {MAX_RESULT_CHARS} 字）",
        sources=outcome.sources,
        added=outcome.added,
    )
