"""执行：把沙箱端点的那套闸接到**模型能调的工具**上（v0.33）。

`api/v1/sandbox.py` 那条路是"用户自己点一条命令去跑"；这一条是"模型想跑"。
三道闸一层不省，与端点同源：

| 闸 | 在哪 | 不通过时 |
| --- | --- | --- |
| 权限 | ``Caller.is_admin`` | 拒绝，并说明执行只对管理员开放 |
| 策略 | ``command_policy`` 的规则 + ``sandbox.exec_policy`` 总开关 | 拒绝，并说明怎么放开 |
| 内核隔离 | ``isolation.detect()`` / ``run_isolated`` | 拒绝——**不回退成裸跑** |

**``ask`` 这一档在这里是"真的会问"**（v0.41）。端点那条路一次请求就是一次执行，
所以它那里的 ask 是"回 409，界面确认后带 ``approved`` 重调"；对话是一条连续链路，
没有第二次请求可以承载那个决定，于是这里的 ask 走
``services/approvals``：登记一条待确认，由工具循环把它发到界面上（SSE）、
**停下来等**（见 ``tool_loop._perform``），拿到决定之后带着 ``approval=…``
把这一步重跑一遍。三条边界：

- **先发事件、再阻塞**：等待发生在生成器**被恢复之后**——反过来的话界面根本收不到
  那个询问，两边一起等死（那条死锁的形状记在 ``agent_tools._call_mcp`` 的说明里）；
- **等不到按"没批准"处理**（``approvals.TIMEOUT``），并**如实告诉模型"对方没有回应"**：
  说成"对方拒绝了"会让它以为对方看过并否了，下一轮的措辞就说错话；
- **没有人可以问的链路上仍然是"拒绝并说清"**（定时任务走 ``approvals.UNAVAILABLE``）：
  那条链路没有界面，等满 120 秒只会白占一个消费者线程。

默认档就是 ``ask``，所以"接上就能跑命令"这件事仍然不会发生——用户点过的那一下
才是允许；而"这轮用不了"的出路（改总开关、加放行规则）照旧写进回给模型的话里。
"""

from __future__ import annotations

import logging
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.caller import Caller
from app.core.exceptions import InvalidRequestError
from app.services import approvals as approval_service
from app.services import isolation as isolation_service
from app.services import modes
from app.services.agent_files import Roots, describe_roots, resolve_roots
from app.services.approvals import ApprovalRequest
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    append_allow_rule,
    rules_from_runtime,
    suggest_rule,
    tool_arguments,
)
from app.services.sandbox import POLICY_ASK, POLICY_DENY, require_isolation, sandbox_for

if TYPE_CHECKING:
    # 只为类型标注：**运行时不导入**，免得与组合根（core.services）成环
    from app.core.services import Services

__all__ = ["MAX_TIMEOUT_SECONDS", "ExecOutcome", "run_command"]

logger = logging.getLogger(__name__)

#: 模型可以要求的超时上限（秒）。默认取隔离层自己的 20 秒。
#:
#: 为什么给到 60：跑一次构建、装一次依赖确实要更久。为什么**不给更多**：
#: 整轮还有墙钟闸（``tool_loop.DEFAULT_MAX_SECONDS`` = 300 秒），而同一批里的
#: 几条命令是并发的——允许单条跑几百秒，等于让这一轮的其余部分全被它拖住。
MAX_TIMEOUT_SECONDS = 60

#: 隔离探测的缓存时长（秒）。``detect()`` 在装了 docker 的机器上会**真的去问一次
#: daemon**（那是一次子进程调用），而它每个 run_command 都要用一次。缓存 30 秒是
#: 一个折中：一轮对话里不会重复探测，而"刚装完 docker"最迟半分钟就能用上，
#: 不必重启进程。设置页那个能力端点不走这里，它每次都真探（那是用户主动点"看看现在有什么"）。
_ISOLATION_TTL_SECONDS = 30.0

_cached_isolation: tuple[float, isolation_service.Isolation] | None = None


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """一次执行的结果（或"为什么没执行"）。"""

    ok: bool
    """真的跑起来了且退出码为 0。**没执行也是 False**——两者在界面上要分开显示。"""
    text: str
    """回给模型的文本（跑成了是输出，没跑成是原因 + 怎么放开）。"""
    summary: str
    """过程面板上那一行。"""
    ran: bool = False
    """是否真的起了进程。``False`` 时 ``exit_code`` 无意义。"""
    exit_code: int | None = None
    approval: ApprovalRequest | None = None
    """这条命令**在等用户点头**（``ask`` 档，v0.41）。

    非空时这一轮**什么都没跑**：工具循环拿它发一条 approval 事件、停下来等人回答，
    拿到决定之后再带着 ``approval=…`` 把这一步重跑一遍（见 ``tool_loop._perform``）。
    所以 ``text`` 在这个阶段只是一句占位——它不会成为回给模型的那句话。
    """


def _detect(*, force: bool = False) -> isolation_service.Isolation:
    global _cached_isolation
    now = time.monotonic()
    if not force and _cached_isolation is not None:
        stamp, found = _cached_isolation
        if now - stamp < _ISOLATION_TTL_SECONDS:
            return found
    found = isolation_service.detect()
    _cached_isolation = (now, found)
    return found


def run_command(
    services: Services,
    caller: Caller,
    *,
    conversation_id: str | None,
    args: dict[str, object],
    approval: str | None = None,
) -> ExecOutcome:
    """跑一条命令（或说清为什么不跑）。

    ``approval`` 是**工具循环带回来的那个决定**（``services/approvals`` 里的取值）：

    - ``None`` = 还没有人问过，而这一档又需要问 → 登记一条待确认，这次不执行；
    - ``allow_once`` / ``allow_always`` = 对方点了同意 → 照跑（后者顺手写下放行规则）；
    - ``deny`` / ``timeout`` / ``unavailable`` = 没拿到许可 → 不跑，并说清是哪一种。

    **没见过的取值一律当"没许可"**：这条路上放行必须是明确的，不能靠"没匹配上"。
    """
    argv = _argv_of(args)
    timeout = _timeout_of(args)
    allow_network = bool(args.get("allow_network") is True)

    # 闸 1：权限。**与端点同一档**（require_admin）：它不是"读写知识库"级别的能力，
    # 而是"在这台机器上执行代码"。成员账号的模型不该绕过它。
    if not caller.is_admin:
        return ExecOutcome(
            ok=False,
            ran=False,
            text=(
                "执行命令只对管理员开放，当前是成员账号，这一轮没有执行。"
                "需要的话请把这件事告诉对方（换管理员账号，或者让他自己跑）。"
            ),
            summary="成员账号不能执行命令",
        )

    # 闸 2：策略。deny 优先（规则或权限档），命中 allow 才是放行；
    # 没命中规则就用**权限档**——与端点的判定顺序逐条对齐（那里有两段说明为什么）。
    #
    # 权限档怎么落到命令这一侧（2026-09-29 改成四档 + 抄 Codex 的 Auto 口径，
    # 见 services/modes.py 那段"抄成熟的"注释）：
    #   仅查看     → 不跑（下一条直接拒，并把"这一档不许动东西"说清楚）
    #   手动批准   → 问一句（每条都问）
    #   默认（智能）→ **工作区内不问；要出工作区 / 要联网 / 剥不干净包装器才问**
    #                 （判定在 modes.command_needs_ask，纯函数）
    #   全自动     → 直接跑
    #
    # ⚠️ 这里**拿不到工作区根**（roots 要到本函数后面才解析，见 `resolve_roots`），
    # 所以按"相对路径算工作区内、绝对路径算外面"的保守口径判——绝对路径会多问一句，
    # 而"多问一句"是安全的那一侧 ✓。
    arguments = tool_arguments(argv)
    decision = rules_from_runtime(services.runtime, source="执行").decide("Bash", arguments)
    permission = modes.coerce_permission(services.runtime.get("chat.permission"))
    command = " ".join(argv)
    global_mode = {
        modes.PERMISSION_VIEW: POLICY_DENY,
        modes.PERMISSION_MANUAL: POLICY_ASK,
        modes.PERMISSION_SMART: (
            POLICY_ASK if modes.command_needs_ask(command, permission) else ACTION_ALLOW
        ),
        modes.PERMISSION_FULL: ACTION_ALLOW,
    }[permission]
    if decision.action == ACTION_DENY:
        return _refused(
            f"这条命令被拒绝规则拦下：{decision.reason}。换一条路，不要重试这条。",
            gate="拒绝规则拦下",
        )
    if global_mode == POLICY_DENY:
        return _refused(
            "这一轮的权限是「仅查看」：不改动任何东西，也不执行命令。"
            "请如实告诉对方：要让我能跑命令，得把权限改成「手动批准」「默认」或「全自动」"
            "（输入框那一排的「权限」，或设置 → 聊天）。",
            gate="权限档为「仅查看」",
        )
    # **命中规则时规则说了算，没命中才用总开关**（与端点逐条对齐）：
    # 少了这半句，"总开关设成允许"这件事会变成 no-op——用户改完照样被拒，
    # 而界面上写着"允许"，那种不一致最难查（第一版就是这么错的，用例抓住了）。
    mode = decision.action if decision.rule is not None else global_mode
    rule = suggest_rule("Bash", arguments).describe()
    if mode not in (ACTION_ALLOW, ACTION_ASK):
        # ``sandbox`` 那一档（以及任何没见过的取值）在这里**与今天一样不放行**。
        # 端点那条路把 sandbox 当"照跑"，两处的口径本来就不一致——那是策略层
        # 该单独定的一件事，不该由"把 ask 做成真的会问"这一次改动顺手改掉。
        return _needs_confirm(rule)

    # 闸 3：内核隔离。没有可用后端时**默认降级为直接执行**（v0.55），
    # 除非设置里打开了「无隔离时拒绝执行」——那时按纪律拒绝，不回退成裸跑。
    #
    # **它排在"问用户"之前**：严格模式下没有隔离就根本没有跑起来的可能，
    # 先问等于让对方白点一次（点完他还是看到这句）。三道闸一道没少，只是换了个顺序。
    #
    # 为什么默认降级（而不是照旧一律拒绝）：Windows 上没有 bwrap / sandbox-exec 的
    # 原生等价物、容器里也常没挂 docker，一律拒绝会让**本地源码启动与容器部署两边
    # 都跑不了命令**（用户报的"明明指定了工作区，为啥还是不能执行工具"）。
    # 降级时**如实标注未隔离**（见 `isolation.direct_isolation`），风险摆明面上。
    found = _detect()
    if not found.available:
        if require_isolation(services.runtime):
            return ExecOutcome(
                ok=False,
                ran=False,
                text=(
                    "这台机器上没有可用的内核级隔离，而设置里开着「无隔离时拒绝执行」，"
                    f"所以这一轮没有跑。{found.detail}"
                    "要跑命令：装 Docker（或 Linux 上的 bubblewrap），"
                    "或者把设置里那一项关掉（关掉即降级为直接执行，风险更高）。"
                ),
                summary="这台机器没有隔离，拒绝执行",
            )
        found = isolation_service.direct_isolation()

    if mode == ACTION_ALLOW:
        pass  # 规则命中或总开关放行：不必问
    elif approval in (approval_service.ALLOW_ONCE, approval_service.ALLOW_ALWAYS):
        if approval == approval_service.ALLOW_ALWAYS:
            # 「这类都允许」：把建议的那条规则写进放行清单，**之后同类调用不再问**。
            # 先写再跑：这一次跑失败了也不该让"以后都允许"这一下白点
            append_allow_rule(services.runtime, suggest_rule("Bash", arguments))
    elif approval is None:
        return _awaiting(
            services, arguments=arguments, rule=rule, found=found, allow_network=allow_network
        )
    else:
        return _not_approved(approval, rule)

    roots = resolve_roots(services, conversation_id=conversation_id, caller=caller)
    box, root = _roots_for_run(services, roots, conversation_id)
    try:
        result = isolation_service.run_isolated(
            argv,
            workspace_root=root,
            sandbox_dir=box,
            timeout=timeout,
            allow_network=allow_network,
            isolation=found,
            bind_ro=isolation_service.bind_paths_from(services.runtime.get("sandbox.bind_ro")),
        )
    except Exception as exc:
        logger.info("隔离执行失败：%s", exc)
        return ExecOutcome(
            ok=False,
            ran=False,
            text=f"命令没能跑起来：{exc}",
            summary="命令没能跑起来",
        )

    return ExecOutcome(
        ok=result.ok,
        ran=True,
        exit_code=result.exit_code,
        text=_render(argv, result, roots=roots, allow_network=allow_network, timeout=timeout),
        summary=_summary(result),
    )


def _roots_for_run(
    services: Services, roots: Roots, conversation_id: str | None
) -> tuple[Path, Path]:
    """``(沙箱目录, 隔离里当"工作区"挂的那个根)``。

    **没挂工作区时根就是沙箱自己**，而不是 ``data/sandbox`` 这一层：那一层里
    装着**其它会话**的沙箱（端点的默认值是它，因为那条路没有会话），
    挂进去等于让这一轮的模型能读到别人的试错目录。
    """
    box = sandbox_for(services.runtime.data_dir, conversation_id or "adhoc").ensure()
    return box, (roots.workspace or box)


def _argv_of(args: dict[str, object]) -> list[str]:
    """取命令。``argv`` 优先（数组，不做任何解析）；只给了 ``command`` 字符串时
    用 ``shlex`` 拆一次，并把拆出来的结果回显给模型——拆错了它下一轮能自己改。

    执行侧用的一律是**数组**（``subprocess`` 不走 shell）：这是端点的口径，
    也是这里唯一的口径。字符串进来只是为了让模型少踩一次"参数形状不对"。
    """
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        argv = [str(item) for item in raw]
    else:
        text = str(args.get("command") or "").strip()
        if not text:
            raise InvalidRequestError("缺少参数：command（或用 argv 给一个数组）")
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            raise InvalidRequestError(f"命令拆不开：{exc}") from exc
    if not argv or any(not item for item in argv):
        raise InvalidRequestError("命令是空的")
    if len(argv) > 64:
        raise InvalidRequestError("参数太多了（上限 64 个）")
    return argv


def _timeout_of(args: dict[str, object]) -> float:
    raw = args.get("timeout_seconds")
    try:
        value = float(raw) if raw is not None else isolation_service.DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        raise InvalidRequestError("timeout_seconds 要是数字") from None
    if value <= 0:
        raise InvalidRequestError("timeout_seconds 要大于 0")
    return min(value, float(MAX_TIMEOUT_SECONDS))


def _refused(reason: str, *, gate: str = "权限档拦下") -> ExecOutcome:
    """不执行的统一形状；``gate`` 说清**是哪一道闸拦的**。

    原先这一格一律是"策略拦下"：它把"拒绝规则""权限档不让跑""对方没批准"
    "需要确认但没有可确认的入口"四种原因糊成同一句话——用户在过程面板那一行看到的
    因此答不出"到底是谁拦的、我该去改哪里"（模式那道闸另有一句，见 ``tool_loop``）。
    回给模型的 ``text`` 本来就说得很细（三档拒批分开写），这里只是让它也进摘要。
    """
    return ExecOutcome(ok=False, ran=False, text=reason, summary=f"没有执行（{gate}）")


def _how_to_open(rule: str) -> str:
    """**每条"没执行"的回话都要带上这两条出路。**

    只说"不允许"的报错等于没说：模型只能反复重试同一件事，而界面上的用户
    根本不知道要去改哪个设置（这一条是从第一版就有的口径）。

    2026-09-27：那句"去设置 → 沙箱执行改策略"改成**权限档**的说法——"命令执行策略"
    那一项已经折进权限轴，指向一个不存在的设置项等于把用户引沟里。
    2026-09-29：权限轴改成四档（仅查看 / 手动批准 / 默认 / 全自动），这句话跟着换名字。
    """
    return (
        "要让我跑命令，得把权限改成「手动批准」「默认」或「全自动」"
        f"（输入框那一排的「权限」，或设置 → 聊天），或者在放行清单里加一行 `{rule}`"
        "（加完只放行这一族命令，其它仍然要确认）。"
    )


def _needs_confirm(rule: str) -> ExecOutcome:
    """没拿到许可、也没处去问时的那句回话（``sandbox`` 档与"没有人可问"的链路共用）。"""
    return _refused(
        "执行需要对方先确认，**这一轮没有执行**。"
        f"请如实告诉对方：{_how_to_open(rule)}"
        "**不要假装执行过，也不要凭猜测编造命令的输出。**",
        gate="需确认，但这一轮没有可确认的入口",
    )


def _not_approved(approval: str, rule: str) -> ExecOutcome:
    """对方没给许可：不执行，并**说清是哪一种"没给"**。

    三种必须分开说，因为模型下一轮该讲的话不一样：拒绝了（别再提这条命令）、
    没有回应（可以说"刚才那条我没等到你确认"）、没有人可以问（定时任务那条链路，
    得让用户自己知道"它跑不了"）。混成一句"被策略拦下"就全错了。

    摘要（过程面板那一行）也按这三种分开：那一行原来只写"策略拦下"，
    而"对方点了拒绝"与"没人可问"在界面上要做的下一步完全不同。
    """
    if approval == approval_service.DENY:
        head = "对方**拒绝**了这次执行"
        gate = "对方没批准：拒绝"
    elif approval == approval_service.TIMEOUT:
        head = "**对方一直没有回应**（等到超时），按没有批准处理"
        gate = "对方没批准：等不到回应"
    else:
        # **这条链路没人可确认 ≠ 用户拒绝**（2026-09-29 用户原话："就算是拒绝，也应该
        # 由用户来，不能直接拒绝"）。这里如实说"在等你确认、只是此刻没人可确认"，
        # **不替用户做决定** ✗ —— 措辞里不许出现"没批准/没有批准"（那等于替他说了拒绝），
        # 但**结论不变**：没有许可就一次都不执行 ✓（`_refused` 的 calls 为空 ✓）。
        head = "**待确认**：这条链路上此刻没有人可以确认（这条链路没有界面可问）"
        gate = "待确认：这条链路没人可确认"
    return _refused(
        f"{head}，**这一轮没有执行**。不要重试这条命令，也不要假装执行过。{_how_to_open(rule)}",
        gate=gate,
    )


def _awaiting(
    services: Services,
    *,
    arguments: str,
    rule: str,
    found: isolation_service.Isolation,
    allow_network: bool,
) -> ExecOutcome:
    """登记一条待确认，**这次不执行任何东西**。

    真正的执行发生在拿到决定之后（工具循环会带着 ``approval=…`` 再调一次），
    所以这里返回的 ``text`` 只是一句占位——它不会成为回给模型的那句话。
    """
    request = services.approvals.open(
        tool="run_command",
        # 标题与过程面板用的是同一句话（见 tool_loop._LABELS）：用户在确认条上看到的
        # 与那一行步骤是同一个动作，两处措辞不同会让人以为是两件事
        label="执行命令",
        args=arguments,
        detail=(
            "在本机直接执行（未隔离）：cwd 是这次会话的沙箱目录，网络不受限"
            if found.backend == isolation_service.BACKEND_DIRECT
            else (
                f"在 {found.backend} 隔离里执行；cwd 是这次会话的沙箱目录；"
                f"{'允许联网' if allow_network else '已断网'}"
            )
        ),
        rule=rule,
    )
    return ExecOutcome(
        ok=False,
        ran=False,
        text="（这条命令在等对方确认，拿到决定之前没有执行。）",
        summary="等待确认",
        approval=request,
    )


def _executable_note(argv: list[str], backend: str) -> str:
    """这条命令**实际启动的是哪一个可执行文件**（§12.341 ⑥）。

    现场（2026-09-29 用户点名的会话 `conv_a5f4628f405f`）：模型在回答里说
    "Python 环境是 `E:\\gitlab\\kylab\\backend\\.venv`，里面没装 pip"——
    它验的其实是**沙箱目录里 `python` 解析到的那个解释器**，而 `.venv` 是它自己**外推**出来的
    （结果里从来没有出现过解释器路径）。这类"外推事实"没法靠提示词堵，
    只能**把事实摆出来**：把 `argv[0]` 在本机 PATH 上解析到的**绝对路径**写进结果。

    两档的诚实度不一样，所以分开说：

    - **直接执行**（``BACKEND_DIRECT``）：命令就是在**本机**跑的，`shutil.which` 解析到的
      那个路径**就是**它启动的进程 ✓（找不到时也如实说"PATH 上没有它"——那说明这条命令
      根本起不来，而现场第二问正是这种：这台机器没有 `ls`）；
    - **隔离执行**：`which` 是在**本机**解析的，隔离里那套 PATH 由后端自己的镜像决定，
      两者不一定同一个 ✗ —— 所以这里**只报事实、不给结论**：写清"本机解析到 X，
      但隔离里用的不是这一套"。宁可说"不确定"，也不要制造一个新的外推。
    """
    if not argv:
        return ""
    name = argv[0]
    found = shutil.which(name)
    if backend == isolation_service.BACKEND_DIRECT:
        if found:
            return (
                f"本条命令启动的可执行文件：`{name}` → `{found}`"
                "（本机 PATH 上解析到的那个，**不是**项目里的虚拟环境）"
            )
        return f"本机 PATH 上找不到 `{name}`：这条命令不由本机的同名程序执行（它起不来）。"
    if found:
        return (
            f"可执行文件：本机 PATH 上 `{name}` → `{found}`；"
            "**但这一条是在隔离里跑的**，隔离内用的是那个后端镜像自己的 PATH，"
            "与上面这个路径不是一回事（别把它当成隔离里的解释器）。"
        )
    return f"可执行文件：本机 PATH 上没有 `{name}`；这一条在隔离里跑，由那个后端自己的 PATH 决定。"


def _summary(result: isolation_service.ExecutionResult) -> str:
    if result.timed_out:
        return "命令超时被终止"
    lines = len([item for item in result.stdout.splitlines() if item.strip()])
    head = "跑完了" if result.ok else f"退出码 {result.exit_code}"
    return f"{head} · 输出 {lines} 行" if lines else head


def _render(
    argv: list[str],
    result: isolation_service.ExecutionResult,
    *,
    roots: Roots,
    allow_network: bool,
    timeout: float,
) -> str:
    """给模型看的执行结果。

    **四样东西每次都要有**：命令（它自己写的，回显一遍便于它确认拆对了）、
    退出码、标准输出、标准错误。少了标准错误，"命令没输出"与"命令失败了"
    在它眼里就是同一件事。
    """
    # 这一行要说清"在哪儿跑的"：未隔离那一档必须明写（模型据此才可能在回答里如实交代），
    # 而"已断网"对它是**假话**——直接执行不控网络（见 `isolation.direct_isolation`）。
    where = (
        "（**未隔离：直接在本机执行**；cwd = 工作区根目录；"
        f"工作区{'已挂载' if roots.workspace else '未挂载'}；网络不受限；超时 {timeout:g} 秒）"
        if result.backend == isolation_service.BACKEND_DIRECT
        else (
            f"（在隔离里执行：{result.backend}；cwd = 沙箱目录；"
            f"工作区{'已挂载' if roots.workspace else '未挂载'}；"
            f"{'允许联网' if allow_network else '已断网'}；超时 {timeout:g} 秒）"
        )
    )
    parts = [f"$ {' '.join(argv)}", where]
    executable = _executable_note(argv, result.backend)
    if executable:
        parts.append(executable)
    if result.timed_out:
        parts.append(
            f"命令超过 {timeout:g} 秒被终止。要跑更久的事，请把它拆小，或者告诉对方改超时。"
        )
    parts.append(f"退出码：{result.exit_code}")
    parts.append(f"标准输出：\n{result.stdout}" if result.stdout.strip() else "标准输出：（空）")
    if result.stderr.strip():
        parts.append(f"标准错误：\n{result.stderr}")
    if result.truncated:
        parts.append(f"（输出超过 {isolation_service.MAX_OUTPUT_CHARS} 字已截断）")
    parts.append(describe_roots(roots))
    return "\n\n".join(parts)
