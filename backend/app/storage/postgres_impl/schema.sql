-- KYLAB PostgreSQL 基线 schema（v1）
--
-- 这份文件是**目标设计草案**，取代 SQLite 那 24 条增量迁移（migrations.py）。
-- 因为本次迁移**不做数据迁移**（旧库直接舍弃），所以不需要逐版重放：
-- 这一份 DDL 就是第 1 版基线，之后的演进再用 schema_migrations 记版本。
--
-- 与 SQLite schema 的有意差异（都是"趁重来一次把它做对"的地方）：
--
--  1. **ID 保持 text，不换 uuid**。ID 是应用生成且带前缀的业务标识
--     （kb_… / doc_… / user_…），前缀本身是接口契约的一部分。换 uuid 要动
--     173 个方法与全部 API 响应，收益为零。
--  2. **布尔用真 boolean**，不再是 INTEGER 0/1 + CHECK。Python 侧不再需要
--     int(bool)/bool(row) 来回转。
--  3. **时间戳用 timestamptz**，不再是 ISO 文本。排序/比较交给数据库，
--     不再依赖"ISO 字符串字面序恰好等于时间序"这个巧合。
--  4. **JSON 列用 jsonb**（原为 json.dumps 的 TEXT）。仍然由 Python 侧
--     json.loads/dumps 读写，但可以用 jsonb_array_length 这类函数、也便于将来加索引。
--  5. **全文检索不再是独立虚拟表**。原 chunks_fts（FTS5）与 chunks 是两张表、
--     查询要 JOIN；现在 tokens 就是 chunks 上的一个生成列，索引与过滤同一行完成。
--     中文分词继续在应用侧用 jieba 做（架构 §8），数据库只负责索引与排序。
--  6. **向量分区的维度也不靠解析建表语句**。原实现从 sqlite_master 的建表 SQL 里
--     正则抠维度；PG 这边既有 pg_attribute.atttypmod（维度）、
--     pg_class（分区列表）可用，就不需要任何"分区登记表"——分区即真相。
--  7. **队列领取改用 FOR UPDATE SKIP LOCKED**，不再依赖 SQLite 的"单写者"语义。
--
-- 过渡说明：应用侧 PG 实现（storage/postgres_impl/）尚未落地。在它接管之前，
-- 这份文件由 compose 挂进 PG 容器的 docker-entrypoint-initdb.d 供检视；
-- **PG 实现落地后，DDL 的唯一属主是应用**（启动时校验/迁移），届时请删除
-- compose 里那行挂载，避免出现第二个真相来源。

CREATE EXTENSION IF NOT EXISTS vector;

-- 可选：模糊匹配/相似度（文档名搜索）。用到时再开，避免装无用的扩展。
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ============================================================
-- 账号与鉴权
-- ============================================================

CREATE TABLE users (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    note          text NOT NULL DEFAULT '',
    username      text,
    password_hash text,
    role          text NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    disabled      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 显示名唯一（原 idx_users_name）
CREATE UNIQUE INDEX idx_users_name ON users (name);
-- 用户名唯一，但允许"还没开通账号"的成员为 NULL（原部分唯一索引）
CREATE UNIQUE INDEX idx_users_username ON users (username) WHERE username IS NOT NULL;


CREATE TABLE sessions (
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sessions_user ON sessions (user_id);
-- 清理过期会话按 expires_at 扫，给它一条索引
CREATE INDEX idx_sessions_expires ON sessions (expires_at);


CREATE TABLE api_keys (
    id                 text PRIMARY KEY,
    name               text NOT NULL,
    key_hash           text NOT NULL UNIQUE,
    key_prefix         text NOT NULL DEFAULT '',
    permission         text NOT NULL CHECK (permission IN ('readonly', 'readwrite')),
    knowledge_base_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by         text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_used_at       timestamptz
);


CREATE TABLE app_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 模型注册表（供应商 → 模型）；默认模型的选择存在 app_settings 里
-- ============================================================

CREATE TABLE model_providers (
    id         text PRIMARY KEY,
    kind       text NOT NULL,
    name       text NOT NULL,
    base_url   text NOT NULL DEFAULT '',
    api_key    text NOT NULL DEFAULT '',
    -- 注意：这里存的是**可还原的明文**（要发给供应商，不能只存摘要）。
    -- 与 api_keys 的"只存摘要"是两回事。将来若要静态加密，落点在这一列。
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE model_registry (
    id           text PRIMARY KEY,
    provider_id  text NOT NULL REFERENCES model_providers (id) ON DELETE CASCADE,
    model_id     text NOT NULL,
    label        text NOT NULL DEFAULT '',
    dim          integer,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    options      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_model_registry_provider_model
    ON model_registry (provider_id, model_id);


-- ============================================================
-- 知识库与文档
-- ============================================================

CREATE TABLE knowledge_bases (
    id                text PRIMARY KEY,
    name              text NOT NULL,
    description       text NOT NULL DEFAULT '',
    -- 嵌入模型是**知识库属性**，建库时定、随库冻结（架构 §6.4）
    embedding_model_id text NOT NULL,
    embedding_model_pk text,
    embedding_dim     integer NOT NULL CHECK (embedding_dim > 0),
    embedding_base_url text,
    chunk_strategy    text NOT NULL DEFAULT 'fixed',
    chunk_size        integer NOT NULL DEFAULT 512 CHECK (chunk_size > 0),
    chunk_overlap     integer NOT NULL DEFAULT 64 CHECK (chunk_overlap >= 0),
    -- 推荐问题（出题）
    suggested_enabled boolean NOT NULL DEFAULT true,
    suggested_count   integer NOT NULL DEFAULT 6,
    suggested_model_pk text,
    suggested_prompt  text NOT NULL DEFAULT '',
    -- 库形态（v24）：是否额外开启 Wiki
    wiki_enabled      boolean NOT NULL DEFAULT false,
    owner_id          text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE kb_folders (
    id         text PRIMARY KEY,
    kb_id      text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_id, name)
);

CREATE INDEX idx_kb_folders_kb ON kb_folders (kb_id);
-- 原 SQLite 用 ORDER BY name COLLATE NOCASE；PG 没有 NOCASE，
-- 改用 ORDER BY lower(name)，配这条索引。
CREATE INDEX idx_kb_folders_kb_name ON kb_folders (kb_id, lower(name));


CREATE TABLE kb_shares (
    kb_id      text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    user_id    text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    permission text NOT NULL CHECK (permission IN ('read', 'write')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kb_id, user_id)
);

CREATE INDEX idx_kb_shares_user ON kb_shares (user_id);


CREATE TABLE data_sources (
    id                text PRIMARY KEY,
    knowledge_base_id text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    kind              text NOT NULL,
    name              text NOT NULL,
    config            jsonb NOT NULL DEFAULT '{}'::jsonb,
    etag              text,
    last_pulled_at    timestamptz,
    enabled           boolean NOT NULL DEFAULT true
);

CREATE INDEX idx_data_sources_kb ON data_sources (knowledge_base_id);


CREATE TABLE documents (
    id                text PRIMARY KEY,
    knowledge_base_id text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    folder_id         text,
    name              text NOT NULL,
    source_kind       text NOT NULL,
    content_hash      text NOT NULL,
    stage             text NOT NULL,
    size_bytes        bigint NOT NULL DEFAULT 0,
    mime_type         text,
    page_count        integer,
    is_split          boolean NOT NULL DEFAULT false,
    disabled          boolean NOT NULL DEFAULT false,
    error             text,
    uploaded_by       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (knowledge_base_id, content_hash)
);

CREATE INDEX idx_documents_kb ON documents (knowledge_base_id, created_at DESC);
CREATE INDEX idx_documents_stage ON documents (stage);
CREATE INDEX idx_documents_folder ON documents (folder_id);


CREATE TABLE document_parts (
    id          text PRIMARY KEY,
    document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    part_index  integer NOT NULL,
    page_start  integer NOT NULL,
    page_end    integer NOT NULL,
    stage       text NOT NULL,
    error       text,
    UNIQUE (document_id, part_index),
    CHECK (page_end >= page_start)
);


CREATE TABLE parse_results (
    document_id   text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    part_id       text NOT NULL DEFAULT '',
    parser_name   text NOT NULL,
    markdown_path text NOT NULL,
    probe_meta    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, part_id)
);


-- ============================================================
-- 分段与检索
-- ============================================================

CREATE TABLE chunks (
    chunk_id          text PRIMARY KEY,
    document_id       text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    knowledge_base_id text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    part_id           text REFERENCES document_parts (id) ON DELETE SET NULL,
    ordinal           integer NOT NULL,
    text              text NOT NULL,
    content_hash      text NOT NULL,
    heading_path      text,
    page              integer,
    disabled          boolean NOT NULL DEFAULT false,
    questions         jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- 全文检索：应用侧 jieba 切词后写 tokens_text（空格分隔），
    -- 生成列由 PG 维护成 tsvector。查询用 to_tsquery('simple', …)，排序用 ts_rank_cd。
    --
    -- 为什么用 'simple'：词已经切好了，不需要 stemming/停用词；
    -- 用 'simple' 就**不需要装 zhparser 之类的分词扩展**（架构 §8 中文方案）。
    -- 显式写 ::regconfig 是因为生成列要求表达式 immutable，而单参数形式是 stable。
    tokens_text       text NOT NULL DEFAULT '',
    tokens            tsvector GENERATED ALWAYS AS
                          (to_tsvector('simple'::regconfig, tokens_text)) STORED,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX idx_chunks_document ON chunks (document_id, ordinal);
CREATE INDEX idx_chunks_kb ON chunks (knowledge_base_id);
-- 全文索引：GIN + 生成列，取代原来的 chunks_fts 虚拟表
CREATE INDEX idx_chunks_tokens ON chunks USING gin (tokens);


CREATE TABLE images (
    image_id     text PRIMARY KEY,
    document_id  text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    storage_path text NOT NULL,
    page         integer,
    bbox         text,
    caption      text
);

CREATE INDEX idx_images_document ON images (document_id);


CREATE TABLE chunk_images (
    chunk_id text NOT NULL REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    image_id text NOT NULL,
    PRIMARY KEY (chunk_id, image_id)
);


-- ============================================================
-- 向量分区
-- ============================================================
--
-- 每个知识库一张 vec_<kb_id> 表（维度随库而定），与原 SQLite 模型一致：
-- pgvector 的列维度是固定的，无法一列装多种维度。
-- 真正的改进在于**维度不再靠反查系统表**——登记在这张表里：
--   dim 建分区时写入，list_partitions() 直接查它，不需要正则抠建表语句。
-- 分区表的 DDL（由应用在 ensure_partition 时执行）：
--   CREATE TABLE vec_<kb_id> (
--       chunk_id  text PRIMARY KEY,
--       embedding vector(<dim>) NOT NULL
--   );
--   CREATE INDEX ON vec_<kb_id> USING hnsw (embedding vector_cosine_ops);
-- 查询（取代 sqlite-vec 的 MATCH ... k=?）：
--   SELECT chunk_id, embedding <=> :q AS distance FROM vec_<kb_id>
--    ORDER BY embedding <=> :q LIMIT :k;
--
-- **不建"分区登记表"**：分区就是那些表本身，维度可从系统目录直接读出
-- （pgvector 把维度放在 pg_attribute.atttypmod），分区列表查 pg_class 即得。
-- 登记表会引入第二处事实来源——建表成功但登记失败、或 drop 了忘了删行，
-- 两种漂移都要额外代码兜。这与 sqlite_impl 里"不另建分区表"的判断一致。


-- ============================================================
-- 对话
-- ============================================================

CREATE TABLE conversations (
    id              text PRIMARY KEY,
    title           text NOT NULL DEFAULT '',
    kb_ids          jsonb NOT NULL DEFAULT '[]'::jsonb,
    owner_id        text,
    model_pk        text,
    thinking        boolean,
    thinking_effort text,
    pinned          boolean NOT NULL DEFAULT false,
    context_summary text NOT NULL DEFAULT '',
    summary_upto    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversations_owner ON conversations (owner_id, pinned DESC, updated_at DESC);


CREATE TABLE chat_messages (
    id              text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            text NOT NULL,
    content         text NOT NULL,
    sources         jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_messages_conversation ON chat_messages (conversation_id, created_at);


-- ============================================================
-- 笔记
-- ============================================================

CREATE TABLE notes (
    id          text PRIMARY KEY,
    user_id     text,
    title       text NOT NULL DEFAULT '',
    content_md  text NOT NULL DEFAULT '',
    source_kind text NOT NULL DEFAULT 'manual',
    source_ref  text,
    kb_id       text,
    doc_id      text,
    pinned      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_notes_owner ON notes (user_id, pinned DESC, updated_at DESC);


CREATE TABLE note_tags (
    note_id text NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    tag     text NOT NULL,
    PRIMARY KEY (note_id, tag)
);

CREATE INDEX idx_note_tags_tag ON note_tags (tag);


-- ============================================================
-- Wiki（v24：库形态开关 + 带出处的页面树）
-- ============================================================

CREATE TABLE wiki_pages (
    id           text PRIMARY KEY,
    kb_id        text NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    parent_id    text,
    level        integer NOT NULL DEFAULT 0,
    ord          integer NOT NULL DEFAULT 0,
    slug         text NOT NULL DEFAULT '',
    title        text NOT NULL,
    brief        text NOT NULL DEFAULT '',
    content_md   text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'ready',
    model        text,
    generated_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_wiki_pages_kb ON wiki_pages (kb_id, level, ord);
-- 树形结构按 parent 找子页，给它一条索引
CREATE INDEX idx_wiki_pages_parent ON wiki_pages (parent_id);


CREATE TABLE wiki_page_sources (
    page_id      text NOT NULL REFERENCES wiki_pages (id) ON DELETE CASCADE,
    chunk_id     text NOT NULL,
    document_id  text NOT NULL,
    rank         integer NOT NULL DEFAULT 0,
    heading_path text,
    page         integer,
    PRIMARY KEY (page_id, chunk_id)
);

CREATE INDEX idx_wiki_sources_chunk ON wiki_page_sources (chunk_id);


-- ============================================================
-- 任务队列（租约 + 心跳 + 超时回收）
-- ============================================================

CREATE TABLE tasks (
    id               text PRIMARY KEY,
    kind             text NOT NULL,
    state            text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    document_id      text REFERENCES documents (id) ON DELETE CASCADE,
    part_id          text,
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 5,
    lease_owner      text,
    lease_expires_at timestamptz,
    next_run_at      timestamptz,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_document ON tasks (document_id);
-- 领取查询的部分索引：只覆盖待领的行（取代原 idx_tasks_claim(state, next_run_at)）。
-- 领取写法（取代 SQLite 的 UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING *）：
--   UPDATE tasks SET state='RUNNING', lease_owner=$1, lease_expires_at=now()+$2
--    WHERE id = (SELECT id FROM tasks WHERE state='PENDING' AND next_run_at <= now()
--                ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 1)
--    RETURNING *;
-- SKIP LOCKED 让多个 worker 互不阻塞地各领一行，无需依赖单写者锁。
CREATE INDEX idx_tasks_claim ON tasks (next_run_at) WHERE state = 'PENDING';
-- 超时回收按租约到期扫
CREATE INDEX idx_tasks_lease ON tasks (lease_expires_at) WHERE lease_owner IS NOT NULL;


-- ============================================================
-- 回收站 / 用量 / Webhook / 幂等
-- ============================================================

CREATE TABLE trash (
    id           text PRIMARY KEY,
    document_id  text NOT NULL,
    kind         text NOT NULL,
    storage_path text NOT NULL,
    expires_at   timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_trash_expires ON trash (expires_at);


CREATE TABLE usage_events (
    id                text PRIMARY KEY,
    kind              text NOT NULL,
    provider          text NOT NULL DEFAULT '',
    model_id          text NOT NULL DEFAULT '',
    prompt_tokens     integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    items             integer NOT NULL DEFAULT 0,
    duration_ms       integer NOT NULL DEFAULT 0,
    reported          boolean NOT NULL DEFAULT false,
    source            text NOT NULL DEFAULT 'reported',
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_usage_events_created ON usage_events (created_at);


CREATE TABLE webhooks (
    id      text PRIMARY KEY,
    url     text NOT NULL,
    events  jsonb NOT NULL DEFAULT '[]'::jsonb,
    secret  text,
    enabled boolean NOT NULL DEFAULT true
);


CREATE TABLE idempotency_keys (
    key          text PRIMARY KEY,
    request_hash text NOT NULL,
    response     text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_idempotency_created ON idempotency_keys (created_at);


-- ============================================================
-- schema 版本（v1 基线即本文件；之后的演进在此记版本）
-- ============================================================

CREATE TABLE schema_migrations (
    version     integer PRIMARY KEY,
    description text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, description)
VALUES (1, 'PG 基线：由 SQLite 24 条迁移整合而来，不迁移数据（v1）');
