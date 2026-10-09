-- KYLAB 本机库基线 schema（kylab.db，v1）
--
-- 本机档（桌面壳的边车进程）的**唯一权威库**：会话、消息、事件、产物、笔记、设置、
-- 工作区、定时任务、MCP 服务、模型注册、用量。方案见
-- `docs/规范/会话落本机-实施方案-v0.1.md` §1。知识库那半边（文档 / 切块 / 向量 /
-- 全文 / Wiki / 任务队列 / 回收站 / 账号会话）**一行都不进本机**——那是 NAS 的家当
-- （v0.3 §4：解析、嵌入、检索、向量索引全在服务端；本机不设门禁）。
--
-- **表数说明**：实施方案 §1.3 的小节标题写"18 张"，它逐行列出的实际是 **17 张**
-- （14 张照搬自 PG + imports/import_items 两张本机独有 + schema_metadata），
-- 本文件按**逐行那份清单**实现。第 18 张不是漏了哪张表，是标题的计数与前文不符。
--
-- DDL 的**唯一属主是应用**：启动时由 `schema.py::ensure_schema` 执行本文件与
-- 其后的增量迁移，不靠人工 sqlite3 命令（第二处真相来源必然漂）。
--
-- ------------------------------------------------------------------ 类型映射（§1.2）
--
-- 本文件里每一条都能对回 PG 侧的写法，机械翻译只有下面六类：
--
--   PG              本机 SQLite(STRICT)                  说明
--   --------------  ----------------------------------  ------------------------------
--   text            TEXT                                照搬
--   boolean         INTEGER + CHECK (x IN (0,1))        可空的三态列仍是 INTEGER，
--                                                       NULL 合法（= 跟随默认）
--   timestamptz     INTEGER，列名带 _ms 后缀            UTC 毫秒
--   jsonb           TEXT + CHECK (json_valid(x))        只整读整写，不做 JSON 内查询
--   bigserial       INTEGER PRIMARY KEY AUTOINCREMENT   session_events.id
--   外键            原样写                              **必须配 PRAGMA foreign_keys=ON**
--                                                       （见 connection.py）
--
-- 时间列**不带 DEFAULT**：毫秒值的唯一属主是应用侧那三个 helper
-- （`_now()` / `_dump()` / `_load()`，与 PG 侧同名同形），SQLite 没有"当前毫秒"的
-- 表达式默认值（`unixepoch()` 要 3.38，本机下限是 3.37）。写侧一律显式给值，
-- 于是"谁写的时间"只有一处，也才可能有 `MAX(旧值+1, now)` 那条推进纪律（§1.2）。
--
-- ------------------------------------------------------------------ 五条规范（§1.4）
--
--  1. **STRICT 表**：全部表都写 STRICT；JSON 列补 CHECK(json_valid(...))。
--     最小 SQLite 版本由此定为 3.37（`connection.py::MIN_SQLITE_VERSION`）。
--  2. **schema_metadata + 迁移前自动备份**：见文件末尾那张表与 `schema.py`。
--     版本号只住 `schema_metadata.version` 一处——**不另写 `PRAGMA user_version`**，
--     那会变成第二个真相来源（与 PG 侧"不建分区登记表"同一条理由）。
--  3. **列表查询走覆盖索引**：索引一律照**服务层真正下推的那条 ORDER BY** 建，
--     不照"看起来该有的"建。见下面每条索引上的注释。
--  4. **列表预览与正文分离**：正文留在表内（理由见 `meta_store.py` 的
--     `LIST_CONVERSATIONS_SQL`），落成两条机械纪律：列表 SQL 不碰正文列、
--     预览在 SQL 里 substr 截断到 200 字符。
--  5. **单库文件**：`kylab.db` + `kylab.db-wal` / `kylab.db-shm`，位置固定可整份备份。
--
-- **建档顺序按外键依赖排**（workspaces 在 conversations 之前、note_folders 在 notes
-- 之前）：SQLite 在 `foreign_keys=ON` 时不会在 CREATE 阶段校验父表存在，
-- 但 INSERT 时会报 "no such table"，顺序反了就会变成一条只在写入时才炸的坑。
--
-- **明文凭据（风险 R6，必须如实标注）**：`model_providers.api_key` 与
-- `mcp_servers.env` / `headers` 是本机库里的**明文**（要发给供应商/服务，不能只存摘要），
-- 与服务器侧同口径；收编钥匙串是 M5 的事。NAS token 绝不落库（走命令行参数）。
-- 也就是说：**`kylab.db` 这个文件本身就该当成秘密对待**。


-- ============================================================
-- 库自身的元数据（规范 2）
-- ============================================================

CREATE TABLE schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
-- 键：`version`（当前 schema 版本，十进制文本）、`created_at_ms`（建库时刻）、
-- `last_backup`（最近一次迁移前备份的路径）、`migration_log`（已应用迁移的 JSON 数组）。


-- ============================================================
-- 设置 / 工作区 / 对话（对话依赖工作区，所以工作区在前）
-- ============================================================

CREATE TABLE app_settings (
    key            TEXT PRIMARY KEY,
    value          TEXT NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;


CREATE TABLE workspaces (
    id             TEXT PRIMARY KEY,
    owner_id       TEXT,
    name           TEXT NOT NULL,
    root_path      TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    kb_ids         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(kb_ids)),
    archived_at_ms INTEGER,
    -- 本机档里这一列是**这台机器**；仍然可空，语义与 PG 侧一致：
    -- NULL = "路径在服务器的盘上"（网页版/直连 API 建的那些）。
    device_id      TEXT,
    device_name    TEXT,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_workspaces_owner    ON workspaces (owner_id, updated_at_ms DESC);
CREATE INDEX idx_workspaces_archived ON workspaces (archived_at_ms, updated_at_ms DESC);


CREATE TABLE conversations (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    kb_ids          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(kb_ids)),
    owner_id        TEXT,
    model_pk        TEXT,
    -- 可空的三态列：NULL = 跟随全局默认，不要折成 0
    thinking        INTEGER CHECK (thinking IN (0, 1)),
    thinking_effort TEXT,
    pinned          INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    -- 这两列不进 API，但**导入/回读要完整**（§1.3）：丢了它们，导入进来的
    -- 长期会话就失去了"已被压缩到哪里"这个信息，续聊会从头重算。
    context_summary TEXT NOT NULL DEFAULT '',
    summary_upto    TEXT,
    -- ON DELETE SET NULL 是刻意的：删工作区**不该删掉里面的会话**（会话里有用户
    -- 问过的内容，误删无法恢复；失去归属只是"掉回未归档"）。
    workspace_id    TEXT REFERENCES workspaces (id) ON DELETE SET NULL,
    archived_at_ms  INTEGER,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
) STRICT;

-- 规范 3：这三条索引照**服务层真正下推的那条 ORDER BY**（`pinned DESC,
-- updated_at_ms DESC`）建——`ConversationService.list` 的归属/工作区/归档过滤
-- 发生在 Python 侧，SQL 只做这条排序（PG 侧也一样），所以别照"看起来该有的"建。
CREATE INDEX idx_conversations_recent    ON conversations (pinned DESC, updated_at_ms DESC);
CREATE INDEX idx_conversations_workspace ON conversations (workspace_id, pinned DESC, updated_at_ms DESC);
CREATE INDEX idx_conversations_archived  ON conversations (archived_at_ms, pinned DESC, updated_at_ms DESC);


CREATE TABLE chat_messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    sources         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sources)),
    steps           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(steps)),
    thinking        TEXT NOT NULL DEFAULT '',
    attachments     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attachments)),
    created_at_ms   INTEGER NOT NULL
) STRICT;

-- 读形状只有一种：某条会话的消息、按先后。索引就照这个形状建。
-- 同一毫秒内的两条消息靠 `rowid`（插入序）兜底，见 `list_messages`。
CREATE INDEX idx_chat_messages_conversation ON chat_messages (conversation_id, created_at_ms);


CREATE TABLE session_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    created_at_ms   INTEGER NOT NULL,
    CONSTRAINT uq_session_events_seq UNIQUE (conversation_id, seq)
) STRICT;
-- **不另建索引**：上面那条唯一约束建出的索引正好就是读形状
-- （前缀 conversation_id + seq 有序），多一条只会多一份写侧代价与一处会漂的定义。
--
-- 这张表**只有 append**：没有 updated_at，也没有任何写侧方法（只有 append / list）。
-- 能被改的日志回答不了"当时发生了什么"，而那正是它存在的理由。


CREATE TABLE conversation_artifacts (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    format            TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    storage           TEXT NOT NULL,
    location          TEXT NOT NULL DEFAULT '',
    workspace_id      TEXT,
    owner_id          TEXT,
    knowledge_base_id TEXT,
    document_id       TEXT,
    created_at_ms     INTEGER NOT NULL
) STRICT;
-- **不加 `origin` 列**（§1.3）：导入来源记在导入台账（imports / import_items）里，
-- 不往业务表上挂一个只有导入器关心的字段。

CREATE INDEX idx_conversation_artifacts_conv ON conversation_artifacts (conversation_id, created_at_ms);


-- ============================================================
-- 笔记 / 笔记文件夹（文件夹依赖自身，先建）
-- ============================================================

CREATE TABLE note_folders (
    id             TEXT PRIMARY KEY,
    user_id        TEXT,
    name           TEXT NOT NULL,
    -- 自引用：删父文件夹连带删子文件夹，**但里面的笔记不跟着消失**（见下一条）。
    parent_id      TEXT REFERENCES note_folders (id) ON DELETE CASCADE,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_note_folders_owner ON note_folders (user_id, name);
-- 同级重名在数据库层也挡住：唯一索引里 NULL 互不相等，所以根级文件夹
-- （parent_id 为 NULL）与无归属通道（user_id 为 NULL）要显式 coalesce 成 ''。
-- `''` 不会与真实 id 撞：id 一律是 `fld_<hex>` / `usr_<hex>` 前缀。
CREATE UNIQUE INDEX uq_note_folders_sibling_name
    ON note_folders (coalesce(user_id, ''), coalesce(parent_id, ''), name);


CREATE TABLE notes (
    id             TEXT PRIMARY KEY,
    user_id        TEXT,
    title          TEXT NOT NULL DEFAULT '',
    content_md     TEXT NOT NULL DEFAULT '',
    source_kind    TEXT NOT NULL DEFAULT 'manual',
    source_ref     TEXT,
    kb_id          TEXT,
    doc_id         TEXT,
    -- 删文件夹 → 里面的笔记**回到未归档**，不是跟着消失（删容器不该销毁内容）。
    folder_id      TEXT REFERENCES note_folders (id) ON DELETE SET NULL,
    pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_notes_owner  ON notes (user_id, pinned DESC, updated_at_ms DESC);
CREATE INDEX idx_notes_folder ON notes (user_id, folder_id);


CREATE TABLE note_tags (
    note_id TEXT NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
) STRICT;

CREATE INDEX idx_note_tags_tag ON note_tags (tag);


-- ============================================================
-- 定时任务 / MCP 服务 / 模型注册 / 用量
-- ============================================================

CREATE TABLE scheduled_tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    cron            TEXT NOT NULL DEFAULT '',
    run_at_ms       INTEGER,
    -- 调度侧**唯一的游标**：同时承担"下次什么时候跑"与"这一次有没有人认领"（CAS）。
    next_run_at_ms  INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    kb_ids          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(kb_ids)),
    model_pk        TEXT,
    thinking        INTEGER CHECK (thinking IN (0, 1)),
    thinking_effort TEXT,
    -- 删掉那条会话**不等于**取消这个任务：下一轮它会重新建一条
    -- （比"任务悄悄没了"好得多）。
    conversation_id TEXT REFERENCES conversations (id) ON DELETE SET NULL,
    owner_id        TEXT,
    last_run_at_ms  INTEGER,
    last_status     TEXT NOT NULL DEFAULT '',
    last_error      TEXT NOT NULL DEFAULT '',
    run_count       INTEGER NOT NULL DEFAULT 0,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
) STRICT;

-- 扫描形状只有一种：**启用的、到点的、按时间正序**。部分索引正好对上它
-- （停用的那些永远不进这个索引，而它们通常不少）。
CREATE INDEX idx_scheduled_tasks_due ON scheduled_tasks (next_run_at_ms) WHERE enabled;


CREATE TABLE mcp_servers (
    id             TEXT PRIMARY KEY,
    owner_id       TEXT,
    name           TEXT NOT NULL,
    transport      TEXT NOT NULL,
    target         TEXT NOT NULL,
    args           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(args)),
    -- **凭据是明文**（风险 R6）：要发给那个服务，不能只存摘要。见文件头。
    env            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(env)),
    headers        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(headers)),
    policy         TEXT NOT NULL DEFAULT 'ask',
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_mcp_servers_owner ON mcp_servers (owner_id, updated_at_ms DESC);


CREATE TABLE model_providers (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    base_url       TEXT NOT NULL DEFAULT '',
    -- **可还原的明文**（要发给供应商）。与"API Key 只存摘要"是两回事。见文件头。
    api_key        TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;


CREATE TABLE model_registry (
    id             TEXT PRIMARY KEY,
    provider_id    TEXT NOT NULL REFERENCES model_providers (id) ON DELETE CASCADE,
    model_id       TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    dim            INTEGER,
    capabilities   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(capabilities)),
    options        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(options)),
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
) STRICT;

-- 这个唯一约束就是 `create_registered_model` 把 IntegrityError 翻成
-- `ConflictError("该供应商下已经登记过模型 …")` 的依据。
CREATE UNIQUE INDEX idx_model_registry_provider_model ON model_registry (provider_id, model_id);


CREATE TABLE usage_events (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT '',
    model_id          TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    items             INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    -- 三态而不是布尔：`reported`（供应商实测）/ `estimated`（按字符数估的）/
    -- `none`（没报也没得估）。混成一类会让统计页显示一个假精度。
    -- 记录上的 `reported` 是 `source == 'reported'` 的属性，所以这里是**表列多于
    -- dataclass 字段**的唯一一处（字段一致性用例里登记在例外表）。
    reported          INTEGER NOT NULL DEFAULT 0 CHECK (reported IN (0, 1)),
    source            TEXT NOT NULL DEFAULT 'reported',
    created_at_ms     INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_usage_events_created ON usage_events (created_at_ms);


-- ============================================================
-- 旧会话一次性导入的台账（本机独有；§3 / 阶段 5 的落点）
-- ============================================================
--
-- 这张表是**幂等**与**可回滚**两件事的地基：
--  - 幂等键 `(source, conversation_id, source_updated_at_ms)`：重跑同一批时
--    全部命中已导入 → 报告里全是 skipped → **一行不写**；
--  - `outcome` 记下"这一条当时是新建还是替换"，回滚才有规则可依：
--    `created` 且本机没再动过 → 删；`replaced` → 用快照恢复；
--    本机改过的一律保留并如实报数。
CREATE TABLE imports (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    since_ms      INTEGER,
    state         TEXT NOT NULL
                  CHECK (state IN ('planned', 'running', 'done', 'failed', 'rolled_back')),
    counts_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(counts_json)),
    error         TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
) STRICT;


CREATE TABLE import_items (
    batch_id             TEXT NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    conversation_id      TEXT NOT NULL,
    source               TEXT NOT NULL,
    source_updated_at_ms INTEGER NOT NULL,
    outcome              TEXT NOT NULL CHECK (outcome IN ('created', 'replaced')),
    local_updated_at_ms  INTEGER,
    created_at_ms        INTEGER NOT NULL,
    PRIMARY KEY (source, conversation_id, source_updated_at_ms)
) STRICT;

CREATE INDEX idx_import_items_batch ON import_items (batch_id);


-- ============================================================
-- 版本号（规范 2）
-- ============================================================
--
-- **写在本脚本末尾，而不是由 Python 另补一句 INSERT**：DDL 与版本号必须同一个
-- 事务落地，否则中途崩掉会得到一个"表建好了、版本没记"的库——下一轮启动把它
-- 读成版本 0，而 0 比基线还旧，于是既不重建也不通过，只能靠人修（`current_version`
-- 那三种状态里最难受的一种）。与 PG 的 `schema.sql` 末尾那条
-- `INSERT INTO schema_migrations` 是同一手法。
--
-- 这个字面量必须与 `schema.py::BASELINE_VERSION` 相等，用例会机械核。
--
-- 建库时刻（`created_at_ms`）由 Python 侧在脚本之后补写——它只是信息性字段
-- （版本判断不依赖它），丢了不影响任何结论，所以不为了把它塞进同一事务而把
-- 整份 DDL 拆成两段。
INSERT INTO schema_metadata (key, value) VALUES ('version', '1');
