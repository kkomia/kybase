"""v1 路由聚合。

鉴权不在这里挂全局依赖，而是**逐个端点显式声明**（``ReadDep`` / ``WriteDep``）：
全局依赖无法表达"这个端点需要读写、那个只要只读、用户管理端还只认管理员"，
只能一律放同一个档位——那等于没有作用域隔离。

**只有一张表**（v0.1.0 瘦身）：这里就是知识库服务对外的全部路由。原先还有一张
``local_router``（桌面壳的边车那一份白名单），它随边车一起拆除了——本机档、
它的会话面、笔记、记忆、技能、插件、工作区、定时任务、沙箱、网页取页、前端资源包
分发都没有了，Agent 侧的那些服务实现也一并删除。

留下的每一族都是知识库本体或它绕不开的共享底座：

- 知识库：`/knowledge-bases` `/documents` `/folders` `/search` `/chunks` `/wiki`
  `/tabular` `/data-sources` `/knowledge-bases/{id}/shares` `/tasks` `/lifecycle`
  `/webhooks` `/maintenance` `/stats` `/provider/handshake`；
- 对外能力目录：`/tools`（工具面 + 随附技能，管理界面据此渲染）；
- 账号体系（随 KB 走，见剥离方案 §5.3）：`/auth` `/users` `/api-keys` `/avatars`；
- 模型登记（KB 要登记嵌入 / 对话模型）：`/model-registry`；
- 健康探针：`/health`。
"""

from fastapi import APIRouter

from app.api.v1 import (
    api_keys,
    auth,
    avatars,
    chunks,
    data_sources,
    documents,
    folders,
    health,
    knowledge_bases,
    lifecycle,
    maintenance,
    model_registry,
    provider,
    search,
    shares,
    stats,
    tabular,
    tasks,
    tools,
    users,
    webhooks,
    wiki,
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
# 知识库提供者握手（M3 阶段 1）：这个服务**就是**提供者，所以这一族是它的家。
api_router.include_router(provider.router)
api_router.include_router(stats.router)
api_router.include_router(tasks.router)
api_router.include_router(tools.router)
api_router.include_router(api_keys.router)
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
