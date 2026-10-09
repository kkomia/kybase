"""沙箱端点（v0.16，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §4）。

两件事：**告诉界面这台机器上有什么隔离**、以及**在隔离里跑一条命令**。

**跑命令是这个产品里权限最大的一个动作**——它按设计就是"在你的机器上执行东西"。
所以三道闸一层不省：

1. **管理员专属**（``require_admin``）：它不是"读写知识库"级别的能力，
   而是"在这台机器上执行代码"。与设置页同一档；
2. **策略闸**（``ExecutionPolicy``）：``ask`` 时未确认回 409，界面确认后带
   ``approved=true`` 重调；``deny`` 直接 403；
3. **内核隔离**（``services/isolation.py``）：有真隔离就用它；没有时**按
   ``sandbox.require_isolation`` 那一项判**——这一项是**开**就按纪律拒绝执行，
   是**关**才降级为直接执行并如实标注"未隔离"（v0.55，见 ``isolation.direct_isolation``）。

第 3 条的那一项**出厂值是"关"**（`services/runtime_config.py` 的 ``DEFAULTS`` 里
``"sandbox.require_isolation": "false"``，2026-09-30 用户裁定：不装 Docker 也要能跑命令），
所以**默认形态是降级直执**；要严格拒绝就显式把它打开。
读侧还有一层兜底：``services/sandbox.py::require_isolation`` 的默认参数是 ``True``
——库 / ``.env`` / ``DEFAULTS`` **三层都取不到值**（例如那一列为空串）时按"拒绝"算，
宁可少跑一条命令，也不悄悄裸跑。

为什么默认是"降级"而不是"一律拒绝"：Windows 上没有 bwrap / sandbox-exec 的原生等价物、
容器里也常没挂 docker，一律拒绝会让**本地源码启动与容器部署两边都跑不了命令**。
降级那一档**不是沙箱**，所以它把"未隔离"写在明面上（详情与给模型的话里都写），
而不是让"用户点了同意"被误读成"在一个沙箱里跑"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.auth import require_admin
from app.api.v1.schemas import (
    SandboxCapabilityOut,
    SandboxExecIn,
    SandboxExecOut,
    SandboxPlanOut,
)
from app.core.caller import Caller
from app.core.exceptions import ForbiddenError
from app.core.services import Services, get_services
from app.services import isolation as isolation_service
from app.services import modes
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    append_allow_rule,
    rules_from_runtime,
    suggest_rule,
    tool_arguments,
)
from app.services.sandbox import ExecutionPolicy, require_isolation, sandbox_for

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


@router.get("", response_model=SandboxCapabilityOut, summary="这台机器上的隔离能力")
def get_capability(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SandboxCapabilityOut:
    """**探测而不是假设**：真去盘上找 bwrap / sandbox-exec / docker，
    并检查 docker daemon 是否活着（装了 CLI 但 daemon 没起是最常见的假阳性）。

    界面据此显示当前状态；没有真隔离时**默认换成降级档**（``direct``，未隔离直接执行，
    v0.55），只有设置里开了「无隔离时拒绝执行」才报"执行会被拒绝"——两种都在这里说清，
    而不是等用户点了执行再报错（那时他已经以为它能跑了）。
    """
    found = _isolation_or_direct(services)
    return SandboxCapabilityOut(
        backend=found.backend,
        available=found.available,
        detail=found.detail,
        max_output_chars=isolation_service.MAX_OUTPUT_CHARS,
        default_timeout_seconds=isolation_service.DEFAULT_TIMEOUT_SECONDS,
    )


@router.post("/plan", response_model=SandboxPlanOut, summary="看这条命令会被怎么隔离")
def preview_plan(
    payload: SandboxExecIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SandboxPlanOut:
    """**只算不跑**：把隔离后的 argv 显示出来。

    给用户一条"我能核对"的路：他说不准要不要同意执行时，最需要的不是我们替他判断，
    而是看清楚到底会发生什么——`--unshare-net` 在不在、工作区有没有被挂成读写。
    """
    root, box = _paths(services, payload)
    plan = isolation_service.build_plan(
        payload.argv,
        workspace_root=root,
        sandbox_dir=box,
        isolation=_isolation_or_direct(services),
        allow_network=payload.allow_network,
        bind_ro=isolation_service.bind_paths_from(services.runtime.get("sandbox.bind_ro")),
    )
    return SandboxPlanOut(
        backend=plan.backend,
        available=plan.available,
        detail=plan.detail,
        argv=plan.argv,
        workdir=plan.workdir,
    )


@router.post("/exec", response_model=SandboxExecOut, summary="在隔离里执行一条命令")
def exec_command(
    payload: SandboxExecIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SandboxExecOut:
    """策略闸 + 内核隔离都过了才真跑。

    ``ask`` 策略下第一次会拿到 **409**（"要先确认"）：界面据此弹确认框，
    确认后带 ``approved=true`` 再调一次。409 不是错误，是流程的一步。
    """
    root, box = _paths(services, payload)
    arguments = tool_arguments(payload.argv)
    decision = _decide(services, "Bash", arguments)

    # 判定的两半：总开关（粗）与规则（细）。**规则比总开关更具体，所以规则说了算**。
    #
    # 两条顺序上的讲究，都是这套方案里最要紧的地方：
    #
    # 1. **deny 永远优先**（无论来自规则还是权限档）——先判 deny，再谈别的档位。
    #    少了它，一条更宽的 allow 会把用户的 deny 静默盖掉；
    # 2. **命中 allow 就真的跳过确认**（第一版漏了这条：规则说放行、而权限档还是
    #    ask，于是"以后都允许"点完照样再问一遍——那正是规则存在的意义被架空了）。
    #    实现上就是把命中的那条规则当作生效档位；没命中规则才用权限档。
    #
    # 权限档（2026-09-29 四档 + 抄 Codex 的 Auto 口径，见 services/modes.py 那段
    # "抄成熟的"注释）：仅查看 → 不跑；手动批准 → 每条都问；
    # 默认（智能）→ 工作区内不问、出界/联网/剥不干净才问；全自动 → 直接跑。
    # 这里**有工作区根**（`root`），所以按真实范围判 ✓。
    permission = modes.coerce_permission(services.runtime.get("chat.permission"))
    command = modes.command_from_arguments(arguments)
    workspace = str(root) if root is not None else None
    global_mode = {
        modes.PERMISSION_VIEW: ACTION_DENY,
        modes.PERMISSION_MANUAL: ACTION_ASK,
        modes.PERMISSION_SMART: (
            ACTION_ASK
            if modes.command_needs_ask(command, permission, workspace)
            else ACTION_ALLOW
        ),
        modes.PERMISSION_FULL: ACTION_ALLOW,
    }[permission]
    if decision.action == ACTION_DENY:
        raise ForbiddenError(f"这条命令被拒绝规则拦下：{decision.reason}")
    if global_mode == ACTION_DENY:
        raise ForbiddenError("这一轮的权限是「仅查看」：不改动任何东西，也不执行命令")

    policy = ExecutionPolicy(
        mode=decision.action if decision.rule is not None else global_mode,
        workspace_root=root,
    )
    policy.require_allowed(approved=payload.approved, what="在沙箱里执行命令")

    # 用户点了「以后都允许」：把**建议的那条规则**写进放行清单。
    # 建议的是词前缀（`Bash(git status:*)`）而不是完整命令——记住完整命令
    # 等于没记住（下次参数就不同了）。
    if payload.remember and decision.action == ACTION_ASK:
        _remember_rule(services, "Bash", arguments)

    result = isolation_service.run_isolated(
        payload.argv,
        workspace_root=root,
        sandbox_dir=box,
        isolation=_isolation_or_direct(services),
        timeout=payload.timeout_seconds or isolation_service.DEFAULT_TIMEOUT_SECONDS,
        allow_network=payload.allow_network,
        bind_ro=isolation_service.bind_paths_from(services.runtime.get("sandbox.bind_ro")),
    )
    return SandboxExecOut(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        timed_out=result.timed_out,
        backend=result.backend,
    )


def _isolation_or_direct(services: Services) -> isolation_service.Isolation:
    """这次执行用哪个后端：有真隔离就用它；没有且**没开严格模式**时换成降级档 ``direct``。

    **严格模式下原样返回 ``detect()`` 的结果**（``available=False`` 的 ``none``）：
    ``run_isolated`` 见到它会抛 ``UnsupportedContentError``（端点据此报"拒绝执行"），
    ``build_plan`` 给出一份不可用的计划——两处行为与降级前逐字一致。
    """
    found = isolation_service.detect()
    if found.available or require_isolation(services.runtime):
        return found
    return isolation_service.direct_isolation()


def _rule_set(services: Services):  # type: ignore[no-untyped-def]
    """按用户配的三张清单建规则集（构造共用 ``command_policy.rules_from_runtime``）。"""
    return rules_from_runtime(services.runtime, source="s")


def _decide(services: Services, tool: str, arguments: str):  # type: ignore[no-untyped-def]
    return _rule_set(services).decide(tool, arguments)


def _remember_rule(services: Services, tool: str, arguments: str) -> None:
    """把这次调用建议的规则追加到放行清单（合并去重共用 ``append_allow_rule``）。"""
    append_allow_rule(services.runtime, suggest_rule(tool, arguments))


def _paths(services: Services, payload: SandboxExecIn):  # type: ignore[no-untyped-def]
    """``(工作区根, 沙箱目录)``。

    - 给了 ``workspace_id`` → 用它登记的 ``root_path``（**Agent 干活的地方**），
      沙箱仍按会话/请求分（**试错的地方**）。两者的区分见设计文档 §4；
    - 没给 → 根取数据目录下的 ``sandbox`` 父目录，也就是"只在沙箱里跑，
      不碰任何真实工作区"。这是更保守的默认。
    """
    box = sandbox_for(services.runtime.data_dir, payload.session_id or "adhoc").ensure()
    if payload.workspace_id:
        # **设备维度不过滤**（`any_device=True`，v0.59）：这条命令的 cwd 由请求里那个
        # 工作区决定（判断"能不能拿它当 cwd"是另一回事，见 sandbox 自己的闸），
        # 而设备隔离管的是"界面上看得见哪些项目"。这里加一道设备闸会让桌面端的
        # 会话一换到 web 发起就找不到自己的工作区。
        workspace = services.workspaces.get(payload.workspace_id, user_id=None, any_device=True)
        return workspace_root_of(workspace.root_path), box
    return box.parent, box


def workspace_root_of(raw: str):  # type: ignore[no-untyped-def]
    """工作区记录里存的是**字符串**（创建时已规范化过），这里转回 ``Path``。"""
    from pathlib import Path

    return Path(raw)
