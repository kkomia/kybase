"""全局配置。

约定（工程规范 §6）：
- 密钥只从环境变量读取，模板见 ``backend/.env.example``，禁止入库；
- 用户级配置（解析节点、API Key、模型登记）运行期落**数据库**，M1 起接管。

**两级配置，别混**（v0.12 起明确）：

1. **引导级**（本文件）：连不上就起不来的东西——数据库连接串、对象存储凭据、
   数据目录。它们**只能来自环境变量**，因为"把数据库连接串存进数据库"是鸡生蛋。
2. **运行期级**（``app_settings`` 表）：可以在网页上改、改完即刻生效的东西——
   解析节点 token、模型登记、推荐问题开关。这些落库，带掩码，不回显明文。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_VERSION = "v1"
"""对外 API 主版本，所有路径前缀 ``/api/v1``（工程规范 §3.2）。"""


class Settings(BaseSettings):
    """进程级配置，环境变量前缀 ``KYLAB_``。"""

    model_config = SettingsConfigDict(
        env_prefix="KYLAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "kylab"
    app_version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    cors_origins: str = "http://127.0.0.1:5173"
    frontend_dist_dir: Path | None = None
    """桌面壳要取的那份**前端产物**目录（`GET /api/v1/app/frontend/*` 从这里打包）。

    **留空 = 仓库里的 `frontend/dist`**（"从仓库跑"与开发都成立）；容器或别的部署形态
    用 `KYLAB_FRONTEND_DIST` 指到产物所在目录。契约见《Tauri-壳资源分离与前端热更新-
    实现规格》§4，落地说明见 `app/api/v1/frontend.py` 的模块注释。
    """
    slow_query_ms: int = 500
    """超过该耗时的检索留一条 WARNING（架构 §12 可观测性）。"""

    run_worker: bool = True
    """应用启动时是否内嵌任务消费者。测试里关掉它，改为手动驱动，避免时序不确定。"""
    worker_lease_seconds: int = 60
    """任务租约时长：worker 执行期间按 1/3 周期续租，崩溃后由超时回收兜底。"""
    worker_concurrency: int = 1
    """同一进程里跑几个消费者（1–8，默认 1）。

    **为什么这件事值得一个开关**：摄入链路的绝大部分时间在等远端（云端解析轮询、
    embedding、出题），单消费者时这些等待会**串行**——实测一篇文档的分段出题有
    51 批、每批 1~2 分钟，它一个人就把后面 30 篇排队文档堵死（见 §12.113）。
    换成 N 个消费者，等待就能重叠，吞吐按倍数走。

    **为什么默认仍是 1**：并发要花 CPU 与内存（本地切词、向量化都在进程内），
    而"默认炸内存"比"默认慢"更糟。负载面板会把 ``在跑 / 上限`` 显示出来，
    让人看见瓶颈再决定加不加。

    **上限 8 是跟着连接池走的**（``Database`` 默认 ``max_size=10``）：
    消费者越多抢连接越凶，超过池子只会变成互相等待。要更高得先抬池子。
    """
    # ---- 存储后端（引导级：只能在环境变量里给）----------------------------
    #
    # **两个部署档**（M2 §4.1）。服务器档是既有形态：**PostgreSQL（元数据 + 向量 +
    # 全文）+ 对象存储（原件）+ DuckDB（表格副本）**；本机档（桌面壳的边车进程）里
    # 会话与设置落本机 SQLite，知识库那半**没有数据源**（在 NAS 上——M3 起本机是它的
    # **客户端**：`services/knowledge_provider.py`，方案 §1.3）。
    # 装配点仍只有一处：``core/storage.py::build_stores()``，它按这里的两个字段分流，
    # 并在启动时校验（连得上、schema 版本对、扩展在）。**档位只在进程启动时定一次**：
    # ``get_stores()`` / ``get_services()`` 两个单例都是 ``lru_cache``，运行期换不了。

    deployment: Literal["server", "local"] = "server"
    """部署档：``server``（默认，NAS 上的网页端/API）或 ``local``（桌面壳的本机边车）。

    **默认必须是 server**：既有部署一位行为都不变；本机档由入口自己钉死——``app/sidecar.py``
    启动时强制 ``KYLAB_DEPLOYMENT=local`` 并把 ``KYLAB_DATABASE_URL`` 压成空串，
    不给环境继承的机会（"边车误连服务器库"是最糟的失败形态）；
    ``app/main.py`` 只是**按环境变量判档**（挂哪个路由、起不起消费者）。
    """

    database_url: str | None = None
    """PostgreSQL 连接串，例如 ``postgresql://kylab:secret@postgres:5432/kylab``。

    **服务器档必填**（本机档不许配，见下面的校验）。这里声明成可选只是为了"缺配置"
    能由 ``build_stores()`` 给一句可操作的报错，而不是在构造 Settings 时就抛一句
    pydantic 的字段错误——前者能告诉运维该填什么。
    启动时会校验连通性、``vector`` 扩展与 schema 版本，失败即退出。
    """

    local_db: Path | None = None
    """本机档的库文件（默认 ``<data_dir>/kylab.db``）。

    只在 ``deployment=local`` 下有意义：给了就落这个路径（导入、排障、把库放到另一个
    盘上都靠它），不给就用数据目录下的默认名。
    """

    server_url: str | None = None
    """NAS 的 API 基址（含 ``/api/v1``），例如 ``http://nas:8000/api/v1``。

    **只在本机档有意义**（M2 §4.1 那张表：本机档的 KB/模型两头都在 NAS 上）：
    知识库（检索与入库）与模型代理都在那一侧，所以本机的服务图要照着它建远端客户端。
    不设 = 这台机器这次没接 NAS：检索会走"知识库不可用"那条**如实报错**的路
    （见 ``split_impl``），而不是悄悄回退成进程内检索（本机根本没有那些表）。
    """

    token: str | None = None
    """NAS 的用户会话令牌（与桌面壳、前端用的是同一把）。

    名字就叫 ``token`` 是跟着环境变量 ``KYLAB_TOKEN`` 走的——壳起边车时读的就是它
    （``app/sidecar.py`` 的 ``--token`` 默认值）。**不落库、不进日志**：它是通往 NAS 的凭据，
    本机库里只放会话数据（M2 风险表 R6）。
    """

    kb_url: str | None = None
    """**知识库提供者**的地址覆盖（含 ``/api/v1``），例如 ``http://另一台nas:8000/api/v1``。

    只在本机档有意义（M3 §4.1），而且**默认空 = 继承 ``server_url``**——壳里那台 NAS
    就是知识库提供者，绝大多数部署一个字都不用填。它存在的理由是两件事：
    **排障**（临时指到另一个地址看看）与**多 NAS 入口**（知识库在另一台机器上）。
    **凭据不跟着走**：``kb_token`` 不填时仍用 ``token``，要连另一台得先有它的钥匙
    （M3 明确不做多 NAS 凭据管理，方案 §9-3）。

    运行期那一处的覆盖键是 ``provider.knowledge.base_url``（设置页可改、改完即刻生效：
    提供者客户端每次调用现取目标，见 ``services/knowledge_provider.py``）。
    """

    kb_token: str | None = None
    """知识库提供者的凭据覆盖。**默认空 = 继承 ``token``**（壳领的那把 API Key）。

    与 ``token`` 同一条纪律：**不落库、不进日志**（M2 风险表 R6 / M3 R3）——它只从
    引导级（环境变量 / 壳）来，本机库里存不下它，设置面板也只显示"配没配"。
    """

    device_id: str = ""
    """**这台机器的设备身份**（桌面壳生成的 UUID v4，M5 §3.1 / R12）。

    它从哪来：壳在登录那一刻生成一次、写进自己的 ``config.json``，之后**每次起边车都
    用 ``--device-id`` 传下来**（``app/sidecar.py::main`` 的默认值读 ``KYLAB_DEVICE_ID``）。
    所以本机后端这一侧只需要"接住它"，不需要任何生成逻辑。

    **绝不自动生成**（R12 的核心）：快照按 ``<device_id>-<ts>-<hash8>`` 命名，恢复点按
    "哪台设备"分组——编一个 id 会让同一台机器在 NAS 上长出两套互不相认的恢复点，
    而"恢复时该信哪一份"就没有答案了。没有它时服务层**如实拒绝打快照**
    （``BackupSnapshotService.create`` 那句"还有设备身份"），而不是降级成匿名。

    **它不是凭据**：写进日志/状态页没有危害（它只是一个不透明的机器标签），
    但它同样不进本机库的 ``app_settings``——它是引导级的事实，不是用户可改的设置。
    """

    s3_endpoint: str | None = None
    """S3 兼容对象存储的端点（MinIO 形如 ``http://minio:9000``）。

    留空时原件落本地文件系统（``data_dir/{originals,markdown,images}``）——
    测试与本地开发需要这条不联网的路径。
    与 ``database_url`` 同一原则：这是引导级配置，不能存库。
    """
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "kylab"
    s3_region: str = "us-east-1"
    """MinIO 不校验 region，但 S3 SDK 需要一个非空值。"""
    s3_secure: bool = False
    """是否用 HTTPS 连对象存储。容器内网互通时为假（MinIO 默认 http）。"""
    s3_prefix: str = ""
    """桶内的 key 前缀，便于一个桶放多套环境。留空即桶根。"""

    # 备份提供者（M5 阶段 1，路 B 的服务端那一半：`api/v1/backup.py`）
    #
    # 四项全是**引导级**（环境变量一次定死，不落库、不进设置页）：它们定的是"快照
    # 落在哪、留多少"，改它们等于改数据布局，不该由页面上的一个开关来动。
    # 与 `kb_url` / `s3_bucket` 同一条原则。
    backup_bucket: str = "kylab-backup"
    """快照桶的名字。**与知识库那个桶分开**（v0.3 §7："落 MinIO 快照桶，不落知识库的 PG"）。

    分开的理由是两条实操：知识库那个桶里的对象是**索引的原件**（删了要重建库），
    备份桶里的对象是**整机快照**（用户会按恢复点整份删）——两个生命周期完全不同，
    混在一个桶里，"清一下备份"就可能误伤原件。凭据**不分开**：同一个 MinIO、
    同一对 KYLAB_S3_* 钥匙（少一套要轮换的秘密）。
    """
    backup_prefix: str = ""
    """备份桶内的公共前缀（形如 ``dev/``），便于一个桶放多套环境。留空即桶根。

    与 ``s3_prefix`` 同一个用途、同一个写法，但**各管各的桶**：这两个前缀没有理由绑在一起。
    """
    backup_keep: int = 30
    """每台设备**最多留几份**快照（"保留份数"这一档策略 = ``keep_n``）。

    超过时**服务端不替用户删**（方案 §1.2 规矩 4）：上传回 409 并说清"先显式删一份"。
    握手的 ``capabilities.snapshot.max_snapshots_per_device`` 与
    ``capabilities.retention.keep`` 报的都是这个数——**一份口径，不是两处**。
    """
    backup_quota_bytes: int = 20 * 1024 * 1024 * 1024
    """备份桶的用量上限（字节，默认 20 GiB）。超了同样报 409，不静默淘汰。

    它是**整把钥匙看得见的全部设备**的总量（与可见范围同一口径，见 `api/v1/backup.py`
    的模块头）；要按设备细分额度是"将来要按设备限权"那条登记（方案 R9）的一部分。
    """

    # 鉴权（M4 T4.4；《架构设计 v0.2》§3.2）
    #
    # v0.11：**鉴权永远生效**，没有开关也没有第二种管理员凭据。
    # 第一次打开时唯一能调的是 /auth/status 与 /auth/setup（先建管理员账号），
    # 之后一律用会话令牌或 API Key。原先的 KYLAB_AUTH_ENABLED 与
    # KYLAB_CONSOLE_TOKEN 都已取消——"还没配"与"忘了配"在代码上无法区分，
    # 于是"没配就放行"等于"无账号即裸奔"。

    url_signing_secret: str | None = None
    """下载签名 URL 的密钥。

    浏览器里 ``<img>`` 与下载链接带不了 Authorization 头，所以"有权访问"这件事
    要编码进 URL 本身——这就是签名 URL 的全部理由。
    首次 ``POST /auth/setup`` 会自动生成一条落库；这里是给不想落库的部署用的
    环境变量入口。
    """

    # 云端解析节点（M2 启用）
    mineru_token: str | None = None
    paddleocr_token: str | None = None
    github_token: str | None = None
    """浏览 GitHub 上的技能源时用的 token（可选）。

    **不给也能用**：看公开仓库不需要认证，只是匿名配额是 60 次/小时/IP。
    给了就提到 5000 次/小时——常逛技能市场时值得配一个只读的细粒度 token。
    """
    questions_concurrency: int = 4
    """分段出题同时发几批（默认 4）。

    出题是摄入链路里最慢的一步，而这些调用是在**等远端**：串行发等于把等待叠起来。
    实测一份 400 段的文档有 50 批、每批 1~2 分钟（思考型模型），
    串行就是 50~100 分钟，还把后面所有排队文档一起堵住（§12.113）。

    别开太大：模型的 RPM/TPM 限额是真的，撞上去只会拿到 429。
    """

    mineru_daily_page_quota: int = 1000
    """MinerU 每日**优先额度**的页数（官方口径 1000 页/天，超出后不再保证速度）。

    它的意义在于解释"为什么解析忽然变慢"：额度用尽不是报错，是**降级排队**，
    用户只会看到"卡住不动"。负载面板把``今日已用 / 额度``显示出来，
    这种"看起来卡住"就有了可核查的原因。配额是云端政策、会变，所以做成配置。"""

    # Embedding（M2 启用；模型切换规则见架构设计 v0.2 §6.4）
    #
    # **模型的地址 / 密钥 / 名称 / 维度不在这里**：它们属于"注册了哪个模型"，
    # 统一由模型注册表（供应商 → 模型）承担，见 services/model_registry.py。
    # 这里是**行为参数**，不是模型身份。
    embedding_batch_size: int = 32
    embedding_protocol: str = "openai"
    """嵌入协议（``openai`` / ``wemm``），默认 ``openai``——既有部署一位不变。

    只作为 ``.env`` 里的**引导值**（``KYLAB_EMBEDDING_PROTOCOL``）：部署时预设一次，
    之后以设置页里的值为准（与批大小同一条规则）。**地址不在 .env 里**：那台 WeMM 服务
    的 ``base_url`` 是"注册了哪个供应商"的一部分，由模型注册表承担——本机部署示例见
    ``backend/.env.example`` 与 ``docs/归档/调研/WeMM-Embedding-2B-接入文档-v0.1.md``。
    """

    dev_embedding: bool = False
    """开发用确定性嵌入（哈希）开关，**默认关闭，生产不要开**。

    关闭时"没配嵌入模型"就是**没配**：建库被拒、向量通道跳过，界面上如实说明。
    打开它才会退回无语义的词面哈希——这个开关存在的唯一理由是离线开发与测试
    需要一条不联网的链路，而不是给产品留一条"看起来能用"的假路径。
    """

    # 对话模型（M6 快速检索问答）
    # 地址 / 密钥 / 模型名同样由注册表决定，这里只留采样与思考开关
    llm_temperature: float = 0.3
    llm_enable_thinking: bool = True
    """思考开关，**默认开**：主流模型默认都思考，关掉是例外而不是常态。

    不同供应商用不同字段表达它（DeepSeek 认 ``thinking.type``、Qwen 认
    ``enable_thinking``），翻译见 ``services/thinking.py``。
    """
    llm_thinking_effort: str = "medium"
    """思考强度（low / medium / high），同样按方言翻译或丢弃。"""

    # ---- 长期记忆（v0.1.1）------------------------------------------------
    #
    # 这里的**两项只是 .env 引导值**：记忆的开关与落点本来由运行期配置
    # （数据库 ``app_settings`` / 设置页）持有，加这一层是为了让**容器部署**能在
    # compose 里一次写清（deploy/ 下两份 compose 就是这么用的），而不是让用户
    # 先去界面上找开关。优先级仍是"库 > 这里 > 代码默认"，
    # 见 services/runtime_config.py 的三层优先级。
    #
    # 默认值一律留 ``None``（= 没设），这样不设时**回落到代码默认**而不是
    # 在配置层再抄一份——两处各写一份默认值迟早会分叉。
    #
    # **原先还有一项 ``memory_base_url``（记忆服务地址），v0.46 删了**：
    # 记忆已经并进我们自己的进程，没有第二个进程可连（见 services/memory.py）。
    # 环境变量里若还留着 ``KYLAB_MEMORY_BASE_URL``，会被静默忽略
    # （``Settings`` 是 ``extra="ignore"``）——不影响启动。
    memory_enabled: bool | None = None
    """是否启用长期记忆。**默认（代码里的）是关**：开着它，对话收尾会真的调一次
    模型去做自动沉淀，升级之后默默开始烧 token 是最不该有的默认。
    注意开关管的是"召回与自动沉淀"；``MEMORY.md`` / 人设那几份文件的读写
    与注入不受它影响（见 services/memory.py）。"""
    memory_workspace: str | None = None
    """记忆工作区目录，**相对数据目录**。不设时用代码默认 ``memory``，
    于是容器里是挂载卷下的 ``/data/memory``。"""

    # ---- agent 模式（P1-1）------------------------------------------------
    #
    # 与记忆那三项同一条口径：这里**只是 .env 引导值**，真正的值在运行期配置
    # （``app_settings`` 的 ``chat.mode``——输入框那一排的控件改的就是它）。
    # 加这一层是为了让容器部署能在 compose 里一次写清"这个部署默认用哪一档"。
    chat_mode: str | None = None
    """Agent 模式四档 ``plan / build / edit / yolo``（枚举与语义见 ``services/modes.py``，
    照 ZCode 抄的）。不设时用代码默认 ``build``（与 ZCode 的默认档一致）。"""

    @model_validator(mode="after")
    def _reject_two_sources_of_truth(self) -> Self:
        """本机档配了 ``database_url`` → **当场拒绝**（M2 §2.2「配置口径」）。

        这一条不是洁癖：本机档的会话与设置落 SQLite，而 ``database_url`` 说的是
        "元数据在 PostgreSQL"——两个真相源同时在场时，"会话写到哪儿"取决于哪段代码
        先读哪个字段，而失败形态是**数据被写进错误的库**（最糟的那一类：不报错）。
        所以宁可在启动的第一秒失败。

        校验放在构造期（而不是 ``build_stores()`` 里）：配置错了就不该造出一个
        "看起来能用的 ``Settings``"。``build_stores()`` 里另有一条同样的守卫，
        那是给绕过校验构造的 Settings 兜底的。
        """
        if self.deployment == "local" and self.database_url:
            raise ValueError(
                "本机档（KYLAB_DEPLOYMENT=local）不接受 KYLAB_DATABASE_URL："
                "本机档的元数据落本机 SQLite（KYLAB_LOCAL_DB 或 <data_dir>/kylab.db），"
                "配了连接串等于同时声明了两个数据源。要连 PostgreSQL 就去掉 "
                "KYLAB_DEPLOYMENT=local（服务器档）。"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 白名单拆成列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，供依赖注入使用。"""
    return Settings()
