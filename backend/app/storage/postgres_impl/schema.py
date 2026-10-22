"""PG schema 的创建与校验（v0.12）。

本次迁移**不做数据迁移**（旧 SQLite 库直接舍弃），所以没有"24 条增量迁移要重放"
这件事：``schema.sql`` 就是第 1 版基线，之后的演进往 ``MIGRATIONS`` 里追加、
由启动时的 ``ensure_schema`` 自动应用（不需要人工 psql——DDL 的唯一属主是应用）。

职责边界：DDL 的**唯一属主是应用**。启动时由这里负责建 schema / 校验版本，
不依赖容器 initdb 脚本或人工 psql（那会造出第二个真相来源）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.storage.postgres_impl.connection import Database

#: 惰性拿到的 psycopg（**故意不是模块级导入** ×）。
_PSYCOPG: Any = None


def _psycopg() -> Any:
    """第一次真用 psycopg 时才导入它（模块级导入会让**客户端运行时**凭空多背一份 ×）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 √，而"建库 / 加索引"只有
    **服务器**启动那条路会做 √ —— 边车（客户端运行时）不碰 PostgreSQL 的 schema √。

    **行为一个字没变** ×：第一次调用时才导入 √，psycopg 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` √（只是从导入期挪到了调用期 √）。
    """
    global _PSYCOPG
    if _PSYCOPG is None:
        import psycopg

        _PSYCOPG = psycopg
    return _PSYCOPG

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

BASELINE_VERSION = 1
"""``schema.sql`` 对应的版本号，与文件末尾写入 schema_migrations 的值一致。"""

SCHEMA_VERSION = 8
"""应用期望的 schema 版本：基线 v1 + ``MIGRATIONS`` 里已追加的增量。

**这是一条独立的 KB 连号序列**（2..8）。2026-10-20 的数据分家之前，它和 Agent
侧的迁移交错编号到 19——那台存量库要先跑一次运维动作（``remap --apply``）
把 ``schema_migrations`` 重写完，否则本版本会以 ``LEGACY_INTERLEAVED_HEAD``
明确拒绝启动。

**启动时会对不上就自动补**：低于它就按序应用缺的那些迁移，高于它才报错
（库被更新版应用升过级）。这与"库比基线还旧"是两码事——后者要人工处理。"""


@dataclass(frozen=True, slots=True)
class Migration:
    """一条增量迁移。**只增不改**：已经发布的条目一律不许改字面量。

    ``legacy_version`` 是 kylab 合体期那条**交错序列**里的旧编号（KB 迁移与
    Agent 迁移连在一起编号，本服务瘦身前的线上库停在 v19）。2026-10-20 的
    数据分家把 Agent 迁移整体删掉、KB 迁移重编成 2..8 的独立连号，这个字段
    只服务**一次性运维动作**（``python -m app.storage.postgres_impl.remap``）；
    新库从基线一路走过来，永远用不到它。
    """

    version: int
    description: str
    statements: tuple[str, ...]
    legacy_version: int | None = None


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
        legacy_version=2,
        description="文档阶段事件：记录每次进入某阶段的时间，用于进度时间线（v24）",
        statements=(
            # 时间线的数据源。为什么另存事件而不是给 documents 加几个时间戳列：
            # 阶段会**重复进入**（失败重试、重新摄入、取消后重跑），一行时间戳
            # 存不下历史；而"每个环节各花多久"正是要看相邻两次进入的间隔。
            """
            CREATE TABLE document_stage_events (
                id          bigserial PRIMARY KEY,
                document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
                stage       text NOT NULL,
                entered_at  timestamptz NOT NULL DEFAULT now(),
                error       text
            )
            """,
            # 时间线永远按"某文档、先后顺序"读，所以索引带上 id 而不是只 document_id：
            # entered_at 可能同微秒，id 才是稳定的先后关系。
            "CREATE INDEX idx_document_stage_events_doc ON document_stage_events (document_id, id)",
        ),
    ),
    Migration(
        version=3,
        legacy_version=3,
        description="补三条索引：云端额度统计、阶段事件清理、任务状态聚合（v24 性能审阅）",
        statements=(
            # ① 云端额度统计（负载面板每 2 秒问一次）按 (parser_name, created_at) 过滤，
            #    而 parse_results 原来只有 (document_id, part_id) 主键 —— 那是**全表扫**。
            #    实测这条查询的代价随"解析过的文件数"线性涨，而它跑在每次面板轮询上。
            "CREATE INDEX idx_parse_results_parser_created"
            " ON parse_results (parser_name, created_at)",
            # ② 阶段事件表只增不减（每次重试/重新摄入都追加），要按时间做保留期清理，
            #    没有 entered_at 索引时那条 DELETE 也是全表扫。
            "CREATE INDEX idx_document_stage_events_entered ON document_stage_events (entered_at)",
            # ③ 队列深度改成一条 `GROUP BY state, kind` 聚合（不再把整表行搬到 Python）；
            #    这个索引让那次聚合走 index-only scan，不必读堆里的每一行。
            "CREATE INDEX idx_tasks_state_kind ON tasks (state, kind)",
        ),
    ),
    Migration(
        version=4,
        legacy_version=4,
        description="文档摘要：入库时生成一段紧凑摘要，供问答上下文与界面复用（v25）",
        statements=(
            # 摘要存在的**唯一理由**是省 token：命中 6 段资料时，把每段"所在小节"
            # 整段塞给模型要上万字；有了每篇文档的摘要，就能只带摘要 + 更短的片段，
            # 模型仍然知道"这几段来自一篇讲什么的文档"。见 services/summary.py。
            "ALTER TABLE documents ADD COLUMN summary text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=5,
        legacy_version=8,
        description="知识库提示词：回答这个库的问题时该怎么答，从对话页搬到库上（v0.19）",
        statements=(
            # **空串 = 用内置提示词**（`services/chat.build_messages` 在空的时候会退回
            # `DEFAULT_SYSTEM_PROMPT`）。存量库全部落在"没配过"这一档，
            # 行为与迁移前一致——这条迁移不该让任何一个既有库的答案变样。
            "ALTER TABLE knowledge_bases ADD COLUMN system_prompt text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=6,
        legacy_version=11,
        description="用户头像：只存对象存储的 key（v0.29）",
        statements=(
            # 只加一个 key 列：图片本体在对象存储里（见 services/avatars.py
            # 模块头的两条理由——账号是每个页面都读的东西；内容寻址的 key
            # 让换头像天然避开 <img> 的缓存）。
            "ALTER TABLE users ADD COLUMN avatar_key text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=7,
        legacy_version=18,
        description="API Key 的工具范围：一把钥匙可以只允许调某几个 MCP 工具（v0.60）",
        statements=(
            # 这把钥匙"能碰哪些库"（knowledge_base_ids）之外的第二维：**能用哪些工具**。
            # 典型场景是只给一个检索客户端配 search：它能查，但不能传、不能删
            # （此前只有只读/读写两档，只读档仍然能调 `list_documents` 这种
            # 把库的全貌读走、"看见但改不动"的接口）。
            #
            # `'[]'::jsonb` 这个默认值是**语义的一部分，不是占位**：空 = 不受限
            # （与 knowledge_base_ids 同一口径）。所以存量钥匙全部落在"不限工具"这一档，
            # 行为与迁移前**一字不差**——这条迁移不该让任何一把在用钥匙失灵。
            "ALTER TABLE api_keys"
            " ADD COLUMN IF NOT EXISTS allowed_tool_names jsonb NOT NULL DEFAULT '[]'::jsonb",
        ),
    ),
    Migration(
        version=8,
        legacy_version=19,
        description="检索命中事件：每次检索按知识库记一行，供控制台看命中趋势（v0.61）",
        statements=(
            # 这张表回答的是**另一个问题**：`usage_events(kind='search')` 只记了
            # "这次检索返回几条"，没有库这一维，于是概览页能画检索量、却画不出
            # "哪个库的命中在涨/在跌"，KB 卡片上那条迷你曲线也无处取数
            # （设计文档 §9 第 6 条记着这个缺口）。
            #
            # 一行 = **一次检索 × 一个库**。`candidates` / `kept` 刻意分开：
            # 前者是分布收敛**之前**这个库贡献的条数，后者是收敛后真返回的条数，
            # 两者之差 = 这个库这一趟被砍掉多少（见 base.SearchEventRecord 的说明）。
            # 只记一个数的话，"给 12 条留 2 条"与"给 2 条留 2 条"在曲线上长得一模一样。
            #
            # `fit` 是整次检索的判定（strong / weak / none / unknown），同一批行相同：
            # 它是分布的性质而不是某个库的，存下来是为了能回答"这几天有多少次
            # 是「资料里没有」"——那正是这份数据的另一边价值。
            #
            # `ON DELETE CASCADE`：库删了，它的历史命中也跟着走。留一批指向不存在
            # 的库的行，只会让趋势图出现一条没有名字、也点不进去的曲线。
            """
            CREATE TABLE search_events (
                id         text PRIMARY KEY,
                kb_id      text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
                candidates integer NOT NULL DEFAULT 0,
                kept       integer NOT NULL DEFAULT 0,
                fit        text NOT NULL DEFAULT 'unknown',
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            # 主查询形状是"某个库、某段时间"（KB 卡片上那条迷你趋势），这条索引直接对上它
            "CREATE INDEX idx_search_events_kb_created ON search_events (kb_id, created_at)",
            # 概览页那条全局曲线与保留期清理都按时间过滤；聚合成 (kb_id, created_at)
            # 的前缀用不了，所以单独一条（与 usage_events 同一种形状）
            "CREATE INDEX idx_search_events_created ON search_events (created_at)",
        ),
    ),
)


#: 旧序列（kylab 合体期：KB 与 Agent 交错连号）的最后一版。
#: 存量库在分家前停在这里；新库见不到这个数。
LEGACY_INTERLEAVED_HEAD = 19


def legacy_remap_plan() -> dict[int, int]:
    """旧交错序列版本号 → 新 KB 独立序列版本号（含基线 ``1 -> 1``）。

    **只给一次性运维脚本用**（``python -m app.storage.postgres_impl.remap``）：
    新库用不到它——没有哪台新库会带着旧序列启动。单点事实是
    ``Migration.legacy_version``，这里只是把它展开成一张表。
    """
    plan = {BASELINE_VERSION: BASELINE_VERSION}
    for item in MIGRATIONS:
        if item.legacy_version is not None:
            plan[item.legacy_version] = item.version
    return plan


def legacy_agent_versions() -> set[int]:
    """旧序列里那些**只属于 Agent 域**的版本号（迁移脚本要把这些行删掉）。"""
    return set(range(1, LEGACY_INTERLEAVED_HEAD + 1)) - set(legacy_remap_plan())


REQUIRED_EXTENSIONS = ("vector",)
"""启用项目功能所必需的扩展。缺失时**启动即失败**并给出可操作的提示——
pgvector 不是 PG 自带扩展，用错镜像就会缺，这是本次切换最容易踩的坑。"""

_MISSING_VECTOR_HINT = (
    "数据库缺少 pgvector 扩展（vector）。请改用自带它的镜像，"
    "例如 pgvector/pgvector:pg17-trixie 或 paradedb/paradedb:v0.22.2-pg17；"
    "若 PG 是本机安装的，需要先装扩展文件再执行 CREATE EXTENSION vector。"
)


class SchemaError(RuntimeError):
    """schema 无法满足应用要求（缺扩展、版本不符、基线 DDL 执行失败）。"""


def current_version(db: Database) -> int | None:
    """库当前的 schema 版本。

    三种情况要分清（混起来会让"库状态不完整"被误当成"空库"而重跑建表）：

    - ``None`` —— 连 ``schema_migrations`` 表都没有，是全新库，可以建基线；
    - ``0``    —— 表在但一行版本记录都没有：不完整状态，比任何基线都低；
    - ``n``    —— 正常版本号。
    """
    with db.read() as conn:
        row = conn.execute("select to_regclass('public.schema_migrations') as t").fetchone()
        if row is None or row["t"] is None:
            return None
        version = conn.execute("select max(version) as v from schema_migrations").fetchone()
        if version is None or version["v"] is None:
            return 0
        return version["v"]


def ensure_schema(db: Database) -> int:
    """确保 schema 就位；返回当前版本。

    空库 → 执行基线 DDL；已有 schema → 校验版本不低于基线。**不降级、不乱改**：
    版本比应用期望的还新（说明库被更新版应用升过）同样报错，避免旧代码写新结构。
    """
    version = current_version(db)

    if version is None:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        try:
            with db.session() as conn:
                # schema.sql 是无参数多语句脚本，psycopg 会用简单查询协议整段执行
                conn.execute(ddl)
        except _psycopg().errors.UndefinedFile as exc:
            # 58P01：扩展的控制文件不在磁盘上——镜像里根本没带 pgvector。
            # 这是本次切换最常见的一种部署错误，单独给一句可操作的提示。
            raise SchemaError(f"基线 schema 执行失败：{exc}\n{_MISSING_VECTOR_HINT}") from exc
        except Exception as exc:
            raise SchemaError(f"基线 schema 执行失败：{exc}") from exc
        version = current_version(db)

    if version is None or version < BASELINE_VERSION:
        raise SchemaError(
            f"schema 版本为 {version}，低于应用要求的基线 {BASELINE_VERSION}；"
            "请执行 backend/app/storage/postgres_impl/schema.sql"
        )
    if version > SCHEMA_VERSION:
        if version == LEGACY_INTERLEAVED_HEAD:
            raise SchemaError(
                f"schema 版本为 {version}：这是 kylab 合体期的**交错迁移序列**"
                "（KB 与 Agent 交错连号），本版起 KB 走自己的独立序列（1..8）。"
                "库里的字段与表都齐，只差把版本号重写一遍——**升级前先跑一次**："
                "\n    cd backend && python -m app.storage.postgres_impl.remap --apply"
                "\n不加 --apply 是 dry-run（只打印计划）。"
                "详见 docs/部署指南-v0.1.md 的「升级」一节。"
            )
        raise SchemaError(
            f"schema 版本为 {version}，高于本应用已知的 {SCHEMA_VERSION}；"
            "库是被更新版应用升过级的，请升级应用而不是降级数据库"
        )
    return _apply_migrations(db, version)


def _apply_migrations(db: Database, version: int) -> int:
    """把缺的增量迁移按序补上，返回补完后的版本。

    **每条一个事务、并把版本号写在同一事务里**：DDL 在中途失败时要么整条生效、
    要么整条回滚，不会留下"表建了但版本没记"的半截状态（下一轮启动会重跑它）。
    这也让并发启动的多个副本天然安全——两个进程同时补同一条时，先提交的那个赢，
    后一个会因为版本已推进而在下一轮跳过（`schema_migrations` 的主键还会挡住重复写入）。
    """
    pending = [item for item in MIGRATIONS if item.version > version]
    for item in pending:
        try:
            with db.session() as conn:
                for statement in item.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (%s, %s)",
                    (item.version, item.description),
                )
        except Exception as exc:
            raise SchemaError(
                f"增量迁移 v{item.version}（{item.description}）执行失败：{exc}"
            ) from exc
        logger.info("已应用增量迁移 v%d：%s", item.version, item.description)
    return current_version(db) if pending else version


def check_extensions(db: Database) -> None:
    """必需的扩展是否已启用。缺了就抛，不降级运行。

    **只在 schema 之后调用**：``CREATE EXTENSION vector`` 就在基线 DDL 里，
    所以"新库还没有扩展"是正常中间态，不是错误。要在 ensure_schema 之后
    才检查，否则全新数据库会被误判成"缺扩展"。

    **不在这里补 CREATE EXTENSION**：那需要超级用户；部署若用非超级用户跑应用，
    悄悄尝试只会留下半截状态。缺扩展时把"该换镜像还是该装扩展"说清楚就够了。
    """
    with db.read() as conn:
        rows = conn.execute(
            "select extname from pg_extension where extname = any(%s)",
            (list(REQUIRED_EXTENSIONS),),
        ).fetchall()
    installed = {row["extname"] for row in rows}
    missing = [name for name in REQUIRED_EXTENSIONS if name not in installed]
    if missing:
        raise SchemaError(
            f"缺少扩展 {missing}。" + (_MISSING_VECTOR_HINT if "vector" in missing else "")
        )


def prepare(db: Database) -> int:
    """装配时的门面：建/校验 schema → 校验扩展。返回当前版本。

    **顺序不能反**（踩过）：基线 DDL 里含 ``CREATE EXTENSION IF NOT EXISTS vector``，
    所以必须先让它跑完再检查扩展，否则空库会被误报成"缺少 vector"。
    """
    version = ensure_schema(db)
    check_extensions(db)
    return version
