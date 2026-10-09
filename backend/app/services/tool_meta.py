"""工具元数据表：**并发放行、审批口径、界面分类都读这一张表**（v0.42）。

照抄 ZCode 的工具六字段与 DSH 的 fail-closed 调度，见
《Agent-与对话架构对标调研 v0.1》§2.2。三家（ZCode / DSH / QwenPaw）在这件事上
收敛得很一致：**工具的元数据同时是"给模型看的说明"与"给引擎用的策略"**，
两边各写一套必然相漂。

| 字段 | 它回答的问题 |
| --- | --- |
| ``read_only`` | 会不会改东西（只读的可以随便重放、可以审计） |
| ``destructive`` | 会不会删/覆盖（比 read_only 更严一档） |
| ``concurrent_safe`` | 能不能和同一批里的别的调用**同时**跑 |
| ``side_effect_scope`` | 影响面：``none``/``session``/``workspace``/``system``/``network`` |
| ``risk_level`` | ``low`` / ``medium`` / ``high`` / ``critical`` |
| ``needs_approval`` | 这一档是不是**总是**要问用户（策略之外的第二道） |

## 为什么这件事是个安全修复，不只是抄

v0.41 之前，工具循环把**一批调用一律并发**（``tool_loop._execute_batch``）——
"读三页网页"确实该并发，但"导出文档 + 建笔记 + 上传文档"并发就是在赌它们之间没有共享状态，
而这个赌注是隐式的：加一个写类工具的人根本不知道自己在被并发调用。
ZCode 的判定是 ``concurrentSafe && !destructive && !needsApproval && scope == "none"``，
DSH 是 **fail-closed**（没声明或判定抛异常一律当独占）。这里照抄两者的交集：

- **默认独占**（未知工具 = 不并发）：加新工具忘了声明，最坏是慢一点，不是坏数据；
- 只有显式声明 ``concurrent_safe=True`` 的才进同一个并发组（例如只读检索、抓网页）；
- 独占调用是**天然的屏障**：它自己一组，前后两组不会跨过它并发。

## 还没接的字段（写在这里，免得被当成死数据）

``side_effect_scope`` / ``risk_level`` / ``needs_approval`` 是给后面两件事预备的：
P1-1 的 agent 模式（plan 档按 scope 拦写类工具）与 P2-1 的界面（工具卡按 risk 显示徽标）。
**在它们落地之前，这三个字段只被"表要完整"这条门禁用到**（见
``tests/unit/services/test_tool_meta.py``：内置工具一个都不能漏）——这是刻意的：
先定字段名与取值，抄的时候不至于各写各的。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CORE_ALWAYS",
    "EXPOSURE_CORE",
    "EXPOSURE_PERIPHERAL",
    "KINDS",
    "PERIPHERAL_TOOLS",
    "RISKS",
    "SCOPES",
    "TOOL_META",
    "ToolMeta",
    "is_peripheral",
    "kind_of",
    "meta_of",
    "parallel_groups",
]

#: 影响面取值（照 ZCode 的 ``sideEffectScope``）。
SCOPES: tuple[str, ...] = ("none", "session", "workspace", "system", "network")

#: 风险分级（照 ZCode 的 ``riskLevel``；``critical`` 留给"不可逆的外部动作"）。
RISKS: tuple[str, ...] = ("low", "medium", "high", "critical")

#: 暴露档取值（见 ``ToolMeta.exposure``）。核心 = 每轮随工具表常驻；外围 = 靠发现通道取。
EXPOSURE_CORE = "core"
EXPOSURE_PERIPHERAL = "peripheral"

#: **永远常驻的那几个**（用户点名的硬约束，见《Agent-暴露机制-对标与落点-v0.1》）。
#:
#: 三条理由各不一样，但结论都是"不能延迟"：
#:
#: - **记忆四件**（``recall`` / ``remember`` / ``forget`` / ``read_memory``）：
#:   "被延迟发现就等于没有记忆"——用户原话。``write_memory`` 原先在这个名单里，
#:   v0.56 随档案制退场（§7.4：整份覆盖与条目级预算/变更流不相容），
#:   接替它的是 ``forget``；
#: - **技能两件**（``read_skill`` / ``list_skills``）：技能是"索引常驻 + 正文按需"，
#:   取正文的入口要是也延迟，那套设计就断了；
#: - **发现通道自身**（``find_tools`` / ``call_tool``）：**这是死锁约束**——
#:   发现工具自己若也要被发现，外围就永远不可达（用例专门钉这一条）。
CORE_ALWAYS: frozenset[str] = frozenset(
    {
        "recall",
        "remember",
        "forget",
        "read_memory",
        "read_skill",
        "list_skills",
        "find_tools",
        "call_tool",
    }
)

#: **外围工具名单**（一处定义，别在别处再抄一份）。
#:
#: 判据两条（照用户给的划分口径）：**是不是每轮都可能用到** + **有没有替代物**。
#: 落到具体工具上就是三类：
#:
#: 1. **要交东西的**（导出四件 / 建笔记）：多数回合用不到，用到时是"这一步的终点"，
#:    而它自己会出现在发现结果里——延迟的代价只是一次发现；
#: 2. **管库的**（建库 / 传文档 / 删文档 / 数据源 / 入库 / 表格查询…）：
#:    配置性动作，一轮里顶多用一次；
#: 3. **重或危险的**（``run_command`` / ``spawn_subagent`` / 定时任务）：
#:    它们"sandbox/权限"成本最高，而"每轮都可能用到"这一条明显不成立。
#:
#: 反过来留在核心的是"看一眼就有用"的那些：读文件/读记忆/读技能/检索/联网/看会话文件。
#: MCP 工具**不在这个集合里**：它们按前缀自动归外围（见 :func:`is_peripheral`）——
#: 外部服务的工具随时可能装几十个，那正是外围这一档存在的理由。
PERIPHERAL_TOOLS: frozenset[str] = frozenset(
    {
        # 产出/写入类
        "create_note",
        "list_notes",
        "export_document",
        "export_table",
        "export_deck",
        "export_file",
        # 本机执行与派活
        "run_command",
        "spawn_subagent",
        "schedule_task",
        "list_scheduled_tasks",
        # 文件列举/搜索（读单个文件是核心：``read_file``）
        "list_files",
        "search_files",
        # 知识库管理面
        "list_knowledge_bases",
        "create_knowledge_base",
        "upload_document",
        "add_data_source",
        "get_document_status",
        "delete_document",
        "list_documents",
        "attach_note_to_kb",
        "ingest_artifact",
        "list_tables",
        "query_table",
        "ingest_file",
    }
)


def is_peripheral(name: str) -> bool:
    """这个工具要不要走"发现通道"。

    三件事按顺序判：

    1. 在 :data:`CORE_ALWAYS` 里 → **永远核心**（哪怕有人把它写进外围名单，
       或它的 meta 写了 ``peripheral``——硬约束优先，静默失效不允许）；
    2. ``mcp__`` 前缀 → 外围（外部服务的工具随时可能几十个）；
    3. 在 :data:`PERIPHERAL_TOOLS` 里，或它自己的 meta 标了 ``peripheral`` → 外围。

    其余一律核心（``ToolMeta.exposure`` 的默认值就是 ``core``，fail-safe）。
    """
    if name in CORE_ALWAYS:
        return False
    if name.startswith("mcp__"):
        return True
    if name in PERIPHERAL_TOOLS:
        return True
    return meta_of(name).exposure == EXPOSURE_PERIPHERAL

#: 工具卡的**语义种类**（P2-1，照 ZCode 的固定枚举，见调研报告 §2.5 第 1 条）。
#:
#: ZCode 的工具卡只认``（kind, status, input, output）``四元组，kind 是**类别**
#: 而不是工具名——所以它加几十个工具不用动界面。我们照这条：
#: 种类在这里定、随 ``StepEvent.kind`` 发给界面，界面按它选图标与配色，
#: **工具名只用来显示**（那一行的中文标签）。
#:
#: 九档，对应"这件事对用户是什么"（前八档照 ZCode 的枚举，``tool`` 是补的那一档）：
#:
#: | kind | 是什么 |
#: | --- | --- |
#: | ``read`` | 读一眼：列清单、看状态、读文件 |
#: | ``search`` | 找东西：检索、联网搜、抓网页、在文件里搜 |
#: | ``write`` | 写入/产出：建库、上传、写笔记、导出、挂定时任务 |
#: | ``delete`` | 删除/覆盖（不可逆） |
#: | ``exec`` | 在这台机器上跑东西 |
#: | ``skill`` | 技能目录与技能正文 |
#: | ``session`` | 动的是这一轮的会话上下文（子 Agent、会话内的产物） |
#: | ``message`` | 消息/回复（**今天没有内置工具用它**：留给"发消息"那一类） |
#: | ``tool`` | 认不出来的（外部 MCP 工具）：中性一档，不猜 |
#:
#: ``tool`` 这一档是**我们补的第九个取值**（八档之外），理由是一条实测过的坑：
#: 未知工具的元数据是 fail-closed（``_UNKNOWN``，按"动整台机器"算），
#: 把它画成 ``exec`` 会让界面上一个只读的 MCP 工具看起来像"在跑命令"——
#: 策略上的保守是对的，**显示上的保守不能靠同一档**。
KINDS: tuple[str, ...] = (
    "read",
    "search",
    "write",
    "delete",
    "exec",
    "skill",
    "session",
    "message",
    "tool",
)


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """一个工具的元数据。**只读是默认**——不声明的工具按最保守的解释走。"""

    read_only: bool = False
    destructive: bool = False
    concurrent_safe: bool = False
    #: 影响面。默认 ``system``：不知道它动什么，就按"可能动整台机器"算。
    side_effect_scope: str = "system"
    risk_level: str = "high"
    needs_approval: bool = False
    #: **暴露档**：``core``（核心，每轮随工具表常驻）还是 ``peripheral``（外围，靠发现通道取）。
    #:
    #: 默认 ``core`` 是**故意的 fail-safe**：没声明的工具宁可多占一点上下文，
    #: 也不能因为"忘了标"而变成模型看不见——那属于静默失效（用户点名的硬约束）。
    #: 真正的名单见 :data:`PERIPHERAL_TOOLS`（一处定义），MCP 工具按前缀自动归外围。
    exposure: str = "core"
    #: 显式指定工具卡种类（空 = 按 ``_derive_kind`` 推）。
    #:
    #: 只有"网关"这种**它自己不干活、替别的工具干活**的工具需要它：
    #: ``use_tool`` 若按"只读"推出来会画成一个读书图标，而它可能正在导出一份 Excel。
    kind: str = ""

    @property
    def parallel(self) -> bool:
        """能不能进并发组（ZCode 的判定，取 DSH 的 fail-closed 兜底）。

        **一处有据的偏离**：ZCode 的字面判定是 ``sideEffectScope == "none"``，
        那会把联网工具（搜索、抓网页）也串行化——而 KYLAB 这边恰恰相反，
        "一批抓三页"是实测省下最多时间的那一类（v0.27：三页串行 3 秒、并行 1.2 秒），
        也是系统提示词第 2 条鼓励模型做的事。两家对 ``sideEffectScope`` 的用法不同：
        ZCode 那一档把"碰网络"和"可能写"混在一个枚举里，我们的 ``network``
        明确是**只发请求、不改任何东西**（它上面还压着 ``read_only`` 这一位）。
        所以这里放宽到 ``none`` 与 ``network``：**真正的门槛是"不动东西"**。
        """
        return (
            self.concurrent_safe
            and not self.destructive
            and not self.needs_approval
            and self.side_effect_scope in ("none", "network")
        )


#: 一个"只读 + 可并发 + 无副作用"的模板：检索、列举、读文件都归它。
_READ = ToolMeta(
    read_only=True,
    concurrent_safe=True,
    side_effect_scope="none",
    risk_level="low",
)

#: **表**。键是工具名（与 ``tools.TOOL_NAMES`` / ``agent_tools`` 里那几组一致）。
#:
#: 取值只看两件事：**它动不动东西**、**动了什么**。改一处之前先问一句
#: "它能不能和同批的别的调用同时跑"——这正是这张表存在的理由。
TOOL_META: dict[str, ToolMeta] = {
    # ---- 知识库（内置那批，见 services/tools.py）----
    "list_knowledge_bases": _READ,
    "create_knowledge_base": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "upload_document": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "add_data_source": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "search": _READ,
    "list_documents": _READ,
    "get_document_status": _READ,
    "delete_document": ToolMeta(
        destructive=True, side_effect_scope="workspace", risk_level="high"
    ),
    "create_note": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "attach_note_to_kb": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "list_notes": _READ,
    "recall": _READ,
    # 读用户档案原文（自驾/自查它写进去的是什么）：与 recall / read_file 同一档——
    # 只读、无副作用，最该和同批里别的读并发
    "read_memory": _READ,
    # 写的是用户档案（四个分区），长期数据；与 remember 同一档。
    # **不是只读**，所以 plan 档会把它拦下（见 modes.is_write）
    "remember": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    # 删档案里的一条（变更流留痕、可还原）：长期数据上的删除动作，
    # 与 delete_document 同一档（high）——判错的代价要靠"还原"补回来
    "forget": ToolMeta(
        destructive=True, side_effect_scope="workspace", risk_level="high"
    ),
    # 产物落在**这一轮的会话**里（artifacts），不是用户的工作区
    "export_document": ToolMeta(side_effect_scope="session", risk_level="low"),
    "export_table": ToolMeta(side_effect_scope="session", risk_level="low"),
    "export_deck": ToolMeta(side_effect_scope="session", risk_level="low"),
    # 交付沙箱里那份文件（v0.56）：它读沙箱、写的仍是**这一轮的产物区**——
    # 与上面三个同一档。不改用户的东西，所以既不必并发也不必问
    "export_file": ToolMeta(side_effect_scope="session", risk_level="low"),
    # 把它自己产出的东西收进库里：动的是长期数据
    "ingest_artifact": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    # 联网：只读且**最该并发**（一批抓三页是常态）
    "web_search": ToolMeta(
        read_only=True,
        concurrent_safe=True,
        side_effect_scope="network",
        risk_level="low",
    ),
    "web_fetch": ToolMeta(
        read_only=True,
        concurrent_safe=True,
        side_effect_scope="network",
        risk_level="low",
    ),
    # 技能：读一次、看一眼
    "list_skills": _READ,
    "read_skill": _READ,
    # ---- 暴露网关（v0.57，见 agent_tools 的"暴露：核心常驻 + 外围可发现"一节）----
    #
    # 两个都是**只读且无副作用**的入口：真正有风险/要审批的是它们**转发**到的那个工具，
    # 而内层工具的判断照原路走（审批、权限、plan 档的写类门闸在网关那一支里自己补判，
    # 见 agent_tools `use_tool` 分支）。在这里标成"会写"会让用户被问两遍：
    # 一次问"要不要调用 use_tool"、一次问"要不要执行 run_command"。
    "find_tools": ToolMeta(
        read_only=True,
        concurrent_safe=True,
        side_effect_scope="none",
        risk_level="low",
        kind="search",
    ),
    "use_tool": ToolMeta(
        read_only=True,
        side_effect_scope="none",
        risk_level="low",
        kind="tool",
    ),
    # 子代理是一次完整的调研（贵、有副作用、要落消息），独占
    "spawn_subagent": ToolMeta(side_effect_scope="session", risk_level="medium"),
    # ---- 这台机器上的能力（v0.33，见 agent_tools._LOCAL_TOOLS）----
    "list_files": _READ,
    "read_file": _READ,
    "search_files": _READ,
    # 执行命令：动的是**整台机器**，风险最高，而且总是要用户点头（策略之外的第二道）
    "run_command": ToolMeta(
        destructive=True, side_effect_scope="system", risk_level="high", needs_approval=True
    ),
    "list_tables": _READ,
    "query_table": _READ,
    # 挂定时任务：它自己不动文件，但会**在未来动手**——不并发，改天再说
    "schedule_task": ToolMeta(side_effect_scope="workspace", risk_level="medium"),
    "list_scheduled_tasks": _READ,
    # ---- 会话文件区（v0.55，见 agent_tools._CONVERSATION_FILE_TOOLS）----
    # 列/读文件区是**只读**：它与 read_file 同一档（读一眼，最该和同批里别的读并发）
    "list_conversation_files": _READ,
    "read_conversation_file": _READ,
    # 把会话里的一份文件加进知识库：写的是**用户的长期资产**，与 upload_document /
    # ingest_artifact 同一档。**并且要问一次**（`needs_approval=True`）——
    # 用户点名的 2026-09-29 走查："在用户没有明确把数据上传到知识库的时候不得往知识库里面塞东西" ✗。
    # 判据按"宁可多问一次"落：**默认拦**，用户确需时在确认条上点一下就是了；
    # 免问的口子仍由权限档位那条既有策略决定（`tool_loop._resolve_permission`）。
    "ingest_file": ToolMeta(
        side_effect_scope="workspace", risk_level="medium", needs_approval=True
    ),
}

#: 未知工具（外部 MCP 服务暴露的）：**fail-closed**——不并发、按动系统算。
#:
#: 为什么不去读 MCP 的 ``annotations``（DSH 是用 ``idempotentHint`` 反推的）：
#: 我们这边的客户端目前**没有把 annotations 带上来**，凭工具名猜并发安全是在赌。
#: 等 MCP 客户端把 annotations 透出来，再按 DSH 那条做（见调研报告 §2.2）。
#:
#: **它不参与种类推导**：``kind_of`` 对没在表里的名字直接给 ``tool``
#: （策略上按"动整台机器"算是对的，显示上画成"执行"是错的，见 ``KINDS``）。
_UNKNOWN = ToolMeta()


def meta_of(name: str) -> ToolMeta:
    """取一个工具的元数据；**不认识就按最保守的算**（fail-closed）。"""
    return TOOL_META.get(str(name or ""), _UNKNOWN)


#: 六字段**分不出来**的那几个：种类在这里显式指名（P2-1）。
#:
#: 为什么需要这张表：``read_only`` / ``side_effect_scope`` / ``destructive``
#: 回答的是"动不动东西、动了什么"，而"读一眼"与"找东西"在它们眼里是同一档
#: （都是只读、影响面 none）——那个区别是**语义**，不是策略，元数据里没有它的位置。
#: 所以这几条按名字指名，**仍然写在这一张表旁边**（不在前端，也不另起一份
#: 名字→样子的映射）：界面对工具名一无所知，它只认 ``kind``。
#:
#: 导出三件套同理：它们的 scope 是 ``session``（产物落在这一轮的会话里），
#: 按元数据推会得到 ``session``；但用户看到的是"它做出来一份东西"，
#: 归 ``write`` 才与"建笔记、上传文档"是同一件事（ZCode 把这类单列成"任务输出"，
#: 我们的八档里没有那一档，收进 ``write``；真正的那份文件由产物卡片单独承担）。
_KIND_OVERRIDES: dict[str, str] = {
    # 从"已有的东西里找一段"——与 read（列清单、看状态）不是一回事
    "search": "search",
    "recall": "search",
    "search_files": "search",
    # 技能是能力层的一档（ZCode 的枚举里也单列）
    "list_skills": "skill",
    "read_skill": "skill",
    # 产出交付物（见上面那段）
    "export_document": "write",
    "export_table": "write",
    "export_deck": "write",
    # 交付沙箱里那份文件（v0.56）：同样是"它做出来一份东西"（用户看到的是
    # 「交付文件」+ 一张卡片），与上面三个走同一档图标与配色。
    # 按元数据推会得到 session（scope 是 session），那在界面上是"另开一段对话"的意思，
    # 说错了这一件事
    "export_file": "write",
}


def _derive_kind(meta: ToolMeta) -> str:
    """按元数据推种类（推导规则，顺序即优先级）。

    顺序是有讲究的：``run_command`` 同时是 ``destructive`` 与 ``system``——
    先看影响面，它才是"执行"；``delete_document`` 是 ``workspace`` + ``destructive``，
    落到"删除"。反过来（先看 destructive）会把命令画成删除，
    而"删了一份文档"与"在这台机器上跑了一条命令"对用户是两件事。
    """
    if meta.side_effect_scope == "system":
        return "exec"
    if meta.destructive:
        return "delete"
    if meta.read_only:
        return "search" if meta.side_effect_scope == "network" else "read"
    if meta.side_effect_scope == "session":
        return "session"
    return "write"


def kind_of(name: str) -> str:
    """工具名 → 语义种类（P2-1）。**界面拿到的就是它，不再自己按名字分类。**

    与 ``meta_of`` 一样对未知工具 fail-closed，但兜的那一档不同：``tool``
    （中性图标），不是按"动整台机器"推出来的 ``exec``——见 ``KINDS`` 最后一条。
    """
    tool = str(name or "")
    if tool not in TOOL_META:
        return "tool"
    explicit = _KIND_OVERRIDES.get(tool) or TOOL_META[tool].kind
    if explicit:
        return explicit
    return _derive_kind(TOOL_META[tool])


def parallel_groups(calls: list[str]) -> list[list[int]]:
    """把一批调用切成组：能并发的连成一组，独占的各成一组（**天然屏障**）。

    返回的是**下标**（调用方要按原顺序回填结果）。形状与 DSH 的
    ``{parallelGroups, executionOrder}`` 一致：组序 = 调用序，组内可并发。

    例：``[read_file, read_file, run_command, read_file, web_fetch]`` →
    ``[[0, 1], [2], [3, 4]]``——中间那条命令把它前后的读操作隔开，
    因为"它自己会改文件"，两边的读结果不可能互相作数。
    """
    groups: list[list[int]] = []
    open_group = False
    for index, name in enumerate(calls):
        if meta_of(name).parallel:
            if open_group and groups:
                groups[-1].append(index)
            else:
                groups.append([index])
                open_group = True
        else:
            groups.append([index])
            open_group = False
    return groups
