"""域间引用约束（剥离阶段 0）：把《kylab 知识库剥离方案 v0.1》§3.1 的归属表落成机械检查。

背景：知识库要从 kylab 剥离成独立服务。方案 §4 末尾自己点出了一个结构性盲区——
`scripts/check_layering.py` 只管 `app.api` / `app.mcp_server` / `app.services` / `app.parsers`
四个前缀，**`app.core` / `app.models` / `app.pipeline` / `app.storage.*` 内部、
`services/` 内部互引完全没有机械约束**。于是"KB 域与 Agent 域不许互引"这条剥离线，
今天只写在文档里，谁都能悄悄踩回去。

这个脚本把归属名单（方案 §3.1 + 2026-10-08 老板拍板的口径）编成数据，扫描
`backend/app/` 全部 import，报四类跨界引用：

- ``kb→agent``：KB 域模块 import Agent 域模块（禁）；
- ``agent→kb``：Agent 域模块 import KB 域模块——**只许经三处已知接缝**
  （`services/knowledge_client.py` 协议、`services/knowledge_provider.py`、
  `services/remote_clients.py`，方案 §2 事实 2），其余全报；
- ``mixed``：混合体（`services/chat.py` / `services/tools.py` / `api/v1/schemas.py`）
  与两侧之间的引用——**只记录、不拦门**（它的动作是阶段 1 的对切，不是"改 import"）；
- ``shared→domain``：共享底座被两边引用是合法的（不报），但共享底座自己 import
  任何一侧域模块要报——它必须两边都不依赖。

前三类里只有 `kb→agent` / `agent→kb` 转红灯（`BLOCKING_KINDS`）。

另有两件"名单本身与调用面"的事，和处理越界同等重要：

1. **逐个条目核实存在性**：名单里的条目在磁盘上真有吗（笔误要能看见）；
2. **列出未被任何名单认领的模块**：没有名单 = 没有约束。这正是
   `check_layering.py` 的 L6 吃过的那次教训（`app/agent_tools.py` 住在 app 根，
   一条规则都不作用于它，而它管着工具准入与会话范围收口）——所以这里宁可多一张
   "未认领"清单，也不许有文件悄悄溜过去；
3. **验方案那句"进程内跨域调用只有一处"**：按方法名扫 `retrieve_sources` 的**全部调用点**
   （论断说的是一次调用，不是 import），逐个定性——见报告第 9 节；
4. **扫组合根属性访问**：`services.ingest.submit(...)` 这类跨域用法**不走 import**，
   靠 `Services` 数据类的注解把字段映射回模块后判定（另一条泳道，只记录不拦门）。

**数字会因口径补齐而变大**：一个模块从"未认领"变成有归属，它原先根本没进统计的边
就会一次性全部出现——**那不是新长出来的耦合**（报告第 1 节写着这条，免得被误读）。

**本阶段只报告不拦门**：暂不接进 `scripts/check-backend.sh`（先跑出基线，下一步才把红灯
拧成门禁）。接入位置就是 `check-backend.sh` 里 `check_layering.py` 那一行之后，
`step "域间引用（KB ↔ Agent）" "$PY" "$ROOT/scripts/check_domains.py" "$ROOT"`。
退出码今天就按最终语义给（有 KB↔Agent 越界即非零），所以"接进门禁"只是加一行调用，
不改这里的判据。

用法：python scripts/check_domains.py [仓库根目录，默认当前目录]
产出：终端报告 + `docs/计划与记录/域间引用基线-v0.1.md`（同名文件每次重跑覆盖）
退出码：0 = 无 KB↔Agent 越界；1 = 有越界（共享底座越界、组合根属性访问与未认领清单
只记录，不拦）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------- 域与名单
#
# 名单数据来自方案 §3.1 的归属表，**再加 2026-10-08 老板拍板的口径**（补齐那一轮）：
# - 账号体系（users / sessions / api_keys / auth / avatars，api 与 services 两侧）→ KB 域；
# - 备份（`backup_*.py` + `api/v1/backup.py`）→ Agent 域（§5.3 的口径，
#   覆盖 §3.1 把它算进共享底座那一条）；
# - `model_proxy` / `model_registry` / `llm` 接入 → 共享底座（两边各拷一份的口径，
#   物理共享期先进共享名单）；
# - `health` / `frontend` / `router` / `main` → 共享底座（两档都要用，阶段 1 才分家）；
# - `app.services.chat` / `app.services.tools` / `app.api.v1.schemas` 是**混合体**
#   （§4 第 2/3/5 条）：不许整模块归入一侧，单列第三类（越界只记录、不拦门）；
# - `kb_cache`（M4 降级链）、`schedule_runner`、`skill*`、`site_icons` / `web` /
#   `model_client` / `legacy_import` 按语义补齐；
# - 逐条判断的理由写在 `CLAIM_NOTES`（报告第 2 节会打出来），**有争议的都在那里**。

KB = "kb"
AGENT = "agent"
SHARED = "shared"
#: 第三类：混合体。跨域引用只记录、不拦门——它今天本来就同时装着两边的东西，
#: 判定"越界"没有意义（真正的动作是 §6 阶段 1 第 4/5 步的**对切**）。
MIXED = "mixed"

DOMAIN_LABELS = {KB: "KB 域", AGENT: "Agent 域", SHARED: "共享底座", MIXED: "混合体（待对切）"}

#: 方案 §3.1「KB 带走」+ 老板口径的账号体系。
KB_MODULES: tuple[str, ...] = (
    # api（15 个）
    "app.api.v1.knowledge_bases",
    "app.api.v1.documents",
    "app.api.v1.folders",
    "app.api.v1.search",
    "app.api.v1.chunks",
    "app.api.v1.wiki",
    "app.api.v1.tabular",
    "app.api.v1.data_sources",
    "app.api.v1.shares",
    "app.api.v1.tasks",
    "app.api.v1.lifecycle",
    "app.api.v1.webhooks",
    "app.api.v1.maintenance",
    "app.api.v1.stats",
    "app.api.v1.provider",
    # services
    "app.services.knowledge_base",
    "app.services.documents",
    "app.services.folder",
    "app.services.chunk",
    "app.services.chunking",
    "app.services.batch",
    "app.services.ingest",
    "app.services.parser_router",
    "app.services.splitting",
    "app.services.retrieval",
    "app.services.embedding",
    "app.services.summary",
    "app.services.timeline",
    "app.services.wiki",
    "app.services.suggested_questions",
    "app.services.kb_prompt",
    "app.services.tabular",
    "app.services.tabular_sql",
    "app.services.office",
    "app.services.sources",
    "app.services.connectors",
    "app.services.retrieval_eval",
    "app.services.lifecycle",
    "app.services.maintenance",
    "app.services.share",
    "app.services.stats",
    "app.services.webhook",
    "app.services.observability",
    # 账号体系（2026-10-08 口径）：库分享、库范围钥匙、管理员都是 KB 语义的资产
    "app.api.v1.auth",
    "app.api.v1.users",
    "app.api.v1.api_keys",
    "app.api.v1.avatars",
    "app.services.auth",
    "app.services.users",
    "app.services.api_key",
    "app.services.avatars",
    # 插件层与摄入状态机、KB 侧存储、摄入消费者、对外 MCP 面
    "app.parsers",
    "app.pipeline.state_machine",
    "app.storage.postgres_impl",
    "app.storage.duckdb_impl",
    "app.workers.queue_worker",
    "app.mcp_server",
)

#: 方案 §3.1「Agent 留下」+ 老板口径补齐的那几件。
AGENT_MODULES: tuple[str, ...] = (
    # api
    "app.api.v1.conversations",
    "app.api.v1.chat",
    "app.api.v1.notes",
    "app.api.v1.memory",
    "app.api.v1.skills",
    "app.api.v1.plugins",
    "app.api.v1.mcp_servers",
    "app.api.v1.sandbox",
    "app.api.v1.workspaces",
    "app.api.v1.schedules",
    "app.api.v1.web",
    "app.api.v1.site_icons",
    "app.api.v1.local",
    "app.api.v1.backup",
    # 2026-10-08 主代理拍板：设置面是 agent 产品设置页的 API 面（读共享底座的
    # `runtime_config` 合法），归 Agent 域。
    "app.api.v1.settings",
    # services
    "app.services.agent",
    "app.services.tool_loop",
    "app.services.agent_tools",
    "app.services.agent_exec",
    "app.services.agent_files",
    "app.services.subagent",
    "app.services.sandbox",
    "app.services.isolation",
    "app.services.command_policy",
    "app.services.commands",
    "app.services.approvals",
    # `skill*` 而不是方案写的 `skills*`：后者收不到 skill_blurb / skill_categories /
    # skill_market / skill_sources / skill_tools（2026-10-08 口径把这五件一并定在 Agent 侧）。
    "app.services.skill*",
    "app.services.plugins",
    "app.services.mcp_client",
    "app.services.workspace",
    "app.services.notes",
    "app.services.note_ai",
    "app.services.artifacts",
    # 同理：`schedule*` 收 schedule_runner（定时任务执行器），方案写的 `schedules*` 收不到。
    "app.services.schedule*",
    "app.services.cron",
    "app.services.conversation*",
    "app.services.session_events",
    "app.services.live_turns",
    "app.services.resume",
    "app.services.modes",
    "app.services.prompt",
    "app.services.thinking",
    "app.services.plan_gate",
    "app.services.tool_meta",
    "app.services.failures",
    "app.services.deck",
    "app.services.memory*",
    "app.services.archive*",
    # 2026-10-08 口径补齐：降级链、站点图标、网页抓取、历史导入
    "app.services.kb_cache",
    "app.services.site_icons",
    "app.services.web",
    "app.services.legacy_import",
    # 备份（§5.3：备份的是本机数据，`BackupProvider` 本来就是与 KB 平行的第二个提供者）
    "app.services.backup_*",
    # `split_impl/` 改归 Agent：方案 §3.1 自己说它"历史使命结束，可留 Agent 侧做降级实现"，
    # 而它唯一的外部依赖就是 Agent 侧的 `sqlite_impl`（`LOCAL_METHODS`）——归 KB 会一直红着。
    "app.storage.split_impl",
    # 本机存储、本机 worker、桌面壳入口
    "app.storage.sqlite_impl",
    "app.workers.local_worker",
    "app.sidecar",
)

#: 共享底座：方案 §3.1 + 2026-10-08 口径（model 接入 / health / frontend / router / main）。
#: **整包 `app.core` 在册**（方案括注只点了 8 个文件，但写的是 `core/`）——这条刻意保守：
#: 宁可把 `core/services.py`（§4 点名的单一组合根，第一刀要切它）先算进共享底座，
#: 让它的越界真的亮出来，也不要因为"括注里没列"就把它漏在名单之外。
SHARED_MODULES: tuple[str, ...] = (
    "app.core",
    "app.models.enums",
    # 模型接入：两边各拷一份（`llm` 是实现、`model_registry` 是注册表、
    # `model_proxy` 是出面、`model_client` 是那条接缝的协议声明）。
    "app.services.llm",
    "app.services.model_registry",
    "app.services.model_client",
    "app.api.v1.model_registry",
    "app.api.v1.model_proxy",
    "app.services.provider_presets",
    "app.services.runtime_config",
    "app.services.credentials",
    "app.services.secrets",
    # 请求级鉴权依赖：两档都 import 它（本机档在读它时短路成"本机主人"），
    # 剥离后两侧各留一份——理由见 `CLAIM_NOTES`。
    "app.api.auth",
    "app.services.usage",
    "app.services.idempotency",
    "app.services.system_load",
    "app.storage.base",
    "app.storage.s3_impl",
    "app.storage.local_impl",
    "app.storage.repositories",
    "app.storage.text",
    # 两档都要用、阶段 1 才分家的那几个入口与出面
    "app.api.v1.health",
    "app.api.v1.frontend",
    "app.api.v1.router",
    "app.main",
)

#: 混合体（§4 第 2/3/5 条）：KB 与 Agent 的东西同处一个模块，**不许整模块归一侧**。
#: 它们对两侧的 import 与经组合根取两侧服务都**只记录、不拦门**（报告第 6 节）。
MIXED_MODULES: tuple[str, ...] = (
    "app.services.chat",
    "app.services.tools",
    "app.api.v1.schemas",
)

NAME_ENTRIES: tuple[tuple[str, str], ...] = (
    *((KB, entry) for entry in KB_MODULES),
    *((AGENT, entry) for entry in AGENT_MODULES),
    *((SHARED, entry) for entry in SHARED_MODULES),
    *((MIXED, entry) for entry in MIXED_MODULES),
)

#: 三处已知接缝（方案 §2 事实 2）：Agent 域经它访问 KB 是**设计如此**，不算越界。
SEAMS: tuple[str, ...] = (
    "app.services.knowledge_client",
    "app.services.knowledge_provider",
    "app.services.remote_clients",
)

#: 接缝对应的仓库内路径（报"哪些调用点在接缝里"时用）。
SEAM_FILES: tuple[str, ...] = tuple(
    f"backend/{seam.replace('.', '/')}.py" for seam in SEAMS
)


#: 仍未认领的模块：键是点分模块名，值是"为什么它不归任何一侧"。
#: 报告第 3 节会把这张表打出来。**这一栏现在只剩三处接缝**——它们不是"还没决定"，
#: 而是**刻意不归任何一侧**（归哪边都不对），空着不再是目标：接缝就是接缝。
UNCLAIMED_NOTES: dict[str, str] = {
    "app.services.knowledge_client": "**接缝一**：`KnowledgeClient` 协议（方案 §2 事实 2）。"
    "刻意不归任何一侧——Agent 域经它访问 KB 是设计如此（`agent→kb` 对它放行）",
    "app.services.knowledge_provider": "**接缝二**：本机档的 KB 客户端实现（方案 §2 事实 2）。"
    "同上，归任何一侧都不对",
    "app.services.remote_clients": "**接缝三**：窄 API 的 HTTP 客户端"
    "（`RemoteKnowledgeClient` / `RemoteModelClient`）。同上",
}

#: 已认领、但归属**有判断成分**的模块：逐条写清"按哪条口径定的、另一条路是什么"。
#: 这张表是给审核用的——名单里每一处不明显的决定都能在这里找到出处（报告第 2 节打出来）。
CLAIM_NOTES: dict[str, str] = {
    "app.api.auth": "**请求级鉴权依赖**"
    "（`current_caller` / `ReadDep` / `WriteDep` / `require_admin`），"
    "全仓 42 个模块 import 它——两档都要用，且本机档的短路分支就在它里面。"
    "账号体系的**服务实现**（services 的 auth/api_key/users/avatars + api/v1 下四个面）"
    "按 2026-10-08 口径归 KB，而这一层按「两档都要用、阶段 1 才分家」进共享底座"
    "（与 health/frontend/router 同一条）。**主代理 2026-10-08 已确认这一条**；"
    "若改判 KB，会立刻多出一批 agent→kb：Agent 侧各 api 模块都 import 它，"
    "剥离线会变成 13 处「各留一个 shim」的工作项——那是阶段 1 的事",
    "app.services.api_key": "账号体系在 services 侧的实现"
    "（`check_access` / `visible_kb_ids` 直接读 KB 表，见方案 §4 第 7 条）→ KB 域。"
    "**身份契约已搬走**（`Caller` / `READ` / `WRITE` / `LOCAL_CALLER` / `LOCAL_USER_ID` → "
    "`app.core.caller`，见报告第 10.1 节）：本模块只留发放与校验，"
    "那几个名字仍从这里的 `__all__` 再导出，KB 侧调用点一行没动",
    "app.core.caller": "**身份契约**：`Caller`（frozen dataclass，含 `permission` / "
    "`knowledge_base_ids` / `owner_id` 三个派生属性）、`READ` / `WRITE`（权限别名）、"
    "`LOCAL_CALLER` + `LOCAL_USER_ID`（本机档唯一的调用主体）。"
    "两侧都要它：KB 侧用它做准入判定，Agent 侧每个 api 模块拿它当类型、判 `WRITE`。"
    "住共享底座是 2026-10-08 那一刀的结果（实现仍留 `services/api_key.py`）",
    "app.core.html_format": "**网页正文提取**（`extract_article` / `html_to_markdown` / "
    "`html_to_text`），从 `app/parsers/html_format.py` **例外上移**："
    "KB 侧的连接器与上传路径要用它，Agent 侧的阅读模式（`services/web.py`）也要用。"
    "`app.parsers.html_format` 仍是一个再导出的壳，KB 侧调用点一行没动",
    "app.parsers.html_format": "**再导出的壳**（实现已上移 `app.core.html_format`）。"
    "仍按 `app.parsers` 前缀算 KB 域——它只是给 KB 侧解析器与连接器留的入口，"
    "KB → 共享底座这个方向本来就合法，所以它不产生任何越界",
    "app.api.v1.settings": "**主代理 2026-10-08 拍板：Agent 域**——它是 agent 产品设置页的 API 面，"
    "读共享底座的 `runtime_config` 合法。这一刀同时修掉它原来的两处 KB import："
    "身份契约改从 `app.core.caller` 取，`NOT_CONFIGURED_HINT` 复制一份（双份并存期，"
    "权威在 KB 侧的 `services/embedding/__init__.py`，KB 面收口时删）",
    "app.services.model_client": "**模型调用接缝的协议声明**"
    "（`ModelClient`，只有 Protocol、无实现）。"
    "按 2026-10-08 的另一条口径「llm 接入 → 共享底座（两边各拷一份）」归共享底座；"
    "口径原句把 `model_client` 列在「按语义归 Agent 侧」那串里，"
    "但它语义上就是 `llm.py` 那条接缝——"
    "**这一处判错了也不影响数字：今天没有任何模块 import 它**（只有注释里提到）",
    "app.storage.split_impl": "**刀 1**：方案 §3.1 自己写着「历史使命结束，"
    "可留 Agent 侧做降级实现」，而它唯一的外部依赖是 Agent 侧的 `sqlite_impl`"
    "（取 `LOCAL_METHODS`）。归 KB 会一直红着；"
    "改归 Agent 后那处 kb→agent 随之消失（`remote_meta.py` 只依赖 `storage/base.py` 与自己人）",
    "app.services.backup_*": "§3.1 把备份放在共享底座，§5.3 判给 Agent/本机侧"
    "（「备份的是本机数据，`BackupProvider` 本来就是与 KB 平行的第二个提供者」）。"
    "2026-10-08 口径取 §5.3 → Agent 域",
    "app.api.v1.backup": "同上：备份的出面在 Agent 侧",
    "app.services.skill*": "方案写的是 `skills*.py`，收不到 `skill_blurb` / `skill_categories` / "
    "`skill_market` / `skill_sources` / `skill_tools` 五件；"
    "2026-10-08 口径把这五件定在 Agent 侧，所以通配改成 `skill*`（一条收全）",
    "app.services.schedule*": "同理：方案写的 `schedules*.py` 收不到 `schedule_runner.py`"
    "（定时任务执行器，Agent 域）。通配改成 `schedule*`；它调 `retrieve_sources` 那一处"
    "就是方案漏报的第 4 个跨域调用点",
    "app.services.kb_cache": "M4 降级链（KB 元数据缓存）——"
    "方案 §6 第 13 条明确留在 kylab 侧 → Agent 域",
    "app.services.site_icons": "api 侧的 `app.api.v1.site_icons` 本来就在 Agent 名单里，"
    "services 侧按语义补齐",
    "app.services.web": "同上（网页抓取）：`app.api.v1.web` 在 Agent 名单，services 侧补齐",
    "app.services.legacy_import": "历史数据导入（归档迁移那一族）→ Agent 域",
    "app.api.v1.router": "路由组合根：按 `KYLAB_DEPLOYMENT` 挂两张表（方案 §2 事实 1 就是它）。"
    "两档都要用，**阶段 1 才分家** → 共享底座（同 health / frontend）",
    "app.main": "服务器档入口（`sidecar.py` 是本机档入口）。与 sidecar 对称、两档都要，"
    "进共享底座（阶段 1 与 router 一起分家）",
    "app.api.v1.health": "健康探针：两档都要 → 共享底座",
    "app.api.v1.frontend": "前端静态托管（应用壳）：与域无关 → 共享底座",
    "app.api.v1.model_registry": "模型注册表的出面 → 共享底座"
    "（与 `services/model_registry.py` 同一口径）",
    "app.api.v1.model_proxy": "模型代理的出面 → 共享底座"
    "（与 `RemoteModelClient` 那一条链同一口径）",
    "app.services.chat": "**混合体**（§4 第 2 条）：`retrieve_sources`（KB 检索本体）与 "
    "`tool_loop` / `agent_messages`（Agent）同处一类。按 2026-10-08 口径单列第三类，"
    "它 import 两边都**只记录、不拦门**（真正的动作是阶段 1 第 4 步的对切）",
    "app.services.tools": "**混合体**（§4 第 3 条）：KB 工具与 Agent 交付工具同表，"
    "切分线是 `agent_tools.py:511-524` 的 `_KB_TOOLS` 白名单",
    "app.api.v1.schemas": "**混合体**（§4 第 5 条）：KB 与 Agent 的请求/响应模型同住 3407 行",
}

#: 进程内**跨域调用**扫描的 KB 方法名。
#:
#: 方案的论断是"Agent → KB 的进程内调用，全仓只有一处：`services/agent_tools.py:1263`
#: → `services.chat.retrieve_sources(...)`"（§2、§9）。这个论断说的不是 import，
#: 而是一次**调用**——而 `chat.py` 是混合体（两张名单都没收它），按模块归属判不出来，
#: 所以这里按**属性名**扫调用点（`xxx.retrieve_sources(...)`），先把论断验成事实或反例。
KB_SURFACE_CALLS: tuple[str, ...] = ("retrieve_sources",)

#: 组合根接收者的名字。这个仓库里"拿到某个服务"的写法就这几种：
#: 注入进来的参数 `services`、`self._services`、`self.services`。**按接收者名收窄**是有意的：
#: 不限定接收者的话，`payload.documents` 这种同名字段会成片误报。
COMPOSITION_ROOT_RECEIVERS: tuple[str, ...] = ("._services", ".services")

#: 拆组合根之后 `Services`（Agent 根）上那一格**过渡访问器**的名字：`services.kb.<KB 字段>`
#: 是"经组合根取 KB 服务"的现形态（见 `core/services.py` 的 `Services.kb` 字段说明）。
KB_ACCESSOR = "kb"

#: 组合根那两个数据类的名字（`app/core/services.py`）：字段映射要**两张表合起来看**。
ROOT_CLASS_NAMES = ("Services", "KbServices")

#: 分类：越界三态 + 混合体牵涉的边 + 组合根属性访问（另一条泳道）的两种走向
KB_TO_AGENT = "kb→agent"
AGENT_TO_KB = "agent→kb"
SHARED_TO_DOMAIN = "shared→domain"
MIXED_IMPORT = "mixed"
KB_TO_AGENT_ROOT = "kb→agent·组合根"
AGENT_TO_KB_ROOT = "agent→kb·组合根"
MIXED_ROOT = "mixed·组合根"
KB_ACCESSOR_KIND = "经 kb 访问器"

#: 哪些类别转红灯（本阶段口径 = 方案 §8 验收第 5 条）。
#: **共享底座与组合根越界只记录、不拦门**：前者 §5.3 还没拍板归属（`core/services.py`
#: 那次要先做阶段 1 第 3 步的拆组合根，现在红着没有可执行的修法）；后者是另一条泳道
#: （调用面的收口，阶段 1 第 4/5 步做），且它按"接收者名字 + `Services` 字段名"判定，
#: 与 import 那三条比确定性略低，不该和前者同一个红灯。归属与切法定了再往这里加。
BLOCKING_KINDS: frozenset[str] = frozenset({KB_TO_AGENT, AGENT_TO_KB})

REPORT_RELATIVE = "docs/计划与记录/域间引用基线-v0.1.md"

#: 本检查看不见什么——写在报告里，免得"报告绿了"被读成"域解耦了"。
#: 三条都是这个仓库里真实存在的耦合形状，且都**不产生 import**（所以任何 import 扫描
#: 都看不见它们），修法也都不在"改 import"这一层。
LIMITATIONS_SECTION = "\n".join(
    [
        "## 8. 本检查看不见什么",
        "",
        "这份报告只覆盖 import 与两类调用点/属性访问。以下三类耦合真实存在，但它**不产生 "
        "import**，本检查看不见，报告绿了也不代表域已经解耦：",
        "",
        "1. **组合根注入的对象**。`notes.py` / `artifacts.py` 持有的 `ingest` / `documents` 是"
        "构造函数里的**无注解参数**"
        "（`def __init__(self, stores, *, ingest=None, documents=None)`），"
        "方案 §4 第 8 条说的那对真 `IngestService` / `DocumentService` 引用就在这里——"
        "既不 import 也拿不到类型，扫描抓不到。第 6 节那类属性访问只覆盖 `services.<字段>` 写法。",
        "2. **接口内部混装**。`storage/base.py` 的 `MetaStore` 有 200+ 个方法，KB 与本机域"
        "混在同一个抽象类里，靠 `split_impl/router.py` 的 `LOCAL_METHODS` 补集机械切两半——"
        "这是**接口内部分域**，import 层面看起来完全正常。",
        "3. **共享契约被两边当同一个对象用**。`Settings`（`core/config.py`）、`StoreBundle` 之类"
        "被两边同时持有，`data_dir` 派生出所有存储路径（方案 §4 第 10 条）。两边都 import 它"
        "是合法的（共享底座被两边引用不报），但「同一个 `data_dir`」这个耦合要在配置分家时解决。",
        "",
        "`TYPE_CHECKING` 块里的 import **照算**（本检查走整棵 AST）：类型引用同样要在两仓之间"
        "解决（`app/api/v1/local.py` 的一句注释里写着同一条口径）。两处调用扫描都按**写出名字**"
        "的那种写法找：`xxx.retrieve_sources(...)` 与 `services.<字段>`，`getattr` 那种间接写法"
        "扫不到（今天全仓没有这么写的）。",
        "",
    ]
)


class Violation:
    """一条越界记录。"""

    def __init__(self, kind: str, path: Path, lineno: int, detail: str) -> None:
        self.kind = kind
        self.path = path
        self.lineno = lineno
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path.as_posix()}:{self.lineno}  [{self.kind}] {self.detail}"


# ---------------------------------------------------------------- 名单匹配


def _segment_matches(segment: str, pattern: str) -> bool:
    """名单条目的尾段：`skills*` 收 `skills` 与 `skills_x`（方案里的写法就是前缀通配）。"""
    if pattern.endswith("*"):
        return segment.startswith(pattern[:-1])
    return segment == pattern


def matches(module: str, entry: str) -> bool:
    """点分模块名是否落在这个名单条目里。

    两种形状（都来自方案 §3.1 的写法）：
    - 具体模块 / 包名：整段相等，或以 `条目.` 开头（`app.parsers` 收整个包）；
    - 尾段带 `*`：逐段比对，尾段前缀匹配。
    """
    if "*" in entry:
        entry_parts = entry.split(".")
        module_parts = module.split(".")
        if len(module_parts) < len(entry_parts):
            return False
        # `strict=False` 是写出来的：模块段数可以多于条目段数（通配条目收它的子模块），
        # 用 `strict=True` 会在这种正常情形下抛异常。
        return all(
            _segment_matches(part, pattern)
            for part, pattern in zip(module_parts, entry_parts, strict=False)
        )
    return module == entry or module.startswith(f"{entry}.")


def domain_of(module: str) -> str | None:
    """模块属于哪一域；不在任何名单里返回 None（= 未认领）。

    多条目同时命中时取**最具体的那个**（段数最多）：`app.storage.base` 与
    `app.storage` 若都被认领，具体条目说了算。同深度命中两个不同域属于名单数据冲突，
    由 `entry_conflicts()` 单独报出来。
    """
    best: str | None = None
    best_depth = -1
    for domain, entry in NAME_ENTRIES:
        if not matches(module, entry):
            continue
        depth = len(entry.split("."))
        if depth > best_depth:
            best, best_depth = domain, depth
    return best


def entry_conflicts() -> list[str]:
    """同深度、不同域的条目撞在一起（名单数据自相矛盾，得先修名单）。"""
    return [
        f"{entry} 同时被 {DOMAIN_LABELS[first]}与 {DOMAIN_LABELS[second]}认领"
        for first, entry in NAME_ENTRIES
        for second, other in NAME_ENTRIES
        if first != second
        and entry == other
        and first < second
    ]


# ---------------------------------------------------------------- 源码扫描


def module_index(app_dir: Path) -> dict[str, Path]:
    """点分模块名 → 文件路径。`__init__.py` 按**包名**登记（`app/services/__init__.py`
    → `app.services`），这样 `from app.services import chat` 里的 `chat` 才有东西可比。"""
    index: dict[str, Path] = {}
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(app_dir.parent).with_suffix("")
        parts = [part for part in rel.parts if part != "__init__"]
        parts = ["app", *parts[1:]] if parts and parts[0] == "app" else parts
        if parts:
            index[".".join(parts)] = path
    return index


def _absolute_base(node: ast.ImportFrom, current: str, package: str) -> str:
    """`from … import …` 的基模块（把相对导入还原成点分绝对名）。"""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    base_parts = parts[: len(parts) - node.level + 1]
    base = ".".join(base_parts) if base_parts else package
    return f"{base}.{node.module}" if node.module else base


def imported_modules(
    tree: ast.AST, current: str, index: dict[str, Path]
) -> list[tuple[int, str]]:
    """收集 import 的目标模块名，**把 `from 包 import 子模块` 解析到那个子模块**。

    比 `check_layering.py` 那版多做一步：`from app.services import chat` 的目标是
    `app.services`（包）而不是 `app.services.chat`（真正被引用的模块），按包名分类
    会把整条规则判成"没命中"。所以这里对每个 alias 试一次 `基模块.alias`，
    在磁盘上真有这个模块就用它。
    """
    package = current.rsplit(".", 1)[0] if "." in current else current
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(node, current, package)
            if not base:
                continue
            found.append((node.lineno, base))
            found.extend(
                (node.lineno, f"{base}.{alias.name}")
                for alias in node.names
                if f"{base}.{alias.name}" in index
            )
    return sorted(set(found))


def kb_surface_calls(tree: ast.AST) -> list[tuple[int, str, str]]:
    """`xxx.retrieve_sources(...)` 这类调用点：``(行号, 方法名, 接收者文本)``。"""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in KB_SURFACE_CALLS:
            continue
        found.append((node.lineno, node.func.attr, _receiver_text(node.func.value)))
    return found


def _receiver_text(node: ast.AST) -> str:
    """接收者的源码文本（只用于报告，截短以免把一整行塞进表格）。"""
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - 只在 AST 有脏节点时发生
        return "?"
    return text if len(text) <= 40 else f"{text[:37]}..."


def services_field_modules(root: Path) -> dict[str, str]:
    """组合根数据类的字段 → 它装的那个服务所在模块（机械解出来，不手抄）。

    §4 第 1 条说这个数据类是"单一组合根"：KB 与 Agent 的服务混装在一张图里。
    **经它取服务是属性访问，不是 import**——所以光扫 import 会看不见
    `services.ingest.submit(...)` 这类跨域调用（`agent_tools.py:1712` 就是这么调的）。
    这里靠注解把字段映射回模块：`ingest: IngestService | IngestGateway` +
    `from app.services.ingest import IngestService` → `app.services.ingest`，
    再交给 `domain_of` 分类。

    **2026-10-08 起看两个类**：拆组合根之后 KB 字段住在 `KbServices`、Agent 字段住在
    `Services`（`app/core/services.py`），两张表要合起来看——只看 `Services` 的话，
    KB 那半边会整体从这张表里消失（跨根访问随之静默不报）。

    拿不到注解（`workers: list[TaskWorker]` 这类要用下标里那个名字）就跳过那个字段——
    漏一个字段只是少一条记录，不会误报。
    """
    path = root / "backend" / "app" / "core" / "services.py"
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    fields: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in ROOT_CLASS_NAMES:
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            if item.target.id == KB_ACCESSOR:
                continue  # 过渡访问器本身不是"装了一个服务"的字段（见 KB_ACCESSOR 的说明）
            module = aliases.get(_annotation_head(item.annotation))
            if module and domain_of(module) in (KB, AGENT, SHARED):
                fields[item.target.id] = module
    return fields


def _annotation_head(annotation: ast.AST) -> str:
    """注解里第一个类型名：`DocumentService | EnqueueGateway` → `DocumentService`；
    `list[TaskWorker]` → `TaskWorker`；`LegacyImporter | None` → `LegacyImporter`。"""
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            return node.id
    return ""


def composition_root_accesses(tree: ast.AST, fields: dict[str, str]) -> list[tuple[int, str, str]]:
    """`services.<字段>` 属性访问：``(行号, 字段, 接收者文本)``。

    接收者按名字收窄（见 `COMPOSITION_ROOT_RECEIVERS`）——这个仓库里组合根就注入成
    `services` / `self._services` / `self.services` 三种写法，收窄之后同名的请求体字段
    （`payload.documents`）不会被算进来。

    **`services.kb.<字段>` 也收**（2026-10-08 拆组合根之后新增的那条路）：接收者文本记成
    `services.kb`，调用方据此判它是"经过渡访问器取 KB 服务"。不收它的话，跨根访问会在
    报告里静默消失——而这正是最该被看见的那一类（它标记着"还没画完的剥离线"）。
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in fields:
            continue
        receiver = _receiver_text(node.value)
        if receiver == "services" or receiver.endswith(COMPOSITION_ROOT_RECEIVERS):
            found.append((node.lineno, node.attr, receiver))
        # `services.kb.<字段>`：value 是 `Attribute(attr="kb")`，再往外是根接收者
        elif isinstance(node.value, ast.Attribute) and node.value.attr == KB_ACCESSOR:
            outer = _receiver_text(node.value.value)
            if outer == "services" or outer.endswith(COMPOSITION_ROOT_RECEIVERS):
                found.append((node.lineno, node.attr, f"{outer}.{KB_ACCESSOR}"))
    return found


class Scan:
    """一次完整扫描的结果。"""

    def __init__(self) -> None:
        self.violations: list[Violation] = []
        #: 模块名 → 域（含 None：未认领）
        self.domains: dict[str, str | None] = {}
        #: 未认领模块 → 它引用的 KB/Agent 域模块数（`import` 条数）
        self.blind_spots: dict[str, list[tuple[str, str, int]]] = {}
        #: 全部 `retrieve_sources` 调用点：(相对路径, 行号, 接收者, 调用方域)
        self.calls: list[tuple[str, int, str, str | None]] = []
        #: 经组合根取对侧服务的属性访问：(相对路径, 行号, 接收者, 字段, 服务所在模块)
        self.root_accesses: list[tuple[str, int, str, str, str]] = []
        #: 经 `Services.kb` 过渡访问器取 KB 服务：(同上的形状)，`receiver` 里带 `.kb`
        self.accessor_accesses: list[tuple[str, int, str, str, str]] = []
        #: 未认领模块 → 它经组合根取对侧服务的次数（盲区量化，第 7 节）
        self.root_blind_spots: dict[str, int] = {}
        #: 名单条目 → 在磁盘上匹配到的模块数（存在性核实）
        self.entry_hits: dict[str, list[str]] = {}
        self.parse_errors: list[tuple[Path, int, str]] = []
        self.files_scanned = 0


def scan_tree(root: Path) -> Scan:
    """扫 `backend/app/` 全部 .py。"""
    result = Scan()
    app_dir = root / "backend" / "app"
    if not app_dir.exists():
        return result
    index = module_index(app_dir)
    fields = services_field_modules(root)

    for entry in {entry for _, entry in NAME_ENTRIES}:
        result.entry_hits[entry] = sorted(
            module for module in index if matches(module, entry)
        )

    for module, path in sorted(index.items(), key=lambda item: str(item[1])):
        if path.name == "__init__.py":
            # `__init__.py` 不参与判定：它随所在包归属，包有没有归属由包内模块决定。
            # （否则每个包都会以"包名未认领"进清单，把真正要决策的文件淹掉。）
            continue
        source_domain = domain_of(module)
        result.domains[module] = source_domain
        result.files_scanned += 1

        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError as exc:
            result.parse_errors.append((path, exc.lineno or 1, exc.msg))
            continue

        display = path.relative_to(root)
        targets: dict[str, list[int]] = {}
        for lineno, target in imported_modules(tree, module, index):
            targets.setdefault(target, []).append(lineno)

        if source_domain is None:
            touches = [
                (target, domain_of(target), len(linenos))
                for target, linenos in sorted(targets.items())
                if domain_of(target) in (KB, AGENT)
            ]
            if touches:
                result.blind_spots[module] = touches
        else:
            for target, linenos in sorted(targets.items()):
                target_domain = domain_of(target)
                kind = _violation_kind(source_domain, target, target_domain)
                if kind is None:
                    continue
                for lineno in linenos:
                    result.violations.append(
                        Violation(
                            kind,
                            display,
                            lineno,
                            f"{DOMAIN_LABELS[source_domain]} import "
                            f"{DOMAIN_LABELS[target_domain]}模块 {target}",
                        )
                    )

        for lineno, name, receiver in kb_surface_calls(tree):
            result.calls.append((display.as_posix(), lineno, f"{receiver}.{name}", source_domain))

        for lineno, field, receiver in composition_root_accesses(tree, fields):
            target_module = fields[field]
            target_domain = domain_of(target_module)
            if source_domain is None:
                # 未认领模块经组合根取对侧服务：它没归属，不算越界，但**这正是盲区有多大**
                # ——数进第 7 节那张表。
                if target_domain in (KB, AGENT) and not _is_seam(target_module):
                    result.root_blind_spots[module] = result.root_blind_spots.get(module, 0) + 1
                continue
            kind = _root_violation_kind(
                source_domain, target_module, via_accessor=receiver.endswith(f".{KB_ACCESSOR}")
            )
            if kind is None:
                continue
            target_list = (
                result.accessor_accesses if kind == KB_ACCESSOR_KIND else result.root_accesses
            )
            target_list.append((display.as_posix(), lineno, receiver, field, target_module))
            result.violations.append(
                Violation(
                    kind,
                    display,
                    lineno,
                    f"{DOMAIN_LABELS[source_domain]}经组合根取 "
                    f"{DOMAIN_LABELS[target_domain]}服务 "
                    f"{receiver}.{field}（{target_module}）",
                )
            )

    result.calls.sort(key=lambda item: (item[0], item[1]))
    result.root_accesses.sort(key=lambda item: (item[0], item[1]))
    result.accessor_accesses.sort(key=lambda item: (item[0], item[1]))
    return result


def _is_seam(module: str) -> bool:
    """这个模块是不是三处已知接缝之一（或它的子模块）。"""
    return any(matches(module, seam) for seam in SEAMS)


def _root_violation_kind(
    source: str, target_module: str, *, via_accessor: bool = False
) -> str | None:
    """经组合根取对侧服务：是越界返回类别，否则 None。

    `target_module` 命中三处接缝时放行（`services.provider` 就是 `knowledge_provider`，
    它正是设计上要保留的那条通道）。混合体（`services.chat` / `services.tools`）按
    "只记录不拦门"处理——`MIXED_ROOT` 只记录。

    `via_accessor=True` 指这次访问走的是 `services.kb.<KB 字段>`（2026-10-08 拆组合根
    之后 Agent 侧取 KB 服务的那条过渡路）：单独一类，**只记录**——它是"剥离线还没画完"
    的标记，等 Agent 侧改走 KB 客户端窄面时这一格与这些访问一起删。
    """
    if _is_seam(target_module):
        return None
    target_domain = domain_of(target_module)
    if via_accessor and target_domain == KB:
        return KB_ACCESSOR_KIND
    if MIXED in (source, target_domain):
        return MIXED_ROOT
    if source == KB and target_domain == AGENT:
        return KB_TO_AGENT_ROOT
    if source == AGENT and target_domain == KB:
        return AGENT_TO_KB_ROOT
    return None


def _violation_kind(source: str, target: str, target_domain: str | None) -> str | None:
    """这一条 import 是不是越界；是则返回类别，否则 None。"""
    if target_domain is None or target_domain == source:
        return None
    # 混合体（§4 第 2/3/5 条）牵涉到的边只记录、不拦门：它今天本来就装着两边的东西，
    # 单看某一条 import 判"越界"没有意义——真正的动作是阶段 1 的对切。
    if MIXED in (source, target_domain):
        return MIXED_IMPORT
    if source == KB and target_domain == AGENT:
        return KB_TO_AGENT
    if source == AGENT and target_domain == KB:
        # 三处接缝：Agent 侧访问 KB 的**唯一**合法通道（方案 §2 事实 2）。
        # 命中即放行——这正是剥离后要保留的形态（`KnowledgeProviderClient`）。
        if any(matches(target, seam) for seam in SEAMS):
            return None
        return AGENT_TO_KB
    if source == SHARED:
        return SHARED_TO_DOMAIN
    return None


# ---------------------------------------------------------------- 报告


def _group_counts(violations: list[Violation]) -> list[tuple[str, int]]:
    """按文件聚合越界条数，倒序。"""
    counts: dict[str, int] = {}
    for item in violations:
        key = f"{item.path.as_posix()}（{item.kind}）"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _table(rows: list[tuple[str, int]]) -> list[str]:
    if not rows:
        return ["（无）"]
    return [
        "| 文件（越界类别） | 条数 |",
        "| --- | --- |",
        *(f"| `{key}` | {value} |" for key, value in rows),
    ]


def _call_verdict(module: str, path: str, domain: str | None) -> str:
    """这个 `retrieve_sources` 调用点算什么（定性的唯一出处）。"""
    for seam, label in (
        ("services/knowledge_client.py", "接缝一：协议定义"),
        ("services/knowledge_provider.py", "接缝二：KB 客户端内部转发"),
        ("services/remote_clients.py", "接缝三：KB 远端客户端内部转发"),
    ):
        if path.endswith(seam):
            return label
    if path.endswith("services/chat.py"):
        return "混合体 chat.py 内部（KB 检索本体 / 内部转发）"
    if domain == AGENT:
        return "**越界：Agent 域直接调 KB 检索**"
    if domain == MIXED:
        return "混合体内部调用（待对切）"
    return "未认领模块内的调用"


def build_report(root: Path, result: Scan) -> str:
    kb_to_agent = [item for item in result.violations if item.kind == KB_TO_AGENT]
    agent_to_kb = [item for item in result.violations if item.kind == AGENT_TO_KB]
    shared = [item for item in result.violations if item.kind == SHARED_TO_DOMAIN]
    mixed_imports = [item for item in result.violations if item.kind == MIXED_IMPORT]
    mixed_roots = [item for item in result.violations if item.kind == MIXED_ROOT]
    root_accesses = [
        item
        for item in result.violations
        if item.kind in (KB_TO_AGENT_ROOT, AGENT_TO_KB_ROOT)
    ]
    accessor_accesses = [item for item in result.violations if item.kind == KB_ACCESSOR_KIND]
    mixed = mixed_imports + mixed_roots

    missing = sorted(entry for entry, hits in result.entry_hits.items() if not hits)
    unclaimed = sorted(module for module, domain in result.domains.items() if domain is None)
    unclaimed_without_note = [module for module in unclaimed if module not in UNCLAIMED_NOTES]

    seam_calls = [item for item in result.calls if item[0].endswith(SEAM_FILES)]
    agent_calls = [
        item
        for item in result.calls
        if _call_verdict(_module_of(item[0]), item[0], item[3]).startswith("**")
    ]

    lines: list[str] = [
        "# 域间引用基线 v0.1（阶段 0）",
        "",
        "> 本文由 `python scripts/check_domains.py` 生成，每次重跑覆盖，**不要手改**。",
        "> 数据来源：《[kylab 知识库剥离方案 v0.1]"
        "(../../../kybase/kylab知识库剥离方案-v0.1.md)》§3.1 归属清单 + 2026-10-08 拍板口径；",
        "> 口径：KB 域与 Agent 域不得互引（Agent 侧只经三处已知接缝访问 KB）；",
        "> 共享底座被两边引用合法，但它自己不得 import 任何一侧域模块；",
        "> 混合体（`chat.py` / `tools.py` / `schemas.py`）与两侧之间的引用只记录、"
        "不拦门（动作是阶段 1 的对切）。",
        "> 本阶段**只报告不拦门**（未接进 `scripts/check-backend.sh`）："
        "先有基线，下一步才把红灯拧成门禁。",
        "",
        f"扫描范围：`backend/app/` 全部 Python 模块"
        f"（{result.files_scanned} 个非 `__init__` 模块）。",
        "",
        "## 1. 名单与越界总表",
        "",
        "| 类别 | 条数 | 是否拦门 |",
        "| --- | --- | --- |",
        f"| KB 域 import Agent 域（kb→agent） | {len(kb_to_agent)} | 是 |",
        f"| Agent 域 import KB 域（agent→kb，接缝外） | {len(agent_to_kb)} | 是 |",
        f"| 混合体牵涉的跨域引用（mixed） | {len(mixed)} | 否（待对切） |",
        f"| 共享底座 import 某一侧（shared→domain） | {len(shared)} | 否（两边都不该依赖） |",
        f"| 经组合根取对侧服务（属性访问，不是 import） | {len(root_accesses)} | "
        "否（另一条泳道） |",
        f"| 经 `Services.kb` 过渡访问器取 KB 服务 | {len(accessor_accesses)} | "
        "否（过渡标记，见第 6.2 节） |",
        f"| 未认出归属的模块（未认领） | {len(unclaimed)} | 否（需要决策） |",
        "",
        "名单本身的几张单子（条目数）：",
        "",
        f"- KB 域 {len(KB_MODULES)} 条；Agent 域 {len(AGENT_MODULES)} 条；"
        f"共享底座 {len(SHARED_MODULES)} 条；混合体 {len(MIXED_MODULES)} 条；"
        f"三处接缝 {'、'.join(f'`{seam}`' for seam in SEAMS)}。",
        "",
        "**读数字时注意**：2026-10-08 那一轮把 30 个原本「未认领」的模块补齐了归属，"
        "于是它们的边第一次进入统计——上一版基线里 `agent→kb` 是 8 处、这一版是 "
        f"{len(agent_to_kb)} 处，涨的那部分**不是新长出来的耦合，是原来的盲区变可见**"
        "（比如 `api_key` 的 `Caller` 契约被两侧共用，那一批 import 以前根本没进统计）。"
        "凡是从「未认领」变成「已认领」的模块，其边都经历过这一次跳变。",
    ]

    lines += ["", "## 2. 名单核实（逐个条目对磁盘）", ""]
    if missing:
        lines += ["名单所列但磁盘上**不存在**的条目（笔误或已删除的模块）：", ""]
        lines += [f"- `{entry}`" for entry in missing]
    else:
        lines += ["名单所列条目**逐个在磁盘上都有对应模块**（无笔误、无遗留）。"]
    conflicts = entry_conflicts()
    if conflicts:
        lines += ["", "名单自相矛盾（同深度条目被两个域认领）：", ""]
        lines += [f"- {item}" for item in conflicts]
    lines += [
        "",
        "与方案 §3.1 的写法相比，两处口径差（不是笔误，但要说明）：",
        "",
        "- `core/` 按**整包**在册，方案括注只点了 8 个文件；`core/services.py`（§4 第 1 条点名的"
        "单一组合根）与 `core/storage.py`、`core/lazy_httpx.py` 都在这个前缀里。",
        "- 尾段通配按 2026-10-08 口径改成 `skill*` / `schedule*`（方案原本写 `skills*.py` / "
        "`schedules*.py`，收不到 `skill_blurb.py` 与 `schedule_runner.py`）；其余通配"
        "（`memory*` / `archive*` / `conversation*` / `backup_*`）按字面收。",
        "",
        "### 2.1 有判断成分的归属（逐条写出处）",
        "",
        "| 模块（或一族） | 归属 | 判断 |",
        "| --- | --- | --- |",
    ]
    for module, note in CLAIM_NOTES.items():
        domain = domain_of(module)
        label = DOMAIN_LABELS[domain] if domain else "**未认领**"
        lines.append(f"| `{module}` | {label} | {note} |")

    lines += [
        "",
        "## 3. 未认领清单（问题②）",
        "",
    ]
    if unclaimed and all(module in UNCLAIMED_NOTES for module in unclaimed):
        lines += [
            "这一节现在**只剩三处接缝**——它们不是「还没决定」，而是**刻意不归任何一侧**："
            "归哪边都不对（Agent 域经它访问 KB 是设计如此，`agent→kb` 对它放行）。"
            "除此之外，`backend/app/` 下每个非 `__init__` 模块都有归属。",
            "",
            "（没有名单 = 没有约束，这条教训来自 `check_layering.py` 的 L6：`app/agent_tools.py` "
            "当时住在 app 根，一条规则都不作用于它。所以这一栏只要有**非接缝**的模块，"
            "就说明又有东西没被认领。）",
            "",
        ]
    else:
        lines += [
            "未被任何一张名单认领的模块。**没有名单 = 没有约束**（`check_layering.py` 的 L6 就是"
            "被这件事教出来的），所以这一节是必须消灭的名单。",
            "",
        ]
    if not unclaimed:
        lines += ["（无：`backend/app/` 下每个非 `__init__` 模块都有归属。）"]
    else:
        lines += ["| 模块 | 判断 |", "| --- | --- |"]
        for module in unclaimed:
            note = UNCLAIMED_NOTES.get(
                module, "**需要决策**：方案 §3.1 未列，也还没有人给它定归属"
            )
            lines.append(f"| `{module}` | {note} |")
        if unclaimed_without_note:
            lines += [
                "",
                f"其中 {len(unclaimed_without_note)} 个**没有判断标注**"
                "（新出现的文件或本表未覆盖）："
                + "、".join(f"`{module}`" for module in unclaimed_without_note),
            ]

    lines += ["", "## 4. 越界明细", ""]
    for title, items, note in (
        (
            "4.1 KB 域 → Agent 域（禁）",
            kb_to_agent,
            "KB 服务剥离后这些 import 在 KB 仓里不存在，必须先在 kylab 仓内切断。",
        ),
        (
            "4.2 Agent 域 → KB 域（只许经三处接缝）",
            agent_to_kb,
            "接缝（`knowledge_client` / `knowledge_provider` / `remote_clients`）内的引用已放行，"
            "这里列的是接缝外的全部。",
        ),
        (
            "4.3 混合体 → / ← 两侧（只记录，不拦门）",
            mixed,
            "`chat.py` / `tools.py` / `schemas.py` 今天同时装着两边的东西（§4 第 2/3/5 条），"
            "按 2026-10-08 口径**单列第三类**：它们对两侧的引用只记录，"
            "真正的动作是阶段 1 的对切"
            "（`tools.py` 的切分线是 `_KB_TOOLS`；"
            "`chat.py` 是 `retrieve_sources` 与工具循环对切）。"
            f"其中 import {len(mixed_imports)} 处、经组合根取服务 {len(mixed_roots)} 处。",
        ),
        (
            "4.4 共享底座 → 某一侧（共享底座必须两边都不依赖）",
            shared,
            "不拦门：共享底座被两边**引用**是合法的，但它自己不该 import 任何一侧。"
            "修法在阶段 1（拆组合根 / 存储分家 / 配置分家）。",
        ),
    ):
        lines += [f"### {title}", "", f"{len(items)} 处。{note}", ""]
        if items:
            lines += [f"- `{line}`" for line in (str(item) for item in items)]
        else:
            lines += ["（无）"]
        lines += [""]

    lines += ["## 5. Top 违规文件（按越界条数）", ""]
    for title, items in (
        ("KB → Agent", kb_to_agent),
        ("Agent → KB（接缝外）", agent_to_kb),
        ("混合体牵涉的跨域引用", mixed),
        ("共享底座 → 某一侧", shared),
        ("经组合根取对侧服务", root_accesses),
        ("经 kb 访问器取 KB 服务", accessor_accesses),
    ):
        lines += [f"### {title}", ""]
        lines += _table(_group_counts(items))
        lines += [""]

    lines += [
        "## 6. 经组合根取对侧服务的属性访问（import 扫不到的那一半）",
        "",
        "### 6.1 直接跨根取（`services.<对侧字段>`）",
        "",
        "拆组合根之前，`Services` 数据类把两边的服务混装在一张图里（方案 §4 第 1 条），"
        "于是 `services.ingest.submit(...)` 这种跨域用法**完全不走 import**——它只是取一个字段。"
        "下表按「接收者名是组合根（`services` / `self._services` / `self.services`）+ 字段名是"
        "`Services` 的字段 + 该字段的注解类型属于对侧域」三条同时成立判，共 "
        f"{len(root_accesses)} 处。**不拦门**：这是另一条泳道"
        "（阶段 1 第 4/5 步的调用面收口），且它按名字判定，确定性略低于 import 那几条。"
        "混合体与未认领模块的同类访问记在第 4.3 节与第 7 节。",
        "",
    ]
    if not root_accesses:
        lines += [
            "（无。2026-10-08 拆组合根之后，Agent 侧与混合体取 KB 服务的那批访问已经"
            "改写成 `services.kb.<字段>`——见下面 6.2。）",
        ]
    else:
        lines += ["| 位置 | 表达式 | 服务的模块 |", "| --- | --- | --- |"]
        for path, lineno, receiver, field, target_module in result.root_accesses:
            lines.append(f"| `{path}:{lineno}` | `{receiver}.{field}` | `{target_module}` |")
    lines += [""]

    lines += [
        "### 6.2 经过渡访问器取（`services.kb.<KB 字段>`）",
        "",
        "`KbServices` 拆出来之后，Agent 根上留了一格 `kb`（见 `core/services.py` 里那个字段的"
        "说明）：Agent 侧与混合体**暂时**经它取 KB 服务，共 "
        f"{len(accessor_accesses)} 处。**这是过渡标记，不是终态**——剥离线画完之后这些访问"
        "要么改走 `provider` 的窄面、要么随调用点搬到 KB 侧，这一格随之删。"
        "列在这里是为了让「还没画完的线」看得见；它不拦门。",
        "",
    ]
    if not accessor_accesses:
        lines += ["（无。）"]
    else:
        lines += ["| 位置 | 表达式 | 服务的模块 |", "| --- | --- | --- |"]
        for path, lineno, receiver, field, target_module in result.accessor_accesses:
            lines.append(f"| `{path}:{lineno}` | `{receiver}.{field}` | `{target_module}` |")
    lines += [""]

    lines += [
        "## 7. 未认领模块的引用规模（盲区有多大）",
        "",
        "未认领模块今天不受任何域间约束。下表是它们实际引用的 KB / Agent 域模块数——"
        "数越大，切这两刀时越要先给它定性。",
        "",
    ]
    if not result.blind_spots and not result.root_blind_spots:
        lines += ["（无：未认领模块都没有 import 任何一侧域模块。）"]
    else:
        lines += [
            "| 未认领模块 | 引用的域模块数 | 其中 KB 域 | 其中 Agent 域 | 经组合根取对侧服务 |",
            "| --- | --- | --- | --- | --- |",
        ]
        rows = set(result.blind_spots) | set(result.root_blind_spots)
        for module in sorted(rows, key=lambda name: (-_blind_spot_weight(result, name), name)):
            touches = result.blind_spots.get(module, [])
            kb_count = sum(1 for _, domain, _ in touches if domain == KB)
            agent_count = sum(1 for _, domain, _ in touches if domain == AGENT)
            root_count = result.root_blind_spots.get(module, 0)
            lines.append(
                f"| `{module}` | {len(touches)} | {kb_count} | {agent_count} | {root_count} |"
            )

    lines += ["", LIMITATIONS_SECTION]

    lines += [
        "## 9. 与方案论断的对照（问题①）",
        "",
        "方案 §2 / §9 的论断：「**Agent → KB 的进程内调用，全仓只有一处**："
        "`services/agent_tools.py:1263` → `services.chat.retrieve_sources(...)`」。",
        "论断说的是**调用**，不是 import；而 `chat.py` 是混合体（两张名单都没收它），"
        "按模块归属判不出来。所以这里按方法名 `retrieve_sources` 扫全部调用点，逐个定性：",
        "",
    ]
    if not result.calls:
        lines += ["（没扫到任何 `retrieve_sources` 调用点——论断不成立，因为它至少有一处。）"]
    else:
        lines += ["| 调用点 | 接收者 | 调用方域 | 定性 |", "| --- | --- | --- | --- |"]
        for path, lineno, receiver, domain in result.calls:
            label = DOMAIN_LABELS[domain] if domain else "未认领"
            verdict = _call_verdict(_module_of(path), path, domain)
            lines.append(f"| `{path}:{lineno}` | `{receiver}` | {label} | {verdict} |")
        lines += [
            "",
            f"其中定性为越界的共 **{len(agent_calls)} 处**，"
            f"接缝内的调用 {len(seam_calls)} 处是设计如此。",
            "",
            "**结论：论断不属实。** 按调用点重数，Agent 侧直接调 KB 检索的有 "
            f"{len(agent_calls)} 处（`agent_tools.py:1263` 只是其中之一）；"
            "再加第 6 节那些经组合根取 KB 服务的属性访问（同一份 `agent_tools.py` 里就有 "
            "`services.ingest.submit` / `services.documents.enqueue_ingest` / "
            "`services.tabular.*`），「全仓只有一处」这个量级与现状差一个数量级。"
            "它作为「**工具循环里只有一处**」是成立的。"
            "（2026-10-08 口径把 `schedule_runner.py` 定在 Agent 侧之后，"
            "它那一处也从「未认领」变成了名正言顺的第 4 个越界调用点。）",
        ]

    composition_root = sum(
        1 for item in shared if item.path.as_posix().endswith("core/services.py")
    )
    #: `agent→kb` 的两簇：一簇是**契约型**（import `api_key` 的身份类型与权限常量），
    #: 一簇是**功能型**（真要调 KB 的某个功能）。切法完全不同，所以分开数。
    contract_imports = [item for item in agent_to_kb if "app.services.api_key" in item.detail]
    functional = [
        item for item in agent_to_kb if "app.services.api_key" not in item.detail
    ]
    functional_files = sorted({item.path.as_posix() for item in functional})
    lines += [
        "",
        "## 10. 下一步（按越界密度排）",
        "",
        "第 4.2 节按**切法**分两簇——它们不是同一件事：",
        "",
    ]
    if not contract_imports:
        lines += [
            "### 10.1 契约型（0 处）——**2026-10-08 已切完**",
            "",
            "身份契约（`Caller` / `READ` / `WRITE` / `LOCAL_CALLER` / `LOCAL_USER_ID`）已搬到 "
            "`app.core.caller`，`services/api_key.py` 只留账号体系的实现"
            "（`check_access` / `visible_kb_ids` / `authenticate` / `resolve_caller`）并从那里"
            "再导出——**KB 侧调用点一行没动**（还有 17 个 KB 侧 api 模块 + `mcp_server/auth.py` "
            "经再导出取它）。需要契约的模块（Agent 侧 19 个、共享侧 2 个）改从共享底座取，"
            "这一簇随之归零。同类的一件：`parsers/html_format.py` 例外上移到 "
            "`app.core.html_format`（Agent 的阅读模式要用它，旧位置留壳），"
            "`services/web.py` 那处越界也一并消失。",
            "",
            "**顺带说明**：`app.api.auth`（请求级鉴权依赖）与 `core/services.py` 仍从 "
            "`api_key` 取（前者混取 `resolve_caller`、后者取 `ApiKeyService` 实现），"
            "那是共享底座 → KB 的方向，只记录、不拦门——真正的切法是阶段 1 的拆组合根。",
            "",
        ]
    else:
        lines += [
            f"### 10.1 契约型（{len(contract_imports)} 处）：身份类型与权限常量被两侧共用",
            "",
            "`app.services.api_key` 里同时住着两样东西：**身份契约**（`Caller` 这个 frozen "
            "dataclass、`READ` / `WRITE` / `LOCAL_CALLER` 三个常量）和**账号体系的实现**"
            "（`check_access` / `visible_kb_ids`，方案 §4 第 7 条说它「既是 Agent 工具准入闸，"
            "又直接读 KB 表」）。Agent 侧的每个 api/服务模块都要那个契约（拿 `Caller` 当类型、"
            "判 `WRITE`），于是它们全被算成 agent→kb。**切法**：把契约上移共享底座"
            "（`app/core/caller.py`），`api_key.py` 再导出一次，行为不变。",
            "",
        ]
    lines += [
        f"### 10.2 功能型（{len(functional)} 处）：真的要调 KB 的功能",
        "",
    ]
    if functional:
        lines += [
            "逐条性质不同，一条条说（文件清单："
            + "、".join(f"`{name}`" for name in functional_files)
            + "）：",
            "",
            "- `api/v1/local.py` → `api/v1/stats`：本机档要 KB 的 dashboard 读数——"
            "**等「KB 客户端能拿这份读数」**（接缝收口那一批）。",
            "- `workers/local_worker.py` → `workers/queue_worker`：只取两个间隔常量——"
            "**等「`tasks` 队列归属落地」**（方案 §5.3：队列随 KB，Agent 定时任务另起机制）。",
            "- `api/v1/backup.py` → `api/v1/provider`：复用 KB 提供者的 `caller_brief`"
            "（一个把 `Caller` 折成「提供者视角」的小函数，返回 KB 侧的响应模型）——"
            "**在备份侧复制一份**即可（双份并存期，权威在 KB 侧）。",
            "",
        ]
    else:
        lines += ["（无。）", ""]
    lines += [
        "**另一处同类、但不走 import 的**：`api/v1/settings.py` 归 Agent 之后，"
        "它「测试向量化连接」那一格经组合根取 KB 的 `services.embedder` / `services.reranker`"
        "（第 6 节，只记录）——那是功能耦合，等 KB 提供「测连接」入口时一起收。",
        "",
        "### 10.3 其余",
        "",
        f"- 共享底座的越界（`core/services.py` 组合根 {composition_root} 条）随方案 §6 阶段 1 "
        "第 3 步一起做；它切完，第 6 节的属性访问也就有了可判定的归属。",
        "- 混合体（第 4.3 节）按 §6 阶段 1 第 4/5 步对切——它们只是记录，不是红灯。",
        "",
    ]
    pending = [module for module in unclaimed if not _is_seam(module)]
    if pending:
        lines += [
            "还有一件小事：第 3 节的未认领清单里，"
            f"{len(unclaimed) - len(pending)} 个是三处接缝（刻意不归任何一侧，见那里的说明），"
            "真正等你一句话定性的是 "
            + "、".join(f"`{module}`" for module in pending)
            + "。",
            "",
        ]
    return "\n".join(lines) + "\n"


def _blind_spot_weight(result: Scan, module: str) -> int:
    """盲区的量级：import 条数 + 经组合根取对侧服务的次数。"""
    return len(result.blind_spots.get(module, [])) + result.root_blind_spots.get(module, 0)


def _module_of(path: str) -> str:
    """报告里的仓库相对路径 → 点分模块名（`backend/app/services/chat.py` →
    `app.services.chat`）。"""
    rel = path.removeprefix("backend/").removesuffix(".py")
    return rel.replace("/", ".")


def print_summary(root: Path, result: Scan, report_path: Path) -> None:
    """终端人话报告（完整版在文件里）。"""
    kb_to_agent = [item for item in result.violations if item.kind == KB_TO_AGENT]
    agent_to_kb = [item for item in result.violations if item.kind == AGENT_TO_KB]
    shared = [item for item in result.violations if item.kind == SHARED_TO_DOMAIN]
    mixed = [item for item in result.violations if item.kind in (MIXED_IMPORT, MIXED_ROOT)]
    root_accesses = [
        item
        for item in result.violations
        if item.kind in (KB_TO_AGENT_ROOT, AGENT_TO_KB_ROOT)
    ]
    unclaimed = sorted(module for module, domain in result.domains.items() if domain is None)

    print(f"扫描 backend/app/：{result.files_scanned} 个模块")
    print(
        f"越界：KB→Agent {len(kb_to_agent)} 处（拦门）、Agent→KB {len(agent_to_kb)} 处（拦门）、"
        f"混合体牵涉 {len(mixed)} 处（记录）、共享底座→某一侧 {len(shared)} 处（记录）、"
        f"经组合根取对侧服务 {len(root_accesses)} 处（记录）"
    )
    for violation in result.violations:
        print(f"  {violation}")

    print(f"\n问题② 未认领模块 {len(unclaimed)} 个：")
    for module in unclaimed:
        note = UNCLAIMED_NOTES.get(module, "需要决策")
        print(f"  {module} —— {note}")

    agent_calls = [
        item
        for item in result.calls
        if _call_verdict(_module_of(item[0]), item[0], item[3]).startswith("**")
    ]
    print("\n问题① 「进程内跨域调用只有 agent_tools.py:1263 一处」是否属实：")
    print(f"  不属实。Agent 侧直接调 `retrieve_sources` 的调用点共 {len(agent_calls)} 处：")
    for path, lineno, receiver, _domain in agent_calls:
        print(f"    {path}:{lineno} → {receiver}")
    print("  另：接缝内的调用（知识库客户端自己转发）与混合体 chat.py 内部调用不在此列；")
    print(f"  经组合根取对侧服务的属性访问另有 {len(root_accesses)} 处（见报告第 6 节）。")

    print(f"\n完整报告：{report_path.relative_to(root).as_posix()}")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    result = scan_tree(root)
    if not (root / "backend" / "app").exists():
        print(f"找不到 {root / 'backend' / 'app'}：请传仓库根目录")
        return 2

    for path, lineno, message in result.parse_errors:
        print(f"{path.relative_to(root).as_posix()}:{lineno}  语法错误：{message}")

    report_path = root / REPORT_RELATIVE
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(root, result), encoding="utf-8")

    print_summary(root, result, report_path)

    # 退出码只认 KB↔Agent 的 **import** 越界（本阶段口径）。
    # 共享底座越界、组合根属性访问与"未认领"都只记录：前者归属未拍板、后两者是另一条泳道，
    # 今天把它们拧成红灯只会逼着绕过（详见 BLOCKING_KINDS 的说明）。
    blocked = [item for item in result.violations if item.kind in BLOCKING_KINDS]
    recorded = [
        item for item in result.violations if item.kind not in BLOCKING_KINDS
    ]
    if blocked:
        print(
            f"\n共发现 {len(blocked)} 处 KB↔Agent import 越界（另有 {len(recorded)} 处"
            f"混合体/共享底座/组合根引用只记录、未接门禁——见报告第 4.3 / 4.4 / 6 节）。"
        )
        return 1
    print(f"\nKB↔Agent import 无越界（另有 {len(recorded)} 处只记录）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
