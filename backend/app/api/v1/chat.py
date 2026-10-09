"""对话端点：最简快速检索问答（M6 验证版）。

协议选择上有一处刻意：**流式（SSE）是默认**。
快速验证场景里"看着它一个字一个字写"远比"等十秒然后整段出现"有用，
而且出问题时能立刻看出是模型在胡扯还是检索没命中。

事件形状（``text/event-stream``，每行一个 JSON）：

```
data: {"type":"step","phase":"intent","label":"意图：查事实","detail":"…"}   # Agent 步骤
data: {"type":"sources","items":[...]}      # 依据；多轮检索会多次发出，始终是累计列表
data: {"type":"thinking","text":"…"}         # 思考增量（推理模型的 reasoning_content）
data: {"type":"delta","text":"向"}           # 逐块增量
data: {"type":"done","answer":"…"}           # 收尾（含拼接后的全文，便于前端兜底）
data: {"type":"error","message":"…"}         # 任何失败都在流内报，不吞
```

Agent 工作流（v20）默认开启，可用设置项 ``chat.agent_enabled`` 关掉退回单轮检索。

**同一批事件还顺手记一份会话事件日志**（P0-2，照 ZCode 的只追加日志设计）：
``turn/start``、``step``、``tool_call``、``thinking``、``error``、``interrupted``、
``turn/end`` 逐个进 ``session_events`` 表，词表与投影在 ``services/session_events.py``。
消息里那份 ``steps`` 快照**形状不变**（老读法不受影响），但它从此是日志的投影：
``GET /conversations/{id}/events`` 读的是原始事件，回看时算出来的过程面板与它等价。

**一轮跑在后台任务里，不跟着连接走**（P2-2 的后半，抄 ZCode 的重连锚点与
QwenPaw 的"后台 run + 环形缓冲 + reconnect 重放"）：``/chat/stream`` 一开头就把
这一轮交给 ``services/live_turns`` 的后台线程，响应体只是**它的一个订阅者**。
于是客户端断开不再取消那一轮（取消只有 ``/stop`` 一个入口），
断了之后可以 ``GET /chat/turns/{id}/live?after=<seq>`` 把没看到的事件补回来、
接着流，或者（那一轮已经跑完时）拿到一条收口的 ``done``。
事件带的 ``seq`` **就是** ``session_events`` 里的那个编号（种子取自库里当前最大的
seq，见 ``services/live_turns`` 的模块头），所以"补发"与"读日志"是同一套位置。

事件形状新增一位 ``seq``（可缺省）：不带会话的调用（脚本、MCP 的 ``history`` 那条路）
没有可补发的地方，那些响应里就不带它。前端只认自己认识的 ``type``，
多一个键不会有影响（见 ``frontend/src/api/chat.ts`` 的 ``emit``）。

## NAS 网页端退役（2026-10-05）：这一族**在两个档里都不再对外**

产品的那个网页端退役之后，本模块这一族**整条都不再是"装机形态会服务的东西"**：
它们全都钉在**会话面**上（读会话历史、写会话消息与事件日志），而会话面本身
已经从服务器档摘掉（`api/v1/router.py` 的模块头写着那一步），本机档的对话又走
**边车**的 `/turn*`（`app/sidecar.py`：同一份 `ToolLoop`、同一批服务、写本机的会话）。

落点（**形态仍是甲：只摘挂载、不删代码、不删表** ✓，把 `api_router` 那一行换回整
include 就回退了）：

- `/chat/stream`、`/chat`、`/chat/turns/{id}/live`：**没有人**——本机档的对话走边车
  的 `/turn/stream`；
- `/conversations/{id}/resume`、`.../steps/{i}/retry`、`/chat/approvals/{id}`：同上
  （边车那一侧有它自己的审批入口 `/turn/approvals/{id}`）；
- `/conversations/{id}/events`、`/chat/context-usage`：**本机档**——`local.py` 的
  `chat_reads` 薄重声明了这两条（只读本机数据）；
- `/chat/turns/record`：**没有人**——边车写回本机那一半早删了（`sidecar::_record_turn`）；
- `/chat/commands`：**本机档**——`router.py` 的 `local_router` 按原路径重挂了同一个端点
  函数（`_chat_commands_local`，函数体一个字没改）。它读的是**这台机器上的**命令与技能
  （`data/commands/` + 仓库命令 + `<data_dir>/skills/`），执行那一轮也在本机（边车），
  于是**目录与执行同源**；服务器档那一份 2026-10-05 同一天摘掉（服务器没有这些数据，
  留着就是同一个端点两个答案）。前端也跟着改走本机
  （`frontend/src/api/chat.ts` 的 `listCommands`，走 `requestLocal`）；
- `/chat/suggested-questions`：**服务器档照旧**（`_chat_survivors` 如今只剩这一条）
  ——它从**知识库**里已存的分段问题取材（`services/suggested_questions.py`），
  而知识库那一族本来就留在服务器档（`router.py` 的模块头），它一个字节都不碰会话面，
  所以**这一条的家就在服务器档**（本机档的知识库在 NAS 上，问题清单得问提供者）。

**这一族在本模块里的代码一个字没删**（可回退、也免得下次要用时从 git 里捞）——
它现在是一座"无人挂载的端点库"：读它的时候记住上表，别把"没挂"当成"没实现"。

**它的 HTTP 用例也一起摘了**：`tests/integration/api/test_chat_*.py` 那六份
（`api` / `commands` / `live` / `resume` / `step_retry` / `approvals`，共 107 条）
打的就是上表里"没有人服务"的那几条（`commands` 那一份打的是当时**服务器档还挂着**的
那一条——它 2026-10-05 才摘掉，见上表那一行），所以整份删掉了——逐条的承接者写在新加的
`tests/integration/api/test_retired_nas_web_face.py` 里（那一份同时把"哪张表上有什么"
钉成了断言，含"服务器档 404 `/chat/commands` ↔ 本机档 200"这一对）。**要把上面那一行
include 换回来（回退），就得连那六份用例一起从 git 历史里捞回来**——否则回退之后的链路
没有任何 HTTP 用例。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import check_kb_scope, require_admin, require_read, require_write
from app.api.v1.schemas import (
    ChatApprovalIn,
    ChatApprovalOut,
    ChatCommandEventOut,
    ChatRequestIn,
    ChatResponseOut,
    ChatResumeIn,
    ChatSourceOut,
    ChatStepRetryIn,
    CommandListOut,
    CommandOut,
    ContextUsageItemOut,
    ContextUsageOut,
    SessionEventListOut,
    SessionEventOut,
    SuggestedQuestionsOut,
)
from app.core.caller import Caller
from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.core.services import Services, get_services
from app.services import commands, live_turns, modes, plan_gate
from app.services import resume as resume_service
from app.services.agent import (
    ApprovalEvent,
    DeltaEvent,
    DoneEvent,
    SourcesEvent,
    StepEvent,
    StepThinking,
    ThinkingEvent,
    step_snapshot,
)
from app.services.agent_tools import build_runner, build_tool_table
from app.services.approvals import ALLOW_ALWAYS, ALLOW_ONCE
from app.services.chat import ChatTurn, SourceRef
from app.services.conversation import LastTurn
from app.services.failures import failure_text
from app.services.live_turns import LiveEmit
from app.services.llm import (
    ChatError,
    ChatMessage,
    TextMarkerFilter,
    TextToolCalls,
    split_text_tool_calls,
    tidy_text,
)
from app.services.memory import CaptureOutcome
from app.services.session_events import (
    KIND_ERROR,
    KIND_INTERRUPTED,
    KIND_STEP,
    KIND_TURN_START,
    TURN_DEGRADED,
    TURN_EMPTY,
    TURN_ERROR,
    TURN_OK,
    EventDraft,
    command_draft,
    interrupted_payload,
    mode_changed_draft,
    step_event_draft,
    thinking_draft,
    turn_end_draft,
    turn_start_draft,
)
from app.services.tool_loop import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_STEPS,
    MARKER_ONLY_ANSWER,
    MARKER_STEP_LABEL,
    # 计划卡与工具确认发的是**同一个事件形状**（照搬清单第 1 条要求复用审批通道）。
    # 借用那个私有构造器而不是复制一遍字段映射：复制一份迟早与 ApprovalEvent 的字段漂开。
    _approval_event,
    text_marker_step,
    tool_label,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 一类反代的缓冲，否则流式会被攒成一坨再发
    "X-Accel-Buffering": "no",
}

#: SSE 心跳间隔（秒）。
#:
#: **为什么要有它**（P2-2，抄 ZCode 的长连接处置）：这条流经常**几十秒没有任何字节**
#: ——等端点出第一个字（推理模型首字延迟实测可到十几秒）、跑一个抓网页的工具、
#: 派一个子 Agent，那些时间里模型那边一切正常，而我们一个事件都发不出来。
#: 问题是"长时间没有数据"这件事在中间层看起来与"连接死了"一模一样：nginx 的
#: ``proxy_read_timeout`` 默认 60 秒就会把上游读断、浏览器与部分企业代理也有各自的
#: 空闲上限。被掐掉时用户看到的是"回答写到一半没了"，而那一刻模型还在正常生成
#: ——**报错指不到真正的病因**（看起来像模型坏了）。
#: 每 15 秒一个极小的心跳：对应用语义没有任何影响（见 ``_ping``），
#: 对中间层来说这条连接一直在动。
#:
#: 15 秒是"远小于最常见的 60 秒反代超时、又不会把空闲流刷成噪声"的量级。
SSE_PING_SECONDS = 15.0


@router.post(
    "/chat/stream",
    summary="快速检索问答（流式）",
    response_class=StreamingResponse,
)
def chat_stream(
    payload: ChatRequestIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> StreamingResponse:
    # 对话会读到库内原文，所以同样受密钥的库范围约束
    check_kb_scope(services.kb, caller, payload.kb_ids)
    _require_conversation(services, payload, caller)
    _warn_on_scope_drift(services, payload)
    # **命令先于模型**（P1-2，照 QwenPaw 的"进 LLM 之前短路"）：
    # 这一段排在 `llm_config` 校验之前是刻意的——`/help` 与 `/mode` 不该因为
    # "还没配对话模型"而报错，它们根本不需要模型。
    plan = _plan_command(services, payload, caller)
    if plan is not None and plan.short_circuit:
        return StreamingResponse(
            _with_pings(_command_events(services, payload, plan)),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    model_pk = _effective_model(services, payload)
    thinking, effort = _effective_thinking(services, payload)
    # **在流开始前把模型校验掉**：坏 pk 应当是 422，而不是流内的一条 error 事件
    # （流一旦开始，状态码已经发出去了）。没配任何模型不算错，交由流内报可读文案。
    services.chat.llm_config(model_pk)
    if not payload.conversation_id:
        # **不带会话的调用**（脚本、MCP 的 history 那条路）：没有会话就没有可补发
        # 的地方，也没有可落的日志，所以照旧跟着这条连接走（断开即结束）。
        # 这也让这条链路的老行为与老用例一个字不变。
        return StreamingResponse(
            # 套一层心跳（P2-2）：流里长时间没事件时也要有字节出去，见 SSE_PING_SECONDS
            _with_pings(
                _sse_stream(_events(services, payload, model_pk, thinking, effort, caller, plan))
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    # **带会话的调用：一轮转成后台任务**（P2-2，抄 QwenPaw 的"后台 run + 环形缓冲"）。
    # 这一行之后，客户端断开只是少一个订阅者，不再是"取消那一轮"
    # （取消只有 /stop 一个入口，见 ``_turn_events`` 里那个协作式检查）。
    turn = _start_live_turn(
        services,
        payload.conversation_id,
        _events(services, payload, model_pk, thinking, effort, caller, plan),
    )
    return StreamingResponse(
        _live_stream(services, payload.conversation_id, turn, after=0),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/chat/turns/{conversation_id}/live",
    summary="接上这条会话正在跑（或刚跑完）的那一轮",
    response_class=StreamingResponse,
)
def live_turn(
    conversation_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
    after: int = Query(
        default=0,
        ge=0,
        description=(
            "已经看过的事件 seq：只补发它之后的。新连接给 0（整圈都补给它）"
        ),
    ),
) -> StreamingResponse:
    """**重连锚点**（P2-2 的后半，抄 ZCode 的 ``stream_recovery_anchor_*``）。

    为什么要有这个端点：一轮开始之后就跑在后台任务里（见 ``services/live_turns``），
    客户端断开只是少了一个订阅者。切页、断网、手机锁屏回来之后，前端拿
    "上一条收到的事件 seq"调这里，就能把没看到的那几条补回来**接着流**，
    而不是等到那一轮跑完再刷新一次整条会话。

    三种结局，前端都能收口：

    1. **还在跑**：补发 ``seq > after`` 的那些，然后挂到同一个后台任务上继续收
       （事件带 ``seq``，下次断线再拿它来补）；
    2. **已经跑完**：补发完再给一条 ``done``（带 ``recovered`` 与一句说明）——
       "这一轮已收尾"这句话必须说出来，否则前端会一直等下去；
       正文增量补不出来（那是流内的东西，不进缓冲），所以那条 done 带着
       **这一轮的完整答复**（后端拼好的全文，与 ``/chat/stream`` 收尾那条同一个形状）。
    3. **缓冲区里已经没有它**（跑完很久、或服务重启过）：给一条带说明的 done，
       并顺手带上库里最后那条回答——前端据此收口，要完整过程再去读会话消息
       （``GET /conversations/{id}`` 是持久的那条路）。

    归属判定与既有的会话端点**同一套**（成员越主 404，不暴露存在性）。
    """
    _require_visible_conversation(services, conversation_id, caller)
    turn = live_turns.shared_hub().current(conversation_id)
    return StreamingResponse(
        _live_stream(services, conversation_id, turn, after=after),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/conversations/{conversation_id}/resume",
    summary="续跑上一轮（工具循环没跑完时）",
    response_class=StreamingResponse,
)
def resume_turn(
    conversation_id: str,
    payload: ChatResumeIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> StreamingResponse:
    """**接着上一轮继续做**，而不是重发一遍（v0.32，见 ``services/resume.py``）。

    什么时候有得续：上一轮是**降级收尾**的（工具步数或整轮墙钟用尽了）。
    那时候模型还想查，只是没机会了——这里把已拿到的材料交回给它、把出处接上、
    再给一点预算，让它把话说完。用户看到的是同一个回合被补完，而不是两条回答。

    契约与「重新生成」一致：**要么能续、要么报错**（422），不静默做别的事。
    找不到可续的回答时宁可让前端把「继续」按钮藏起来，也不要在这里悄悄换个行为。

    库范围、模型档位、思考档位**取会话已存的**——续跑是接着同一轮做，
    不是新一轮提问；只有钉住的技能从界面来（它不入库，见 ``ChatResumeIn``）。
    """
    if caller.user is not None and not caller.is_admin:
        conversation = services.conversations.get_for_owner(conversation_id, caller.user.id)
    else:
        conversation = services.conversations.get(conversation_id)
    check_kb_scope(services.kb, caller, conversation.kb_ids)

    turn = services.conversations.last_turn(conversation_id)
    if turn is None:
        raise InvalidRequestError("这段对话里没有可续的回答")
    reason = resume_service.degraded_reason(turn.steps)
    if not reason:
        raise InvalidRequestError("这一轮是正常跑完的，没有可续的地方")

    # 档位取**会话已存的**，界面给了就用界面的（与 `_effective_*` 同一套优先级）
    model_pk = payload.model_pk or conversation.model_pk
    thinking = payload.thinking if payload.thinking is not None else conversation.thinking
    effort = payload.thinking_effort or conversation.thinking_effort
    # **先把那条没做完的回答删掉**：新的回答会顶替它（同一个问题不该挂两条答案，
    # 理由见 services/conversation.drop_answer）。这一步在流开始之前做——
    # 失败了要当场 4xx/5xx，而不是"流里报个错、库里还留着旧的"。
    services.conversations.drop_answer(conversation_id, answer_id=turn.answer_id)

    # 续跑**与正常提问同一处置**（P2-2）：它也跑在后台任务里，断开可以重连补发。
    # 两条路各写一套的话，"续跑时断开"会退回到 v0.41 之前那个行为——
    # 而续跑恰恰是最容易断的一种（它本来就要跑不少工具步）。
    live = _start_live_turn(
        services,
        conversation_id,
        _resume_events(
            services,
            conversation_id=conversation_id,
            # 库范围取**会话已存的**：续跑是接着同一轮做，不是新一轮提问
            kb_ids=conversation.kb_ids,
            payload=payload,
            question=turn.question,
            previous=turn,
            reason=reason,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
            caller=caller,
        ),
    )
    return StreamingResponse(
        _live_stream(services, conversation_id, live, after=0),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/steps/{step_index}/retry",
    summary="重跑这一轮里的某一步（工具级重试），再从那里接着答完",
    response_class=StreamingResponse,
)
def retry_step(
    conversation_id: str,
    message_id: str,
    step_index: int,
    payload: ChatStepRetryIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> StreamingResponse:
    """**只重跑那一步**（D24，2026-09-28 走查），再接着把这一轮答完。

    寻址用 ``(message_id, step_index)``：**步骤在库里是消息 ``steps`` 数组里的一个 dict、
    没有自己的 id**，而这两个东西天然稳定——消息 id 是 ``msg_*``（``LastTurn.answer_id``
    就拿得到），下标在数组里不动（重试是**就地替换**，见 ``_retry_steps``）。
    一个存储字段都不用加。

    三处与「续跑」（``/resume``）分开的判定，每一处都有理由：

    1. **能不能重试看那一步自己**（见 ``_retry_target``）：非工具步骤、没跑完的、
       还在等确认的、**已经成功的**、以及入参没存下来的，都 422 带一句话——
       这些情况点了按钮也没有意义，说清楚比悄悄做别的事强；
    2. **只有最后一轮那条回答能重试**：重试之后这一轮会重写一遍回答，
       而更早的轮次已经被后面的回答接过去了（"接着答"会落在错的顺序上）；
       **别人的会话 / 别人的消息一律 404**（不暴露存在性，与 ``_get_artifact`` 同一条）；
    3. **执行不绕闸**：重跑走 ``ToolLoop.retry_step``，模式闸 / 权限档 / 计划门闸 /
       审批（确认条）全在原处判定（见那个方法的说明）——历史里的一步不是后门。

    旧回答**当场被顶替**（``drop_answer``）：与续跑同一条取舍——同一句提问底下不挂
    两条回答。落库失败不影响已经付过费的回答（与 ``_resume_turn_events`` 同一处置）。
    """
    _require_visible_conversation(services, conversation_id, caller)
    conversation = services.conversations.get(conversation_id)
    check_kb_scope(services.kb, caller, conversation.kb_ids)
    turn, step = _retry_target(services, conversation_id, message_id, step_index)

    # 模型档位 / 思考档位 / 库范围**取会话已存的**：重试一步是接着同一轮做，
    # 不是新一轮提问（与续跑同一条；界面能改的只有"钉住的技能"，它不入库）
    model_pk = conversation.model_pk
    thinking = conversation.thinking
    effort = conversation.thinking_effort

    # 先把那条回答删掉：新的回答会顶替它。**在流开始之前**做——失败了要当场
    # 4xx/5xx，而不是"流里报个错、库里还留着旧的"（与 ``resume_turn`` 逐字同一条）
    services.conversations.drop_answer(conversation_id, answer_id=turn.answer_id)

    # 与提问 / 续跑同一处置（P2-2）：跑在后台任务里，断开可以按锚点重连补发——
    # 重跑一步也要跑工具（可能几十秒），不能绑在那条连接上
    live = _start_live_turn(
        services,
        conversation_id,
        _retry_events(
            services,
            caller,
            conversation_id=conversation_id,
            # 库范围取**会话已存的**（与续跑同一条：接着同一轮做，不是新一轮提问）
            kb_ids=conversation.kb_ids,
            step_index=step_index,
            step=step,
            turn=turn,
            payload=payload,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
        ),
    )
    return StreamingResponse(
        _live_stream(services, conversation_id, live, after=0),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/chat/approvals/{approval_id}",
    response_model=ChatApprovalOut,
    summary="对一条待确认的工具调用做出决定（允许一次 / 这类都允许 / 拒绝）",
)
def decide_approval(
    approval_id: str,
    payload: ChatApprovalIn,
    services: Services = Depends(get_services),
    _: Caller = Depends(require_admin),
) -> ChatApprovalOut:
    """把用户在确认条上点的那一下，交给**正在等它的那个执行器**（v0.41）。

    与 `/chat/stream` 的关系是这条协议的全部要点：那条流**还开着**、停在
    ``approvals.ApprovalRegistry.wait`` 上（见 ``services/approvals.py``），
    这一条请求只是把决定送回它手里。所以这里有两件事不能做：

    - **不能等**：这一阻塞，那一头就没人叫醒了；
    - **失效必须回话**（409）：超时之后（默认 120 秒）那一头已经按"没有回应"
      往下跑了；这时回一句"已记录"，用户就会以为命令执行了——那是最不能有的一种错觉。

    门槛取 ``require_admin``，与那个动作本身同一档（``agent_exec`` 的闸 1）：
    点这一下等于同意"在这台机器上执行代码"。成员账号根本不会收到这条询问
    （命令在执行前就被权限闸拦掉了），所以这里的门槛与它能答的东西是对齐的。

    ``reason``（P2-1）：拒绝时用户可以捎一句给模型的话。它与决定**同一次请求**
    送进去（见 ``ChatApprovalIn`` 的说明），到 ``approvals`` 那一层被收成一行、
    限长，再由 ``tool_loop._with_reason`` 拼进回灌给模型的工具结果。
    """
    if not services.approvals.decide(approval_id, payload.decision, payload.reason):
        raise ConflictError(
            "这条确认已经失效了（等太久超时，或者已经点过一次）。"
            "这一轮会按「没有批准」处理；让它重来一次，它会再问你一遍。"
        )
    return ChatApprovalOut(accepted=True, detail="已交给正在等它的那一步")


def _clean_answer(
    sink: _TurnSink, answer: str, *, had_tools: bool, dropped: TextToolCalls | None = None
) -> tuple[str, StepEvent | None]:
    """正文里的工具调用标记**在这里收口**（§12.227）：剥掉、必要时补一句人话与一条说明。

    返回 ``(要落库也要显示的那段正文, 要不要补一条说明)``——那条说明由调用方喂给
    ``sink``（**喂法各条链路不同**：流式那条要 yield 出去，非流式那条只收进 sink）。

    为什么收在协议层：**"这一轮回答的是什么"就在这一层定**（收尾那条 ``done`` 带的是
    这里的 answer，消息里存的也是它）。散到产生正文的那几处（``tool_loop._answer``、
    ``chat.answer``）各写一份的话，同一条口径就有四份，而"库里干净、屏幕上脏"
    这种偏差正是从这种分散里长出来的。

    那几种标记为什么会出现：收尾那两条路（时间 / 步数用尽）与"这条链路本来就没有
    工具"都**不带工具表**，而上下文还在催它去查时，模型只剩"把调用写进正文"一条路
    （§12.219 实测 5 例）。``had_tools`` 只影响措辞——不能对用户说错话。

    ``dropped`` 是**流式过滤器**已经扣掉的那一份（§12.228）：扣掉的字不会出现在
    ``answer`` 里，所以"要不要补说明"得看它，不能只看 ``answer`` 里还剩什么。
    """
    marker = split_text_tool_calls(answer)
    streamed = dropped or TextToolCalls(text="", names=(), found=False)
    if not (marker.found or streamed.found):
        return answer, None
    # 命中的那一份剥出来的正文；标记是**流式那层**扣掉的时候（它的输出里已经没有它们）
    # 只剩收拾一下空白——标记前面那点空白本该随它一起走，不收拾的话回答末尾会多一个换行
    # （一次性剥的那条路走的是同一个 `tidy_text`，两处口径一致）
    cleaned = marker.text if marker.found else tidy_text(answer)
    # 剥完什么都不剩时给一句人话：空回答会让**整轮**从会话里消失
    # （落库的判据是 answer 非空，见 `_events` 里那段说明），
    # 用户回头连"我问过、它没答"都看不到
    cleaned = cleaned or MARKER_ONLY_ANSWER
    names = marker.names or streamed.names
    # 已经说过就不再补（工具循环那条路会自己发一条同样的说明，见 `tool_loop.text_marker_step`）：
    # 同一个现象说两遍，用户会以为出了两次问题
    already = any(step.get("label") == MARKER_STEP_LABEL for step in sink.steps)
    return cleaned, None if already else text_marker_step(names, had_tools=had_tools)


@router.post("/chat", response_model=ChatResponseOut, summary="快速检索问答（一次性）")
def chat_once(
    payload: ChatRequestIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> ChatResponseOut:
    """非流式版本：给脚本、MCP 与自动化测试用，逻辑与流式完全相同。

    P0-2 起这条链路也记事件日志：它此前只把答案与出处落库、
    **过程一个字都不存**——于是同一句话从 `/chat` 问和从 `/chat/stream` 问，
    会话里的过程面板一个有内容一个空着。现在两条路共用 ``_TurnSink`` 那一份映射
    （事件、快照、思考都是它攒的），差别只剩"怎么把事件发出去"。
    """
    check_kb_scope(services.kb, caller, payload.kb_ids)
    _require_conversation(services, payload, caller)
    _warn_on_scope_drift(services, payload)
    # **命令同样先于模型**（P1-2）：脚本、MCP 通道也要能用 `/help` `/mode` `/stop`
    # ——"停止与审批要是一等命令"（调研报告 §2.7 抄点第 5 条）说的就是没有前端按钮
    # 的那条路也得跑通。短路类命令不消耗模型调用、不落消息，答案就是命令那段话。
    plan = _plan_command(services, payload, caller)
    if plan is not None and plan.short_circuit:
        _record_command(services, payload, plan)
        return ChatResponseOut(answer=plan.text, sources=[])
    model_pk = _effective_model(services, payload)
    thinking, effort = _effective_thinking(services, payload)
    history, summary, _ = _context(services, payload, model_pk)
    prompt_query = plan.prompt if plan is not None and plan.prompt else payload.query
    sink = _TurnSink()
    mode = services.chat.current_mode()
    _note_turn_mode(services, sink, payload.conversation_id, mode)
    sink.start_turn(query=payload.query, model_pk=model_pk, mode=mode)
    services.commands.turns.begin(payload.conversation_id)
    try:
        if _use_agent(services):
            # **与流式走同一条链路**（P0）：这个端点的文档里写着"逻辑与流式完全相同"，
            # 而工具循环已经是流式那条路的主流程——这里不跟上的话，
            # 同一句话从 `/chat` 问和从 `/chat/stream` 问会得到两种性质的回答
            loop = _agent_loop(
                services,
                caller,
                kb_ids=payload.kb_ids,
                conversation_id=payload.conversation_id,
                model_pk=model_pk,
                thinking=thinking,
                effort=effort,
            )
            answer_text = ""
            for event in loop.run(
                messages=services.chat.agent_messages(
                    query=prompt_query,
                    history=history,
                    summary=summary,
                    kb_ids=payload.kb_ids,
                    skill_names=payload.skill_names,
                    model_pk=model_pk,
                )
            ):
                if isinstance(event, DoneEvent):
                    # 收尾那条带的是后端拼好的全文，**以它为准**（避免个别增量丢失后
                    # 正文与出处对不上）——与流式那条路同一个口径
                    answer_text = event.answer
                # 事件不发出去（这里没有流），但要**收进 sink**：快照与日志都由此而来
                for _ in sink.feed(event):
                    pass
            answer = ChatTurn(answer=answer_text or sink.answer, sources=list(sink.sources))
        else:
            sources = services.chat.retrieve_sources(
                query=prompt_query,
                kb_ids=payload.kb_ids,
                top_k=payload.top_k or services.runtime.get_int("chat.top_k") or 6,
            )
            answer = services.chat.answer(
                query=prompt_query,
                sources=sources,
                history=history,
                summary=summary,
                model_pk=model_pk,
                thinking=thinking,
                thinking_effort=effort,
            )
        # 正文里的工具调用标记在这里收口（§12.227，理由见 `_clean_answer`）
        cleaned, notice = _clean_answer(
            sink, answer.answer, had_tools=_use_agent(services)
        )
        if notice is not None:
            # 这条链路没有流：事件不发出去，但**要收进 sink**（快照与日志由此而来）
            for _ in sink.feed(notice):
                pass
        answer = ChatTurn(answer=cleaned, sources=answer.sources)
    finally:
        services.commands.turns.end(payload.conversation_id)
    sink.close_turn(status=_turn_status(sink.steps), answer=answer.answer)
    _record_turn(
        services,
        payload,
        answer=answer.answer,
        sources=answer.sources,
        steps=sink.steps,
        thinking="".join(sink.thinking),
        caller=caller,
        events=sink.events,
    )
    return ChatResponseOut(answer=answer.answer, sources=_sources_out(answer.sources))


@router.get(
    "/chat/context-usage",
    response_model=ContextUsageOut,
    summary="上下文用量（按来源分解，估算）",
)
def context_usage(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
    conversation_id: str = Query(description="要算哪条会话的上下文；必填"),
) -> ContextUsageOut:
    """这一轮上下文**被什么占着**（P1-3 的仪表，照 ZCode 的 ``chat.contextUsage.breakdown``）。

    为什么值得有：用户看到"它怎么变笨了 / 怎么变慢了"，能回答的一句话是
    "上下文里 60% 是技能目录"。按来源分解比一个百分比有用得多，
    也是"装了多少技能、给了多少工具"这件事第一次变得可核对。

    三件事按顺序说清：

    1. **算的是这一轮真会发出去的那一份**：历史（摘要 + 摘要之后的消息）、
       基础提示词 + 当前模式那段 + 库级提示词、技能目录、工具表、人设文件，
       外加框架开销那一项。工具表由协议层现拼（要调用者身份与这一轮的库范围）。
    2. **只读**：调它**不会**触发压缩（``prepare_context`` 那条路才会），
       所以它可以被界面随时刷新。
    3. **是估算**：按字符数算（刻意偏高），``estimated`` 恒真、``note`` 里写着这句话
       ——真实的用量只有模型端点返回的 ``usage`` 才知道。

    归属判定与既有的会话端点同一套（成员越主 404，不暴露存在性）。
    """
    _require_visible_conversation(services, conversation_id, caller)
    conversation = services.conversations.get(conversation_id)
    usage = services.chat.context_usage(
        conversation_id=conversation_id,
        owner_id=caller.owner_id,
        # 工具表与那一轮给模型的一模一样：同一处拼装（``_agent_loop``），
        # 两处各拼一份的话，仪表会显示一套、模型拿到另一套。
        # v0.57 起交给模型的是**核心常驻那一份**（外围靠发现通道），所以这里也按它算——
        # 否则仪表会把"其实没发出去的外围工具"算进上下文。
        tools=build_tool_table(
            services, owner_id=caller.owner_id, kb_ids=list(conversation.kb_ids)
        ).resident(),
    )
    return ContextUsageOut(
        items=[
            ContextUsageItemOut(
                kind=part.kind,
                label=part.label,
                chars=part.chars,
                tokens=part.tokens,
                # 注入内容的开头一段（D09）：界面上给"本轮注入了什么"一个内容级入口
                preview=part.preview,
                share=(part.tokens / usage.used) if usage.used else 0.0,
            )
            for part in usage.parts
        ],
        used=usage.used,
        total=usage.total,
        ratio=round(usage.ratio, 4),
        compress_at=usage.compress_at,
        # **实际**阈值（比例与绝对上限取小的那个，D37）：界面按这个说"到多少会自动压"，
        # 否则窗口调大之后那句提示是个永远到不了的数（实测 1M 窗口报 70 万，
        # 而那条会话总共才 1.2 万）
        compress_budget=usage.compress_budget,
        estimated=True,
        note=(
            "按字符数估算：中日韩 1 字约 1 token、其余 4 字符约 1 token（偏高一点），"
            "不是分词器给的准确值；真实用量看每次调用返回的 usage。"
        ),
    )


@router.get(
    "/conversations/{conversation_id}/events",
    response_model=SessionEventListOut,
    summary="会话事件日志（只追加，按 seq 正序）",
)
def conversation_events(
    conversation_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
    kinds: str = Query(
        default="",
        description=(
            "逗号分隔的事件类型（turn/start、turn/end、step、tool_call、"
            "thinking、error、interrupted、mode/changed、command）；留空返回全部"
        ),
    ),
) -> SessionEventListOut:
    """这条会话的**事件日志**——"当时到底发生了什么"的原始记录（P0-2）。

    与 ``GET /conversations/{id}`` 的分工：那个端点返回消息（含 ``steps`` 快照，
    是**回看时要显示的东西**），这个返回**只追加的原始事件**（流式过程中的每一步、
    每一次工具调用、中断与失败）。两者是"投影"与"事实"的关系——快照的每一条
    都能在日志里找到出处（``services/session_events.steps_from_events``
    就是那条换算，端到端用例比对了两者相等）。

    **落在 chat.py 而不是 conversations.py**：事件的写入在对话链路里
    （``_TurnSink``），读写放一处，那一侧改动时不会漏掉另一侧。
    归属判定与既有的会话端点**同一套**（成员越主 404，不暴露存在性）。

    ``kinds`` 里出现词表之外的取值会 **422**：这个端点是给人读日志、给脚本做
    "只看中断"这类筛选用的，拼错了却拿到空列表会让人以为"这条会话没有这类事件"。
    """
    _require_visible_conversation(services, conversation_id, caller)
    wanted = [item.strip() for item in kinds.split(",") if item.strip()]
    events = services.conversations.session_events(conversation_id, kinds=wanted or None)
    return SessionEventListOut(items=[SessionEventOut.model_validate(event) for event in events])


#: 推荐问题的条数边界。**双份并存期**：权威在 KB 侧
#: （`services/suggested_questions.py` 的 `MIN_QUESTIONS` / `MAX_QUESTIONS` / `DEFAULT_LIMIT`），
#: 这里留一份拷贝是为了让这个模块不 import KB 域（剥离阶段 0 的刀 2）。
#: `/chat/suggested-questions` 按方案 §3.2 属 KB 面，**剥离收口时这条路由与这份拷贝一起删**。
SUGGESTED_MIN = 1
SUGGESTED_MAX = 5
SUGGESTED_DEFAULT_LIMIT = 6


@router.get(
    "/chat/suggested-questions",
    response_model=SuggestedQuestionsOut,
    summary="推荐问题（取自入库时为各分段生成的问题）",
)
def suggested_questions(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
    kb_ids: str = Query(default="", description="逗号分隔的知识库 id；留空返回空列表"),
    limit: int | None = Query(
        default=None,
        ge=SUGGESTED_MIN,
        le=SUGGESTED_MAX,
        description="最多返回几条；留空用默认",
    ),
) -> SuggestedQuestionsOut:
    """给对话页空状态那排胶囊喂数据。

    **不再调模型**（v23）：问题在**入库时**就为每个分段生成好了（见
    `services/suggested_questions.py`），这里只是随机抽几段、把它们的问题取回来。
    这样空状态看到的"你可以这样问"与库里真实内容一致，也不必为一个引导多花一次
    模型调用。

    **失败返回空列表而不是报错**（``generated=false``）：库里还没有问题
    （功能没开、或文档还没重新摄入）时如此，界面据此回退到静态样例——
    不该把"打开对话页"变成一次错误提示。
    """
    ids = [item.strip() for item in kb_ids.split(",") if item.strip()]
    if ids:
        check_kb_scope(services.kb, caller, ids)
    questions = services.kb.suggested_questions.list_questions(
        kb_ids=ids, limit=limit or SUGGESTED_DEFAULT_LIMIT
    )
    return SuggestedQuestionsOut(questions=questions, generated=bool(questions))


# --------------------------------------------------------------------- 内部


def _effective_model(services: Services, payload: ChatRequestIn) -> str | None:
    """这一轮实际用哪个对话模型。

    优先级：**请求里的 ``model_pk`` > 会话已存的 ``model_pk`` > 全局默认（``None``）**。

    请求带了 ``model_pk`` 且指定了会话时**回写会话**——用户在输入框换了模型，
    这条会话就该记住新选择（v12"跟随会话保存"）。回写失败只记日志：模型选择没存上
    不该让这一轮问不出来。
    """
    if payload.model_pk:
        if payload.conversation_id:
            try:
                record = services.conversations.get(payload.conversation_id)
                if record.model_pk != payload.model_pk:
                    services.conversations.set_model(payload.conversation_id, payload.model_pk)
            except Exception:
                logger.exception("会话模型回写失败：%s", payload.conversation_id)
        return payload.model_pk
    if payload.conversation_id:
        # 走到这里说明 `_require_conversation` 已经把"不存在/越主"挡掉了（404）
        return services.conversations.get(payload.conversation_id).model_pk
    return None


def _effective_thinking(
    services: Services, payload: ChatRequestIn
) -> tuple[bool | None, str | None]:
    """这一轮实际用哪档思考设置。

    与 ``_effective_model`` 同一套优先级：**请求 > 会话已存 > 全局默认（``None``）**。
    请求里带了任一项且指定了会话时回写会话，让这条会话记住用户在输入框里的选择；
    回写失败只记日志——偏好没存上不该让这一轮问不出来。

    返回 ``None`` 表示"交给设置页/模型 options 决定"，而不是"关闭"：
    两层都用 ``None`` 当"没选"，把 False 混进去会让"没选"变成"显式关闭"。
    """
    thinking = payload.thinking
    effort = payload.thinking_effort
    if thinking is None and effort is None:
        if payload.conversation_id:
            record = services.conversations.get(payload.conversation_id)
            return record.thinking, record.thinking_effort
        return None, None

    if payload.conversation_id:
        try:
            record = services.conversations.get(payload.conversation_id)
            merged_thinking = thinking if thinking is not None else record.thinking
            merged_effort = effort if effort is not None else record.thinking_effort
            if record.thinking != merged_thinking or record.thinking_effort != merged_effort:
                services.conversations.set_thinking(
                    payload.conversation_id, merged_thinking, merged_effort
                )
        except Exception:
            logger.exception("会话思考偏好回写失败：%s", payload.conversation_id)
    return thinking, effort


class _TurnSink:
    """把工具循环的事件摊成**要发出去的那一条条**（``live_turns.LiveEmit``），
    同时攒下**落库要用的快照**。

    两条链路共用它：正常提问（``_events``）与续跑（``_resume_events``）。
    抽出来的理由很实在——这套映射里有好几处"踩过才知道"的细节
    （`running` 的步骤不入快照、`degraded` 要落库、空字段不发键省带宽、
    思考要攒全文否则刷新后只剩一句"已生成回答"）。**复制一份就一定会分叉**。

    它只管攒与发，不管收尾：`done` 事件与落库由调用方在循环结束后统一做，
    这样两处的口径不可能不一致。

    **产出的是载荷而不是拼好的 SSE 串**（P2-2 起）：一条载荷要能被两处消费——
    直播那一侧要给它编上 ``seq`` 再序列化（见 ``live_turns``），
    不带会话那条路则直接发出去（``_sse_stream``）。序列化只有 ``_sse`` 一处，
    所以"带 seq 的那份"与"不带的那份"形状不可能分叉。
    里面的 ``log_index`` 说明这条载荷对应本轮第几条会话事件（1 起，``None`` = 不落库）。

    **P0-2 起它还顺手攒一份会话事件日志**：同一批事件按 kind 记进
    ``self.events``（词表见 ``services/session_events.py``），收尾时与消息
    **同一个事务**落库。快照照旧攒（老读法一个字不变），但它从此是那份日志的
    投影——"当时到底发生了什么"由日志回答，"回看时显示什么"由投影回答。

    刻意**不记**的几样，各有理由：

    - 正文增量（``delta``）：它就是答案本身，已经随消息落库，而且收尾那条
      ``done`` 带着全文——所以它也**不进直播缓冲**（最多的一种事件，进缓冲只会挤掉别人）；
    - 出处（``sources``）：不进日志（消息里另存快照），但**进缓冲**——补发时它是必要的；
    - 待确认（``approval``）：词表里没有它，而且它"还没被回答"——审批自己有
      登记表（``services/approvals.py``）。它同样进缓冲：断在一条待确认上的人
      回来必须重新看到那句询问；
    - 压缩那一步（``_events`` 里直接发的那条 step）：它是给界面看的一句提示，
      不进消息快照，记进日志会让"投影等于快照"这条验收当场不成立。
    """

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []
        self.thinking: list[str] = []
        self.deltas: list[str] = []
        self.sources: list = []
        #: 这一轮的会话事件（草稿）。``seq`` / ``id`` 由存储层在写那个事务里给。
        self.events: list[EventDraft] = []
        #: 这一轮**有没有结论**（``close_turn`` 之后为真）。
        #: 中断收尾据此判断"要不要补一条 interrupted"：已经收尾过的一轮不该被
        #: 记成"被中断"（用户读完答复才关页面，是很正常的一件事）。
        self.finished = False
        #: 当前这段连续思考的 payload。同一次思考在日志里只占一条：增量拼进来，
        #: 每来一块写一行会让长会话的日志膨胀几十倍，而那些行在投影里没有区别。
        self._thinking_draft: dict[str, object] | None = None
        #: 推理的**切段**累加器（见 ``agent.StepThinking``）：它决定"下一处步骤快照
        #: 该带上哪一段推理"。与上面的 ``thinking`` 是两个累加器，不能合并——
        #: 那份攒的是整轮的全文（整串落库，既有行为），这份每挂到一处快照上就取空。
        self._step_thinking = StepThinking()
        #: 那一段思考**在直播缓冲里的那条**（P2-2）。后续增量同时拼进它，
        #: 于是重连补发的人拿到的是完整思考，而正在看的人收到的仍是逐段增量。
        self._thinking_emit: LiveEmit | None = None
        #: **已开始、还没有结果**的调用（``running`` 有、``done`` 没等到）。
        #: 中断时它就是要补进 ``interrupted.payload.unpaired`` 的那份名单。
        self._pending_calls: list[dict[str, object]] = []

    @property
    def answer(self) -> str:
        return "".join(self.deltas)

    def start_turn(
        self,
        *,
        query: str,
        model_pk: str | None,
        resume_reason: str | None = None,
        mode: str | None = None,
    ) -> None:
        """这一轮开始（``turn/start``）。

        **在流开始之前**就记下：之后无论正常收尾、失败还是被中断，
        日志的第一条都是"这一轮要做什么"，不会出现"有步骤、不知道在答什么"。

        ``mode``（P1-1）：这一轮是哪一档开跑的。它同时是"档换过了没有"的基线
        （见 ``_note_turn_mode`` 与 ``services/commands.ModeWatch``）。
        """
        self.events.append(
            turn_start_draft(
                query=query, model_pk=model_pk, resume_reason=resume_reason, mode=mode
            )
        )

    def note_mode_change(self, *, previous: str, mode: str, source: str) -> None:
        """这一轮开始前档换过了：补一条 ``mode/changed``（P1-1 遗留 #6）。

        由 ``_note_turn_mode`` 调用（进来的顺序在 ``turn/start`` 之前），
        ``/mode`` 那条**在会话里当场切**的路不走这里——它自己写、立刻写
        （见 ``_record_mode_change``）。
        """
        self.events.append(mode_changed_draft(previous_mode=previous, mode=mode, source=source))

    def close_turn(self, *, status: str, answer: str) -> None:
        """这一轮有结论了（``turn/end``）。

        终止原因是**枚举**（``ok`` / ``degraded`` / ``error`` / ``empty``），
        不是"有没有异常"（调研报告 §2.1 第 2 条：异常分不清"没预算"与"崩了"）。
        """
        self.events.append(
            turn_end_draft(status=status, answer_chars=len(answer), steps=len(self.steps))
        )
        self.finished = True

    def mark_interrupted(self, *, reason: str) -> None:
        """用户中途停止 / 断开：补一条 ``interrupted``（QwenPaw 那条教训）。

        补的不只是"被中断了"这句话，还有**哪些调用没有结果**
        （``unpaired``）与用户**已经看到**的那段正文——否则下一轮不知道
        哪些动作是半截的，回看时也说不清当时屏幕上写到了哪。

        补完就当作"这一轮有结论了"（``finished``）：同一轮不会再被记第二次。
        """
        self.events.append(
            EventDraft(
                kind=KIND_INTERRUPTED,
                payload=interrupted_payload(
                    reason=reason, unpaired=self._pending_calls, answer=self.answer
                ),
            )
        )
        self.finished = True

    def note_error(self, message: str) -> None:
        """这一轮在流里失败了：``error`` 那一条（``turn/end`` 由调用方收）。"""
        self.events.append(EventDraft(kind=KIND_ERROR, payload={"message": message}))

    def _track_call(self, event: StepEvent) -> None:
        """跟踪"已开始、还没有结果"的调用（中断时那份名单，见 ``mark_interrupted``）。

        配对按**工具名**先进先出：``tool_loop._perform`` 的发法是"running 按调用顺序
        发全 → 执行 → done 也按调用顺序发"（见那里的说明），所以同名调用会按顺序
        一一对上。配不上对的那条 running，就是这一轮里"结果没等到"的调用。
        """
        if not event.tool:
            return
        if event.status == "running":
            self._pending_calls.append({"tool": event.tool, "label": event.label})
            return
        for index, pending in enumerate(self._pending_calls):
            if pending["tool"] == event.tool:
                del self._pending_calls[index]
                return

    def feed(self, event: object) -> Iterator[LiveEmit]:
        # 换了一种事件就意味着这段思考结束了：下一条思考增量的到来会开新的一段
        if not isinstance(event, ThinkingEvent):
            self._thinking_draft = None
            self._thinking_emit = None

        if isinstance(event, StepEvent):
            # 快照的收法在服务层（``agent.step_snapshot``）：定时任务那条链路
            # 也要落同一份，两处各写一份必然分叉（见那个函数的说明）。
            # 这个累加器把"产生这一步的那一轮推理"挂到快照上（同一处判定）
            snapshot = step_snapshot(event, thinking=self._step_thinking)
            if snapshot is not None:
                self.steps.append(snapshot)
            # 同一条事件**同时**进日志：写侧的映射也只有一处（``step_event_draft``
            # 复用 step_snapshot），所以"日志投影 == 快照"不是靠约定而是靠同一份代码。
            # **已经算好的那份原样传进去**：再算一遍会第二次取走推理增量，
            # 于是消息里那份带上思考、日志里那份没有（或反过来）
            self.events.append(step_event_draft(event, snapshot))
            self._track_call(event)
            # ``log_index`` = 它刚写进日志的那条是第几条（1 起）：
            # 后台那一条据此算出与 ``session_events`` **同一个** seq（见 live_turns）
            yield LiveEmit(
                {
                    "type": "step",
                    "phase": event.phase,
                    "label": event.label,
                    "detail": event.detail,
                    "status": event.status,
                    # 工具名（v0.26）：界面按它选图标、把同类调用并成一组。
                    # 非工具步骤没有，所以空就不发这个键
                    **({"tool": event.tool} if event.tool else {}),
                    # 语义种类（P2-1）：**图标与配色的依据是它，不是工具名**——
                    # 加一个工具时界面一个字都不用改（照 ZCode 的四元组）。
                    # 老的步骤（P2-1 之前落库的快照）没有这个键，界面按旧的
                    # 工具名映射兜底（见 useChatTurns 的 stepIcon）
                    **({"kind": event.kind} if event.kind else {}),
                    # 两个都是"可选补充"，只在有意义时发（v25）：
                    # degraded 让界面给续跑/重试入口，added 让界面说清这轮找了几条新资料
                    **({"degraded": True} if event.degraded else {}),
                    **({"added": event.added} if event.added is not None else {}),
                    # 结果类别（D22）：界面据此知道"这一行不是成功，是拦截"，
                    # 不再去匹配句式。空串不发（与上面同一个理由：别给每条步骤白扛一个空字段）
                    **({"outcome": event.outcome} if event.outcome else {}),
                    # 入参与原文（v0.25）：界面默认不展开，点开才看。
                    # 空串就**不发这个键**——每一条步骤都带两个空字段，
                    # 一个二十步的长会话会白扛几十 KB
                    **({"args": event.args} if event.args else {}),
                    **({"result": event.result} if event.result else {}),
                    # 这一步的推理（v0.54）：**从已经算好的那份快照里取**，不再算一遍
                    # （再算会第二次取走增量，于是消息里有、直播里没有）。
                    #
                    # 为什么要发给直播而不是"等回看时再说"：这一批要解决的正是
                    # "正在跑的那一轮里，思考全挤成一团"。只给快照的话，用户答完
                    # 看到的过程面板仍然没有每步推理，得刷新页面才出现——
                    # 而"刚答完那一眼"正是他要看的那一刻。
                    #
                    # 一段只发一次（挂在产生它的那一步上），不是每个增量都发：
                    # 逐帧带的话，一次长思考会让这条流白扛几十 KB。
                    **(
                        {"thinking": snapshot["thinking"]}
                        if snapshot and snapshot.get("thinking")
                        else {}
                    ),
                    **(
                        {"artifacts": [dict(a) for a in event.artifacts]} if event.artifacts else {}
                    ),
                },
                log_index=len(self.events),
            )
        elif isinstance(event, SourcesEvent):
            self.sources = event.sources
            # 出处不带 ``log_index``：它不落库（消息里另存一份快照）。
            # 但它**要进缓冲**——重连的人指着那条确认在哪几段上，
            # 补发时把它一并带上（同一个 payload 对象，界面那侧是累计语义，重复无害）
            yield LiveEmit(
                {
                    "type": "sources",
                    "items": [item.model_dump() for item in _sources_out(self.sources)],
                }
            )
        elif isinstance(event, ApprovalEvent):
            # **问用户**（v0.41）：这一条发出去之后，循环那边就停在 `wait` 上了
            # （见 tool_loop._resolve_approvals），所以它必须**原样、立刻**发出去——
            # 攒着不发等于让两边一起等死。
            #
            # 进缓冲是 P2-2 补的：断在一个待确认上的人回来时必须重新看到这条询问，
            # 否则那一头等满超时、这一头永远不知道发生过什么。
            #
            # 不进 `self.steps`：过程快照是"这一轮做过什么"，而这是一句还没被回答的问题。
            # 落进库的话，回看历史时会冒出一条永远等不到人点的确认。
            yield LiveEmit(
                {
                    "type": "approval",
                    "approval_id": event.approval_id,
                    "tool": event.tool,
                    "label": event.label,
                    "args": event.args,
                    "detail": event.detail,
                    "rule": event.rule,
                    "timeout_seconds": event.timeout_seconds,
                }
            )
        elif isinstance(event, ThinkingEvent):
            # 顺手攒一份全文：落库时要把它存下来，否则用户离开这一页再回来
            # 就只剩一句"已生成回答"（v0.25）
            self.thinking.append(event.text)
            # 同一段推理也喂给切段那个累加器：它攒到下一处快照被取走为止
            # （"这段推理属于哪一步"因此有了位置，见 ``agent.StepThinking``）
            self._step_thinking.note(event.text)
            # 日志里同一次连续思考只占**一条**：增量拼进同一个 payload。
            # 每来一块写一行的话，一条长思考就是几百行，而那些行在投影里
            # 完全一样（思考不进 steps）——只增长度，不增信息。
            if self._thinking_draft is None:
                draft = thinking_draft()
                self.events.append(draft)
                self._thinking_draft = draft.payload
                payload: dict[str, object] = {"type": "thinking", "text": event.text}
                emit = LiveEmit(payload, log_index=len(self.events))
                # 缓冲里那条留着：后面的增量**拼进它**（``live_turns`` 的
                # ``publish`` 存的就是这个 dict），于是重连补发时拿到的是完整思考，
                # 而当前正在看的人收到的仍是逐段的增量
                self._thinking_emit = emit
                yield emit
                return
            self._thinking_draft["text"] = f"{self._thinking_draft['text']}{event.text}"
            if self._thinking_emit is not None:
                # 第一条那条**也**要跟着长：它已经在缓冲里了（引用同一个 dict）
                text = self._thinking_emit.payload.get("text")
                self._thinking_emit.payload["text"] = f"{text}{event.text}"
            # 后续增量只发给正在看的人（``keep=False``）：每条都进缓冲的话，
            # 一段长思考就能把整圈挤掉（见 ``live_turns.LiveEmit``）
            yield LiveEmit({"type": "thinking", "text": event.text}, keep=False)
        elif isinstance(event, DeltaEvent):
            self.deltas.append(event.text)
            # 正文增量**不进缓冲**：它的内容由收尾那条 ``done`` 带着全文兜底，
            # 而它是最多的一种事件（一段 2000 字的回答就是两千条）
            yield LiveEmit({"type": "delta", "text": event.text}, keep=False)
        # DoneEvent 不在这里发：收尾统一放在循环外，保证 done 里的全文
        # 与落库用的 answer 是同一个字符串


def _source_from_snapshot(item: dict[str, object]) -> SourceRef:
    """把落库的出处快照（``ChatSourceOut`` 的 dict）还原成 ``SourceRef``。

    **按字段名过滤**而不是直接 ``SourceRef(**item)``：快照里可能带着
    ``SourceRef`` 没有的键（前端模型加的展示字段），直接展开会在某天多一个字段时
    炸在续跑这条路上——而那是一条"偶尔才走一次"的路，炸了很难被发现。
    """
    allowed = {field.name for field in dataclasses.fields(SourceRef)}
    return SourceRef(**{key: value for key, value in item.items() if key in allowed})


def _resume_marker(reason: str) -> dict[str, object]:
    """续跑时那条「继续上一轮」的标记步骤。

    它是**消息快照与事件日志共用的同一份 dict**：消息那边的 ``_resume_steps``
    与日志那边的 ``step`` 事件都从它来。两处各写一份的话，"投影等于快照"
    只会在续跑这条路上悄悄不成立——而那条路难得走一次。
    """
    return {
        "phase": "tool",
        "label": "继续上一轮",
        "detail": f"上一次停下来是因为：{reason}",
        "status": "done",
    }


def _resume_steps(
    previous: Sequence[dict[str, object]], fresh: Sequence[dict[str, object]], *, reason: str
) -> list[dict[str, object]]:
    """续跑这一轮的过程快照：**上一轮那些 + 一条"继续"标记 + 这一轮新的**。

    为什么不只留新的：过程面板是"这一轮是怎么来的"。只看新的那半段，
    用户会看到"它一步都没查就回答了"——而事实是查过了，只是在上半场。
    那条标记把两半接上，也顺手回答了"为什么会有两段"。
    """
    return [*previous, _resume_marker(reason), *fresh]


def _agent_loop(
    services: Services,
    caller: Caller,
    *,
    kb_ids: Sequence[str] | None,
    conversation_id: str | None,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
    seed_sources: Sequence[SourceRef] = (),
    max_steps: int | None = None,
    max_seconds: float | None = None,
    planning: bool = False,
):
    """建这一轮的工具循环（工具表 + 执行器 + 预算）。

    **提问与续跑共用它**：两处都要"内置工具 + 技能 + 外部 MCP，执行器带调用者身份
    与这一轮的库范围"，复制一份就一定会分叉（续跑那条路少接一个工具，
    表现是"续跑之后它忽然不会用某个工具了"，极难排查）。

    ``seed_sources`` 只有续跑用：把上一轮已经拿到的出处接进来源账本
    （见 ``services/resume.py`` 模块头——编号必须与交给模型的说明一致）。

    ``planning=True``：**研究流程的开场那一轮**（出澄清问题 / 出计划）——
    工具表是**空的**，模型只能说话、不能动手（照搬清单第 1 条：正式检索前先出计划）。
    执行器照旧传着（空工具表下它不会被调用），这样这一条链路与正常那条只差一个工具表。

    **工具表走"核心常驻 + 外围可发现"**（v0.57，用户裁定，见
    `docs/归档/调研/Agent-暴露机制-对标与落点-v0.1.md`）：交给模型的是 ``table.resident()``
    （核心 + ``find_tools`` / ``use_tool`` 两个入口），外围工具靠发现通道取。
    执行器必须拿到**同一张表**——`use_tool` 要知道"这一轮能调什么、已经发现过什么"，
    所以这里建一次、两处共用。
    """
    table = build_tool_table(
        services,
        owner_id=caller.owner_id if caller is not None else None,
        kb_ids=kb_ids,
    )
    return services.chat.tool_loop(
        model_pk=model_pk,
        thinking=thinking,
        thinking_effort=effort,
        # 会话 id：只用来取那条会话的**计划门闸**（P1-1 的 `plan` 档要记
        # "本会话给没给过计划"，见 services/plan_gate.py）。模式档本身从运行期配置读，
        # 不从这里传——那条路上只有一个读点，见 services/chat.tool_loop 的说明
        conversation_id=conversation_id,
        # **工具表含外部 MCP 服务的工具**（v0.20）：用户在能力页接进来的
        # 服务，它们的工具与内置工具一起交给模型；能不能真的调起来由
        # 执行器那一刻的准入策略决定（见 agent_tools._call_mcp）。
        # 开场那一轮给空表：这一轮只准说、不准动手。
        tools=[]
        if planning
        else table.resident(),
        # **这一轮有界面可以问**（v0.41）：`ask` 档的工具调用（目前是 run_command）
        # 挂进这张登记表，由循环发一条 approval 事件、停在那里等人回答；
        # 用户的决定从 `POST /chat/approvals/{id}` 交回来（见 services/approvals.py）。
        # 定时任务那条链路不传它——那里没有人回答，等满超时是白等。
        approvals=services.approvals,
        # 执行器带**调用者身份**与**这一轮允许查的库**：
        # 关掉知识库开关之后，模型也不该能绕过它去检索（见 agent_tools.build_runner）
        runner=build_runner(
            services,
            caller,
            kb_ids=kb_ids,
            # 产物（导出类工具）落在哪：见 services/artifacts.py
            conversation_id=conversation_id,
            # 子 Agent（P1 补上）：它自己解析这一轮的模型档位，
            # 执行器只管"给问题、拿结论与出处"
            subagent=lambda task: services.chat.run_subagent_text(
                question=task,
                kb_ids=kb_ids,
                model_pk=model_pk,
                thinking=thinking,
                thinking_effort=effort,
            ),
            seed_sources=seed_sources,
            # **同一张暴露表**：`find_tools` / `use_tool` 靠它知道自己能调什么
            exposure=table,
        ),
        max_steps=max_steps,
        max_seconds=max_seconds,
    )


def _events(
    services: Services,
    payload: ChatRequestIn,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
    caller: Caller,
    plan: _CommandResult | None = None,
) -> Iterator[LiveEmit]:
    """流式问答的**收尾**：建 sink、记 ``turn/start``、把断开也记进日志（P0-2）。

    这一层刻意薄：它只做"这一轮从哪开始、到哪结束"。正文那一段在
    ``_turn_events`` 里，两条链路（正常提问 / 续跑）共用同一份收尾，
    于是不可能出现"一条链路记日志、另一条不记"。

    ``plan``（P1-2）：输入是**改写类**命令时，它的 ``prompt`` 就是这一轮的提示
    （``/skill`` 与自定义 md 命令）。原始那一行（``/xxx args``）只作为"用户敲了什么"
    留在消息与日志里。

    ``GeneratorExit`` 是这里唯一必须拦的东西：**不带会话**那条路（脚本、MCP）
    仍然跟着连接走，Starlette 在客户端断开时会 close 掉这个生成器。
    不接住的话，库里就只剩半截（QwenPaw 那条教训：中断不补齐，
    下一轮与回看都说不清"当时停在哪一步"）。
    带会话那条路由 ``live_turns`` 在后台跑完，正常走到生成器末尾——
    **断开不再触发这里**，那正是 P2-2 要的。
    """
    sink = _TurnSink()
    # **这一轮的档**（P1-1 遗留 #6）：在 turn/start 之前先看它换过没有
    mode = services.chat.current_mode()
    _note_turn_mode(services, sink, payload.conversation_id, mode)
    sink.start_turn(query=payload.query, model_pk=model_pk, mode=mode)
    # 这一轮的停止登记（P1-2 的 /stop）：从这一刻起"这条会话上有一轮在跑"
    services.commands.turns.begin(payload.conversation_id)
    try:
        yield from _turn_events(
            services,
            payload,
            sink=sink,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
            caller=caller,
            plan=plan,
        )
    except GeneratorExit:
        _record_interruption(services, payload.conversation_id, sink)
        raise
    finally:
        services.commands.turns.end(payload.conversation_id)


def _turn_events(
    services: Services,
    payload: ChatRequestIn,
    *,
    sink: _TurnSink,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
    caller: Caller,
    plan: _CommandResult | None = None,
) -> Iterator[LiveEmit]:
    """把一次问答摊成一串事件（``LiveEmit``：载荷 + 它在会话日志里的位置）。

    任何异常都在**流内**报出去（``type=error``）而不是靠 HTTP 状态码：
    流一旦开始发送，状态码已经发出去了，改不了——这也是最容易漏的一处。

    两条链路：Agent 工作流（默认，见 ``services/agent.py``）与单轮检索（``chat.agent_enabled``
    关掉时）。两条都会把 ``sources`` 与 ``delta`` 用同一套事件形状发出去，
    前端不必关心走的是哪条。

    ``plan``（P1-2）：非空且带 ``prompt`` 时，这一轮问模型的**是那条命令渲染出来的正文**
    （``/skill`` 的流程、自定义命令的模板），而 ``payload.query``（用户敲的那一行）
    照旧作为"他问了什么"落进消息与日志——回看时看得出他用了哪条命令。
    """
    chat = services.chat
    # 过程快照与正文都攒在 sink 里：**只活在内存里、结束时一次性写**——
    # 边流边写会让每一拍都多一次 UPDATE，而回看要的是最终那一份。
    # （P0-2 起同样的取舍套用到事件日志上：攒在 sink 里，与消息同一个事务写。
    # 日志**只追加**这一点不受影响——它不更新任何东西，只是写得更晚一点。）
    collected: list[str] = []
    step_log: list[dict[str, object]] = []
    thinking_parts: list[str] = []
    sources: list = []
    # 正文里的工具调用标记过滤器（§12.228）：**非 Agent 那条链路**用它扣下还没确定
    # 是不是标记的那几块（Agent 那条链路在工具循环里自己有一份，见 ``tool_loop._answer``）。
    # 收尾时它把自己丢掉了什么报出来，用来补那条说明
    marker_stream = TextMarkerFilter()
    # 交给模型的这一轮的提示（改写类命令就是命令渲染出来的正文，见上面）
    prompt_query = plan.prompt if plan is not None and plan.prompt else payload.query
    # 上下文（含压缩）对两条链路都适用：Agent 关掉时同样需要"摘要 + 最近原文"
    history, summary, compressed = _context(services, payload, model_pk)
    if compressed:
        # 不进日志（它是给界面看的一句提示，见 ``_TurnSink`` 的说明），
        # 但**进缓冲**：重连补发的人也该看到"这一轮中间压过一次上下文"
        yield LiveEmit(
            {
                "type": "step",
                "phase": "compress",
                "label": "压缩上下文",
                "detail": "较早的对话已折成摘要，之后的问答仍记得它们",
                "status": "done",
            }
        )

    if _use_agent(services):
        # **工具循环是主流程**（P0）：模型拿到十几个工具（内置 + 技能 + 外部 MCP 服务），
        # 自己决定查什么、做什么；知识库检索是其中一个工具（`search`），
        # 不再是每轮必经的阶段——资料由它取回，而不是我们预先塞进提示词。
        #
        # 事件怎么摊成 SSE、快照怎么攒，全在 `_TurnSink` 里（与续跑共用一份：
        # 那段映射里有好几处踩过才知道的细节，复制一份就一定会分叉）。
        try:
            # ---- 研究型任务的状态机（照搬清单第 1 条：澄清 → 出计划 → 等确认 → 执行）----
            #
            # 判据是**可计算**的（``plan_gate.decide_research``：缺哪几样 / 预计几步 vs
            # 预算一半的阈值），状态是**显式**的。这一段只做三件事：落状态、跑一次
            # **没有工具**的开场调用（只出澄清问题或只出计划）、在计划卡上等确认。
            #
            # 只有"用户正常提问"这一条路进来；续跑与重试那条路不判
            # （它们不是新题目，见 ``_resume_events`` / ``_retry_events``）。
            gate = plan_gate.gate_for(payload.conversation_id)
            decision = plan_gate.decide_research(prompt_query)
            extra_note = ""
            stopped = False
            if gate.stage == plan_gate.STAGE_AWAITING:
                # 计划卡还挂着（超时没点、或用户去改需求了）：**这一轮的文字就是那个决定**。
                # 短句同意 → 接着执行；别的一律当"改了需求/换了话题"，拿新文本重新判。
                if plan_gate.is_affirmative(prompt_query) and gate.confirm_by_text():
                    extra_note = plan_gate.confirmed_note(gate.research_plan())
                else:
                    gate.clear_research()
                    decision = plan_gate.decide_research(prompt_query)
            done_after_opening = False
            if decision.needs_opening:
                gate.open_research(decision)
                yield from sink.feed(plan_gate.opening_step(decision))
                # **没有工具**的那一次调用（``planning=True``）：这一轮只准说、不准动手
                opening = _agent_loop(
                    services,
                    caller,
                    kb_ids=payload.kb_ids,
                    conversation_id=payload.conversation_id,
                    model_pk=model_pk,
                    thinking=thinking,
                    effort=effort,
                    planning=True,
                )
                opening_messages = chat.agent_messages(
                    query=prompt_query,
                    history=history,
                    summary=summary,
                    kb_ids=payload.kb_ids,
                    skill_names=payload.skill_names,
                    model_pk=model_pk,
                    owner_id=_memory_owner(caller),
                )
                opening_messages.append(
                    ChatMessage(role="user", content=plan_gate.opening_note(decision))
                )
                opening_run = opening.run(messages=opening_messages)
                try:
                    for event in opening_run:
                        yield from sink.feed(event)
                        if services.commands.turns.stop_requested(payload.conversation_id):
                            break
                finally:
                    opening_run.close()
                # 开场那一轮的正文就是"澄清问题"或"计划"（此前一个 delta 都没有）
                opening_text = "".join(sink.deltas).strip()
                if decision.stage == plan_gate.STAGE_CLARIFY:
                    # 澄清问出去就收尾：用户补充之后下一轮拿新题目重新判
                    done_after_opening = True
                else:
                    request = services.approvals.open(
                        tool="research_plan",
                        label="研究计划",
                        # 卡片上那一行就是计划正文（压成一行）
                        args=plan_gate.plan_summary(opening_text),
                        detail=f"{decision.reason}；确认后开始检索与产出",
                        timeout=plan_gate.PLAN_CONFIRM_TIMEOUT_SECONDS,
                    )
                    if not gate.await_confirm(opening_text, request.approval_id):
                        # 一个字的计划都没有（端点抽风）：不挂一张空卡片，如实收尾
                        logger.warning(
                            "研究计划为空，未挂确认卡片：conversation=%s",
                            payload.conversation_id,
                        )
                        done_after_opening = True
                    else:
                        # 复用审批通道与前端已有卡片：**不另造一套审批**
                        yield from sink.feed(_approval_event(request))
                        verdict = services.approvals.wait_decision(request.approval_id)
                        if verdict.decision in (ALLOW_ONCE, ALLOW_ALWAYS):
                            gate.confirm_plan(request.approval_id)
                            yield from sink.feed(plan_gate.confirmed_step())
                            extra_note = plan_gate.confirmed_note(gate.research_plan())
                        else:
                            # 没确认：计划留在状态里（下一轮说"确认"接得上），执行权不给
                            gate.reject_plan(verdict.reason, request.approval_id)
                            done_after_opening = True
                            yield from sink.feed(
                                StepEvent(
                                    phase="plan",
                                    label="计划待确认",
                                    detail="没收到确认：下一轮说「确认」就开始，或直接说改哪里",
                                    status="done",
                                )
                            )
            if not done_after_opening:
                loop = _agent_loop(
                    services,
                    caller,
                    kb_ids=payload.kb_ids,
                    conversation_id=payload.conversation_id,
                    model_pk=model_pk,
                    thinking=thinking,
                    effort=effort,
                )
                turn_messages = chat.agent_messages(
                    query=prompt_query,
                    history=history,
                    summary=summary,
                    kb_ids=payload.kb_ids,
                    skill_names=payload.skill_names,
                    model_pk=model_pk,
                    owner_id=_memory_owner(caller),
                )
                if extra_note:
                    # 已确认的计划挂在 user 那一侧（见 ``plan_gate.confirmed_note`` 的说明）
                    turn_messages.append(ChatMessage(role="user", content=extra_note))
                run = loop.run(messages=turn_messages)
                try:
                    for event in run:
                        yield from sink.feed(event)
                        # **/stop 的落点**（P1-2）：在两次事件之间看一眼有没有人叫停
                        # （见 services/commands.TurnControl：停止是协作式的，
                        # 从外面掐线程会让这一轮没有任何收尾）
                        if services.commands.turns.stop_requested(payload.conversation_id):
                            stopped = True
                            break
                finally:
                    # 收掉那一头的生成器：它可能还停在 `yield` 上（那些还没跑完的
                    # 工具调用会跑完当前这一步，这正是"停在哪一步"要记的东西）
                    run.close()
            if stopped:
                # **如实收尾**：已经流出来的正文留着（它仍然有用），过程日志补一条
                # interrupted（含"哪些调用没有结果"），与用户点停止那条路同一处置
                sink.mark_interrupted(reason="用户用 /stop 停止")
                _flush_events(services, payload.conversation_id, sink)
                # ``log_index`` 取收尾那一刻的日志条数：它**是游标**——
                # "到这儿为止（含这一轮的最后一条）都已经看过了"（见 ``_last_event_seq``
                # 同一套口径），重连时按它补发不会漏也不会重
                yield LiveEmit(
                    {"type": "done", "answer": sink.answer},
                    log_index=len(sink.events),
                    terminal=True,
                )
                return
        except ChatError as exc:
            yield _fail(services, payload.conversation_id, sink, failure_text(exc), cause=exc)
            return
        except Exception as exc:
            # 原始异常由 `_fail` 记进日志（cause）：用户看到的只有那一句人话
            yield _fail(services, payload.conversation_id, sink, failure_text(exc), cause=exc)
            return
        sources = sink.sources
        step_log = sink.steps
        thinking_parts = sink.thinking
        collected = sink.deltas
    else:
        try:
            sources = chat.retrieve_sources(
                query=prompt_query,
                kb_ids=payload.kb_ids,
                top_k=payload.top_k,
            )
        except Exception as exc:
            # 检索这一环出的错（向量化、库连接…）同样只把结论给用户
            yield _fail(services, payload.conversation_id, sink, failure_text(exc), cause=exc)
            return
        # 用 pydantic 序列化而不是 ``s.__dict__``：
        # SourceRef 是 slots=True 的 dataclass，**没有 __dict__**，
        # 取它会在流式刚发第一个事件时就抛 AttributeError、把连接截断（踩过）。
        yield LiveEmit(
            {
                "type": "sources",
                "items": [item.model_dump() for item in _sources_out(sources)],
            }
        )
        try:
            for delta in chat.answer_stream(
                query=prompt_query,
                sources=sources,
                history=history,
                summary=summary,
                model_pk=model_pk,
                thinking=thinking,
                thinking_effort=effort,
                owner_id=_memory_owner(caller),
            ):
                # 正文先过标记过滤器（§12.228）：这段标记**不该发出去**——增量是
                # 边到边发的，发出去就收不回来了（见 ``llm.TextMarkerFilter``）。
                # 扣着的那部分在流结束后由 `flush()` 定下来
                shown = marker_stream.feed(delta)
                if not shown:
                    continue
                collected.append(shown)
                # 与工具循环那条路同一处置：正文增量不进缓冲（收尾那条 done 带全文）
                yield LiveEmit({"type": "delta", "text": shown}, keep=False)
        except ChatError as exc:
            yield _fail(services, payload.conversation_id, sink, failure_text(exc), cause=exc)
            return
        except Exception as exc:
            # 原始异常由 `_fail` 记进日志（cause）：用户看到的只有那一句人话
            yield _fail(services, payload.conversation_id, sink, failure_text(exc), cause=exc)
            return
        tail = marker_stream.flush()
        if tail:
            collected.append(tail)
            yield LiveEmit({"type": "delta", "text": tail}, keep=False)

    answer = "".join(collected)
    # 正文里的工具调用标记在这里收口（§12.227，理由见 `_clean_answer`）：
    # 两条链路都要过——工具循环在收尾那两条路上不带工具表、而"Agent 关掉"那条链路
    # 根本没有工具表，模型在两种情形下都只剩"把调用写进正文"一条路。
    # `marker_stream` 是上面那条链路自己扣下的（Agent 那条链路在工具循环里扣，
    # 由它自己发说明步骤——所以这里按标签去重，不重复说）
    answer, marker_notice = _clean_answer(
        sink, answer, had_tools=_use_agent(services), dropped=marker_stream.removed
    )
    if marker_notice is not None:
        yield from sink.feed(marker_notice)
        # 落库与状态**都按 sink 里那份**：`sink.feed` 刚把那条说明收进 steps 与日志
        # （非 Agent 那条链路的 `step_log` 到此为止还是空的，见上面它没有工具循环）
        step_log = sink.steps
    # 只在**回答确实产出了**之后落库：失败的那一轮不留下半截记录，
    # 否则回看时会出现"问了但没答"的空档，而用户无从判断当时发生了什么。
    # 这条判断必须真的写出来——v0.12 之前只有注释、没有 if，于是流"正常结束但一个字都没吐"
    # 时照样落了一条空回答（实测：推理模型的思考吃光预算时就是这样）。
    if answer:
        # 信号捕获那一步必须在 `close_turn` **之前**补：`turn/end` 里的步数读的是
        # `sink.steps`，之后再补就数不上它——而它正是这一轮的最后一步。
        _implicit_capture_step(
            services, payload.query, caller=caller, steps=step_log, sink=sink
        )
        # 这一轮有结论了：先记 ``turn/end``（终止原因是枚举，不是"有没有异常"），
        # 再连消息一起写——消息与事件同一个事务，见
        # `ConversationService.record_turn`。
        sink.close_turn(status=_turn_status(step_log), answer=answer)
        _record_turn(
            services,
            payload,
            answer=answer,
            sources=sources,
            steps=step_log,
            thinking="".join(thinking_parts),
            caller=caller,
            events=sink.events,
        )
    else:
        # 一个字都没吐（推理模型的思考吃光预算时就是这样）：不落消息，
        # 但**日志要留**——"为什么这一轮没有回答"正是回看时要问的。
        logger.warning("对话流没有产出任何正文，本轮不落库：query=%r", payload.query[:80])
        sink.close_turn(status=TURN_EMPTY, answer="")
        _flush_events(services, payload.conversation_id, sink)
    # 收尾那条：``log_index`` 是**游标**（这一轮最后一条日志事件的编号），
    # ``terminal`` 让后台那一条记住它——重连收口时原样再发一遍
    yield LiveEmit(
        {"type": "done", "answer": answer}, log_index=len(sink.events), terminal=True
    )


def _resume_events(
    services: Services,
    *,
    conversation_id: str,
    payload: ChatResumeIn,
    kb_ids: Sequence[str],
    question: str,
    previous: LastTurn,
    reason: str,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
    caller: Caller,
) -> Iterator[LiveEmit]:
    """把一次续跑摊成一串事件（形状与 ``_events`` 完全一致，前端不必区分）。

    与正常提问的三处差别，每一处都有理由：

    1. **不落库提问**：问题上一轮就在库里了，这次只是接着做（落第二遍会出现
       同一条提问挂两次）；
    2. **上下文里去掉这一轮的提问**：`prepare_context` 从库里读历史，而那条提问
       已经在库里了，再把它当 `query` 传一遍就会重复一次（模型尤其容易被
       重复的同一句问话带偏）；
    3. **预算抬高 + 出处接上**：见 ``services/resume.py``。

    事件日志这边与正常提问同一套收尾（建 sink → ``turn/start`` → 断开时补
    ``interrupted``），只是 ``turn/start`` 多带 ``resume_reason``：
    "这一轮为什么接着做"在日志里必须看得出来，否则回看时会以为用户又问了一遍。
    """
    sink = _TurnSink()
    # 与正常提问同一处置：先看档换过没有（P1-1 遗留 #6），再开这一轮
    mode = services.chat.current_mode()
    _note_turn_mode(services, sink, conversation_id, mode)
    sink.start_turn(query=question, model_pk=model_pk, resume_reason=reason, mode=mode)
    # **那条「继续上一轮」的标记也进日志**：消息里的 steps 是"上一轮 + 标记 +
    # 这一轮"（见 ``_resume_steps``），日志若只记新的一半，投影就对不上了
    # ——而"投影等于快照"正是这条链路要守的东西。
    sink.events.append(EventDraft(kind=KIND_STEP, payload=_resume_marker(reason)))
    services.commands.turns.begin(conversation_id)
    try:
        yield from _resume_turn_events(
            services,
            conversation_id=conversation_id,
            payload=payload,
            kb_ids=kb_ids,
            question=question,
            previous=previous,
            reason=reason,
            sink=sink,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
            caller=caller,
        )
    except GeneratorExit:
        # 与 ``_events`` 同一处置：续跑跑到一半被停止 / 断开的，同样要补齐
        _record_interruption(services, conversation_id, sink)
        raise
    finally:
        services.commands.turns.end(conversation_id)


def _resume_turn_events(
    services: Services,
    *,
    conversation_id: str,
    payload: ChatResumeIn,
    kb_ids: Sequence[str],
    question: str,
    previous: LastTurn,
    reason: str,
    sink: _TurnSink,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
    caller: Caller,
) -> Iterator[LiveEmit]:
    """续跑的主体（与 ``_events`` 的收尾分开，理由同 ``_turn_events``）。"""
    chat = services.chat
    # 上一轮的出处还原成对象：**既要接进账本，也要先当作这一轮已有的出处**——
    # 续跑一次都没检索（材料够了直接收尾）时，答案里的 [n] 仍然要有对应的出处记录
    seeds = [_source_from_snapshot(item) for item in previous.sources]
    sink.sources = list(seeds)

    # 历史里去掉这一轮的提问（它在库里，由 `question` 显式带进来）
    context_payload = ChatRequestIn(query=question, conversation_id=conversation_id)
    history, summary, _ = _context(services, context_payload, model_pk)
    if history and history[-1].role == "user" and history[-1].content == question:
        history = history[:-1]

    note = resume_service.resume_note(
        resume_service.ResumeMaterial(
            question=question,
            answer=previous.answer,
            steps=previous.steps,
            sources=previous.sources,
        ),
        reason=reason,
    )

    try:
        loop = _agent_loop(
            services,
            caller,
            kb_ids=kb_ids,
            conversation_id=conversation_id,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
            seed_sources=seeds,
            max_steps=DEFAULT_MAX_STEPS + resume_service.RESUME_EXTRA_STEPS,
            max_seconds=DEFAULT_MAX_SECONDS + resume_service.RESUME_EXTRA_SECONDS,
        )
        for event in loop.run(
            messages=chat.agent_messages(
                # 提问 + 交接说明合成一个用户消息（而不是发两条相邻的 user：
                # 有些端点对连续同角色消息的处理方式不一致，而这里没有任何理由冒那个险）
                query=f"{question}\n\n{note}",
                history=history,
                summary=summary,
                kb_ids=kb_ids,
                skill_names=payload.skill_names,
                model_pk=model_pk,
                owner_id=_memory_owner(caller),
            )
        ):
            yield from sink.feed(event)
    except ChatError as exc:
        yield _fail(services, conversation_id, sink, failure_text(exc), cause=exc)
        return
    except Exception as exc:
        yield _fail(services, conversation_id, sink, failure_text(exc), cause=exc)
        return

    answer = sink.answer
    if answer:
        # 终止原因与落库**同时**发生：``close_turn`` 先记 ``turn/end``，
        # ``append_answer`` 再把消息与这批事件写进同一个事务（P0-2）
        sink.close_turn(status=_turn_status(sink.steps), answer=answer)
        try:
            services.conversations.append_answer(
                conversation_id,
                answer=answer,
                sources=[item.model_dump() for item in _sources_out(sink.sources)],
                steps=_resume_steps(previous.steps, sink.steps, reason=reason),
                thinking="".join(sink.thinking),
                events=sink.events,
            )
        except Exception:
            # 与 `_record_turn` 同一条取舍：落库失败不该让用户丢掉**已经付过费**的回答
            logger.exception("续跑落库失败：%s", conversation_id)
    else:
        logger.warning("续跑没有产出正文：conversation=%s", conversation_id)
        sink.close_turn(status=TURN_EMPTY, answer="")
        _flush_events(services, conversation_id, sink)
    # 与 ``_turn_events`` 的收尾同一条：``log_index`` 是游标，``terminal`` 供重连收口
    yield LiveEmit(
        {"type": "done", "answer": answer}, log_index=len(sink.events), terminal=True
    )


#: 重试一步时**允许**的结果类别（D24）。只有"这一步没做成"的那两类：
#: ``failed``（工具自己失败 / 业务规则拦下）与 ``blocked``（模式闸、权限档、
#: 拒绝规则拦下——用户改完设置/放开权限之后重试正是这条路的用处）。
#: ``""``（已经成功）与 ``"awaiting"``（还在等用户点头）不在这里，理由见 `_retry_target`。
_RETRYABLE_OUTCOMES = frozenset({"failed", "blocked"})


def _retry_target(
    services: Services, conversation_id: str, message_id: str, step_index: int
) -> tuple[LastTurn, dict[str, object]]:
    """要重试的那一步（连同它所属的那一轮）。**不合规就当场报错，什么都不动。**

    判定的顺序是"从外往里"：先确认这条消息是**这条会话里最后一轮的那条回答**
    （更早的轮次已经被后面的回答接过去了，重试会落在错的顺序上），再确认下标指向
    一步**真的能重跑**的动作。每一句报错都要能照着做下一步——这是走查 D24 的起点
    （"被拒绝之后只能靠模型自己换做法"），报一句"不能重试"而不说为什么，等于没修。
    """
    messages = services.conversations.messages(conversation_id)
    answer = next((item for item in messages if item.id == message_id), None)
    if answer is None or answer.role != "assistant":
        # **404 而不是 403/422**：不暴露"这条消息在别处存在"（与 `_get_artifact` 同一条）
        raise NotFoundError(f"消息不存在：{message_id}")
    turn = services.conversations.last_turn(conversation_id)
    if turn is None or turn.answer_id != message_id:
        raise InvalidRequestError(
            "只能重试最后一轮里的那一步：这一轮之后又答过了，重试它会把顺序弄乱"
        )

    steps = list(turn.steps)
    if step_index < 0 or step_index >= len(steps):
        raise InvalidRequestError(
            f"这一步不存在：第 {step_index} 步（这一轮一共 {len(steps)} 步）"
        )
    step = dict(steps[step_index])
    tool = str(step.get("tool") or "")
    if not tool:
        raise InvalidRequestError("这一步不是工具调用，没有可重跑的动作")
    if step.get("status") != "done":
        raise InvalidRequestError("这一步还没跑完，没有结果可以替换")
    outcome = str(step.get("outcome") or "")
    if outcome == "awaiting":
        raise InvalidRequestError("这一步还在等你的确认：先回答那条确认，再谈重试")
    if outcome not in _RETRYABLE_OUTCOMES:
        raise InvalidRequestError("这一步是成功的，没有要重试的东西")
    # 入参是重跑的唯一依据：没有它就只能拿一副空的参数去执行**另一个**调用，
    # 那比拒绝更糟（用户以为重试的就是原来那一步）
    if not str(step.get("args") or "").strip():
        raise InvalidRequestError("这一步的入参没有存下来，没法按原样重跑")
    return turn, step


def _retry_events(
    services: Services,
    caller: Caller,
    *,
    conversation_id: str,
    kb_ids: Sequence[str],
    step_index: int,
    step: dict[str, object],
    turn: LastTurn,
    payload: ChatStepRetryIn,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
) -> Iterator[LiveEmit]:
    """把一次"重跑一步 + 接着答完"摊成一串事件（与 ``_resume_events`` 同构）。

    与续跑同一个外层结构（建 sink → ``turn/start`` → 断开补 ``interrupted``），
    原因也同一条：它同样跑在后台、同样可能被断开/被停止。
    """
    sink = _TurnSink()
    mode = services.chat.current_mode()
    _note_turn_mode(services, sink, conversation_id, mode)
    # 这一轮为什么存在，日志里要看得出来（与续跑的 `resume_reason` 同一个字段）：
    # 否则回看时会以为是用户又问了一遍
    reason = f"重试第 {step_index + 1} 步（{tool_label(str(step.get('tool') or ''))}）"
    sink.start_turn(query=turn.question, model_pk=model_pk, resume_reason=reason, mode=mode)
    services.commands.turns.begin(conversation_id)
    try:
        yield from _retry_turn_events(
            services,
            caller,
            conversation_id=conversation_id,
            kb_ids=kb_ids,
            step_index=step_index,
            step=step,
            turn=turn,
            payload=payload,
            sink=sink,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
        )
    except GeneratorExit:
        # 与 ``_events`` / ``_resume_events`` 同一处置：跑到一半被停止/断开的要补齐
        _record_interruption(services, conversation_id, sink)
        raise
    finally:
        services.commands.turns.end(conversation_id)


def _retry_turn_events(
    services: Services,
    caller: Caller,
    *,
    conversation_id: str,
    kb_ids: Sequence[str],
    step_index: int,
    step: dict[str, object],
    turn: LastTurn,
    payload: ChatStepRetryIn,
    sink: _TurnSink,
    model_pk: str | None,
    thinking: bool | None,
    effort: str | None,
) -> Iterator[LiveEmit]:
    """重试的主体：重跑那一步 → 把结果接回上下文 → 接着把这一轮答完 → 落库。

    与 ``_resume_turn_events`` 逐条对齐的地方（**刻意不各写一份**）：出处接进账本、
    历史里去掉这一轮的提问、交接说明与提问合成一条 user 消息、以及收尾那一段
    （``close_turn`` → ``append_answer`` → 一样带 ``done`` 游标）。
    """
    chat = services.chat
    # 上一轮的出处还原成对象：既要接进来源账本（编号接着往下排），也要先当作
    # 这一轮已有的出处——续答一次都没检索时，答案里的 [n] 仍然要有对应记录
    seeds = [_source_from_snapshot(item) for item in turn.sources]
    sink.sources = list(seeds)

    # 历史里去掉这一轮的提问（它在库里，由 `question` 显式带进来），与续跑同一条：
    # 把同一句问话传两遍，模型容易被重复的提问带偏
    context_payload = ChatRequestIn(query=turn.question, conversation_id=conversation_id)
    history, summary, _ = _context(services, context_payload, model_pk)
    if history and history[-1].role == "user" and history[-1].content == turn.question:
        history = history[:-1]

    note = resume_service.retry_note(
        resume_service.ResumeMaterial(
            question=turn.question,
            answer=turn.answer,
            steps=turn.steps,
            sources=turn.sources,
        ),
        step_label=tool_label(str(step.get("tool") or "")),
        tool=str(step.get("tool") or ""),
        had_answer=bool(turn.answer.strip()),
    )

    try:
        # 预算走**默认那一档**（不像续跑那样抬高）：重试是"把一条调用重做一遍、
        # 然后把话说完"，本来就不该再开一整个长回合的额度。要接着查更多，
        # 那是模型自己的判断（它照常可以调工具，只是额度是常规的一份）
        loop = _agent_loop(
            services,
            caller,
            kb_ids=kb_ids,
            conversation_id=conversation_id,
            model_pk=model_pk,
            thinking=thinking,
            effort=effort,
            seed_sources=seeds,
        )
        for event in loop.retry_step(
            messages=chat.agent_messages(
                # 提问 + 交接说明合成一个用户消息（同续跑：有些端点对连续同角色
                # 消息的处理方式不一致，这里没有理由冒那个险）
                query=f"{turn.question}\n\n{note}",
                history=history,
                summary=summary,
                kb_ids=kb_ids,
                skill_names=payload.skill_names,
                model_pk=model_pk,
                owner_id=_memory_owner(caller),
            ),
            tool=str(step.get("tool") or ""),
            # 入参**原样**（存下来的那个字符串）：端点进来时已经确认它非空，
            # 而"是不是合法 JSON"交给循环里那条既有的判定（不合法就走它那条失败路）
            arguments=str(step.get("args") or ""),
            # 这一步当初的推理：端点要求带工具调用的助手消息把 reasoning 传回来
            reasoning=str(step.get("thinking") or ""),
        ):
            yield from sink.feed(event)
    except ChatError as exc:
        yield _fail(services, conversation_id, sink, failure_text(exc), cause=exc)
        return
    except Exception as exc:
        yield _fail(services, conversation_id, sink, failure_text(exc), cause=exc)
        return

    answer = sink.answer
    if answer:
        sink.close_turn(status=_turn_status(sink.steps), answer=answer)
        try:
            services.conversations.append_answer(
                conversation_id,
                answer=answer,
                sources=[item.model_dump() for item in _sources_out(sink.sources)],
                steps=_retry_steps(turn.steps, step_index, sink.steps),
                thinking="".join(sink.thinking),
                events=sink.events,
            )
        except Exception:
            # 与 `_record_turn` / `_resume_turn_events` 同一条取舍：
            # 落库失败不该让用户丢掉**已经付过费**的回答
            logger.exception("重试一步落库失败：%s", conversation_id)
    else:
        logger.warning("重试一步没有产出正文：conversation=%s", conversation_id)
        sink.close_turn(status=TURN_EMPTY, answer="")
        _flush_events(services, conversation_id, sink)
    yield LiveEmit(
        {"type": "done", "answer": answer}, log_index=len(sink.events), terminal=True
    )


def _retry_steps(
    previous: Sequence[dict[str, object]],
    step_index: int,
    fresh: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """重试之后的步骤快照：**上一轮那些，第 N 步就地换成刚重跑出来的那份**，再接上这一轮新的。

    为什么是"就地替换"而不是"把新的接在末尾"：这一步在原顺序里的位置是它的一部分。
    用户点的是"重试这一步"（同一个动作重做一遍），所以过程面板该看到的仍然是
    "第 3 步：读文件 → 成功"，而不是"第 3 步失败了，末尾又补了一次读文件"。
    顺序与轮次绑定：`sink.steps[0]` 就是刚重跑那一步（``ToolLoop.retry_step`` 先发它），
    其余是"接着答"这一段新长出来的。

    重跑**没有新的推理**：产生这次调用的那一段推理是当初那一次的事，所以原来那份
    ``thinking`` 要接回去——不接，界面上一展开这一步，它当初"为什么这么调"就没了。
    """
    steps = [dict(item) for item in previous]
    if not fresh:
        # 一步都没记下来（理论上不会：重跑那一步的 done 一定进快照）——
        # 那就原样返回，宁可不替换，也不把上一轮的过程抹掉
        return steps
    retried = dict(fresh[0])
    if 0 <= step_index < len(steps):
        carried = steps[step_index].get("thinking")
        if carried and "thinking" not in retried:
            retried["thinking"] = carried
        steps[step_index] = retried
        return [*steps, *(dict(item) for item in fresh[1:])]
    # 下标越界在端点那一层已经拦掉了（`_retry_target`）；真到这儿就接在后面，
    # 别把新跑出来的那一步吞掉
    return [*steps, *(dict(item) for item in fresh)]


def _use_agent(services: Services) -> bool:
    """Agent 工作流是否启用（设置项 ``chat.agent_enabled``，默认开）。

    布尔解析统一走 ``RuntimeConfigService.get_bool``：此前这里是手写的一份，
    记忆层的开关会是第二份，而两处判断迟早分叉（一处把空值当关、另一处当开）。
    """
    return services.runtime.get_bool("chat.agent_enabled", default=True)


def _sse(payload: dict, *, seq: int | None = None) -> str:
    """一条 SSE 事件（``seq`` 非空时带上它，见 ``live_turns``）。

    ``seq`` 是**会话事件日志里的编号**：前端拿"最后收到的那个 seq"当重连锚点，
    见 ``GET /chat/turns/{id}/live``。不带会话的调用没有可补发的地方，也就没有它。
    """
    body = payload if seq is None else {**payload, "seq": seq}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _sse_stream(emits: Iterator[LiveEmit]) -> Iterator[str]:
    """把一串载荷序列化出去（**不编号**那条路：不带会话的调用）。

    ``finally`` 里显式关掉内层生成器：``_events`` 靠 ``GeneratorExit`` 那条路
    补 ``interrupted``（P0-2 的验收之一），而"外层被 close、内层等 GC"是靠不住的
    ——CPython 之外没有谁保证它当场发生。
    """
    try:
        for emit in emits:
            yield _sse(emit.payload)
    finally:
        close = getattr(emits, "close", None)
        if callable(close):
            close()


def _last_event_seq(services: Services, conversation_id: str) -> int:
    """这条会话在事件日志里的**最大 seq**（0 = 一条都还没有）。

    它是直播缓冲那一套编号的种子（见 ``live_turns`` 模块头"为什么 seq 要种子"）。
    **每次开一轮现读一次**：``/mode`` 与各种斜杠命令会在会话里插事件，
    拿进程内上一次的水位当种子迟早会算错号；而这条读按会话走，开一轮一次，
    与那一轮的检索 + 模型调用相比可以忽略。

    读不出来（会话刚被删、库抖了一下）时返回 0：那会让缓冲的编号从头数，
    结果只是"补发时多补几条"，而**不会**漏——这个取舍是刻意的。
    """
    try:
        events = services.conversations.session_events(conversation_id)
    except Exception:
        logger.warning("读会话最大事件编号失败，缓冲从 0 编号：%s", conversation_id, exc_info=True)
        return 0
    return events[-1].seq if events else 0


def _start_live_turn(
    services: Services, conversation_id: str, source: Iterator[LiveEmit]
) -> live_turns.LiveTurn:
    """把一轮交给后台任务（P2-2），返回那个 ``LiveTurn``。

    提问与续跑共用这一处，四件事的顺序不能换：

    1. **登记"这条会话上有一轮在跑"**（``TurnControl.begin``）：在起线程**之前**做，
       否则用户在那几毫秒里发 ``/stop`` 会被回一句"没有在跑的一轮"——
       而 P2-2 之后 ``/stop`` 是唯一的取消入口，它答错一次就等于那一轮没法停。
       （``_events`` 自己还会再 begin 一次；那次会清掉刚设上的停止标记，
       窗口是从这里到线程真正开跑之间那一瞬。）
    2. **读编号种子**：这一轮的事件 seq 从库里当前的最大 seq 往下数（见 ``live_turns``）。
    3. ``hub.begin`` 登记进直播表（重连那条路靠它找到这一轮）。
    4. ``hub.run`` 起后台线程——**从这一刻起，客户端断开不再取消它**。
    """
    services.commands.turns.begin(conversation_id)
    hub = live_turns.shared_hub()
    turn = hub.begin(conversation_id, base_seq=_last_event_seq(services, conversation_id))
    hub.run(turn, source)
    return turn


def _live_stream(
    services: Services,
    conversation_id: str,
    turn: live_turns.LiveTurn | None,
    *,
    after: int,
) -> Iterator[str]:
    """重连那条流：**补发 + 接着流**，或者**补发 + 收口**（P2-2 的后半）。

    这个生成器就是"一个订阅者"的全部：它被 close 掉（客户端断开）时只做一件事
    ——退订（``finally``）。那一轮在另一个线程里照跑，取消只有 ``/stop`` 一条路。

    顺序上有一处不能换：**先 ``subscribe`` 再 ``replay``**。反过来时，
    "读快照"与"登记订阅"之间发出来的事件两边都不在，直接丢了；先订阅的代价是
    那一段窗口里的事件会来两次，由 ``LiveEvent.index`` 对 ``cutoff`` 去重
    （见 ``live_turns.LiveTurn.replay`` 的返回值）。

    收口那条（``done``）刻意带上 ``recovered`` 与一句说明：前端据此知道
    "这一轮已经跑完了，别再等增量"，同时 ``done`` 里仍是**完整答复**，
    所以它连刷新会话都不必（见 ``_finished_payload``）。
    """
    if turn is None:
        # 缓冲里没有它：跑完很久（超了 LIVE_FINISHED_KEEP_SECONDS）、服务重启过、
        # 或者这条会话根本没跑过。不能说"没有这回事"——前端要的是一句能收口的话。
        yield _sse(
            _finished_payload(
                services,
                conversation_id,
                note=(
                    "这条会话当前没有在跑的一轮（可能已经收尾，或者服务重启过）。"
                    "上面这份是它最后一条回答；要看完整过程请刷新会话。"
                ),
            )
        )
        return

    queue = turn.subscribe()
    terminal_sent = False
    try:
        replayed, cutoff = turn.replay(after)
        for event in replayed:
            payload = event.payload
            if event.terminal:
                # **补发到的那条收尾**也要带上"这一轮已收尾"那句话：重连的人
                # 与一路看着的人处境不同——他可能只看到半截，得知道不必再等
                terminal_sent = True
                payload = _noted_finish(payload)
            yield _sse(payload, seq=event.seq)
        if turn.finished:
            # 已经跑完：补发完就给一条收口的 done（它已经含全文，不必再翻库）
            if not terminal_sent:
                yield _sse(_finished_payload(services, conversation_id, turn=turn))
            return
        while True:
            try:
                item = queue.get(timeout=SSE_PING_SECONDS)
            except Empty:
                yield _ping()
                continue
            if live_turns.is_end(item):
                # 那一轮结束了但收尾那条没送到（只可能是队列被挤过）：
                # 该收的口一定要收到，否则前端一直转圈
                if not terminal_sent:
                    yield _sse(_finished_payload(services, conversation_id, turn=turn))
                return
            event = item
            if event.index <= cutoff:
                # 补发时已经发过（订阅与快照之间那一小段窗口）
                continue
            yield _sse(event.payload, seq=event.seq)
            if event.terminal:
                return
    finally:
        turn.unsubscribe(queue)


def _noted_finish(payload: dict, note: str = "") -> dict:
    """给收尾那条补一句"这一轮已收尾"（补发场景才加；直播那条不加）。

    **为什么非要这一句**：重连的人与一路看着的人处境不同——他手里可能是半截状态，
    而"到此为止、不会再有东西来了"只有服务端知道。少了它，前端会一直等下去
    （ZCode 的重连锚点那套同样是"补发 + 一个明确的收尾"）。

    ``recovered`` 是个显式标记：前端据此知道这份收尾是**补发**来的
    （正文增量不会重放，所以界面上的正文可能不完整——完整那份就在 ``answer`` 里）。
    """
    noted = dict(payload)
    noted["recovered"] = True
    noted["detail"] = note or (
        "这一轮已经收尾了：补发到此为止（正文增量不重发，这里给的是完整答复）。"
    )
    return noted


def _finished_payload(
    services: Services,
    conversation_id: str,
    *,
    turn: live_turns.LiveTurn | None = None,
    note: str = "",
) -> dict:
    """收口那条 ``done``：说清"这一轮已收尾"，并尽量带上**完整答复**。

    抄的是 ZCode 的"断流之后按锚点续"里那个收尾语义：续不上的人最需要知道的
    是"到此为止、这就是全部"，而不是一个空响应。

    来源按可靠度依次退让：这一轮自己的收尾载荷（``done`` 带全文）→
    ``done`` 之外的（``error``）原样带上 → 库里最后一条回答（服务重启过时只能这样）。
    """
    terminal = turn.terminal if turn is not None else None
    if terminal and terminal.get("type") in ("done", "error"):
        payload = dict(terminal)
    else:
        payload = {"type": "done", "answer": _last_answer(services, conversation_id)}
    return _noted_finish(payload, note)


def _last_answer(services: Services, conversation_id: str) -> str:
    """库里最后那条回答的正文（读不出来就空串）。

    只给"缓冲里已经没有那一轮"的情形兜底：那时候连它结束在哪都不知道，
    而会话消息是**持久**的那条路（``/conversations/{id}``）——能顺手带上来，
    就不必让前端为了收口再多跑一趟。
    """
    try:
        turn = services.conversations.last_turn(conversation_id)
    except Exception:
        logger.info("读最后一条回答失败：%s", conversation_id, exc_info=True)
        return ""
    return turn.answer if turn is not None else ""


def _ping() -> str:
    """一条心跳（P2-2，见 ``SSE_PING_SECONDS``）。

    **它不带 ``data:`` 行**，与别的 SSE 事件不同，这是刻意的：前端把每一条
    ``data:`` 都当成一条消息去认 ``type``，而它只认识 done / error / step / …
    那几种——多出一个陌生的 type 会落到"其余都当错误"那条分支上
    （``frontend/src/api/chat.ts`` 的 ``emit``）。心跳是**给连接看的，不是给界面看的**，
    所以它连 data 都没有；按 SSE 规范，data 为空的事件不会派发给 EventSource，
    浏览器那边同样什么都不会发生。
    """
    return "event: ping\n\n"


def _with_pings(events: Iterator[str]) -> Iterator[str]:
    """把一条事件流包成"空闲就发心跳"的那条流（P2-2）。

    **现在只有不带会话那条路用它**（``_sse_stream``）：带会话的调用走
    ``live_turns``——那一轮自己在后台线程里跑，心跳由订阅者那侧的空闲超时发出来
    （见 ``_live_stream``）。两处的判据是同一个（``SSE_PING_SECONDS``），
    但驱动方式不同：这里要另起一个线程去推它，那里本来就是"等一条、发一条"。

    **为什么必须另起一个线程**：这条流是同步生成器，"模型说一句我们发一句"，
    它一次 ``next()`` 可能阻塞几十秒（等首字、跑一个抓网页的工具、派子 Agent）。
    在同一根线里等就没法在等待期间发出任何字节——而那正是会被中间层掐掉的事
    （见 ``SSE_PING_SECONDS``）。所以内层生成器在**它自己的线程**里跑，
    本生成器只做一件简单的事：队列里有事件就发，超时没动静就发一个心跳。

    两处细节各有原因，改的时候别省：

    - **断开时（本生成器被 close）由那个线程去关内层生成器**：GeneratorExit 会沿着
      ``_events`` 的 ``except GeneratorExit`` 把 ``interrupted`` 补进会话事件日志
      （P0-2 那条验收）。从本线程直接 ``close()`` 不行——生成器可能正在那个线程里
      执行，那会抛 "generator already executing"。所以本线程只置一个停止标记，
      由那边**在两次 next 之间**自己收工（正在跑的那一步会跑完，那正是"停在哪一步"
      要记的东西；不会等到整轮结束——``stop`` 在每次取到事件后都要检查一次）。
    - **内层抛出的异常原样带回本线程再抛**：它的类型是上层分支的依据
      （``_turn_events`` 的 ``except ChatError``），在别的线程里抛会被解释成
      "这一层自己崩了"，用户拿到的文案也就成了 500 那套。
    """
    queue: Queue[tuple[str, object]] = Queue()
    stop = threading.Event()

    def pump() -> None:
        try:
            for item in events:
                if stop.is_set():
                    break
                queue.put(("event", item))
        except BaseException as exc:
            # 连 BaseException 一起接：这里的职责只是**原样搬运**，判断留给消费侧
            # （``_turn_events`` 的 except 分支）；漏掉一类异常会让流莫名其妙地断死
            queue.put(("error", exc))
        finally:
            try:
                events.close()
            except Exception:  # 收尾失败不该盖过已经发生的那件事（成功或失败）
                logger.exception("关闭对话流失败")
            queue.put(("end", None))

    worker = threading.Thread(target=pump, name="chat-sse-heartbeat", daemon=True)
    worker.start()
    try:
        while True:
            try:
                kind, payload = queue.get(timeout=SSE_PING_SECONDS)
            except Empty:
                yield _ping()
                continue
            if kind == "event":
                yield payload  # type: ignore[misc]
            elif kind == "end":
                return
            else:
                raise payload  # type: ignore[misc]
    finally:
        stop.set()


def _context(
    services: Services, payload: ChatRequestIn, model_pk: str | None
) -> tuple[list[ChatMessage], str, bool]:
    """本轮要带给模型的历史、摘要，以及"这轮有没有做过压缩"。

    **不指定会话**（脚本/MCP 无状态调用）时不压缩：没有会话就没有历史可压。
    ``prepare_context`` 里的摘要调用可能失败（模型临时不可用），这里兜底成
    "最近若干条历史"——压缩是优化，不该把整轮问答拖垮。
    """
    if not payload.conversation_id:
        return (
            [ChatMessage(role=item.role, content=item.content) for item in payload.history],
            "",
            False,
        )
    try:
        prepared = services.chat.prepare_context(
            conversation_id=payload.conversation_id, query=payload.query, model_pk=model_pk
        )
        return prepared.history, prepared.summary, prepared.compressed
    except Exception:
        logger.warning("上下文准备失败，退回最近历史", exc_info=True)
        return services.conversations.history(payload.conversation_id), "", False


def _sediments_memory(caller: Caller) -> bool:
    """这一轮该不该往长期记忆里**写**（目标清单里的"自动化请求不入记忆"）。

    **API Key 那条路不写**：它是 MCP 客户端、脚本与自动化测试在说话——非流式那个
    端点的说明自己就写着"给脚本、MCP 与自动化测试用"。把它们记进"这个人说过什么"里，
    等于让机器替人往记忆里写话；而记忆一旦被机器的话淹了，**没有任何界面能把它挑回来**
    （用户看到的是一堆自己没说过的事）。

    **召回不受影响**：自动化通道照样能 ``recall``（读记忆是有用的），只是不往里写。

    另外两条自动化路径**结构上**就被排除了，不靠这里判：定时任务走服务层直连
    （``services/schedule_runner.py``，不经过 api 这一层），子 Agent 的消息不进会话。
    """
    return caller.api_key is None


def _capture_implicit(services: Services, query: str, *, caller: Caller) -> CaptureOutcome | None:
    """跑一次隐式捕获——**best-effort 的**（§4.1 第②路）。

    「关着 / 没信号 / 判定说没有值得写的」三种情况都由服务层回答（返回 ``None``
    或一个空的 ``results``），这里只做两件事：挡掉自动化通道，把失败**吞成日志**。

    为什么吞：记忆是加分项，绝不能让它影响一次已经成功的问答（与 webhook 同一条
    口径）。为什么不静默：判定是一次出网的模型调用，它坏了要能在日志里看见。
    """
    if not _sediments_memory(caller):
        return None
    try:
        return services.memory.capture_implicit(query, user_id=_memory_owner(caller))
    except Exception:
        logger.warning("隐式记忆捕获失败，这一轮不写：%r", query[:80], exc_info=True)
        return None


def _implicit_capture_step(
    services: Services,
    query: str,
    *,
    caller: Caller,
    steps: list[dict[str, object]],
    sink: _TurnSink,
) -> None:
    """信号命中且有东西写进去时，把**回执**补进这一轮的过程面（§4.1、§4.4）。

    三条纪律：

    - **回执与显式那条路同一份文案**：这一句就是 ``WriteResult.receipt``
      （「记下了：…」「改成：…」「这一区满了（20/20 条）…」）——模型从 `remember`
      工具听到的、人在界面上看到的、这里冒出来的，三处是一句话；
    - **没写进去就不报这一步**：这一轮没信号、或判定说没有值得写的，过程面上
      什么都不加——报一件没发生的事比不报更糟；
    - **两份用同一个 payload**：P0-2 的硬验收是"日志的投影 == 消息里的快照"
      （`services.session_events.steps_from_events`），所以 ``sink.steps`` 与
      要落库的 ``steps`` 两边都进、而且是同一个对象。``steps`` 在非 Agent 那条
      链路上与 ``sink.steps`` 不是同一个列表，所以两边都要有。

    位置在 ``sink.close_turn`` **之前**：``turn/end`` 里的步数读的是 ``sink.steps``，
    之后再补就数不上它——而它正是这一轮的最后一步。
    """
    outcome = _capture_implicit(services, query, caller=caller)
    if outcome is None or not outcome.results:
        return
    payload: dict[str, object] = {
        "phase": "memory",
        "label": "记进档案",
        "detail": outcome.receipt,
        "status": "done",
    }
    if steps is not sink.steps:
        sink.steps.append(payload)
    steps.append(payload)
    sink.events.append(EventDraft(kind=KIND_STEP, payload=payload))


def _record_turn(  # type: ignore[no-untyped-def]
    services: Services,
    payload: ChatRequestIn,
    *,
    answer: str,
    sources,
    steps: list[dict[str, object]] | None = None,
    thinking: str = "",
    caller: Caller,
    events: Sequence[EventDraft] = (),
) -> None:
    """把这一轮写进会话（仅在指定了 ``conversation_id`` 时）。

    引用**存快照**：``_sources_out`` 出来的就是这一轮实际依据的原文出处。
    事后重查会得到不同的结果，引用编号就对不上了。

    ``events``（P0-2）：这一轮的会话事件，与两条消息**同一个事务**落库
    （见 ``ConversationService.record_turn``）。所以不可能出现"消息在、事件不在"。
    """
    if not payload.conversation_id:
        return
    conversation_id = payload.conversation_id
    try:
        services.conversations.record_turn(
            conversation_id,
            question=payload.query,
            answer=answer,
            sources=[item.model_dump(mode="json") for item in _sources_out(sources)],
            # 过程与回答一起存：回看一条旧回答时，"它是怎么来的"和"它说了什么"
            # 同样重要（v0.25）
            steps=list(steps or ()),
            thinking=thinking,
            events=list(events),
            # 随发的附件（v0.55）**只挂用户那条消息**：用户报的"我发的消息里没有文件"
            attachments=_turn_attachments(services, conversation_id, payload),
        )
        services.conversations.ensure_title(conversation_id, payload.query)
    except Exception:
        # 落库失败不该让用户丢掉已经拿到的回答——那是**已经付过费**的结果。
        # 记日志即可；下一轮的历史会缺这一条，但不影响继续对话。
        logger.exception("对话落库失败：%s", conversation_id)
        return


class TurnRecordIn(BaseModel):
    """边车（P4-2b）在本机跑完一轮后**写回**服务器的一份已完成对话。

    会话是**服务器权威** ✓：边车那一侧跑得再对，这一轮也必须记回服务器 ✓ ——
    否则前端一切到边车，刷新之后那一轮就消失了 ✗✗（数据丢失）。
    """

    conversation_id: str = Field(min_length=1, description="记到哪条会话（必须可见/属于调用方）")
    turn_id: str = Field(
        min_length=1,
        description=(
            "**幂等键（必填）**：同一轮上报两次只落一次 ✓（边车重试、网络重发都是常态）"
        ),
    )
    question: str = Field(min_length=1, max_length=32_000, description="用户那条消息")
    answer: str = Field(default="", description="助手那条回答")
    steps: list[dict[str, Any]] = Field(
        default_factory=list, description="过程快照（与前端「执行过程」同形）"
    )
    thinking: str = Field(default="", description="这一轮的思考（可空）")
    sources: list[dict[str, Any]] = Field(default_factory=list, description="引用快照（可空）")


class TurnRecordOut(BaseModel):
    """写回的结果。``recorded=False`` **不是失败** ✓ —— 是幂等命中（之前已经记过 ✓）。"""

    conversation_id: str
    turn_id: str
    recorded: bool = Field(description="true=这次真的写进去了；false=之前已经记过（没有重复写）")


@router.post(
    # **路径要带 `/chat`** ✗✓（2026-09-29 实测 404 的根因）：这个模块的 `router` 是
    # `APIRouter(tags=["chat"])`（`chat.py:143`，**没有 prefix** ✗），
    # 所以每条路径都自己写全 —— 隔壁那条就是 `/chat/turns/{id}/live` ✗。
    # 原先这里只写 `/turns/record` ✓，于是它注册成了 `/api/v1/turns/record` ✓，
    # 而所有人（边车、文档、用例）打的是 `/api/v1/chat/turns/record` ✗ → 404 ✓。
    "/chat/turns/record",
    response_model=TurnRecordOut,
    summary="记录一轮已完成的对话（边车写回）",
)
def record_turn_endpoint(
    payload: TurnRecordIn,
    services: Annotated[Services, Depends(get_services)],
    # **写端点用写档**（照 `router.py` 那条"逐个端点显式声明"的规矩 ✓）：
    # 成员会话与管理员不受影响 ✓（`check_access` 对 `caller.user` 不判档位 ✓）；
    # 真正的区别在 **API Key 通道** ✗ —— 只读 Key 原先能靠这个端点往会话里写 ✗。
    caller: Annotated[Caller, Depends(require_write)],
) -> TurnRecordOut:
    """把本机跑完的一轮记回服务器（P4-2b）。

    三条口径：

    - **复用 `ConversationService.record_turn`**（`services/conversation.py:453` ✓）——
      与服务器自己那条链路**同一份记录器** ✗（两条消息 + 事件日志同一个事务 ✓），
      绝不另写一份 ✗（两份必然漂移）；
    - **幂等**：`turn_id` 是我们自己写在 `turn/start` 事件载荷里的标记 ✓ → 上报前先查
      **这次记录之前有没有同一轮** ✓（有就原样返回 `recorded=False` ✓，不重复插 ✓）；
    - **鉴权与所有权照旧**：普通成员只能写自己的会话 ✓（别人的 → 404 ✓，与
      `conversations._get_visible` 同口径 ✓，不暴露存在性 ✓）；写不进去就抛错 ✓ ——
      **绝不 200 假装成功** ✗。
    """
    conversation_id = payload.conversation_id
    owner = caller.user.id if (caller.user is not None and not caller.is_admin) else None
    if owner is None:
        services.conversations.get(conversation_id)
    else:
        services.conversations.get_for_owner(conversation_id, owner)

    # 幂等：这一轮的标记（写在 `turn/start` 载荷里的 `turn_id`）已经在事件日志里 → 记过了
    #
    # **为什么是"查事件日志"而不是进程内的一张回执表** ✗：标记与消息在**同一个事务**里落库 ✓
    # （`record_turn` 的 `events=[marker]` ✓），所以这份回执**跨进程重启仍然有效** ✓ ——
    # 而进程内集合一重启就空了 ✗，那时边车重发（它本来就允许重试 ✓）会**重复一轮** ✓。
    # 换句话说：进程内回执只是"尽力而为" ✓，这条是**真的幂等** ✓，别把它降级成前者 ✗。
    # 台账（持久回执表）留给 P4-2c ✓；那一轮要解决的是"按 turn_id 反查/对账"，
    # 不是"要不要重复插" ✓（后者这一行已经答完 ✓）。
    for event in services.conversations.session_events(conversation_id):
        recorded_id = (getattr(event, "payload", None) or {}).get("turn_id")
        if recorded_id and str(recorded_id) == payload.turn_id:
            return TurnRecordOut(
                conversation_id=conversation_id, turn_id=payload.turn_id, recorded=False
            )

    marker = EventDraft(
        kind=KIND_TURN_START,
        payload={"query": payload.question, "turn_id": payload.turn_id},
    )
    services.conversations.record_turn(
        conversation_id,
        question=payload.question,
        answer=payload.answer,
        sources=payload.sources,
        steps=payload.steps,
        thinking=payload.thinking,
        events=[marker],
    )
    services.conversations.ensure_title(conversation_id, payload.question)
    # **落库成功之后才谈隐式捕获**（与服务器自己那条链路同一个口径）：
    # 边车写回的轮次与人在网页里问的那一轮在库里长得一模一样，
    # 不在这里接一下，这些轮次就**永远不进长期记忆**——而"记忆里少了边车那几轮"
    # 是用户事后才发现、且无法补回的那类丢失。
    #
    # 位置就在**幂等命中之后**：同一轮上报两次时上面已经 `return`，
    # 于是重复上报不会把同一轮再判一次。
    #
    # 它没有过程面可补（这条链路上没有对话流），所以只要结果、不报步骤。
    _capture_implicit(services, payload.question, caller=caller)
    return TurnRecordOut(
        conversation_id=conversation_id, turn_id=payload.turn_id, recorded=True
    )


def _turn_attachments(
    services: Services, conversation_id: str, payload: ChatRequestIn
) -> list[dict[str, object]]:
    """这一轮用户消息随带的附件**快照**（v0.55）。

    名字 / 类型 / 字节数**按库里的记录回填**，不信客户端送来的那几个字段——它们
    只是显示用的，而"这条消息当时带的是哪几份文件"要能被信任。

    ``key`` **必须属于这条会话**：不校验的话，任何 key（越会话的、甚至不存在的）
    都能被写进消息里。校验不过的**丢掉并记日志**，而不是让整轮落库失败——
    与这个模块"落库失败也不打扰用户"同一条口径（附件只是展示，不该赔上整轮）。
    """
    if not payload.attachments:
        return []
    known = {item.id: item for item in services.artifacts.list_for_conversation(conversation_id)}
    snapshot: list[dict[str, object]] = []
    for item in payload.attachments:
        record = known.get(item.key)
        if record is None:
            logger.warning("附件不属于这条会话，已忽略：%s / %s", conversation_id, item.key)
            continue
        snapshot.append(
            {
                "key": record.id,
                "name": record.name,
                "kind": record.format or "",
                "size_bytes": record.size_bytes,
            }
        )
    return snapshot


# ------------------------------------------------------------------ 事件日志收尾


def _turn_status(steps: Sequence[dict[str, object]]) -> str:
    """这一轮的终止原因（枚举，见 ``services/session_events`` 的四个取值）。

    降级判定**复用续跑那条路的口径**（``resume.degraded_reason``）而不是自己看
    一眼 ``degraded`` 键：同一个问题（"这一轮跑完了吗"）有两份判断，
    迟早会出现"日志说降级、界面不给继续按钮"这种对不上的状态。
    """
    return TURN_DEGRADED if resume_service.degraded_reason(steps) else TURN_OK


def _flush_events(services: Services, conversation_id: str | None, sink: _TurnSink) -> None:
    """把攒下的事件落库（**没有消息可写**时的收尾）。

    三处用它：流内失败、流跑完但一个字都没吐、以及用户中途停止。
    这三种情况按 v0.12 起的取舍都不留消息（失败的一轮不留半截记录），
    而它们**恰恰最需要日志**——"当时为什么没有回答"只有这里答得出来。

    best-effort：写日志失败绝不能把已经发生的失败再放大一次，只记日志。
    """
    if not conversation_id or not sink.finished or not sink.events:
        return
    try:
        services.conversations.append_events(conversation_id, sink.events)
    except Exception:
        logger.exception("会话事件落库失败：%s", conversation_id)


def _fail(
    services: Services,
    conversation_id: str | None,
    sink: _TurnSink,
    message: str,
    *,
    cause: BaseException | None = None,
) -> LiveEmit:
    """流内失败的收尾：把 ``error`` + ``turn/end`` 记进日志，并给出要发的那条事件。

    返回值就是客户端看到的那个 ``type=error``——**顺序不能反**：
    先记日志再返回，日志里才不会有"没有结尾的一轮"。

    ``message`` **必须是给人看的一句话**（调用方一律传 ``failure_text(exc)``，
    见 ``services/failures.py``）；``cause`` 是那条要被记下来的原始异常——
    **异常原文只走日志这条路**：``error`` 事件会进会话事件表、也会被前端原样显示，
    内部语言（端点地址、上游返回体、``httpx`` 的类名）不该出现在那里。

    ``log_index`` 取收尾那一刻的日志条数（游标口径，见 ``_events`` 的收尾），
    ``terminal=True`` 让后台那一条记住它：重连收口时把这条错误原样再发一遍。
    """
    if cause is not None:
        # 与调用点里那几处 logger.exception 同一个落点：一条失败**总要留下能排错的东西**，
        # 而给用户的那句话里没有它
        logger.warning("这一轮失败（对用户报：%s）：%s", message, cause, exc_info=True)
    sink.note_error(message)
    sink.close_turn(status=TURN_ERROR, answer="")
    _flush_events(services, conversation_id, sink)
    return LiveEmit(
        {"type": "error", "message": message}, log_index=len(sink.events), terminal=True
    )


def _record_interruption(services: Services, conversation_id: str | None, sink: _TurnSink) -> None:
    """用户中途停止 / 断开时的收尾（P0-2 的第 5 条，QwenPaw 那条教训）。

    两件事：补一条 ``interrupted``（含"哪些调用没有结果"与已看到的正文），
    再把这一轮攒到现在的事件一起写进日志。**已经收尾的一轮不补**——用户读完答复
    才关页面是很正常的事，那不该被记成"被中断"。
    """
    if sink.finished:
        return
    sink.mark_interrupted(reason="客户端断开或用户停止")
    _flush_events(services, conversation_id, sink)


def _memory_owner(caller: Caller) -> str | None:
    """这次问答该用**谁的记忆**（v0.15）。

    普通成员 → 自己的账号；管理员会话与 API Key 通道 → 共享桶（``None``）。
    判定本身在 ``Caller.owner_id``（同一个口径要供知识库/会话/工作区/能力用，
    各自写一份迟早会分叉）；这里保留这个函数是因为它在对话链路里被调了多处，
    名字比 ``caller.owner_id`` 更能说明"这是记忆归属"。
    """
    return caller.owner_id


def _require_conversation(services: Services, payload: ChatRequestIn, caller: Caller) -> None:
    """指了会话就必须存在**且属于当前调用方**。

    **不做"静默新建"**：那样用户拼错一个 id 会得到一次正常回答，然后发现历史没存上
    ——而落库失败是静默的（见 ``_record_turn`` 的取舍），两次静默叠起来根本无法排查。
    在**流还没开始**之前抛 404，用户立刻知道 id 不对。

    成员（v10）越主同样 404：对话内容是私有数据，403 会暴露"这条会话存在"。
    不拦的话，成员拿着别人的会话 id 就能把整段历史读走（`_history` 以库里为准）。
    管理员会话不受此限（is_admin）：它要能看到 API Key 建的无主会话。
    """
    if not payload.conversation_id:
        return
    _require_visible_conversation(services, payload.conversation_id, caller)


def _require_visible_conversation(services: Services, conversation_id: str, caller: Caller) -> None:
    """会话要存在且可见，否则 404。**所有"按会话读"的端点共用这一处判定。**

    抽成一个函数的理由与 ``Caller.owner_id`` 一样：这条规则一旦有两份，
    就会出现"某个端点忘了判归属"——而那种漏法不报错，只是把别人的会话读走了。
    """
    if caller.user is not None and not caller.is_admin:
        services.conversations.get_for_owner(conversation_id, caller.user.id)
    else:
        services.conversations.get(conversation_id)


def _warn_on_scope_drift(services: Services, payload: ChatRequestIn) -> None:
    """会话建立的库范围与本次请求不一致时留一条日志。

    刻意**不拦**：用户可能就是想换个库继续问。但这种情况下的多轮指代会指向
    "上一轮在另一个库里看到的资料"，答非所问很难排查——所以留个痕迹。
    """
    if not payload.conversation_id:
        return
    try:
        record = services.conversations.get(payload.conversation_id)
    except Exception:
        return
    if record.kb_ids and set(record.kb_ids) != set(payload.kb_ids):
        logger.info(
            "会话 %s 的库范围与本次请求不同（会话 %s / 请求 %s），多轮上下文可能不连续",
            record.id,
            list(record.kb_ids),
            payload.kb_ids,
        )


def _sources_out(sources) -> list[ChatSourceOut]:  # type: ignore[no-untyped-def]
    return [ChatSourceOut.model_validate(source) for source in sources]


# ------------------------------------------------------------------ 斜杠命令（P1-2）
#
# 这一节是 P1-2 在协议层的落点：**以 ``/`` 开头的输入在进模型之前就被认出来**
# （QwenPaw 的"三类命令进 LLM 之前短路"），短路类命令**不建工具循环、不落消息**
# （DSH 的"命令执行写 session log 但不进模型历史"）。判定与词表都在
# ``services/commands.py``，这里只负责"拿服务把命令执行掉、把结果摊成一条事件"。


@dataclass(slots=True)
class _CommandResult:
    """一条命令的结论：**要么直接答掉**（短路类），**要么给出这一轮的提示**（改写类）。"""

    name: str
    text: str = ""
    """回给用户看的那段话（界面按普通文本渲染，**不进模型上下文**）。"""
    ok: bool = True
    """失败也走这条事件（``ok=False``）：命令的报错是**回话**，不是 HTTP 500——
    它跟"模型没配好"那种环境故障不是一回事，用户要的就是那句解释。"""
    action: dict[str, object] | None = None
    """界面要顺手做的事（开新会话 / 切到某档 / 停掉这一轮）。"""
    prompt: str = ""
    """**改写类**命令渲染出来的这一轮提示（非空 = 接着走正常那条链路）。"""
    refill: str = ""
    """要**回填到输入框**的文字（``/rewind`` 把它撤掉的那句提问交回来）。

    空 = 不回填（绝大多数命令如此）。合同是"有就给、没有就没有"：
    前端据此把输入框填上（用户改一版就能重发，就是 Claude/Gemini 里 ``/rewind``
    的手感），不认这个字段的老客户端一个字都不受影响——它只是少一个便利。
    """

    @property
    def short_circuit(self) -> bool:
        """这一条要不要模型（空的 ``prompt`` 就是短路类，见模块头那两条）。"""
        return not self.prompt


def _plan_command(
    services: Services, payload: ChatRequestIn, caller: Caller
) -> _CommandResult | None:
    """这一轮的输入是命令吗；是的话**当场把它办掉**（返回 ``None`` = 不是命令）。

    四种结局，每一种都有明确去处：

    1. **不是命令** → ``None``（普通提问，一切照旧）；
    2. **短路类内置**（``/help`` ``/new`` ``/stop`` ``/mode`` ``/model`` ``/compact``
       ``/rewind`` ``/context`` ``/status`` ``/skills`` 与**不带描述的** ``/plan``）
       → 在这里执行完，协议层按"一条 ``command`` + 一条 ``done``"回给界面；
    3. **改写类**（``/skill``、**技能自己的那条命令**、**带描述的** ``/plan``
       与全部自定义 md）→ 返回渲染好的 ``prompt``，调用方拿它当这一轮的提示
       **继续往下走那条正常链路**（ZCode：``/skill`` 会重写下一条 prompt）；
    4. **认不出的** → 照 DSH 的「``/`` 行永不静默降级为普通 prompt」回一句"没有这个命令"
       （静默发给模型的话，用户以为自己在用命令，模型却在猜他想说什么）。

    ``caller`` 传给短路类里那几条"要看这一轮上下文"的（``/context`` ``/status``）：
    上下文的分解里有一项是**调用者的**人设文件，而工具表那一份也要按他的库范围拼
    （与 ``GET /chat/context-usage`` 同一处拼装，见那个端点的说明）。

    **本函数不碰模型**（除了 ``/compact`` 那一次摘要调用，那是压缩链路本身）：
    短路的意义就是"这一步不该花钱"。
    """
    parsed = commands.parse(payload.query)
    if parsed is None:
        return None
    record = services.commands.find(parsed.name)
    if record is None:
        return _unknown_command(services, parsed.name)
    if record.short_circuit:
        return _dispatch_builtin(services, payload, record, parsed, caller)
    return _rewrite_prompt(services, record, parsed)


def _rewrite_prompt(
    services: Services, record: commands.CommandDef, parsed: commands.ParsedCommand
) -> _CommandResult:
    """改写类命令：把正文渲染成**这一轮的提示**。

    两条路共用这一处：
    ``/skill`` 与**技能自己的命令**（``/kylab-web 查一下``）都走
    ``ChatService.skill_prompt``——后者只是把"技能名"从参数换成了命令名本身，
    技能注入仍然只有一处实现（照 Claude 的"命令＝技能"）；
    自定义 md 命令走 ``commands.render``（``$ARGUMENTS`` / ``$0`` / ``$N`` /
    ``$ARGUMENTS[N]`` / ``$name`` 与那条兜底追加）。

    技能名打错时**在这里就回一句**（而不是让整轮 404）：用户敲的是命令，
    他要的是"这个技能名不对"，不是一次 HTTP 报错。
    """
    if record.skill:
        # 技能自己的命令：任务就是参数的全文（不多切一层"技能名"）
        return _skill_prompt(services, name=record.skill, task=parsed.args, command=record.name)
    if record.name == commands.NAME_SKILL:
        pieces = parsed.args.split(maxsplit=1)
        if not pieces:
            return _CommandResult(
                name="skill",
                ok=False,
                text="用法：/skill <技能名> [任务]。技能名在「能力」页里能看到，"
                "也可以直接敲 /技能名（见 /skills）。",
            )
        return _skill_prompt(
            services, name=pieces[0], task=pieces[1] if len(pieces) > 1 else "", command="skill"
        )
    return _CommandResult(name=record.name, prompt=commands.render(record, parsed.args))


def _skill_prompt(services: Services, *, name: str, task: str, command: str) -> _CommandResult:
    """读一个技能的正文并渲染成这一轮的提示。**两条入口共用**（见 ``_rewrite_prompt``）。

    读不出来就回一句人话（``NotFoundError``）：与"钉住技能"那条路的处置相反——
    这里是用户明确点名的一次动作，静默什么都没发生，他只会以为技能不好使。
    """
    try:
        prompt = services.chat.skill_prompt(name, task)
    except NotFoundError as exc:
        return _CommandResult(name=command, ok=False, text=f"读不到这个技能：{exc}")
    return _CommandResult(name=command, prompt=prompt)


def _unknown_command(services: Services, name: str) -> _CommandResult:
    """认不出的命令：**如实说没有**，并给两个最可能的自救（``/help``、被遮蔽的原因）。

    被遮蔽的（``shadowed_by``）与加载失败的（``error``）在这里分开说：它们
    "文件在、命令不生效"的原因完全不同，混成一句"没有这个命令"会让用户
    去翻一个明明在那儿的文件。
    """
    existing = services.commands.any_named(name)
    if existing is not None and existing.shadowed_by:
        return _CommandResult(
            name=name,
            ok=False,
            text=(
                f"命令 /{name} 被 /{existing.shadowed_by} 遮蔽了：同名时**内置 > 用户 > 仓库**，"
                f"所以生效的是 /{existing.shadowed_by}。要用你放的那一份，先把它改个名字。"
            ),
        )
    if existing is not None and existing.error:
        return _CommandResult(
            name=name, ok=False, text=f"命令 /{name} 没被加载：{existing.error}"
        )
    return _CommandResult(
        name=name,
        ok=False,
        text=f"没有这个命令：/{name}。敲 /help 看全部可用命令。",
    )


def _dispatch_builtin(
    services: Services,
    payload: ChatRequestIn,
    record: commands.CommandDef,
    parsed: commands.ParsedCommand,
    caller: Caller,
) -> _CommandResult:
    """执行一条短路类内置命令。**每一分支都必须自己回答"没有会话时怎么办"。**"""
    if record.name == commands.NAME_HELP:
        return _help(services, parsed)
    if record.name == commands.NAME_COMPACT:
        return _compact(services, payload, caller)
    if record.name == commands.NAME_NEW:
        return _new_conversation(services, payload)
    if record.name == commands.NAME_STOP:
        return _stop_turn(services, payload)
    if record.name == commands.NAME_MODE:
        return _switch_mode(services, payload, parsed)
    if record.name == commands.NAME_MODEL:
        return _switch_model(services, payload, parsed)
    if record.name == commands.NAME_PLAN:
        return _enter_plan_mode(services, payload, parsed)
    if record.name == commands.NAME_REWIND:
        return _rewind(services, payload, parsed)
    if record.name == commands.NAME_CONTEXT:
        return _context_usage(services, payload, caller)
    if record.name == commands.NAME_STATUS:
        return _status(services, payload, caller)
    if record.name == commands.NAME_SKILLS:
        return _skills(services)
    return _CommandResult(name=record.name, ok=False, text=f"/{record.name} 还没有实现。")


def _help(services: Services, parsed: commands.ParsedCommand) -> _CommandResult:
    """``/help``：没参数列全部，带参数展开一条。

    **与前端那个 ``/`` 菜单读同一份数据**（``services.commands``）——两处各写一份
    清单的话，"菜单里点得到、``/help`` 里查不到"这种不一致迟早出现。
    技能那批也在里面（它们就是命令），所以这一屏同时也是"我有哪些技能"的一览。
    """
    wanted = parsed.args.split()[0].lstrip("/") if parsed.args.split() else ""
    if wanted:
        record = services.commands.any_named(wanted)
        if record is None:
            return _CommandResult(name="help", ok=False, text=f"没有这个命令：/{wanted}")
        lines = [f"/{record.name} — {record.summary}"]
        if record.usage:
            lines.append(f"用法：{record.usage}")
        lines.extend(record.details)
        if record.name == commands.NAME_MODE:
            lines.append(commands.modes_text())
        if record.shadowed_by:
            lines.append(f"注意：这一条正被 /{record.shadowed_by} 遮蔽（同名取优先级最高的那条）。")
        if record.error:
            lines.append(f"注意：这一条没有加载成功——{record.error}")
        # 有文件就报文件：内置命令没有文件，自定义命令与**技能命令**都有
        # （技能那份是 ``SKILL.md``，排错时正是要找它的地方）
        if record.path:
            lines.append(f"文件：{record.path}")
        return _CommandResult(name="help", text="\n".join(lines))
    # **按组分节**（23 条命令平铺已经读不动了，而"分组"正是 `/` 菜单吃的那同一份数据）。
    # 内置按用途再分三节——会话动作 / 上下文与状态 / 模式与模型——否则 `/rewind` 这类会话动作
    # 会被一长串通用命令压到看不见（Claude 的 `/help` 也是按来源分节的）。
    sections: list[tuple[str, tuple[str, ...]]] = [
        (
            "会话动作",
            (commands.NAME_NEW, commands.NAME_STOP, commands.NAME_COMPACT, commands.NAME_REWIND),
        ),
        (
            "上下文与状态",
            (
                commands.NAME_CONTEXT,
                commands.NAME_STATUS,
                commands.NAME_SKILLS,
                commands.NAME_SKILL,
            ),
        ),
        ("模式与模型", (commands.NAME_MODE, commands.NAME_MODEL, commands.NAME_PLAN)),
        ("帮助", (commands.NAME_HELP,)),
    ]
    catalog = list(services.commands.catalog())
    taken: set[str] = set()
    lines: list[str] = []
    for title, names in sections:
        rows = [record for record in catalog if record.name in names]
        if not rows:
            continue
        lines.append(f"{title}：")
        for record in rows:
            taken.add(record.name)
            lines.append(f"  {record.usage or f'/{record.name}'} — {record.summary}")
    # 其余按发现源分节：你放的 / 随代码发布 / 技能（技能那批由 `_skill_commands` 注册）
    for group, title in (("user", "你放的"), ("repo", "随代码发布"), ("skill", "技能")):
        rows = [record for record in catalog if record.group == group and record.name not in taken]
        if not rows:
            continue
        lines.append(f"{title}：")
        for record in rows:
            taken.add(record.name)
            lines.append(f"  {record.usage or f'/{record.name}'} — {record.summary}")
    rest = [record for record in catalog if record.name not in taken]
    if rest:
        lines.append("其它：")
        for record in rest:
            lines.append(f"  {record.usage or f'/{record.name}'} — {record.summary}")
    lines.append("/help <命令名> 看某一条的详细用法；技能也能直接当命令用：/技能名 [任务]。")
    return _CommandResult(name="help", text="\n".join(lines))


def _compact(
    services: Services, payload: ChatRequestIn, caller: Caller
) -> _CommandResult:
    """``/compact``：**现在就把上下文压掉**（走的是自动压缩那条链路的同一个函数）。"""
    if not payload.conversation_id:
        return _CommandResult(
            name="compact", ok=False, text="/compact 需要一条会话：先在这里问一句再压。"
        )
    try:
        count = services.chat.compact(
            conversation_id=payload.conversation_id, model_pk=payload.model_pk
        )
    except Exception as exc:
        # 失败**如实说**（自动压缩那条路是吞掉的，这里不能吞：用户点了一下，
        # 回"已压缩"而其实没压是最糟的一种回话）
        logger.warning("手动压缩失败：%s", payload.conversation_id, exc_info=True)
        return _CommandResult(name="compact", ok=False, text=f"压缩失败：{exc}")
    if count == 0:
        return _CommandResult(name="compact", text="没有可压缩的内容：这条会话的对话都已进摘要。")
    return _CommandResult(
        name="compact",
        text=f"已把 {count} 条较早的消息压成摘要；之后的问答仍记得它们，但上下文短了。",
    )


def _new_conversation(services: Services, payload: ChatRequestIn) -> _CommandResult:
    """``/new``：开一条新会话（**当前这条不删**，只是不再是当前会话）。

    库范围、模型、思考档、归属账号**都取当前这条会话的**：用户敲 ``/new`` 是"换个话题"，
    不是"把这一轮的选择也重置掉"，更不是"换个人"。
    """
    kb_ids = list(payload.kb_ids)
    model_pk = payload.model_pk
    thinking = payload.thinking
    effort = payload.thinking_effort
    owner_id: str | None = None
    if payload.conversation_id:
        try:
            current = services.conversations.get(payload.conversation_id)
            kb_ids = list(current.kb_ids) or kb_ids
            model_pk = model_pk or current.model_pk
            thinking = thinking if thinking is not None else current.thinking
            effort = effort or current.thinking_effort
            owner_id = current.owner_id
        except Exception:
            # 当前这条读不出来（不存在 / 越主）：那就不继承，照请求里给的建
            logger.info("新建会话时读不到当前会话：%s", payload.conversation_id, exc_info=True)
    record = services.conversations.create(
        kb_ids=kb_ids,
        owner_id=owner_id,
        model_pk=model_pk,
        thinking=thinking,
        thinking_effort=effort,
    )
    return _CommandResult(
        name="new",
        text="已新建会话。当前这条留在历史里，随时可以回去。",
        action={"kind": "conversation", "conversation_id": record.id},
    )


def _stop_turn(services: Services, payload: ChatRequestIn) -> _CommandResult:
    """``/stop``：把这一轮叫停（P1-2 的"停止也要是一等命令"）。

    **后端能停后端的那一半**：这一轮正在另一个请求里跑（SSE 那条流），
    这里给它挂一个停止标记，那条流在**下一次拿到事件时**收工并补一条 ``interrupted``
    （见 ``_turn_events`` 里那个检查）。界面同时会自己 abort（``action`` 那一项），
    两条路都到达同一处——比谁先到不影响结果。

    没有在跑的一轮时**如实回一句**：假装停了一下比不回答更糟。
    """
    if not payload.conversation_id:
        return _CommandResult(name="stop", ok=False, text="这条会话上没有在跑的一轮。")
    if not services.commands.turns.request_stop(payload.conversation_id):
        return _CommandResult(
            name="stop",
            ok=False,
            text="这条会话上没有在跑的一轮（可能已经跑完了，或者它刚被别处停掉）。",
        )
    return _CommandResult(
        name="stop",
        text="已请求停止；已经流出来的正文会留着，过程日志里补一条「被中断」。",
        action={"kind": "stop_turn"},
    )


# ------------------------------------------------- 会话自身的动作与状态（P1-2 续）
#
# 这一节是《对话命令-调研 v0.1》§3 第 1-4 条差距的落点：``/rewind`` ``/context``
# ``/status`` ``/skills``。四条的共识是**会话自身的动作与上下文/状态都要是一等命令**
# （Claude / Gemini / ZCode 三家都有 ``/rewind``，Claude 有 ``/context``）——
# 我们此前只有界面上一个按钮和一个仪表，于是"脚本/MCP 那条没有按钮的路"上没有等价物。
#
# 四条全是短路类，而且四条都需要一条会话（除了 ``/skills``）：
# 没有会话时**如实说**，不假装做了什么（与 ``/compact`` ``/model`` 同一口径）。

#: ``/rewind`` 一次最多撤几轮。**与 ``ConversationRewindIn.turns`` 的上限取同一个数**
#: （那儿是 pydantic 的 ``le=20``）：命令与端点差一个数的话，用户会得到
#: "界面按钮能撤 20 轮、命令说最多 10 轮"这种没人解释得清的区别。
REWIND_MAX_TURNS = 20


def _round_count(services: Services, conversation_id: str) -> tuple[int, int]:
    """这条会话有多少轮问答、多少条消息（``(轮数, 消息数)``）。

    轮数按**提问**数算（一轮 = 一问一答）：会话是一份线性记录，而"我问了几轮"
    正是用户说的那个轮。回答可能缺失（半截的一轮不落消息），所以不能拿消息数除 2。
    """
    try:
        messages = list(services.conversations.messages(conversation_id))
    except Exception:
        logger.info("读会话消息失败：%s", conversation_id, exc_info=True)
        return 0, 0
    return sum(1 for item in messages if item.role == "user"), len(messages)


def _rewind(
    services: Services, payload: ChatRequestIn, parsed: commands.ParsedCommand
) -> _CommandResult:
    """``/rewind [轮数]``：撤回最近 N 轮，并把被撤掉的那句提问**交回给界面**。

    照三家的 ``/rewind``（Claude / Gemini / ZCode 都有，见调研报告 §3 第 1 条），
    但**不做交互式选点**（Gemini 那一步要一层终端 UI，我们这里是"输入框里的一条命令"）：
    不给参数撤 1 轮、``/rewind 2`` 撤 2 轮。

    **能力本身早就有了**（``POST /conversations/{id}/rewind``，界面上「重新生成」
    那个按钮走的就是它）——这一条只是把它接到"没有按钮的那条路"上：
    写的是 ``ConversationService.rewind``，不另开一条删除实现。

    越界（撤回数 > 现有轮数）**在这里就回一句人话**：服务层抛的是
    ``InvalidRequestError``，让它穿透成 500 的话，用户看到的是一次故障而不是
    "你只有 3 轮"。
    """
    if not payload.conversation_id:
        return _CommandResult(
            name=commands.NAME_REWIND,
            ok=False,
            text="/rewind 需要一条会话：它撤的是这条会话里最近的几轮问答。",
        )
    pieces = parsed.args.split()
    turns = 1
    if pieces:
        try:
            turns = int(pieces[0])
        except ValueError:
            turns = 0
        if turns < 1 or turns > REWIND_MAX_TURNS:
            return _CommandResult(
                name=commands.NAME_REWIND,
                ok=False,
                text=(
                    f"撤不了 {pieces[0]} 轮：轮数要写 1 到 {REWIND_MAX_TURNS} 之间的整数。\n"
                    "用法：/rewind [轮数]——不带参数撤最近 1 轮。"
                ),
            )
    rounds, _ = _round_count(services, payload.conversation_id)
    if rounds == 0:
        return _CommandResult(
            name=commands.NAME_REWIND,
            ok=False,
            text="这条会话里还没有可撤回的问答（一条提问都没有）。",
        )
    if turns > rounds:
        return _CommandResult(
            name=commands.NAME_REWIND,
            ok=False,
            text=(
                f"这条会话只有 {rounds} 轮问答，撤不回 {turns} 轮。\n"
                f"要清空就写 /rewind {rounds}；不撤就把这句当没看见。"
            ),
        )
    before = services.conversations.message_count(payload.conversation_id)
    try:
        query = services.conversations.rewind(payload.conversation_id, turns=turns)
    except InvalidRequestError as exc:
        # 走到这里说明上面那道判断题漏了一种情形（比如这一轮只有提问、没有回答）
        logger.info("撤回被服务层拒绝：%s", payload.conversation_id, exc_info=True)
        return _CommandResult(
            name=commands.NAME_REWIND, ok=False, text=f"没能撤回：{exc}"
        )
    except Exception as exc:
        logger.warning("撤回失败：%s", payload.conversation_id, exc_info=True)
        return _CommandResult(name=commands.NAME_REWIND, ok=False, text=f"没能撤回：{exc}")
    removed = max(0, before - services.conversations.message_count(payload.conversation_id))
    return _CommandResult(
        name=commands.NAME_REWIND,
        text=(
            f"已撤回 {turns} 轮（连回答一起删掉了 {removed} 条消息）：\n{query}\n"
            "被撤回的对话不再出现在历史里，模型下一轮也看不到它们了；"
            "那句提问已经填回输入框，改一版就能重发。"
        ),
        # **回填输入框**（与前端约定死的那个可选字段）：用户改一版再发就是
        # Claude/Gemini 里 /rewind 的手感。取原样的提问，不截断——截断过的提问
        # 回填回去就是"改一版"改错了地方。
        refill=query,
    )


def _context_of(services: Services, payload: ChatRequestIn, caller: Caller):  # type: ignore[no-untyped-def]
    """这一轮的上下文分解（``None`` = 没有可算的会话）。

    **与 ``GET /chat/context-usage`` 同一处拼装**：工具表要调用者身份与这一轮的
    库范围才能拼出来，两处各拼一份的话，命令说一套、仪表显示另一套。
    只读：调它不会触发压缩（那是 ``prepare_context`` 的事）。
    """
    if not payload.conversation_id:
        return None
    try:
        conversation = services.conversations.get(payload.conversation_id)
    except Exception:
        logger.info("读会话失败（上下文用量算不了）：%s", payload.conversation_id, exc_info=True)
        return None
    return services.chat.context_usage(
        conversation_id=payload.conversation_id,
        owner_id=caller.owner_id,
        # 与 `_agent_loop` 同源：常驻那一份（核心 + 网关），见 build_tool_table
        tools=build_tool_table(
            services, owner_id=caller.owner_id, kb_ids=list(conversation.kb_ids)
        ).resident(),
    )


def _context_lines(usage) -> list[str]:  # type: ignore[no-untyped-def]
    """上下文用量的多行文本（``/context`` 与 ``/status`` 共用）。

    **一行一项、不用 markdown 表格**：命令结果那个面板是纯文本渲染的
    （见调研报告 §3 第 8 条的"只为结果挑一种既有排版"），表格在它里面就是一串竖线。
    """
    lines = [f"上下文占用：{usage.used:,} / {usage.total:,} tokens（{usage.ratio:.0%}，估算）"]
    for part in usage.parts:
        share = part.tokens / usage.used if usage.used else 0.0
        lines.append(f"- {part.label}：{part.tokens:,} tokens（{share:.0%}）")
    # 阈值那一行用 `compress_budget`（比例与绝对上限取小的那个，D37）：
    # 只按 `total × compress_at` 算的话，窗口调大之后这里会报一个永远到不了的数
    lines.append(
        f"自动压缩阈值：{usage.compress_budget:,} tokens"
        f"（按窗口的 {usage.compress_at}% 与绝对上限取小的那个）"
    )
    lines.append("数字按字符数估算（中日韩 1 字约 1 token、其余 4 字符约 1，偏高一点）。")
    return lines


def _context_usage(
    services: Services, payload: ChatRequestIn, caller: Caller
) -> _CommandResult:
    """``/context``：这一轮的上下文**被什么占着**（照 Claude 的 ``/context``）。

    与输入框旁边那个小仪表是同一份数据、同一个口径（``services.chat.context_usage``），
    区别只在"哪一行是重点"：仪表给一个比例，这一条给**按来源的分解**——
    "它怎么变笨了"的那个问题，能回答的一句话通常是"上下文里 60% 是技能目录"。
    """
    usage = _context_of(services, payload, caller)
    if usage is None:
        return _CommandResult(
            name=commands.NAME_CONTEXT,
            ok=False,
            text="/context 需要一条会话：它算的是这条会话这一轮真会发出去的上下文。",
        )
    return _CommandResult(name=commands.NAME_CONTEXT, text="\n".join(_context_lines(usage)))


def _status(services: Services, payload: ChatRequestIn, caller: Caller) -> _CommandResult:
    """``/status``：这条会话的一览（照 Claude 的 ``/status``）。

    只报**已有的**东西（调研报告 §3 第 3 条："没有的能力就少写一行，别现造"）：
    模型 / 模式 / 项目 / 轮数 / 上下文占用——这五样都能从既有服务里读到，
    而"思考开没开""用了哪些工具"这些要么没有会话级取值、要么要翻日志，
    这一条不替它们编一行。每一样都注一句**它现在为什么是这个值**
    （"跟随全局默认"还是"这条会话选的"），否则这一屏只是把界面上的字抄了一遍。
    """
    if not payload.conversation_id:
        return _CommandResult(
            name=commands.NAME_STATUS,
            ok=False,
            text="/status 需要一条会话：它看的是这条会话用的模型、模式、项目与上下文占用。",
        )
    try:
        conversation = services.conversations.get(payload.conversation_id)
    except Exception as exc:
        logger.info("读会话失败（/status）：%s", payload.conversation_id, exc_info=True)
        return _CommandResult(name=commands.NAME_STATUS, ok=False, text=f"读不到这条会话：{exc}")
    rounds, messages = _round_count(services, payload.conversation_id)
    lines = [f"这条会话：{conversation.title or '（还没有标题）'}"]
    lines.append(f"- 轮数：{rounds} 轮问答（{messages} 条消息）")
    current, followed = _current_model(services, payload)
    if not current:
        lines.append("- 模型：还没选过（设置页里也没绑定 chat 用途的默认模型）")
    elif followed:
        lines.append(f"- 模型：「{_model_label(services, current) or current}」（跟随全局默认）")
    else:
        lines.append(f"- 模型：「{_model_label(services, current) or current}」（这条会话选的）")
    mode = services.chat.current_mode()
    definition = modes.MODE_DEFS[mode]
    lines.append(f"- 模式：{definition.label}（{mode}）——{definition.hint}")
    if conversation.workspace_id:
        name = _workspace_name(services, conversation.workspace_id, caller)
        missing = f"- 项目：（这条会话挂的项目查不到了：{conversation.workspace_id}）"
        lines.append(f"- 项目：{name}" if name else missing)
    else:
        lines.append("- 项目：未归档（不属于任何项目）")
    usage = _context_of(services, payload, caller)
    if usage is not None:
        lines.append(
            f"- 上下文：{usage.used:,} / {usage.total:,} tokens"
            f"（{usage.ratio:.0%}，估算）"
        )
    lines.append("上下文按来源的分解见 /context；换档见 /mode。")
    return _CommandResult(name=commands.NAME_STATUS, text="\n".join(lines))


def _workspace_name(services: Services, workspace_id: str, caller: Caller) -> str:
    """工作区（项目）的显示名；看不到或读不出来就空串。

    归属判定交给工作区服务自己（``get`` 的 ``user_id`` 那条路，与侧栏同一处）——
    这里只是把"管理员/成员各看到什么"的口径留在那一处。

    **设备维度不过滤**（``any_device=True``，v0.59）：这是服务端在**回放一条既有
    会话**时读它挂着的项目名（不是某个请求参数指向的工作区），拿请求设备去卡
    只会让"桌面端开的会话在 web 上显示成一串 id"；归属那一维照旧由 ``user_id`` 判。
    """
    try:
        return services.workspaces.get(
            workspace_id, user_id=caller.owner_id, any_device=True
        ).name
    except Exception:
        logger.info("读工作区失败：%s", workspace_id, exc_info=True)
        return ""


def _skills(services: Services) -> _CommandResult:
    """``/skills``：列出可用技能，并说清**每个技能都能直接当命令用**。

    照 Claude 的 ``/skills`` 与 QwenPaw 的"技能目录名即命令"（调研报告 §3 第 4 条）。
    数据来自**命令表里的技能那批**（``CommandService.skill_commands``）而不是技能
    注册表：这样"能不能敲、敲出来是什么"与菜单里那一份天然一致——两处各读一遍
    磁盘就会出现"这里说能用、菜单里没有"。

    三种状态分开说（能用 / 被同名命令遮蔽 / 名字不能当命令），因为**处置各不相同**：
    前一种是换个名字，后一种是改用 ``/skill``。混成一句"不可用"等于什么都没说。
    """
    items = services.commands.skill_commands()
    if not items:
        return _CommandResult(
            name=commands.NAME_SKILLS,
            ok=False,
            text=(
                "还没有扫到任何技能：把技能目录放进 data/skills/，"
                "或者在「能力」页里装一个（技能自带 SKILL.md）。"
            ),
        )
    usable = [item for item in items if item.usable]
    blocked = [item for item in items if not item.usable]
    lines = [
        f"可用技能 {len(usable)} 个。每个技能都能直接当命令用：/技能名 [任务]，"
        "与 /skill 技能名 [任务] 是同一条路（正文注入这一轮）。"
    ]
    lines.extend(f"- {item.usage} — {item.summary}" for item in usable)
    if blocked:
        lines.append("下面这些暂时敲不出来（技能本身还在）：")
        for item in blocked:
            if item.shadowed_by:
                lines.append(
                    f"- /{item.name} 被 /{item.shadowed_by} 遮蔽了（同名取优先级最高的那条）："
                    f"用 /skill {item.skill} [任务] 调它。"
                )
            else:
                lines.append(f"- {item.skill}：{item.error}")
    lines.append("技能正文只在用到时才展开（渐进披露），所以装得多也不会一直占着上下文。")
    return _CommandResult(name=commands.NAME_SKILLS, text="\n".join(lines))


#: ``/model`` 的用法那一行（列清单、认不出名字、没有会话时都要说一遍）。
_MODEL_USAGE = "/model <模型名>：模型 ID（形如 gpt-4o）或 pk（mdl_ 开头那个）都认。"


class _ModelChoice(NamedTuple):
    """``/model`` 清单里的一行：**三个名字都要留着**。

    ``pk`` 是写进会话记录的那一个（界面传下来的是它）；``model_id`` 是用户手打的那一个
    （``gpt-4o`` 这种，他多半不知道 pk）；``label`` 是给人看的那个（可能是"另一家"）。
    只留两个的话，配过 label 的模型就会"显示得出来、却打不进去"——这条用例踩过一次。
    """

    pk: str
    model_id: str
    label: str


def _chat_models(services: Services) -> list[_ModelChoice]:
    """可选的对话模型。

    **筛选口径与前端 ModelPicker 逐字一致**（``stores/modelRegistry.ts`` 的
    ``chatModels`` getter：供应商启用 + 能力为空或含 ``chat``）。两处各筛一份的话，
    "界面上选得到、命令里列不出来"这种不一致迟早出现。
    """
    enabled = {item.id for item in services.models.list_providers() if item.enabled}
    options: list[_ModelChoice] = []
    for model in services.models.list_models():
        if model.provider_id not in enabled:
            continue
        # 空 ``capabilities`` = "没声明"（旧数据），与前端同一个宽容口径
        if model.capabilities and "chat" not in model.capabilities:
            continue
        options.append(
            _ModelChoice(pk=model.id, model_id=model.model_id, label=model.label or model.model_id)
        )
    return options


def _model_label(services: Services, model_pk: str) -> str:
    """一个 pk 的显示名；查不到回空串。

    **不把"查不到"编成一句话在这里说**：调用方才知道该说"你打的名字不认识"还是
    "这个模型已经不在注册表里了"。
    """
    if not model_pk:
        return ""
    try:
        model = services.models.get_model(model_pk)
    except NotFoundError:
        return ""
    return model.label or model.model_id


def _current_model(services: Services, payload: ChatRequestIn) -> tuple[str, bool]:
    """这条会话现在用哪个模型，以及它是**跟来的**还是**自己选的**。

    优先级与 ``_effective_model`` 一致：**请求里的 > 会话已存的 > 全局默认**。
    前两者是这条会话自己的选择；最后那个（注册表里绑定给 ``chat`` 用途的）是"跟随"，
    回话里必须把这两者分开说——不说的话，用户会以为自己选过了，其实是在跟。
    """
    if payload.model_pk:
        return payload.model_pk, False
    if payload.conversation_id:
        try:
            stored = services.conversations.get(payload.conversation_id).model_pk
        except Exception:
            # 读不出来（不存在 / 越主）就当没选：``_require_conversation`` 已经挡过一道
            logger.info("读会话的模型失败：%s", payload.conversation_id, exc_info=True)
            stored = None
        if stored:
            return stored, False
    return str(services.models.bindings().get("chat", "")), True


def _match_model(
    options: list[_ModelChoice], wanted: str
) -> tuple[str, list[_ModelChoice]]:
    """按名字找一个模型：``(命中的 pk, 同级的其它候选)``；没命中时第一项是空串。

    三级，先精确后宽容——用户手打的多半是**模型 ID**，而界面传下来的是 **pk**，
    两个都要认：pk → 模型 ID → ID/显示名（忽略大小写）。同级命中多个时**不替用户猜**
    （同一个模型 ID 挂在两家供应商名下是真会发生的），把候选回给他。
    """
    for matched in (
        [item for item in options if item.pk == wanted],
        [item for item in options if item.model_id == wanted],
        [item for item in options if wanted.lower() in (item.model_id.lower(), item.label.lower())],
    ):
        if len(matched) == 1:
            return matched[0].pk, []
        if len(matched) > 1:
            return "", matched
    return "", []


def _choice_line(choice: _ModelChoice, *, current: bool) -> str:
    """清单里的一行：显示名 + **两个能用来指定的名字** + 是不是现在这个。"""
    names = f"{choice.model_id}，{choice.pk}"
    return f"- {choice.label}（{names}）{' ← 现在这个' if current else ''}"


def _models_text(
    services: Services, current: str, followed: bool, options: list[_ModelChoice]
) -> str:
    """``/model`` 不带参数时回的那段：**现在用哪个 + 可选清单 + 怎么切**。

    "现在"分三种说清楚：会话自己选的 / 跟随全局默认的 / 一个都没有的（走设置页那套）。
    再加一条：当前那个**已经不在可选清单里**时（供应商停用或模型被删）点一句——
    不说的话，用户会以为它仍然是可选项。
    """
    label = _model_label(services, current)
    if not current:
        head = "这条会话还没选模型：跟随设置页那套配置（注册表里也没绑定 chat 用途）。"
    elif followed:
        head = f"现在跟随全局默认：「{label or current}」（{current}）。"
    else:
        head = f"现在这条会话用的是「{label or current}」（{current}）。"
    lines = [head]
    if options:
        lines.append("可选的对话模型：")
        lines.extend(_choice_line(item, current=item.pk == current) for item in options)
    else:
        lines.append("还没有登记过任何能对话的模型（设置页 → 模型注册器里加一个）。")
    if current and current not in {item.pk for item in options}:
        lines.append(f"注意：「{current}」不在可选清单里（供应商停用或模型已删），换一个吧。")
    lines.append(_MODEL_USAGE)
    return "\n".join(lines)


def _switch_model(
    services: Services, payload: ChatRequestIn, parsed: commands.ParsedCommand
) -> _CommandResult:
    """``/model [模型名]``：看或换**这条会话**的对话模型（照 QwenPaw 的 ``/model``）。

    不带参数列清单；带参数就切。**写用的是 ``ConversationService.set_model``**——
    界面上换模型走的是 ``_effective_model`` → 同一个方法，所以"界面换的"与"命令换的"
    落在会话记录同一栏里，不会出现两处各记一份、以谁为准说不清。

    切换只写会话记录、**不写注册表的槽位绑定**：``/model`` 是"这条会话用哪个"，
    改全局默认是设置页的事（注册表那套是"没选时跟谁"）。
    """
    current, followed = _current_model(services, payload)
    options = _chat_models(services)
    pieces = parsed.args.split()
    if not pieces:
        return _CommandResult(
            name="model", text=_models_text(services, current, followed, options)
        )
    wanted = pieces[0]
    found, candidates = _match_model(options, wanted)
    if not found:
        if candidates:
            names = "、".join(item.pk for item in candidates)
            return _CommandResult(
                name="model",
                ok=False,
                text=f"有多个模型叫「{wanted}」：{names}。用 pk 指定其中一个。\n{_MODEL_USAGE}",
            )
        return _CommandResult(
            name="model",
            ok=False,
            text=f"没有叫「{wanted}」的对话模型。\n"
            + _models_text(services, current, followed, options),
        )
    if found == current and not followed:
        return _CommandResult(
            name="model", text=f"这条会话已经在用「{_model_label(services, found)}」了，没有改动。"
        )
    if not payload.conversation_id:
        # 模型是**随会话保存**的：没有会话就没有可写的地方，如实说（与 /compact 同一口径）
        return _CommandResult(
            name="model",
            ok=False,
            text="没有会话可写：模型选择是随会话保存的，先在对话页里提问再换，或者在输入框右侧选。",
        )
    try:
        services.conversations.set_model(payload.conversation_id, found)
    except Exception as exc:
        logger.warning("写会话模型失败：%s", payload.conversation_id, exc_info=True)
        return _CommandResult(name="model", ok=False, text=f"没能写进这条会话：{exc}")
    label = _model_label(services, found) or found
    return _CommandResult(
        name="model",
        text=f"这条会话的模型已换成「{label}」（{found}）。下一轮用它。",
        # **界面据此把选择器同步过去**：输入框右侧那个 ModelPicker 与界面自己切的
        # 模型是同一份值，不带回来的话它显示的还是旧模型，而下一条消息会照它把旧模型
        # 再写回会话（``_effective_model`` 以请求里的为准）——刚切的那次就白切了。
        # 从文案里认 pk 是不行的（那一句是给人读的，措辞随时会改）。
        action={"kind": "model", "model_pk": found},
    )


def _switch_mode(
    services: Services, payload: ChatRequestIn, parsed: commands.ParsedCommand
) -> _CommandResult:
    """``/mode [档名]``：切**任务模式**（目标 / 计划；不带参数就报当前档）。

    三件事按顺序做，顺序不能换：**先读旧档**（写入之后就没有"旧档"了）→ 写设置 →
    记账（会话事件 + 观测表）。漏掉记账的话，下一轮会再补一条重复的 ``mode/changed``。
    """
    current = services.chat.current_mode()
    pieces = parsed.args.split()
    if not pieces:
        label = modes.MODE_DEFS[current].label
        return _CommandResult(
            name="mode",
            text=(
                f"现在是「{label}」档（{current}）。\n{commands.modes_text()}\n"
                "切换：/mode goal|plan"
            ),
        )
    wanted = pieces[0].lower()
    if wanted not in commands.MODE_VALUES:
        return _CommandResult(
            name="mode",
            ok=False,
            text=(
                f"不认识的档：{wanted}。可用的是 {'、'.join(commands.MODE_VALUES)}。\n"
                f"{commands.modes_text()}"
            ),
        )
    if wanted == current:
        return _CommandResult(
            name="mode", text=f"已经是「{modes.MODE_DEFS[wanted].label}」档了，没有改动。"
        )
    services.runtime.set({"chat.mode": wanted})
    if payload.conversation_id:
        _record_mode_change(services, payload.conversation_id, previous=current, mode=wanted)
    return _CommandResult(
        name="mode",
        text=(
            f"已切到「{modes.MODE_DEFS[wanted].label}」档（{wanted}）："
            f"{modes.MODE_DEFS[wanted].hint}。下一轮生效。"
        ),
        action={"kind": "mode", "mode": wanted, "previousMode": current},
    )


def _record_mode_change(
    services: Services, conversation_id: str, *, previous: str, mode: str
) -> None:
    """把一次**在会话里当场切的**模式切换写进会话日志（``source="command"``）。

    抄的是 ZCode 的 ``SessionModeChanged``（调研报告 §2.6 抄点第 4 条）：
    带 ``previousMode`` 的事件让"它是什么时候开始不问我的"可查、可撤销、可供界面动画。

    best-effort：日志写不进去不该让"切模式"这件已经生效的事回一个失败。
    """
    try:
        services.conversations.append_events(
            conversation_id,
            [
                mode_changed_draft(
                    previous_mode=previous, mode=mode, source=commands.MODE_SOURCE_COMMAND
                )
            ],
        )
    except Exception:
        logger.warning("写 mode/changed 事件失败：%s", conversation_id, exc_info=True)
    # 顺手同步观测表：下一轮就不会再补一条重复的 settings 来源事件
    services.commands.mode_watch.note(conversation_id, mode)


def _enter_plan_mode(
    services: Services, payload: ChatRequestIn, parsed: commands.ParsedCommand
) -> _CommandResult:
    """``/plan [描述]``：``/mode plan`` 的**语义化入口**（照 QwenPaw 的 ``/plan``）。

    两件事，顺序不能换：**先切档**（写设置 + 记 ``mode/changed``，与 ``/mode`` 共用
    ``_record_mode_change``——另写一份的话，"从哪一档切过来的 previousMode"会有两种口径）
    → 再看有没有描述：

    - **有描述**：那段描述就是**这一轮的提示**（与 ``/skill`` 同一条改写法），于是这一轮
      照常过模型、照常留回答；plan 档的门闸会把写类工具拦下并把"为什么"回灌给模型
      （见 ``services/plan_gate.py``），所以它先给的是计划、等的是对方那句确认。
    - **没有描述**：当场答一句（不碰模型）——它就是 ``/mode plan``，不需要为一次切档
      花一次模型调用。

    已经在 plan 档时**不重复记事件**（``/mode`` 也是这个口径：没改动就没有事件），
    但描述照样当提示——"再规划一次"是个合理用法。
    """
    wanted = parsed.args.strip()
    current = services.chat.current_mode()
    switched = current != modes.MODE_PLAN
    if switched:
        services.runtime.set({"chat.mode": modes.MODE_PLAN})
        if payload.conversation_id:
            _record_mode_change(
                services, payload.conversation_id, previous=current, mode=modes.MODE_PLAN
            )
    definition = modes.MODE_DEFS[modes.MODE_PLAN]
    if wanted:
        # 切档已经生效，这一轮开跑时读到的就是 plan（``_events`` 里那一次 `current_mode`）
        return _CommandResult(name="plan", prompt=wanted)
    head = (
        f"已切到「{definition.label}」档（{modes.MODE_PLAN}）：{definition.hint}。{definition.detail}"
        if switched
        else f"已经是「{definition.label}」档了。"
    )
    return _CommandResult(
        name="plan",
        text=f"{head}\n带上描述直接开始：/plan <描述>（描述会作为这一轮的提示，模型先给计划）。",
        action={
            "kind": "mode",
            "mode": modes.MODE_PLAN,
            "previousMode": current,
        },
    )


def _note_turn_mode(
    services: Services, sink: _TurnSink, conversation_id: str | None, mode: str
) -> None:
    """**这一轮开始时**看档换过没有，换过就补一条 ``mode/changed``（P1-1 遗留 #6）。

    设置页与输入框那一排的控件改档时手里**没有会话**（模式是应用级配置），所以在那些
    地方写不出事件；这里用"上一轮是哪一档"作基线把它补上（``source="settings"``）。
    基线来自 ``ModeWatch``（首次遇到一条会话时从日志里读一次），见
    ``services/commands.ModeWatch``。

    加在 ``turn/start`` **之前**：事件的顺序就是"先换了档，这一轮才以新档开跑"。
    """
    previous = services.commands.mode_watch.observe(conversation_id or "", mode)
    if previous:
        sink.note_mode_change(previous=previous, mode=mode, source=commands.MODE_SOURCE_SETTINGS)


def _command_events(
    services: Services, payload: ChatRequestIn, result: _CommandResult
) -> Iterator[str]:
    """短路类命令的事件流：**一条 ``command`` + 一条 ``done``**，没有别的。

    形状刻意与正常那一轮一致（同样以 ``done`` 收尾）：前端那条读流的循环只有一份，
    命令只是"内容不同的一次流"。``done`` 的 ``answer`` 是**空串**——命令不产生回答，
    界面据此不建 assistant 气泡；消息也不会落库，这条路根本不碰 ``_record_turn``
    （这就是"不进模型历史"）。

    载荷由 ``ChatCommandEventOut`` 拼（契约一个落点，见那个类）：
    ``action`` 与 ``refill`` **没有就不出现**——老客户端不认 ``refill`` 时
    行为一个字都不变（``/rewind`` 会带上被撤掉的那句提问，供界面回填输入框）。
    """
    _record_command(services, payload, result)
    yield _sse(
        ChatCommandEventOut(
            name=result.name,
            text=result.text,
            ok=result.ok,
            action=result.action,
            refill=result.refill,
        ).wire()
    )
    yield _sse({"type": "done", "answer": ""})


def _record_command(services: Services, payload: ChatRequestIn, result: _CommandResult) -> None:
    """把这一条命令记进会话日志（DSH 的「命令执行写 session log 但不进模型历史」）。

    只写 ``command`` 这一条事件：它**不是一轮问答**，所以没有 ``turn/start`` /
    ``turn/end`` 把它包起来（那两条的含义是"这一轮问的是什么、怎么结束的"，
    套在这里会让回看的人以为模型回答过什么）。没有会话（无状态调用）时不记——没地方记。
    """
    if not payload.conversation_id:
        return
    parsed = commands.parse(payload.query)
    try:
        services.conversations.append_events(
            payload.conversation_id,
            [
                command_draft(
                    name=result.name,
                    args=parsed.args if parsed else "",
                    result=result.text,
                    ok=result.ok,
                )
            ],
        )
    except Exception:
        # best-effort：日志写不进去不该让用户拿不到那句回答
        logger.warning("写 command 事件失败：%s", payload.conversation_id, exc_info=True)


@router.get(
    "/chat/commands",
    response_model=CommandListOut,
    summary="可用命令（内置 + 自定义 + 技能，被遮蔽的也在里面）",
)
def list_commands(
    services: Services = Depends(get_services),
    _: Caller = Depends(require_read),
) -> CommandListOut:
    """斜杠命令的目录：前端那个 ``/`` 菜单就吃这一份（P1-2 第 4 条）。

    **只挂本机档**（``router.py`` 的 ``local_router``；服务器那一份 2026-10-05 摘掉，
    理由见模块头那一行）：这里列的是**这台机器上的**命令与技能，执行那一轮也在本机。

    三条与界面直接相关的约定：

    1. **``{name, summary, usage, group}`` 四个字段是给菜单的**（``group`` 是**菜单
       分组**：``builtin`` / ``user`` / ``repo`` / ``skill``，见 ``CommandDef.group``
       ——技能那批是单独一档，不再借技能自己的发现源分组），其余字段是顺带给出的排错信息；
    2. **被遮蔽的与加载失败的都在列表里**（``shadowed_by`` / ``error``，与插件列表
       同一套做法）：静默藏掉会让用户以为文件没生效，而原因只有这里知道；
    3. **``short_circuit`` 只是"这条通常要不要模型"的说明**：为真的是 ``/help`` ``/mode``
       这一类，为假的是改写类（``/skill``、**技能自己的那条命令**与自定义 md 命令）。
       **界面不据它分流**——它是**表级**的保守口径，判不出 ``/plan`` 这种
       "看有没有参数"的两面派；真正的判据是这一轮的结果（见
       ``_CommandResult.short_circuit`` 与前端 ``ChatView.commandProducedContent``）。

    **技能也是命令**（``/<技能名> [任务]``，照 Claude 的"命令＝技能"）：它们单独成组
    （``group="skill"``，菜单里排在"内置 / 你放的 / 随代码发布"之后）——混在那三档里时
    ``/rewind`` ``/status`` ``/skills`` 这些会话动作会被二十多条命令挤出首屏，而技能
    本该是一眼可辨的一类。摘要也跟着收短：见 ``CommandOut.summary``。
    """
    items = services.commands.list()
    return CommandListOut(
        items=[
            CommandOut(
                name=item.name,
                summary=item.summary,
                usage=item.usage or f"/{item.name}",
                group=item.group,
                details=list(item.details),
                argument_hint=item.argument_hint,
                short_circuit=item.short_circuit,
                shadowed_by=item.shadowed_by,
                error=item.error,
                path=item.path,
            )
            for item in items
        ],
        total=len(items),
        user_dir=str(services.commands.user_dir),
        builtin_dir=str(services.commands.builtin_dir),
    )


__all__ = ["router"]
