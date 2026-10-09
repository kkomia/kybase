"""v1 路由聚合。

鉴权不在这里挂全局依赖，而是**逐个端点显式声明**（``ReadDep`` / ``WriteDep``）：
全局依赖无法表达"这个端点需要读写、那个只要只读、设置端还只认管理员"，
只能一律放同一个档位——那等于没有作用域隔离。

**两张路由表，按部署档挂哪一张**（M2 §4.1，`app/main.py` 判档）：

- ``api_router``：**服务器档**（默认）那张表——知识库 + 备份提供者 + 健康探针，
  外加本机档要调的那几条（导出 / 握手 / 前端资源包）；
- ``local_router``：**本机档**（桌面壳的边车）那张**白名单**——只挂"数据在本机、
  本机服务得了"的那些。白名单而不是黑名单：本机没有知识库数据源（在 NAS 上），
  黑名单意味着"新加的端点默认挂上去"，而它们会在第一次被点到时才 500 ✗。

## NAS 网页端退役（2026-10-04）：服务器档不再提供**会话面**

NAS 上的网页端与桌面端共用同一套前端，而浏览器里没有壳 → 会话 / 笔记 / 设置 /
工作区 / 定时任务 / MCP 那几页读的是 NAS 的 PG（`app/api/sidecar.ts` 的
`resolveLocalBase` 在"无壳"那一支直接回 `API_BASE`）。产品判定：**那一个网页端退役**，
NAS 上那份会话数据不迁移、直接丢；服务器档从此只对外提供**知识库管理台 + 备份 +
健康**（架构设计 v0.3 §2：会话面归本机，服务器侧保留的浏览器界面只该是知识库管理台）。

落地形态是**甲：只删挂载，不删表、不写 Migration、不动 `app/storage/**`**——
可回退（把下面那六行 include 加回来即可），也不动 NAS 的 PG 数据。

**唯一保留的一条会话面端点是 `/conversations/export`**：它是"想再迁就有路"的那一份
（M2 阶段 5 的迁移来源，`services/legacy_import.py::HttpExportSource` 只认它；
按点恢复走的是快照那一条 `SnapshotFileSource`，与它无关）。删掉它等于把
"以后还能把 NAS 上那份会话搬进来"这条路一起删掉，而留着它只是只读一条流。
它的挂法见下面 `_export_only`——**只带这一个端点**，不整 include 那个 router。

## `/chat/*` 那一族的处置（2026-10-05，同一轮退役）

`api/v1/chat.py` 里那一族（`/chat/stream`、`/chat`、`/chat/turns/{id}/live`、
`/conversations/{id}/resume`、`.../steps/{i}/retry`、`/chat/approvals/{id}`、
`/chat/turns/record`、`/conversations/{id}/events`、`/chat/context-usage`）**同样从服务器档退掉**：
它们整条链路都钉在会话面上（读会话历史、写会话消息与事件日志），而会话面上面已经摘了
——留着就是一片"点得到、点下去 404/500"的路 ✗。

**本机档的对话不靠它** ✓：桌面那条链走**边车**的 `/turn*`（`app/sidecar.py`，
同一份 `ToolLoop` 与同一批服务，写的是本机的会话），所以这一族在**两个档里都不再对外**
——这正是"它唯一的消费者（NAS 网页端）退役"的直接后果。形态仍是**甲**：只摘挂载，
不删代码、不删表 ✓（把下面 `_chat_survivors` 换成整 include 就回退了）。

**只剩一条例外照旧挂着**，它不与会话面耦合：

- ``GET /chat/suggested-questions``：推荐问题从**知识库**里已存的分段问题来
  （`services/suggested_questions.py`）——那是 NAS 上的数据，服务器档正是它的家，
  而且**只有这一档有它**（本机档的知识库在别处，问题清单得问提供者）。

``GET /chat/commands``（命令目录）**2026-10-05 又改了一次挂法**：上一轮它从
"只挂服务器档"挪进本机档（见下面 `local_router`），服务器这一份却还留着 ——
于是同一个端点有了**两份**目录（这台机器的 / NAS 的），而它的数据本来只属于前者：
`data/commands/` + 仓库命令 + `<data_dir>/skills/` 与被禁用的技能都在这台机器上，
执行那一轮也在本机（边车）⇒ **服务器档这一条摘掉**，只剩本机档那一条（目录与执行同源）。
浏览器那一档（没有本机后端）因此**没有**命令目录可读：`listCommands` 拿到 404 就回空列表，
`/` 菜单在那一种形态下是空的（那个形态只有 NAS 上的知识库管理台，没有对话页）。
"""

from fastapi import APIRouter

from app.api.v1 import (
    api_keys,
    auth,
    avatars,
    backup,
    chat,
    chunks,
    conversations,
    data_sources,
    documents,
    folders,
    frontend,
    health,
    knowledge_bases,
    lifecycle,
    local,
    maintenance,
    mcp_servers,
    memory,
    model_proxy,
    model_registry,
    notes,
    plugins,
    provider,
    sandbox,
    schedules,
    search,
    settings,
    shares,
    site_icons,
    skills,
    stats,
    tabular,
    tasks,
    users,
    web,
    webhooks,
    wiki,
    workspaces,
)

api_router = APIRouter()
# 健康探针不鉴权：容器编排靠它判断存活，带鉴权会让 readiness 探针误判
api_router.include_router(health.router, tags=["health"])
# 认证端点自己管鉴权（/auth/status 公开、/auth/setup 只在无账号时开放），见 api/v1/auth.py
api_router.include_router(auth.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(folders.router)
api_router.include_router(search.router)
# 知识库提供者握手（M3 阶段 1）：**服务器档专属**——提供者是这台 NAS，
# 本机档是客户端角色，所以它在下面那张白名单里是**故意不挂**的
# （见 `api/v1/provider.py` 的模块头）
api_router.include_router(provider.router)
# 备份提供者（M5 阶段 1）：同样**服务器档专属**——NAS 侧持 S3 凭据收快照（路 B），
# 本机档是客户端角色（它调 `/backup/*`，不提供它们），见 `api/v1/backup.py` 的模块头
api_router.include_router(backup.router)
# 会话链路那一族**从服务器档退掉**（见模块头"`/chat/*` 那一族的处置"）：
# 用"只带上幸存的那一条"而不是整 include，理由与下面 `_export_only` 逐字同一条——
# 路径、依赖、响应模型、OpenAPI 说明还是原来那些对象，**不另写一份实现**，
# 于是"下次改这个端点时漏掉一边"这件事不可能发生。
#
# 留下的这一条**与"不摆注定失败的路"这条规矩不冲突**：它读的是 NAS 库里的分段问题，
# 一个字节都不碰会话面 ✓（理由见模块头）。`/chat/commands` 原先也留在这里，
# 2026-10-05 摘掉——它的数据属于**这台机器**（见模块头那一段），本机档那一条才是它的家。
_chat_survivors = APIRouter()
_chat_survivors.routes.extend(
    route
    for route in chat.router.routes
    if getattr(route, "path", "") == "/chat/suggested-questions"
)
api_router.include_router(_chat_survivors)
api_router.include_router(stats.router)
api_router.include_router(tasks.router)
api_router.include_router(api_keys.router)
# 会话面里**只留导出这一条**（见模块头那一段）：它是 NAS → 本机那条迁移唯一的来源，
# 也是"想再迁就有路"的那份保障。**不整 include `conversations.router`** ——
# 那会把列表 / 详情 / 消息 / 产物 / 文件区一起挂出来，那些就是这一轮要退掉的东西。
#
# 用"过滤出这一个端点"而不是"另写一份导出实现"：路径、依赖（`require_read`）、
# 响应模型、OpenAPI 说明全部还是那一个对象，两处实现分叉的风险为零（写第二份
# 只会在下一次改导出格式时漏掉一边）。
_export_only = APIRouter()
_export_only.routes.extend(
    route
    for route in conversations.router.routes
    if getattr(route, "path", "") == "/conversations/export"
)
api_router.include_router(_export_only)
api_router.include_router(chunks.router)
api_router.include_router(model_registry.router)
api_router.include_router(users.router)
# 头像图片（v0.29）：**不鉴权、只认签名**——`<img src>` 带不了 Authorization 头，
# 而签名绑定了"哪个用户的哪张图"（见 api/v1/avatars.py）
api_router.include_router(avatars.router)
api_router.include_router(lifecycle.router)
api_router.include_router(data_sources.router)
api_router.include_router(shares.router)
api_router.include_router(tabular.router)
api_router.include_router(webhooks.router)
api_router.include_router(maintenance.router)
api_router.include_router(wiki.router)
# 记忆（v0.14 三期）：与知识库是**两个池子**，所以单独一组 /memory
api_router.include_router(memory.router)
# 技能（v0.15）：磁盘上的 SKILL.md，目录进提示词、正文按需展开
api_router.include_router(skills.router)
# 插件包（v0.43）：插件 = 一个目录 + plugin.json，目录即本地市场；
# 与上面那组「插件」（MCP 服务）是两件事——那是外部服务，这是磁盘上的能力包
api_router.include_router(plugins.router)
# 沙箱执行（v0.16）：内核级隔离 + 策略闸（管理员专属）
api_router.include_router(sandbox.router)
# 站点图标（D11-②）：浏览器不直连第三方站点，图标由本机缓存代理
api_router.include_router(site_icons.router)
# 模型代理（Phase B · P2）：服务器用自己的 key 调模型，把增量透传给边车/壳
api_router.include_router(model_proxy.router)
# 前端资源包（v0.56）：桌面壳取界面的那两份（版本清单 + 整包）——
# "服务器发了新前端、客户端下次启动自动用上"那条承诺的服务器半边
api_router.include_router(frontend.router)


# ====================================================================== 本机档
#
# **白名单**（M2 §4.2）：本机档只挂"数据在本机、本机服务得了"的端点。逐个说清理由，
# 因为这张表是"本机运行时到底能做什么"的唯一定义，而漏挂一个与错挂一个都看不出来：

local_router = APIRouter()
# 探活：桌面壳与界面都要能问一句"本机后端在不在"
local_router.include_router(health.router, tags=["health"])
# 会话 / 消息 / 事件 / 产物 / 文件区全在本机（`/files*` 走本机对象存储）
# —— NAS 网页端退役之后，这一组（连同下面笔记 / 设置 / 工作区 / 定时任务 / MCP）
# **只在本机档存在**：服务器档那张表里只留了 `/conversations/export`（见模块头）。
local_router.include_router(conversations.router)
# 笔记落本机
local_router.include_router(notes.router)
# 设置落本机（运行期配置表 `app_settings` 在这份库里）
local_router.include_router(settings.router)
# 模型凭据与注册表落本机（v0.3 §1："模型凭据走本机"）
local_router.include_router(model_registry.router)
# 工作区是机器本地的路径
local_router.include_router(workspaces.router)
# 定时任务的产物是会话（会话在本机）
local_router.include_router(schedules.router)
# 外部 MCP 服务配置属于这台机器
local_router.include_router(mcp_servers.router)
# 记忆本体本来就在 `data_dir/memory`
local_router.include_router(memory.router)
# 技能目录 = `<data_dir>/skills/` + 仓库自带 `skills/` + `~/.agents/skills`（v0.15）：
# 三处都在**这台机器**上，而启停状态存在本地 `app_settings`（`chat.disabled_skills`）。
# 所以它不是一个"服务器能力"——`SkillService` 的目录随 `settings.data_dir` 走，
# 本机档的 data_dir 就是本机的那个（见 `core/services.py` 的组合根）。
local_router.include_router(skills.router)
# 插件包（v0.43）：目录 = `<data_dir>/plugins/` + 仓库自带 `plugins/`，
# 启停/屏蔽写在本地 `app_settings`（`plugins.enabled.*`）——同上，权威在本机的 data_dir。
local_router.include_router(plugins.router)
# 沙箱（v0.16）：**它就是"在这台机器上执行"**（隔离探测 / 策略闸 / 工作区三样都在本机，
# 见 api/v1/sandbox.py 与 services/isolation.py）。边车的工具面早就在本机跑命令了，
# 这一组只是把同一件事给界面一个直接入口——三道闸（管理员 / 策略 / 隔离）一道没绕。
#
# **只挂两条，不挂 `POST /sandbox/exec`**（2026-10-05）：那是一条**裸 HTTP 执行口**
# （一个 body 里写命令就执行），今天**没有任何前端/客户端调用它**——界面上那条链是
# `GET /sandbox`（看这台机器有什么隔离）+ `POST /sandbox/plan`（只算不跑，先把 argv
# 摆出来给人看），而 Agent 真正要执行时走的是**进程内**的 `services/agent_exec.py`
# （工具调用那条路，带模式闸 / 权限档 / 计划门闸 + 审批），不经过这个 HTTP 口。
# 它同时缺三样一般做法都要求的东西：**Origin / Host 校验**（MCP 规范对本地服务是
# `MUST`；Jupyter / code-server / Ollama 那类本机执行口都带令牌或 Host 校验）、
# **本地令牌**、以及 `approved` 由**请求方自填**（自己说"我批准了"就过闸）。
# 留着它=在本机开一个"任何人（含浏览器里任意页面通过 CSRF 打回环）都能执行命令"
# 的洞，而它服务的那一件事已经在别处有了更严的入口。
# 服务器档**照旧有它**（管理员在 NAS 上执行，见 `api_router` 那一行）：这一条只管
# 本机档那张白名单。
_sandbox_local = APIRouter()
_sandbox_local.routes.extend(
    route
    for route in sandbox.router.routes
    if getattr(route, "path", "") in ("/sandbox", "/sandbox/plan")
)
local_router.include_router(_sandbox_local)
# 站点图标（D11-②）：抓取与 30 天磁盘缓存都在本机（`<data_dir>/site-icons/`，见
# services/site_icons.py）——它本来就是"本机代浏览器去取"，服务器那一档才是顺带。
local_router.include_router(site_icons.router)
# 网页取页（阅读模式）：两个请求都是**这台机器**替界面发出去的——"读一页正文"与
# "探一次对方让不让嵌"。同上：出口在本机，服务器档不挂（NAS 那一侧没有"某个用户正在
# 读一页"这个场景，挂上去只会多出一条能被外部打进来、却没人调用的抓取口，见 web.py）。
local_router.include_router(web.router)
# 本机档专属：`/local/status`（导入的 `/local/import*` 与知识库提供者的
# `/local/provider` 都在这个 router 上——三样都是"只在本机档成立"的东西，见 local.py）
local_router.include_router(local.router)
# 几条薄重声明的只读端点（事件日志 / 上下文用量 / 用量面板）——**不整 include
# `chat.router` 或 `stats.router`**：那两个 router 里还有别的域与服务器专属的端点
# （`/chat/stream` 是会话链路那条流、`/stats/dashboard` 数的是知识库的文档与任务），
# 摆出来就是一条会 404/503 的路。理由与做法见 `api/v1/local.py` 的模块头。
local_router.include_router(local.chat_reads)
local_router.include_router(local.stats_reads)
# 命令目录（`GET /chat/commands`）：它读的是**这台机器上的**命令与技能
# （`data/commands/` + 仓库命令 + `<data_dir>/skills/`，含"被禁用的技能"那栏的读法），
# 而桌面真正执行那一轮的是**边车** —— 目录与执行必须同源，否则壳里列出来的
# 与真正能被执行的不是同一批（同一个端点也就有了两个答案）。
#
# **2026-10-05 先挪进来、再摘掉服务器那一份**（见模块头那一段）：两次改动合起来，
# 这条端点如今**只在本机档**。浏览器那一档拿不到它（`listCommands` 回空列表，`/` 菜单为空），
# 那一档本来也只有知识库管理台。
#
# 手法与上面那几条薄重声明逐字相同：**按原路径重挂同一个端点函数**（`chat.list_commands`，
# 函数体一个字不重写、鉴权依赖与响应模型都还是原来那份），只是换一个 router 注册。
_chat_commands_local = APIRouter()
_chat_commands_local.routes.extend(
    route for route in chat.router.routes if getattr(route, "path", "") == "/chat/commands"
)
local_router.include_router(_chat_commands_local)

# **明确不挂**（每一条都因为"数据或能力不在这台机器上"）。按**域**逐个说清，
# 因为这个判定的依据不是"这个文件属于谁"，而是"那个域的表在本机库里有没有"：
#
# - **知识库那一整族**（`documents` / `knowledge_bases` / `search` / `chunks` /
#   `folders` / `wiki` / `tabular` / `data_sources` / `shares`）：那几张表都不在本机
#   （`app/storage/sqlite_impl/schema.sql` 里没有），KB 域的方法在本机档由
#   `RemoteMetaStore` **整体抛** `KnowledgeBaseUnavailable`（逐族的理由见
#   `app/storage/split_impl/remote_meta.py` 的模块头）——挂上来就是一片 503；
# - `tasks` / `lifecycle` / `webhooks` / `maintenance`：**它们数的都是知识库那边的家当**
#   ——`TaskQueueRepo`（"本机档不启动消费者，没有队列可管"）、`TrashRepo`（回收站里是
#   NAS 上删掉的文档）、`WebhookRepo`（三个事件全是 `document.*`，见 `services/webhook.py`
#   的 `EVENTS`）、`MaintenanceService.overview()` 要读向量分区（`UnavailableVectorStore`）
#   ——同一条：数据不在本机，挂了没有一条答得出话。
#   `maintenance` 里那两个真属于本机的方法（`storage_stats` / `vacuum`）**不外露成一个
#   端点**：本机库自己的空间数字在 `/local/status` 上已经给了（`database_bytes` 等）；
# - `users` / `auth` / `api_keys` / `avatars`：**账号体系**。本机档不设门禁——
#   `users` / `sessions` / `api_keys` 三张表都不在本机库里，调用主体由
#   `api/auth.py::current_caller` 短路成"本机主人"（`api_key.LOCAL_CALLER`）。
#   映射它们的仓储等于给本机装第二套鉴权，`IdentityRepo` / `ApiKeyRepo` 的说明里
#   已经写死不许（见 `remote_meta.py`）；头像要的 `users` + `ImageRepo` 同此；
# - `frontend`（`/app/frontend/*`）：那是"**服务器把新前端发出去**"的那一半，
#   壳是接收方（`v0.56` 的承诺就是"服务器发了新前端、客户端下次启动自动用上"）。
#   本机档挂它只会让壳从"本机自带的这一份"取——那正是壳手里已有的东西；
# - `model_proxy`（`/model-proxy/*`）：那是"**服务器用自己的 key 调模型**"的那一半
#   （key 不下发）。本机侧那条链是 `sidecar.py::Clients` 里的 `RemoteModelClient`
#   ——它**打的就是服务器那条代理**，所以本机挂一份只会多出一条没人调的路
#   （本机自己的模型凭据在本机的 `model_registry` 里，那条路走的是别处）。
#
# 本机档的会话事件、上下文用量与**用量面板**在 `local.chat_reads` / `local.stats_reads`
# 上（薄重声明），不靠 include `chat` / `stats`。
#
# `provider`（M3 阶段 1）单独说一句：**知识库提供者是那台 NAS**，
# 本机侧是它的**客户端**——它调 `/provider/handshake`，不提供它（方案 §9-1）。
# 客户端这一侧要看状态、改地址走的是本机自己的那条：`/local/provider`（M3 阶段 5，
# 在上面那个 `local.router` 上）。
# 在服务端 `api_router` 上则是一条正常端点（见上面那行 include）。
#
# `backup`（M5 阶段 1）同理：**备份提供者也是那台 NAS**（路 B：S3 凭据只活在服务端，
# 客户端手里还是那把 API Key）。本机侧是客户端，走 `/local/backup*`（M5 阶段 4 在上面的
# `local.router` 上）。把这一族挂进本机档只会得到一堆"本机自己跟自己说话"的端点。
