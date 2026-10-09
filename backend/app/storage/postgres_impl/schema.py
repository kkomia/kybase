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

#: 惰性拿到的 psycopg（**故意不是模块级导入** ✗）。
_PSYCOPG: Any = None


def _psycopg() -> Any:
    """第一次真用 psycopg 时才导入它（模块级导入会让**客户端运行时**凭空多背一份 ✗）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"建库 / 加索引"只有
    **服务器**启动那条路会做 ✓ —— 边车（客户端运行时）不碰 PostgreSQL 的 schema ✓。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，psycopg 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
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

SCHEMA_VERSION = 17
"""应用期望的 schema 版本：基线 v1 + ``MIGRATIONS`` 里已追加的增量。

**启动时会对不上就自动补**：低于它就按序应用缺的那些迁移，高于它才报错
（库被更新版应用升过级）。这与"库比基线还旧"是两码事——后者要人工处理。"""


@dataclass(frozen=True, slots=True)
class Migration:
    """一条增量迁移。**只增不改**：已经发布的条目一律不许改字面量。"""

    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
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
        description="工作区（Agent 的项目）：会话挂到工作区下，知识库范围跟着工作区走（v0.15）",
        statements=(
            # 工作区 = Agent 的"在哪干活"。`root_path` 是用户指定的真实目录
            # （见 docs/设计/Agent-工作区与能力层设计-v0.1.md §3.2）。沙箱与它是两个概念，
            # 所以这里**没有** sandbox 字段。
            """
            CREATE TABLE workspaces (
                id          text PRIMARY KEY,
                owner_id    text,
                name        text NOT NULL,
                root_path   text NOT NULL,
                description text NOT NULL DEFAULT '',
                kb_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at  timestamptz NOT NULL DEFAULT now(),
                updated_at  timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX idx_workspaces_owner ON workspaces (owner_id, updated_at DESC)",
            # `ON DELETE SET NULL` 是刻意的：删工作区**不该删掉里面的会话**。
            # 会话里有用户问过的内容，误删无法恢复；失去归属只是"掉回未归档那一栏"。
            """
            ALTER TABLE conversations
                ADD COLUMN workspace_id text REFERENCES workspaces (id) ON DELETE SET NULL
            """,
            # 侧栏按工作区分组拉会话，这条索引直接服务那个查询
            """
            CREATE INDEX idx_conversations_workspace
                ON conversations (workspace_id, pinned DESC, updated_at DESC)
            """,
        ),
    ),
    Migration(
        version=6,
        description="MCP 服务登记：接外部工具进来（v0.15，见 Agent-工作区与能力层设计 §6.2）",
        statements=(
            # 在此之前 KYLAB 只做 MCP **服务端**；这张表是客户端的一半。
            # `policy` 默认 `ask`：接一个外部服务进来就默认让它静默执行动作，
            # 是这一层最不该有的默认（QwenPaw 的 Drivers 也是"每次调用过策略闸"）。
            """
            CREATE TABLE mcp_servers (
                id         text PRIMARY KEY,
                owner_id   text,
                name       text NOT NULL,
                transport  text NOT NULL,
                target     text NOT NULL,
                args       jsonb NOT NULL DEFAULT '[]'::jsonb,
                env        jsonb NOT NULL DEFAULT '{}'::jsonb,
                headers    jsonb NOT NULL DEFAULT '{}'::jsonb,
                policy     text NOT NULL DEFAULT 'ask',
                enabled    boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX idx_mcp_servers_owner ON mcp_servers (owner_id, updated_at DESC)",
        ),
    ),
    Migration(
        version=7,
        description="会话归档：把不看了的会话收起来，而不是删掉（v0.17，照 Kimi 的历史会话）",
        statements=(
            # 用**时间戳**而不是布尔：归档这件事"什么时候做的"本身有用
            # （归档视图按它排序），而布尔还得再加一列才行。
            # `NULL` = 未归档，这也是默认值——存量会话全部落在"未归档"里。
            "ALTER TABLE conversations ADD COLUMN archived_at timestamptz",
            # 列表默认 `archived_at IS NULL`，这条索引直接服务它
            """
            CREATE INDEX idx_conversations_archived
                ON conversations (archived_at, pinned DESC, updated_at DESC)
            """,
        ),
    ),
    Migration(
        version=8,
        description="知识库提示词：回答这个库的问题时该怎么答，从对话页搬到库上（v0.19）",
        statements=(
            # **空串 = 用内置提示词**（`services/chat.build_messages` 在空的时候会退回
            # `DEFAULT_SYSTEM_PROMPT`）。存量库全部落在"没配过"这一档，
            # 行为与迁移前一致——这条迁移不该让任何一个既有库的答案变样。
            "ALTER TABLE knowledge_bases ADD COLUMN system_prompt text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=9,
        description="把过程存下来：工具步骤与思考过程随消息落库（v0.25）",
        statements=(
            # **为什么落库**：这两样原先只活在流式那几秒里。用户看着它们跑，
            # 一旦离开这一页再回来（或者刷新），过程就只剩一句"已生成回答"——
            # 而"这句答案是怎么来的"恰恰是他回来要找的东西。
            #
            # 与 `sources` 同一个道理：`sources` 早就落库了（"当时依据的是哪几段，
            # 事后回看必须还是那几段"），步骤与思考是同一类快照。
            "ALTER TABLE chat_messages ADD COLUMN steps jsonb NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE chat_messages ADD COLUMN thinking text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=10,
        description="会话产物：导出文件先落盘、入库变成一个显式动作（v0.26）",
        statements=(
            # 这张表回答两个原先答不出来的问题：
            # ① "这条会话产出了哪些文件"（此前只能翻文档列表猜）；
            # ② "它现在在哪、有没有进知识库"——storage/location 是前者，
            #    document_id 是后者，而后者默认为 NULL（不进）。
            #
            # `ON DELETE CASCADE` 只清记录：**文件本体由服务层决定**。
            # 落在工作区里的那些是他项目里的真实文件，删会话不该动它们。
            """
            CREATE TABLE conversation_artifacts (
                id                text PRIMARY KEY,
                conversation_id   text NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
                name              text NOT NULL,
                format            text NOT NULL,
                size_bytes        bigint NOT NULL DEFAULT 0,
                storage           text NOT NULL,
                location          text NOT NULL DEFAULT '',
                workspace_id      text,
                owner_id          text,
                knowledge_base_id text,
                document_id       text,
                created_at        timestamptz NOT NULL DEFAULT now()
            )
            """,
            # 读取形状只有一种：某条会话的产物、按先后。索引就照这个形状建。
            "CREATE INDEX idx_conversation_artifacts_conv"
            " ON conversation_artifacts (conversation_id, created_at)",
        ),
    ),
    Migration(
        version=11,
        description="用户头像：只存对象存储的 key（v0.29）",
        statements=(
            # 只加一个 key 列：图片本体在对象存储里（见 services/avatars.py
            # 模块头的两条理由——账号是每个页面都读的东西；内容寻址的 key
            # 让换头像天然避开 <img> 的缓存）。
            "ALTER TABLE users ADD COLUMN avatar_key text NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        version=12,
        description="定时任务：到点替用户跑一轮问答（v0.33）",
        statements=(
            # 这张表回答"有没有哪件事是到点就该做、而我不想每次自己去问一遍"。
            #
            # 三处刻意的形状（与 ``base.ScheduledTaskRecord`` 的说明一一对应）：
            # ① 两种时间形态（cron / 一次）各占一列，而不是塞进一个含糊的字段；
            # ② `next_run_at` 同时承担"下次什么时候跑"与"这一次有没有人认领"——
            #    认领走一条带条件的 UPDATE（见 meta_store.arm_scheduled_task）；
            # ③ 结果不另存一套"运行历史"：每次运行就是那条会话里的一轮问答，
            #    这里只留最近一次的结论（状态 / 错误 / 时间）。
            #
            # `conversation_id` 是 ON DELETE SET NULL：用户删掉那条会话**不等于**
            # 取消这个任务——下一轮它会重新建一条（比"任务悄悄没了"好得多）。
            """
            CREATE TABLE scheduled_tasks (
                id              text PRIMARY KEY,
                name            text NOT NULL,
                prompt          text NOT NULL,
                kind            text NOT NULL,
                cron            text NOT NULL DEFAULT '',
                run_at          timestamptz,
                next_run_at     timestamptz,
                enabled         boolean NOT NULL DEFAULT true,
                kb_ids          jsonb NOT NULL DEFAULT '[]'::jsonb,
                model_pk        text,
                thinking        boolean,
                thinking_effort text,
                conversation_id text REFERENCES conversations (id) ON DELETE SET NULL,
                owner_id        text,
                last_run_at     timestamptz,
                last_status     text NOT NULL DEFAULT '',
                last_error      text NOT NULL DEFAULT '',
                run_count       integer NOT NULL DEFAULT 0,
                created_at      timestamptz NOT NULL DEFAULT now(),
                updated_at      timestamptz NOT NULL DEFAULT now()
            )
            """,
            # 扫描形状只有一种：**启用的、到点的、按时间正序**。部分索引正好对上它
            # （停用的那些永远不进这个索引，而它们通常不少——跑完的一次性任务、
            # 用户临时关掉的周期性任务）。
            "CREATE INDEX idx_scheduled_tasks_due ON scheduled_tasks (next_run_at) WHERE enabled",
        ),
    ),
    Migration(
        version=13,
        description="会话事件日志：只追加的事件表，steps 快照变成它的投影（P0-2，抄 ZCode）",
        statements=(
            # 抄的是 ZCode 的**只追加事件日志**（开发计划 §12.225 / P0-2）：
            # 会话是一串不可变的事件，`chat_messages.steps` 那份"流式当时拍下的快照"
            # 从此是它的一个投影。`kind` 的词表在 `services/session_events.py`
            # 一处定义（存储层不认识业务词表，所以这里不加 CHECK）。
            #
            # 三处刻意的形状：
            # ① **只有 append**：没有 updated_at 之类的列，也没有任何写侧方法
            #    （`MetaStore` 只给 append / list）——能被改的日志回答不了
            #    "当时发生了什么"，而那正是这张表存在的理由；
            # ② `seq` 是**会话内**的单调序号，由写那个事务算（max+1）：
            #    同一毫秒里的并发工具调用靠 created_at 分不出先后，而回放要读它；
            # ③ `id` 是 bigserial，与 `document_stage_events` 同一种形状
            #    —— 项目里另一张"只追加的事件表"，不发明第二套写法。
            #
            # `ON DELETE CASCADE`：会话删了，它的事件跟着走（与 chat_messages 同规则）。
            """
            CREATE TABLE session_events (
                id              bigserial PRIMARY KEY,
                conversation_id text NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
                seq             bigint NOT NULL,
                kind            text NOT NULL,
                payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT uq_session_events_seq UNIQUE (conversation_id, seq)
            )
            """,
            # 读形状只有一种：某条会话按 seq 正序（可带 kind 过滤）。上面那条唯一约束
            # 建出的索引**正好就是这个形状**（前缀 conversation_id + seq 有序），
            # 所以这里不再另建一条索引：多一条只会多一份写侧的代价与一处会漂的定义。
        ),
    ),
    Migration(
        version=14,
        description="笔记文件夹：左栏的层级文件夹 + 笔记的归属（parent_id 自引用）",
        statements=(
            # 笔记从"扁平 + 标签"长出一层文件夹。与知识库目录（kb_folders，单层）刻意不同：
            # **笔记允许嵌套**（`parent_id` 自引用），因为这里的文件夹是用户自己的知识
            # 组织方式，"工作 / 会议记录"这样的两层是常态，而知识库的目录只是分组文件。
            #
            # 两条 `ON DELETE` 语义**是这张迁移的核心决定**，不是默认值：
            #
            # ① `parent_id` 用 ``CASCADE``：删一个文件夹连带删掉它的子文件夹。
            #    子文件夹是"这个文件夹下面的一层"，父不在，那一层也就无从挂起；
            #    换成 SET NULL 会把子文件夹悄悄挪到根级（用户会以为它们丢了，
            #    与 folder.py 里"非空目录拒绝删"防的是同一件事）。**注意这里连带删掉的
            #    只是文件夹本身**：子树里的笔记一个都不会没（见下一条）。
            #
            # ② `notes.folder_id` 用 ``SET NULL``：删文件夹 → 里面的笔记**回到未归档**，
            #    而不是跟着消失。这与本产品"删除笔记要先问"是同一条纪律——删一个容器
            #    不该顺手销毁里面的内容，而笔记误删不可恢复（正文没有第二份）。
            #    界面对此负责：确认框里会把"里面的 N 篇笔记会回到未归档"说清楚。
            #
            # 代价写在明处：这两条语义意味着"删文件夹"是一个**会动的**操作，
            # 所以它永远不该被静默触发（界面上是一次显式确认，API 上调 DELETE）。
            """
            CREATE TABLE note_folders (
                id         text PRIMARY KEY,
                user_id    text,
                name       text NOT NULL,
                parent_id  text REFERENCES note_folders (id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            # 树每次都按"谁的文件夹、按名字排"整份读出来（个人规模，不做分页），
            # 这条索引直接服务它。
            "CREATE INDEX idx_note_folders_owner ON note_folders (user_id, lower(name))",
            # 同级重名在**数据库层**也要挡住（服务层先查一次是为了给出人的话，
            # 这一条是最后一道）。写成表达式索引是因为 `coalesce`：
            # 唯一索引里 NULL 互不相等，而"根级文件夹"（parent_id 为 NULL）
            # 与"无归属通道建的文件夹"（user_id 为 NULL）同样要挡住重名。
            # `''` 不会与真实 id 撞：id 一律是 `fld_<hex>` / `usr_<hex>` 前缀。
            """
            CREATE UNIQUE INDEX uq_note_folders_sibling_name
                ON note_folders (coalesce(user_id, ''), coalesce(parent_id, ''), name)
            """,
            """
            ALTER TABLE notes
                ADD COLUMN folder_id text REFERENCES note_folders (id) ON DELETE SET NULL
            """,
            # 列表的过滤形状是"谁的、哪个文件夹"+置顶/时间排序；未归档（folder_id IS NULL）
            # 与具体文件夹两种取值都走这条索引（NULL 也在索引里）。
            "CREATE INDEX idx_notes_folder ON notes (user_id, folder_id)",
        ),
    ),
    Migration(
        version=15,
        description="项目（工作区）归档：把不看了的项目收起来，而不是删掉（v0.55）",
        statements=(
            # 与会话归档（v7）同一口径：用**时间戳**而不是布尔——“什么时候归档的”
            # 本身有用（归档视图按它排序），布尔还得再加一列才行。
            # `NULL` = 未归档，存量项目全部落在“未归档”里，行为与迁移前一致。
            "ALTER TABLE workspaces ADD COLUMN archived_at timestamptz",
            # 列表默认 `archived_at IS NULL`，这条索引直接服务它
            "CREATE INDEX idx_workspaces_archived ON workspaces (archived_at, updated_at DESC)",
        ),
    ),
    Migration(
        version=16,
        description="用户消息带上随发的附件快照：这条消息是带着哪几份文件发出去的（v0.55）",
        statements=(
            # 用户报的："对话中上传的文件，没有在我发送的对话中有文件组件标识。"
            # 查下来不是前端没画——**数据里根本没有这个关联**：上传只记到会话
            # （`conversation_artifacts.conversation_id`），消息是纯文本，
            # 两点之间只有时间先后，指认不出"这几份是随这条消息发出去的"。
            #
            # 存**快照**（key / 名字 / 类型 / 字节数）而不是外键 id，与 `sources`
            # 同一口径：回看一条旧消息时，当时带着什么就该是什么，
            # 哪怕那份文件后来被删了、或文件区的落点变了。
            "ALTER TABLE chat_messages ADD COLUMN attachments jsonb NOT NULL DEFAULT '[]'::jsonb",
        ),
    ),
    Migration(
        version=17,
        description="工作区按设备隔离：记录归属设备，桌面端各见各的（v0.59）",
        statements=(
            # 桌面壳每次请求带 `X-Kylab-Device: <uuid>`，工作区因此有了**第二维归属**：
            # device_id 是**设备的稳定标识**（壳生成的 uuid），device_name 是给人看的名字。
            #
            # `device_id IS NULL` = **服务器端**的工作区：网页版/直连 API 不带设备头，
            # 它们看到的就是这一批——语义不是"没归属"，而是"路径在服务器的盘上"
            # （见 base.py 的 WorkspaceRecord）。所以这一列**必须可空**，
            # 存量记录全部落在"服务器端"这一档，行为与迁移前一致。
            #
            # 不加索引：工作区是几十行规模的表，而列表本来就整表取回来再在服务层
            # 按归属过滤（存储层不认识调用者身份），加一条只为设备用的索引没有收益。
            "ALTER TABLE workspaces ADD COLUMN device_id TEXT",
            "ALTER TABLE workspaces ADD COLUMN device_name TEXT",
        ),
    ),
)


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
