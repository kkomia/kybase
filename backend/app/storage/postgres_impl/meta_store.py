"""``MetaStore`` 的 PostgreSQL 实现（v0.12 存储切换）。

与 ``sqlite_impl/meta_store.py`` 的对应关系：**逐方法平移**，接口、返回值语义与
异常类型都保持一致。机械差异只有下面几类，逐条说明为什么：

- **占位符**：``?`` → ``%s``。动态标识符（表名/列名）仍然只出现在本文件内部的
  字面量里，没有任何一处把外部值拼进 SQL——值一律走参数绑定。
- **时间戳**：SQLite 把时间存成 ISO 文本，排序/比较依赖"ISO 字面序恰好等于时间序"
  这个巧合；PG 的列是 ``timestamptz``，psycopg 原生适配 ``datetime``，所以
  ``_dump``/``_load`` 退化成恒等助手（保留它们只是为了让方法体与 SQLite 版逐行对照）。
- **JSON**：PG 用 ``jsonb`` 列。写入用 :class:`psycopg.types.json.Jsonb` 包一层，
  读回来 psycopg 已经反序列化成 Python 对象，不再是 ``json.loads(row[...])``。
  **唯一例外**是 ``idempotency_keys.response``：schema 里它是 ``text`` 而不是 jsonb，
  仍要用 ``json.dumps(..., ensure_ascii=False)`` 写文本（见 :func:`_json_text`）。
- **布尔**：PG 是原生 ``boolean``，直接绑 Python ``bool``，不再 ``int(bool)``；
  读回来也是 ``bool``。
- **方言**：``INSERT OR IGNORE`` → ``ON CONFLICT ... DO NOTHING``；
  ``INSERT OR REPLACE`` → ``ON CONFLICT (<冲突键>) DO UPDATE``（冲突键照 schema.sql
  的主键/唯一约束确定）；``LIKE`` → ``ILIKE``（SQLite 的 LIKE 对 ASCII 默认不敏感，
  PG 的不敏感版本是 ILIKE）；``ORDER BY name COLLATE NOCASE`` → ``lower(name)``；
  ``ORDER BY RANDOM()`` → ``random()``；``json_array_length`` → ``jsonb_array_length``。
- **统计/维护**：SQLite 的 ``PRAGMA page_count`` / ``VACUUM`` 没有一一对应物，
  换成 ``pg_database_size`` / ``pg_stat_user_tables`` / ``pg_total_relation_size``
  与 autocommit 连接上的 ``VACUUM (ANALYZE)``（详见各方法注释）。

**任务领取是有意的语义改进**：SQLite 版靠"单写者"保证不双领，PG 改用
``FOR UPDATE SKIP LOCKED``，多个 worker 可以互不阻塞地各领一行（见 ``claim_task``）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 只在注解里用到（``_executemany(conn: Connection, …)`` / ``_json(value) -> Jsonb`` ✓）：
    # `from __future__ import annotations` 之下注解不求值 ✓，所以这两句**不进运行时**、
    # 也就不进客户端的导入闭包 ✓。
    from psycopg import Connection
    from psycopg.types.json import Jsonb

from app.core.exceptions import ConflictError, InvalidRequestError
from app.models.enums import (
    ApiKeyPermission,
    DataSourceKind,
    DocumentStage,
    SharePermission,
    TaskKind,
    TaskState,
    TrashKind,
    UserRole,
)
from app.storage.base import (
    ApiKeyRecord,
    ChatMessageRecord,
    ChunkRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    DataSourceRecord,
    DocumentPartRecord,
    DocumentRecord,
    DocumentStageEventRecord,
    DocumentStatRow,
    FolderRecord,
    IdempotencyRecord,
    ImageRecord,
    KnowledgeBaseRecord,
    MCPServerRecord,
    MetaStore,
    ModelProviderRecord,
    NoteFolderRecord,
    NoteRecord,
    ParseResultRecord,
    RegisteredModelRecord,
    ScheduledTaskRecord,
    SessionEventRecord,
    SessionRecord,
    ShareRecord,
    TaskCounts,
    TaskRecord,
    TaskStatRow,
    TrashRecord,
    UsageEventRecord,
    UserRecord,
    WebhookRecord,
    WikiPageRecord,
    WikiSourceRecord,
    WorkspaceRecord,
)
from app.storage.postgres_impl.connection import Database

#: 惰性拿到的 psycopg（**故意不是模块级导入** ✗）。
_PSYCOPG: Any = None


def _psycopg() -> Any:
    """第一次真用 psycopg 时才导入它（模块级导入会让**客户端运行时**凭空多背一份 ✗）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"连 PostgreSQL"只有
    **服务器**那条链会做 ✓ —— 边车（客户端运行时）只跑循环 + 工具 + 沙箱 ✓，
    它的会话权威与库都在**远端**（走 HTTP ✓，不走 psycopg）。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，psycopg 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _PSYCOPG
    if _PSYCOPG is None:
        import psycopg

        _PSYCOPG = psycopg
    return _PSYCOPG


def _jsonb() -> Any:
    """``psycopg.types.json.Jsonb``（每写一个 jsonb 列都要用它包一层 ✓）。"""
    return _psycopg().types.json.Jsonb


def _now() -> datetime:
    return datetime.now(UTC)


def _dump(moment: datetime | None) -> datetime | None:
    """恒等助手（保留只为与 SQLite 版逐行对照）。

    SQLite 版把 ``datetime`` 转 ISO 文本落库；PG 的目标列是 ``timestamptz``，
    psycopg 直接适配 ``datetime``，绑定与读回都不需要任何转换，也就没有
    "文本字面序 vs 时间序"的隐患。
    """
    return moment


def _load(value: datetime | None) -> datetime | None:
    """恒等助手：``timestamptz`` 读回来就是 ``datetime``，不再 ``fromisoformat``。"""
    return value


def _json(value: object) -> Jsonb:
    """写 ``jsonb`` 列：包成 :class:`Jsonb` 让 psycopg 以 jsonb 类型绑定。

    读侧不需要 ``json.loads``——psycopg 会把 jsonb 直接反序列化成 Python 对象。
    """
    Jsonb = _jsonb()
    return Jsonb(value)


def _json_text(value: object) -> str:
    """写 ``text`` 列里的 JSON（目前只用于 ``idempotency_keys.response``）。

    这一列在 schema 里是 ``text`` 而不是 ``jsonb``，不能走 :func:`_json`；
    保持与 SQLite 版一致的 ``json.dumps(..., ensure_ascii=False)`` 文本形态，
    读侧仍用 ``json.loads``。
    """
    return json.dumps(value, ensure_ascii=False)


def _placeholders(count: int) -> str:
    """生成 ``%s, %s, ...``。

    只由固定占位符拼成，不含任何外部值；真正的值一律走参数绑定。
    """
    return ",".join(["%s"] * count)


#: 读 chunks 时**显式列出**的列。别写 `SELECT *`：那张表还有 `tokens_text`
#: （与正文等长）和生成列 `tokens`（tsvector，通常比正文还大），而 `ChunkRecord`
#: 一个都不用——检索与"补小节"都在热路径上，多传的就是 2–3 倍的字节。
_CHUNK_COLUMNS = (
    "chunk_id, document_id, knowledge_base_id, part_id, ordinal, text,"
    " content_hash, heading_path, page, disabled, questions"
)


def _executemany(conn: Connection, sql: str, rows: Iterable[Sequence[object]]) -> None:
    """批量执行。

    **不能用 ``conn.executemany``**：psycopg3 把 executemany 放在 cursor 上，
    Connection 没有这个方法（SQLite 的 ``sqlite3.Connection`` 有，所以从那边
    平移过来的代码会在运行期才炸）。用 ``with`` 确保 cursor 正常关闭，
    否则连接归还池子时会挂着未关闭的 cursor。
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def _webhook_of(row: dict) -> WebhookRecord:
    """行 → 记录。抽出来是因为有多个地方要构造它（列表/单个/改开关后回读）。"""
    return WebhookRecord(
        id=row["id"],
        url=row["url"],
        events=tuple(row["events"]),
        secret=row["secret"],
        enabled=bool(row["enabled"]),
    )


class PostgresMetaStore(MetaStore):
    """元数据仓储的 PostgreSQL 实现。"""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------ 知识库

    def create_knowledge_base(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        stamp = _now()
        record.created_at = record.created_at or stamp
        record.updated_at = record.updated_at or stamp
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_bases
                    (id, name, description, embedding_model_id, embedding_dim, embedding_base_url,
                     chunk_strategy, chunk_size, chunk_overlap, owner_id, embedding_model_pk,
                     suggested_enabled, suggested_count, suggested_model_pk, suggested_prompt,
                     system_prompt, wiki_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.name,
                    record.description,
                    record.embedding_model_id,
                    record.embedding_dim,
                    record.embedding_base_url,
                    record.chunk_strategy,
                    record.chunk_size,
                    record.chunk_overlap,
                    record.owner_id,
                    record.embedding_model_pk,
                    record.suggested_enabled,
                    record.suggested_count,
                    record.suggested_model_pk,
                    record.suggested_prompt,
                    record.system_prompt,
                    record.wiki_enabled,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_knowledge_base(self, kb_id: str) -> KnowledgeBaseRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM knowledge_bases WHERE id = %s", (kb_id,)).fetchone()
        return self._kb_from_row(row) if row else None

    def list_knowledge_bases(self) -> list[KnowledgeBaseRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM knowledge_bases ORDER BY created_at").fetchall()
        return [self._kb_from_row(row) for row in rows]

    def rename_knowledge_base(self, kb_id: str, name: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET name = %s, updated_at = %s WHERE id = %s",
                (name, _dump(_now()), kb_id),
            )

    def set_knowledge_base_chunking(self, kb_id: str, size: int, overlap: int) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET chunk_size = %s, chunk_overlap = %s, updated_at = %s"
                " WHERE id = %s",
                (size, overlap, _dump(_now()), kb_id),
            )

    def set_knowledge_base_description(self, kb_id: str, description: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET description = %s, updated_at = %s WHERE id = %s",
                (description, _dump(_now()), kb_id),
            )

    def set_knowledge_base_suggested(
        self,
        kb_id: str,
        *,
        enabled: bool,
        count: int,
        model_pk: str | None,
        prompt: str,
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET suggested_enabled = %s, suggested_count = %s,"
                " suggested_model_pk = %s, suggested_prompt = %s, updated_at = %s WHERE id = %s",
                (enabled, count, model_pk, prompt, _dump(_now()), kb_id),
            )

    def set_knowledge_base_wiki(self, kb_id: str, *, enabled: bool) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET wiki_enabled = %s, updated_at = %s WHERE id = %s",
                (enabled, _dump(_now()), kb_id),
            )

    def set_knowledge_base_prompt(self, kb_id: str, *, prompt: str) -> None:
        """只改库级提示词（v0.19）。

        与出题提示词分成两个方法而不是一个 `set_knowledge_base_*`：它们是两件事
        （一个管"怎么答"、一个管"怎么出题"），合成一个 setter 会逼调用方把另一边的
        当前值也读出来再原样传回去——读-改-写，容易把别人的改动盖掉。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE knowledge_bases SET system_prompt = %s, updated_at = %s WHERE id = %s",
                (prompt, _dump(_now()), kb_id),
            )

    def storage_stats(self) -> dict:
        """数据库的物理占用与可回收空间（PG 口径）。

        SQLite 版看的是文件里的页（``PRAGMA page_count`` / ``freelist_count``）；
        PG 没有"文件里离散的页"这回事，等价口径是：

        - ``file_bytes``：``pg_database_size(current_database())``，整库物理大小；
        - ``free_bytes``：按 ``pg_stat_user_tables`` 的活/死元组数，在各表
          ``pg_total_relation_size`` 上按死元组占比估算"VACUUM 能收回"的部分。

        这是估算而非审计级精确值（要精确得开 pgstattuple 扩展），但对用户关心的
        "我删了数据，库怎么没变小"这个问题已经够用——它要的是量级，不是账单。
        """
        with self._db.read() as conn:
            size_row = conn.execute("SELECT pg_database_size(current_database()) AS n").fetchone()
            rows = conn.execute(
                "SELECT pg_total_relation_size(relid) AS size, n_live_tup, n_dead_tup"
                " FROM pg_stat_user_tables"
            ).fetchall()
        free_bytes = 0
        for row in rows:
            live = int(row["n_live_tup"] or 0)
            dead = int(row["n_dead_tup"] or 0)
            if live + dead:
                free_bytes += int(row["size"] or 0) * dead // (live + dead)
        return {
            "file_bytes": int(size_row["n"] or 0) if size_row else 0,
            "free_bytes": free_bytes,
        }

    def vacuum(self) -> None:
        """回收死元组并刷新统计（``VACUUM (ANALYZE)``）。

        **不能在事务里跑**：PG 会直接报 "VACUUM cannot run inside a transaction block"。
        ``read()``/``session()`` 借出的池连接默认走隐式事务，所以这里用 ``db.dsn``
        另开一条 autocommit 连接，跑完即关——对应 SQLite 版"单独 connect() 一次"的做法。
        它会重写相关堆表并可能较慢，所以只应由显式的用户动作触发，
        而不是每次删除后自动跑。
        """
        with _psycopg().connect(self._db.dsn, autocommit=True) as conn:
            conn.execute("VACUUM (ANALYZE)")

    def document_stats_by_kbs(self) -> dict[str, tuple[int, datetime | None]]:
        """每个库的"文档数 + 最近更新时间"，一条 `GROUP BY` 出全部。"""
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT knowledge_base_id AS kb_id,
                       COUNT(*) AS total,
                       MAX(updated_at) AS last_update
                  FROM documents
                 GROUP BY knowledge_base_id
                """
            ).fetchall()
        return {row["kb_id"]: (int(row["total"]), _load(row["last_update"])) for row in rows}

    def update_knowledge_base_embedding(
        self, kb_id: str, *, model_id: str, dim: int, base_url: str | None
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                """
                UPDATE knowledge_bases
                   SET embedding_model_id = %s, embedding_dim = %s, embedding_base_url = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (model_id, dim, base_url, _dump(_now()), kb_id),
            )

    def delete_knowledge_base(self, kb_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM knowledge_bases WHERE id = %s", (kb_id,))

    def count_kb_chunks(self, kb_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_base_id = %s", (kb_id,)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ 文档

    def create_document(self, record: DocumentRecord) -> DocumentRecord:
        stamp = _now()
        record.created_at = record.created_at or stamp
        record.updated_at = record.updated_at or stamp
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO documents
                    (id, knowledge_base_id, name, source_kind, content_hash, stage, size_bytes,
                     mime_type, page_count, is_split, error, uploaded_by, folder_id, disabled,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.knowledge_base_id,
                    record.name,
                    record.source_kind.value,
                    record.content_hash,
                    record.stage.value,
                    record.size_bytes,
                    record.mime_type,
                    record.page_count,
                    record.is_split,
                    record.error,
                    record.uploaded_by,
                    record.folder_id,
                    record.disabled,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
            # **初始阶段也要记一条事件**：文档是带着 `uploaded` 建出来的，它不经过
            # ``update_document_stage``，于是"已接收"在时间线上会是 0 次进入、0 耗时
            # ——恰恰把用户最想知道的**排队等待时间**漏掉了（用例抓到过）。
            conn.execute(
                "INSERT INTO document_stage_events (document_id, stage, error) VALUES (%s, %s, %s)",
                (record.id, record.stage.value, record.error),
            )
        return record

    def get_document(self, document_id: str) -> DocumentRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        return self._document_from_row(row) if row else None

    def get_documents_by_ids(self, document_ids: Sequence[str]) -> dict[str, DocumentRecord]:
        if not document_ids:
            return {}
        # 分片拼 `IN (%s, %s, …)`：PG 的绑定参数上限是 65535，远高于 SQLite 旧版的 999；
        # 但仍按 500 一批切，一是与 SQLite 版保持同一形态（便于对照），
        # 二是避免单条语句参数过多。
        result: dict[str, DocumentRecord] = {}
        unique = list(dict.fromkeys(document_ids))
        with self._db.read() as conn:
            for start in range(0, len(unique), 500):
                batch = unique[start : start + 500]
                placeholders = _placeholders(len(batch))
                rows = conn.execute(
                    # placeholders 只由 "%s" 拼成，没有插值任何外部数据；
                    # 真正的值走参数绑定。S608 在这里是误报。
                    f"SELECT * FROM documents WHERE id IN ({placeholders})",  # noqa: S608
                    batch,
                ).fetchall()
                for row in rows:
                    record = self._document_from_row(row)
                    result[record.id] = record
        return result

    def get_document_by_hash(self, kb_id: str, content_hash: str) -> DocumentRecord | None:
        """架构 §6.3 的文件级去重：同库同 hash 只应有一份。"""
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE knowledge_base_id = %s AND content_hash = %s",
                (kb_id, content_hash),
            ).fetchone()
        return self._document_from_row(row) if row else None

    @staticmethod
    def _document_where(
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
    ) -> tuple[str, list[object]]:
        """文档列表的 ``WHERE`` 片段与参数。

        **列与计数共用同一个构造器**：分页要同时给出"这一页"和"共几篇"，
        两处各写一遍过滤条件，迟早会漂——漂的那天接口会返回"第 2 页 0 篇但总数 100"。
        """
        clauses = ["knowledge_base_id = %s"]
        params: list[object] = [kb_id]
        if root_only:
            clauses.append("folder_id IS NULL")
        elif folder_id is not None:
            clauses.append("folder_id = %s")
            params.append(folder_id)
        if q:
            # 转义 LIKE 的通配符：用户搜 "a_b" 时字面匹配，而不是"a 后跟任意一字符"。
            # ESCAPE 子句让转义字符可判——没有它，反斜杠会被当普通字符，
            # `\%` 反而匹配到真正的 "%"。
            # SQLite 的 LIKE 对 ASCII 默认不敏感；PG 的不敏感版本是 ILIKE。
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("name ILIKE %s ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if stage:
            clauses.append("stage = %s")
            params.append(stage)
        if source_kind:
            clauses.append("source_kind = %s")
            params.append(source_kind)
        return " AND ".join(clauses), params

    def list_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        where, params = self._document_where(
            kb_id,
            folder_id=folder_id,
            root_only=root_only,
            q=q,
            stage=stage,
            source_kind=source_kind,
        )
        # `id` 是**分页的定序键**：同秒上传的文档 `created_at` 可能相同，
        # 只按它排时数据库不保证两次查询顺序一致，LIMIT/OFFSET 就会漏行或重复。
        # 与笔记列表（`ORDER BY n.updated_at DESC, n.id DESC`）同一口径。
        sql = f"SELECT * FROM documents WHERE {where} ORDER BY created_at DESC, id DESC"  # noqa: S608
        if limit is not None:
            # 服务内部的调用点（统计/批处理/生命周期）要全量，limit=None 时不分页。
            sql += " LIMIT %s OFFSET %s"
            params.extend([max(0, limit), max(0, offset)])
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._document_from_row(row) for row in rows]

    def count_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
    ) -> int:
        where, params = self._document_where(
            kb_id,
            folder_id=folder_id,
            root_only=root_only,
            q=q,
            stage=stage,
            source_kind=source_kind,
        )
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM documents WHERE {where}",  # noqa: S608
                params,
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 目录（v13）

    def create_folder(self, record: FolderRecord) -> FolderRecord:
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            try:
                conn.execute(
                    "INSERT INTO kb_folders (id, kb_id, name, created_at) VALUES (%s, %s, %s, %s)",
                    (
                        record.id,
                        record.kb_id,
                        record.name,
                        _dump(record.created_at),
                    ),
                )
            except _psycopg().IntegrityError as exc:
                # 同库重名由 UNIQUE(kb_id, name) 兜底；服务层也会先查一次给出可读文案，
                # 这里是并发/直连场景的最后一道
                raise ConflictError(f"目录已存在：{record.name}") from exc
        return record

    def get_folder(self, folder_id: str) -> FolderRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM kb_folders WHERE id = %s", (folder_id,)).fetchone()
        return self._folder_from_row(row) if row else None

    def list_folders(self, kb_id: str) -> list[FolderRecord]:
        # SQLite 用 `ORDER BY name COLLATE NOCASE`；PG 没有 NOCASE 排序规则，
        # 用 `lower(name)` 达到同样的"按名字、忽略大小写"。
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_folders WHERE kb_id = %s ORDER BY lower(name)",
                (kb_id,),
            ).fetchall()
        return [self._folder_from_row(row) for row in rows]

    def rename_folder(self, folder_id: str, name: str) -> None:
        with self._db.session() as conn:
            try:
                conn.execute("UPDATE kb_folders SET name = %s WHERE id = %s", (name, folder_id))
            except _psycopg().IntegrityError as exc:
                raise ConflictError(f"目录已存在：{name}") from exc

    def delete_folder(self, folder_id: str) -> None:
        """只删目录本身——**成员的去向由服务层决定**（当前策略：非空则拒绝删）。"""
        with self._db.session() as conn:
            conn.execute("DELETE FROM kb_folders WHERE id = %s", (folder_id,))

    def count_documents_by_folders(self, kb_id: str) -> dict[str, int]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT folder_id, COUNT(*) AS n FROM documents"
                " WHERE knowledge_base_id = %s AND folder_id IS NOT NULL"
                " GROUP BY folder_id",
                (kb_id,),
            ).fetchall()
        return {row["folder_id"]: int(row["n"]) for row in rows}

    def set_document_folder(self, document_id: str, folder_id: str | None) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET folder_id = %s WHERE id = %s", (folder_id, document_id)
            )

    @staticmethod
    def _folder_from_row(row: dict) -> FolderRecord:
        return FolderRecord(
            id=row["id"],
            kb_id=row["kb_id"],
            name=row["name"],
            created_at=_load(row["created_at"]),
        )

    # ------------------------------------------------------------------ 笔记（v20）

    @staticmethod
    def _note_from_row(row: dict, tags: Sequence[str] = ()) -> NoteRecord:
        return NoteRecord(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            content_md=row["content_md"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            kb_id=row["kb_id"],
            doc_id=row["doc_id"],
            folder_id=row["folder_id"],
            pinned=bool(row["pinned"]),
            tags=list(tags),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _note_filters(
        user_id: str | None,
        query: str | None,
        tag: str | None,
        folder_id: str | None = None,
        unfiled: bool = False,
    ) -> tuple[str, list]:
        """拼 WHERE 子句。

        ``user_id=None`` 表示**不过滤归属**（管理员/API Key 通道要看全部，
        与 ``list_conversations`` 同口径）；成员传自己的 id，只看自己的。
        SQLite 里用 ``user_id IS ?`` 同时覆盖 NULL 与具体值；PG 的 ``IS`` 只接受
        NULL/TRUE/FALSE/UNKNOWN，不能与任意值参数化，所以按分支写成
        "无筛选 / ``= %s``"，语义与 SQLite 版一致。

        ``folder_id`` / ``unfiled`` 与文档列表的 ``folder_id`` / ``root_only`` 同一套
        写法：两者是同一个轴上的两种取法，**由调用方保证不同时给**
        （``unfiled`` 优先，与文档那边一致：``root_only`` 先判）。

        搜索用 ``ILIKE`` 子串匹配而不是全文索引：笔记是个人规模的数据，
        子串匹配对中文天然可用（不需要分词），也没有"改了正文忘了同步索引"这类静默故障。
        多个词之间取 AND，命中更准。``%``/``_`` 会被转义，避免用户输入的它们变成通配符。
        """
        where: list[str] = []
        params: list = []
        if user_id is not None:
            where.append("n.user_id = %s")
            params.append(user_id)
        if unfiled:
            where.append("n.folder_id IS NULL")
        elif folder_id is not None:
            where.append("n.folder_id = %s")
            params.append(folder_id)
        for term in (query or "").split():
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(n.title ILIKE %s ESCAPE '\\' OR n.content_md ILIKE %s ESCAPE '\\')")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        if tag:
            where.append("n.id IN (SELECT note_id FROM note_tags WHERE tag = %s)")
            params.append(tag)
        return (" AND ".join(where) or "1 = 1"), params

    def _tags_of(self, conn: Connection, note_ids: Sequence[str]) -> dict[str, list[str]]:
        """一次取回多条笔记的标签（逐条查就是 N+1）。"""
        if not note_ids:
            return {}
        placeholders = _placeholders(len(note_ids))
        rows = conn.execute(
            # placeholders 只由 "%s" 拼成，值全部走参数绑定；表名是字面量
            f"SELECT note_id, tag FROM note_tags WHERE note_id IN ({placeholders})"  # noqa: S608
            " ORDER BY tag",
            list(note_ids),
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["note_id"], []).append(row["tag"])
        return grouped

    def create_note(self, record: NoteRecord) -> NoteRecord:
        moment = _now()
        record.created_at = record.created_at or moment
        record.updated_at = record.updated_at or moment
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO notes (id, user_id, title, content_md, source_kind, source_ref,"
                " kb_id, doc_id, folder_id, pinned, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.user_id,
                    record.title,
                    record.content_md,
                    record.source_kind,
                    record.source_ref,
                    record.kb_id,
                    record.doc_id,
                    record.folder_id,
                    record.pinned,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
            if record.tags:
                _executemany(
                    conn,
                    "INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)"
                    " ON CONFLICT (note_id, tag) DO NOTHING",
                    [(record.id, tag) for tag in record.tags],
                )
        return record

    def get_note(self, note_id: str) -> NoteRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = %s", (note_id,)).fetchone()
            if row is None:
                return None
            tags = self._tags_of(conn, [note_id]).get(note_id, [])
        return self._note_from_row(row, tags)

    def list_notes(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[NoteRecord]:
        where, params = self._note_filters(
            user_id, query, tag, folder_id=folder_id, unfiled=unfiled
        )
        sql = (
            # where 由本文件内部拼装：列名是字面量，值一律走 %s 绑定
            f"SELECT n.* FROM notes n WHERE {where}"  # noqa: S608
            " ORDER BY n.pinned DESC, n.updated_at DESC, n.id DESC LIMIT %s OFFSET %s"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
            tags = self._tags_of(conn, [row["id"] for row in rows])
        return [self._note_from_row(row, tags.get(row["id"], [])) for row in rows]

    def count_notes(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
    ) -> int:
        where, params = self._note_filters(
            user_id, query, tag, folder_id=folder_id, unfiled=unfiled
        )
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM notes n WHERE {where}",  # noqa: S608
                params,
            ).fetchone()
        return int(row["n"])

    def update_note(
        self,
        note_id: str,
        *,
        title: str,
        content_md: str,
        pinned: bool,
        updated_at: datetime,
        tags: Sequence[str] | None = None,
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE notes SET title = %s, content_md = %s, pinned = %s, updated_at = %s"
                " WHERE id = %s",
                (title, content_md, pinned, _dump(updated_at), note_id),
            )
            if tags is not None:
                # 全量替换：标签是随笔记一起编辑的短列表，diff 没必要
                conn.execute("DELETE FROM note_tags WHERE note_id = %s", (note_id,))
                _executemany(
                    conn,
                    "INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)"
                    " ON CONFLICT (note_id, tag) DO NOTHING",
                    [(note_id, tag) for tag in tags],
                )

    def delete_note(self, note_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM note_tags WHERE note_id = %s", (note_id,))
            conn.execute("DELETE FROM notes WHERE id = %s", (note_id,))

    def attach_note_document(self, note_id: str, *, kb_id: str, doc_id: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE notes SET kb_id = %s, doc_id = %s WHERE id = %s",
                (kb_id, doc_id, note_id),
            )

    def list_note_tags(self, *, user_id: str | None) -> list[tuple[str, int]]:
        where = "" if user_id is None else "WHERE n.user_id = %s"
        params: tuple = () if user_id is None else (user_id,)
        # where 只有两种取值（空串 / 一个字面量条件），用户值走绑定
        sql = (
            "SELECT t.tag AS tag, COUNT(*) AS n FROM note_tags t"  # noqa: S608
            f" JOIN notes n ON n.id = t.note_id {where}"
            " GROUP BY t.tag ORDER BY n DESC, t.tag"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row["tag"], int(row["n"])) for row in rows]

    # ------------------------------------------------------------------ 笔记文件夹（v14）

    @staticmethod
    def _note_folder_from_row(row: dict) -> NoteFolderRecord:
        return NoteFolderRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            parent_id=row["parent_id"],
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    def create_note_folder(self, record: NoteFolderRecord) -> NoteFolderRecord:
        moment = _now()
        record.created_at = record.created_at or moment
        record.updated_at = record.updated_at or moment
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO note_folders (id, user_id, name, parent_id, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.user_id,
                    record.name,
                    record.parent_id,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_note_folder(self, folder_id: str) -> NoteFolderRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM note_folders WHERE id = %s", (folder_id,)
            ).fetchone()
        return self._note_folder_from_row(row) if row else None

    def list_note_folders(self, *, user_id: str | None) -> list[NoteFolderRecord]:
        # 与知识库目录同一条口径：按名字排（PG 没有 NOCASE，用 lower(name)，
        # 服务这条查询的索引在迁移 v14 里）。
        where = "" if user_id is None else "WHERE user_id = %s"
        params: tuple = () if user_id is None else (user_id,)
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM note_folders {where} ORDER BY lower(name), id",  # noqa: S608
                params,
            ).fetchall()
        return [self._note_folder_from_row(row) for row in rows]

    def rename_note_folder(self, folder_id: str, name: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE note_folders SET name = %s, updated_at = %s WHERE id = %s",
                (name, _dump(_now()), folder_id),
            )

    def set_note_folder_parent(self, folder_id: str, parent_id: str | None) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE note_folders SET parent_id = %s, updated_at = %s WHERE id = %s",
                (parent_id, _dump(_now()), folder_id),
            )

    def delete_note_folder(self, folder_id: str) -> None:
        """**一条 DELETE 就是全部**：子文件夹由 ``parent_id`` 的外键级联删掉，
        里面的笔记由 ``notes.folder_id`` 的 ``ON DELETE SET NULL`` 回到未归档。
        在 Python 里手写"先查子树再逐个删"只会把这两条已经声明在表上的语义
        复制成第二处会漂的定义。
        """
        with self._db.session() as conn:
            conn.execute("DELETE FROM note_folders WHERE id = %s", (folder_id,))

    def count_notes_by_folder(self, *, user_id: str | None) -> dict[str | None, int]:
        """``{文件夹 id / None（未归档）: 笔记数}``，一次聚合取全（含未归档那一行）。"""
        where = "" if user_id is None else "WHERE user_id = %s"
        params: tuple = () if user_id is None else (user_id,)
        with self._db.read() as conn:
            rows = conn.execute(
                # where 只有两种取值（空串 / 一个字面量条件），用户值走绑定
                f"SELECT folder_id, COUNT(*) AS n FROM notes {where} GROUP BY folder_id",  # noqa: S608
                params,
            ).fetchall()
        return {row["folder_id"]: int(row["n"]) for row in rows}

    def set_note_folder(self, note_id: str, folder_id: str | None) -> None:
        """**不动 `updated_at`**：移动是"归了个档"，不是改了这条笔记。
        顺手把它顶到列表最前面（列表按 updated_at 排）等于把用户刚翻到的地方打乱。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE notes SET folder_id = %s WHERE id = %s", (folder_id, note_id)
            )

    def update_document_stage(
        self, document_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None:
        """改文档阶段**并追加一条阶段事件**（v24）。

        **事件写在这里而不是各个调用点**：这是全仓唯一改 ``documents.stage`` 的地方
        （摄入的 `_advance`、取消、重新摄入的复位都经过它）。散到调用点去写，
        迟早会漏一处，时间线就会缺一段——而"缺一段"在界面上看起来像"这一步没耗时"，
        比报错还难查。

        两件事同一个事务：阶段与它的时间戳必须一起落地，否则会出现
        "阶段是切分、事件却还停在解析"的错位。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET stage = %s, error = %s, updated_at = %s WHERE id = %s",
                (stage.value, error, _dump(_now()), document_id),
            )
            conn.execute(
                "INSERT INTO document_stage_events (document_id, stage, error) VALUES (%s, %s, %s)",
                (document_id, stage.value, error),
            )

    def replace_document_content(
        self,
        document_id: str,
        *,
        content_hash: str,
        name: str,
        size_bytes: int,
        mime_type: str | None,
    ) -> None:
        """原地替换文档的内容元数据（v0.12）。

        ``page_count`` 一并置 NULL：新内容可能页数不同，留着旧值会显示一个错的页数
        （"没测出来"渲染成"—"，比一个过期的数字诚实）。

        **刻意不碰 ``stage``**：全仓只有 ``update_document_stage`` 改那一列
        （它同时追加时间线事件，见那边的说明）。这里改内容、那里改阶段，
        调用方按顺序各调一次——阶段事件因此天然完整，不会出现"内容换了但时间线说没换"。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET content_hash = %s, name = %s, size_bytes = %s,"
                " mime_type = %s, page_count = NULL, updated_at = %s WHERE id = %s",
                (content_hash, name, size_bytes, mime_type, _dump(_now()), document_id),
            )

    def list_documents_without_summary(self, *, limit: int) -> list[DocumentRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM documents
                 WHERE stage = %s AND summary = ''
                 ORDER BY created_at, id
                 LIMIT %s
                """,
                (DocumentStage.INDEXED.value, max(1, int(limit))),
            ).fetchall()
        return [self._document_from_row(row) for row in rows]

    def update_document_summary(self, document_id: str, summary: str) -> None:
        # **只改 summary 与 updated_at**：不动阶段、不动 error——摘要是内容层的补充，
        # 与流水线状态无关。写成"顺手也改 stage"会让时间线多出一条假事件。
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET summary = %s, updated_at = %s WHERE id = %s",
                (summary, _dump(_now()), document_id),
            )

    def list_document_stage_events(self, document_id: str) -> list[DocumentStageEventRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT id, document_id, stage, entered_at, error FROM document_stage_events"
                " WHERE document_id = %s ORDER BY id",
                (document_id,),
            ).fetchall()
        return [
            DocumentStageEventRecord(
                id=row["id"],
                document_id=row["document_id"],
                stage=row["stage"],
                entered_at=row["entered_at"],
                error=row["error"],
            )
            for row in rows
        ]

    def list_document_stage_events_for_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, list[DocumentStageEventRecord]]:
        wanted = list(dict.fromkeys(document_ids))
        if not wanted:
            return {}
        sql = (
            "SELECT id, document_id, stage, entered_at, error FROM document_stage_events"  # noqa: S608
            f" WHERE document_id IN ({_placeholders(len(wanted))}) ORDER BY id"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, tuple(wanted)).fetchall()
        grouped: dict[str, list[DocumentStageEventRecord]] = {}
        for row in rows:
            grouped.setdefault(str(row["document_id"]), []).append(
                DocumentStageEventRecord(
                    id=row["id"],
                    document_id=row["document_id"],
                    stage=row["stage"],
                    entered_at=row["entered_at"],
                    error=row["error"],
                )
            )
        return grouped

    def active_tasks_by_documents(self, document_ids: Sequence[str]) -> dict[str, TaskRecord]:
        wanted = list(dict.fromkeys(document_ids))
        if not wanted:
            return {}
        sql = (
            "SELECT * FROM tasks"  # noqa: S608
            f" WHERE document_id IN ({_placeholders(len(wanted))}) AND state IN (%s, %s)"
        )
        with self._db.read() as conn:
            rows = conn.execute(
                sql,
                (*wanted, TaskState.PENDING.value, TaskState.RUNNING.value),
            ).fetchall()
        # 一个文档只会有一条未结束的任务；真出现两条（历史脏数据）时留**先入队**的那条，
        # 它才是当前真正的阻塞点
        result: dict[str, TaskRecord] = {}
        for row in rows:
            record = self._task_from_row(row)
            if record.document_id and record.document_id not in result:
                result[record.document_id] = record
        return result

    def delete_document(self, document_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    def rename_document(self, document_id: str, name: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET name = %s, updated_at = %s WHERE id = %s",
                (name, _dump(_now()), document_id),
            )

    def set_document_disabled(self, document_id: str, disabled: bool) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET disabled = %s, updated_at = %s WHERE id = %s",
                (disabled, _dump(_now()), document_id),
            )

    def any_disabled_documents(self, kb_ids: Sequence[str]) -> bool:
        if not kb_ids:
            return False
        placeholders = _placeholders(len(kb_ids))
        with self._db.read() as conn:
            row = conn.execute(
                # placeholders 只由 "%s" 拼成，值走参数绑定（S608 误报）
                f"SELECT EXISTS("  # noqa: S608
                f"SELECT 1 FROM documents WHERE disabled = true "
                f"AND knowledge_base_id IN ({placeholders})"
                f") AS has_disabled",
                tuple(kb_ids),
            ).fetchone()
        return bool(row["has_disabled"])

    # ------------------------------------------------------------------ 子文件

    def create_document_parts(self, records: Sequence[DocumentPartRecord]) -> None:
        if not records:
            return
        with self._db.session() as conn:
            # SQLite 的 `INSERT OR REPLACE` 在 PG 没有一一对应：一条 ON CONFLICT
            # 只能指定一个冲突目标。这里按主键 (id) 冲突做 upsert——重跑时子文件 id
            # 是同一批（由 document_id + 段序号派生），主键与 UNIQUE(document_id,
            # part_index) 会同时命中，所以正常重跑路径行为一致。
            # 若调用方用**新 id** 复用同一个 (document_id, part_index)，
            # PG 会抛唯一约束冲突而不是静默替换（见实现报告里的差异清单）。
            _executemany(
                conn,
                """
                INSERT INTO document_parts
                    (id, document_id, part_index, page_start, page_end, stage, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    document_id = excluded.document_id,
                    part_index = excluded.part_index,
                    page_start = excluded.page_start,
                    page_end = excluded.page_end,
                    stage = excluded.stage,
                    error = excluded.error
                """,
                [
                    (
                        part.id,
                        part.document_id,
                        part.part_index,
                        part.page_start,
                        part.page_end,
                        part.stage.value,
                        part.error,
                    )
                    for part in records
                ],
            )

    def list_document_parts(self, document_id: str) -> list[DocumentPartRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM document_parts WHERE document_id = %s ORDER BY part_index",
                (document_id,),
            ).fetchall()
        return [
            DocumentPartRecord(
                id=row["id"],
                document_id=row["document_id"],
                part_index=row["part_index"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                stage=DocumentStage(row["stage"]),
                error=row["error"],
            )
            for row in rows
        ]

    def update_part_stage(
        self, part_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE document_parts SET stage = %s, error = %s WHERE id = %s",
                (stage.value, error, part_id),
            )

    def delete_document_parts(self, document_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM document_parts WHERE document_id = %s", (document_id,))

    def update_document_page_count(self, document_id: str, page_count: int | None) -> None:
        """把解析阶段得知的页数落库。

        **单独一个方法而不是塞进 ``update_document_stage``**：页数是**解析产物**，
        不是一个阶段推进事件。合成一个方法的话，每个调 ``update_document_stage``
        的地方都要想"我这次要不要带页数"，而那 8 处里的 7 处根本没这个信息。
        """
        if page_count is None or page_count <= 0:
            # 0 页不是"文档有 0 页"，是"没测出来"。写 0 会让界面显示"0 页"，
            # 而 NULL 会被界面渲染成"—"，后者才是诚实的。
            return
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET page_count = %s, updated_at = %s WHERE id = %s",
                (page_count, _dump(_now()), document_id),
            )

    def mark_document_split(self, document_id: str, is_split: bool = True) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET is_split = %s, updated_at = %s WHERE id = %s",
                (is_split, _dump(_now()), document_id),
            )

    # ------------------------------------------------------------------ chunk

    def replace_chunks(self, document_id: str, chunks: Sequence[ChunkRecord]) -> None:
        """整体替换某文档的 chunk（同一事务内先删后插），供增量更新与重跑使用。"""
        with self._db.session() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            if not chunks:
                return
            _executemany(
                conn,
                """
                INSERT INTO chunks
                    (chunk_id, document_id, knowledge_base_id, part_id, ordinal, text,
                     content_hash, heading_path, page, questions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.knowledge_base_id,
                        chunk.part_id,
                        chunk.ordinal,
                        chunk.text,
                        chunk.content_hash,
                        chunk.heading_path,
                        chunk.page,
                        _json(list(chunk.questions)),
                    )
                    for chunk in chunks
                ],
            )
            _executemany(
                conn,
                "INSERT INTO chunk_images (chunk_id, image_id) VALUES (%s, %s)",
                [(chunk.chunk_id, image_id) for chunk in chunks for image_id in chunk.image_ids],
            )

    def iter_chunks(self, document_id: str, *, limit: int | None = None) -> Iterable[ChunkRecord]:
        # LIMIT 直接下推到 SQL：预览只要前几块，没必要把整份正文读出来再切
        sql = f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE document_id = %s ORDER BY ordinal"  # noqa: S608
        params: list[object] = [document_id]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(max(0, limit))
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
            images = self._images_by_chunk(conn, [row["chunk_id"] for row in rows])
        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                part_id=row["part_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                content_hash=row["content_hash"],
                heading_path=row["heading_path"],
                page=row["page"],
                image_ids=tuple(images.get(row["chunk_id"], ())),
                disabled=bool(row["disabled"]),
                questions=tuple(row["questions"]),
            )
            for row in rows
        ]

    def list_chunks_by_heading(self, document_id: str, heading_path: str) -> list[ChunkRecord]:
        """同一小节的切块（见 base.py 的同名方法：为"补全整段小节"省掉读整篇）。"""
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks"  # noqa: S608
                " WHERE document_id = %s AND heading_path = %s ORDER BY ordinal",
                (document_id, heading_path),
            ).fetchall()
            images = self._images_by_chunk(conn, [row["chunk_id"] for row in rows])
        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                part_id=row["part_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                content_hash=row["content_hash"],
                heading_path=row["heading_path"],
                page=row["page"],
                image_ids=tuple(images.get(row["chunk_id"], ())),
                disabled=bool(row["disabled"]),
                questions=tuple(row["questions"]),
            )
            for row in rows
        ]

    def sample_chunks(
        self, kb_ids: Sequence[str], *, limit: int, with_questions_only: bool = False
    ) -> list[ChunkRecord]:
        """从若干知识库随机抽切块（跳过人工禁用的）。

        两个用途：入库时出题的语料采样，以及**读端**（对话页空状态）随机抽几块、
        把它们已存的问题取出来展示（v23）。

        ``with_questions_only=True`` 是读端必须加的：库里的块**绝大部分没有题**
        （只有开过出题或事后补过的文档才有），在全库随机抽只会一次次抽到空块，
        于是"库里明明有上百条问题，空状态却什么都没有"（v24 实测：10 次调用有 5 次为空）。
        加上这个过滤，抽样的分母就是"有题的块"，有几条就能稳定看到几条。
        """
        if not kb_ids or limit <= 0:
            return []
        placeholders = _placeholders(len(kb_ids))
        # questions 的写入侧只经 _json（replace_chunks / update_chunk），永远是合法 JSON 数组；
        # jsonb 判空用 '[]'::jsonb 比较。
        only_questions = " AND questions <> '[]'::jsonb" if with_questions_only else ""
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks "  # noqa: S608
                f"WHERE knowledge_base_id IN ({placeholders}) AND disabled = false"
                f"{only_questions}"
                " ORDER BY random() LIMIT %s",
                [*kb_ids, limit],
            ).fetchall()
        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                part_id=row["part_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                content_hash=row["content_hash"],
                heading_path=row["heading_path"],
                page=row["page"],
                disabled=bool(row["disabled"]),
                questions=tuple(row["questions"]),
            )
            for row in rows
        ]

    def get_chunks(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]:
        """按 ID 批量取回。检索时向量只给得出 chunk_id，正文与图片锚点得回表取；
        逐个查会退化成 N 次查询，所以接口层就要求批量。"""
        if not chunk_ids:
            return []
        placeholders = _placeholders(len(chunk_ids))

        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks "  # noqa: S608
                f"WHERE chunk_id IN ({placeholders})",
                list(chunk_ids),
            ).fetchall()
            images = self._images_by_chunk(conn, [row["chunk_id"] for row in rows])
        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                part_id=row["part_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                content_hash=row["content_hash"],
                heading_path=row["heading_path"],
                page=row["page"],
                image_ids=tuple(images.get(row["chunk_id"], ())),
                disabled=bool(row["disabled"]),
                questions=tuple(row["questions"]),
            )
            for row in rows
        ]

    def count_chunks(self, document_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ 切块人工干预

    def update_chunk(self, record: ChunkRecord) -> None:
        """就地更新一个块。

        **不改 chunk_id**：它是向量表与全文索引的主键，改了就得三处联动重建；
        而"用户改了一段文字"这件事本身不改变这块在文档里的身份。

        ``ordinal`` 要一起更新：删块之后的重排全靠它。原先漏了这一列，
        于是"重排"变成空操作——删掉中间一块后界面仍显示第 1、2、4 块，
        用户以为丢了数据（测试抓到）。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE chunks SET text = %s, content_hash = %s, heading_path = %s,"
                " page = %s, disabled = %s, ordinal = %s, questions = %s WHERE chunk_id = %s",
                (
                    record.text,
                    record.content_hash,
                    record.heading_path,
                    record.page,
                    record.disabled,
                    record.ordinal,
                    _json(list(record.questions)),
                    record.chunk_id,
                ),
            )

    def set_chunk_disabled(self, chunk_id: str, *, disabled: bool) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE chunks SET disabled = %s WHERE chunk_id = %s",
                (disabled, chunk_id),
            )

    def delete_chunk(self, chunk_id: str) -> None:
        """删除单个块及其图片关联。

        只删元数据：**全文索引与向量必须由调用方一并清理**——
        它们分属不同仓储（tsvector 生成列、向量分区），在这里跨仓储删除会破坏分层纪律
        （services/ 之下不该出现跨存储的编排）。所以这个方法的契约是
        "调用方负责连带清理"，由 ``services/chunk.py`` 统一编排。
        """
        with self._db.session() as conn:
            conn.execute("DELETE FROM chunk_images WHERE chunk_id = %s", (chunk_id,))
            conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))

    def count_chunks_by_documents(self, document_ids: Sequence[str]) -> dict[str, int]:
        """一次查完多个文档的切块数。

        文档列表页要显示每个文档"有多少块"，逐个 ``count_chunks`` 就是 N+1：
        1000 个文档 = 1000 次查询。这里用一条 ``GROUP BY`` 拿全，缺的补 0。
        """
        wanted = list(dict.fromkeys(document_ids))  # 去重但保序
        if not wanted:
            return {}
        placeholders = _placeholders(len(wanted))
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT document_id, COUNT(*) AS n FROM chunks "  # noqa: S608
                f"WHERE document_id IN ({placeholders}) GROUP BY document_id",
                tuple(wanted),
            ).fetchall()
        counted = {str(row["document_id"]): int(row["n"]) for row in rows}
        return {document_id: counted.get(document_id, 0) for document_id in wanted}

    def list_document_stats(self, kb_ids: Sequence[str] | None = None) -> list[DocumentStatRow]:
        """统计投影（见 base.py 的同名方法：驾驶舱用，一条查询取代 K+1 次）。"""
        params: tuple[object, ...] = ()
        where = ""
        if kb_ids is not None:
            wanted = list(dict.fromkeys(kb_ids))
            if not wanted:
                return []
            where = f"WHERE d.knowledge_base_id IN ({_placeholders(len(wanted))})"
            params = tuple(wanted)
        with self._db.read() as conn:
            # `GROUP BY d.id` 只按主键分组：其余 d.* 列由主键函数依赖决定，
            # PG 允许这样写（不用把每一列都列进 GROUP BY）。
            rows = conn.execute(
                "SELECT d.id, d.knowledge_base_id, d.name, d.stage, d.source_kind,"  # noqa: S608
                " d.size_bytes, d.created_at, d.updated_at, COUNT(c.chunk_id) AS chunks"
                " FROM documents d"
                " LEFT JOIN chunks c ON c.document_id = d.id"
                f" {where}"
                " GROUP BY d.id"
                # 与 list_documents 同序：按插入序遍历聚合出来的结果与改动前逐字一致
                " ORDER BY d.created_at DESC, d.id DESC",
                params,
            ).fetchall()
        return [
            DocumentStatRow(
                id=str(row["id"]),
                knowledge_base_id=str(row["knowledge_base_id"]),
                name=str(row["name"]),
                stage=str(row["stage"]),
                source_kind=str(row["source_kind"]),
                size_bytes=int(row["size_bytes"] or 0),
                created_at=_load(row["created_at"]),
                updated_at=_load(row["updated_at"]),
                chunks=int(row["chunks"]),
            )
            for row in rows
        ]

    def list_task_stats(self, document_ids: Sequence[str] | None = None) -> list[TaskStatRow]:
        """统计投影（见 base.py 的同名方法）。"""
        params: tuple[object, ...] = ()
        where = ""
        if document_ids is not None:
            wanted = list(dict.fromkeys(document_ids))
            if not wanted:
                return []
            where = f"WHERE document_id IN ({_placeholders(len(wanted))})"
            params = tuple(wanted)
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT state, document_id, created_at, updated_at FROM tasks"  # noqa: S608
                f" {where}",
                params,
            ).fetchall()
        return [
            TaskStatRow(
                state=str(row["state"]),
                document_id=None if row["document_id"] is None else str(row["document_id"]),
                created_at=_load(row["created_at"]),
                updated_at=_load(row["updated_at"]),
            )
            for row in rows
        ]

    def question_stats_by_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, tuple[int, int]]:
        """批量查每个文档的 ``(有题块数, 问题总数)``。

        ``questions`` 这一列的写入侧只经 ``_json``（见 replace_chunks / update_chunk），
        所以永远是合法 JSON 数组——不会因为一行脏数据把整个列表接口打挂。
        ``json_array_length`` 在 PG 里对应 ``jsonb_array_length``。
        """
        wanted = list(dict.fromkeys(document_ids))  # 去重但保序
        if not wanted:
            return {}
        placeholders = _placeholders(len(wanted))
        sql = (
            "SELECT document_id,"  # noqa: S608
            " SUM(CASE WHEN questions <> '[]'::jsonb THEN 1 ELSE 0 END) AS with_questions,"
            " SUM(jsonb_array_length(questions)) AS total"
            f" FROM chunks WHERE document_id IN ({placeholders}) GROUP BY document_id"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, tuple(wanted)).fetchall()
        stats = {
            str(row["document_id"]): (int(row["with_questions"] or 0), int(row["total"] or 0))
            for row in rows
        }
        return {document_id: stats.get(document_id, (0, 0)) for document_id in wanted}

    def active_question_documents(self, document_ids: Sequence[str]) -> set[str]:
        wanted = list(dict.fromkeys(document_ids))
        if not wanted:
            return set()
        placeholders = _placeholders(len(wanted))
        sql = (
            "SELECT DISTINCT document_id FROM tasks"  # noqa: S608
            " WHERE kind = %s AND state IN (%s, %s)"
            f" AND document_id IN ({placeholders})"
        )
        with self._db.read() as conn:
            rows = conn.execute(
                sql,
                (
                    TaskKind.QUESTIONS.value,
                    TaskState.PENDING.value,
                    TaskState.RUNNING.value,
                    *wanted,
                ),
            ).fetchall()
        return {str(row["document_id"]) for row in rows if row["document_id"]}

    # ------------------------------------------------------------------ Wiki（v24）

    @staticmethod
    def _wiki_page_from_row(row: dict) -> WikiPageRecord:
        return WikiPageRecord(
            id=row["id"],
            kb_id=row["kb_id"],
            parent_id=row["parent_id"],
            level=row["level"],
            ord=row["ord"],
            slug=row["slug"],
            title=row["title"],
            brief=row["brief"],
            content_md=row["content_md"],
            status=row["status"],
            model=row["model"],
            generated_at=_load(row["generated_at"]),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    def replace_wiki_pages(
        self,
        kb_id: str,
        pages: Sequence[WikiPageRecord],
        sources: Sequence[WikiSourceRecord],
    ) -> None:
        with self._db.session() as conn:
            # 先删出处再删页面：PG 的外键级联虽然始终生效，但显式删除让意图
            # 写在代码里而不是藏在 schema 里（也少依赖一次级联行为）。
            conn.execute(
                "DELETE FROM wiki_page_sources WHERE page_id IN"
                " (SELECT id FROM wiki_pages WHERE kb_id = %s)",
                (kb_id,),
            )
            conn.execute("DELETE FROM wiki_pages WHERE kb_id = %s", (kb_id,))
            if not pages:
                return
            stamp = _dump(_now())
            _executemany(
                conn,
                "INSERT INTO wiki_pages"
                " (id, kb_id, parent_id, level, ord, slug, title, brief, content_md,"
                "  status, model, generated_at, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        page.id,
                        page.kb_id,
                        page.parent_id,
                        page.level,
                        page.ord,
                        page.slug,
                        page.title,
                        page.brief,
                        page.content_md,
                        page.status,
                        page.model,
                        _dump(page.generated_at) if page.generated_at else None,
                        _dump(page.created_at) if page.created_at else stamp,
                        _dump(page.updated_at) if page.updated_at else stamp,
                    )
                    for page in pages
                ],
            )
            if sources:
                _executemany(
                    conn,
                    "INSERT INTO wiki_page_sources"
                    " (page_id, chunk_id, document_id, rank, heading_path, page)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (
                            item.page_id,
                            item.chunk_id,
                            item.document_id,
                            item.index,
                            item.heading_path,
                            item.page,
                        )
                        for item in sources
                    ],
                )

    def list_wiki_pages(self, kb_id: str) -> list[WikiPageRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = %s ORDER BY level, ord, title",
                (kb_id,),
            ).fetchall()
        return [self._wiki_page_from_row(row) for row in rows]

    def get_wiki_page(self, page_id: str) -> WikiPageRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM wiki_pages WHERE id = %s", (page_id,)).fetchone()
        return self._wiki_page_from_row(row) if row else None

    def list_wiki_sources(self, page_id: str) -> list[WikiSourceRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM wiki_page_sources WHERE page_id = %s ORDER BY rank",
                (page_id,),
            ).fetchall()
        return [
            WikiSourceRecord(
                page_id=row["page_id"],
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                rank=row["rank"],
                index=row["rank"],
                heading_path=row["heading_path"],
                page=row["page"],
            )
            for row in rows
        ]

    def wiki_stats(self, kb_id: str) -> tuple[int, datetime | None]:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(generated_at) AS latest"
                " FROM wiki_pages WHERE kb_id = %s",
                (kb_id,),
            ).fetchone()
        if row is None:
            return 0, None
        return int(row["n"]), _load(row["latest"])

    # ------------------------------------------------------------------ 图片

    def add_images(self, records: Sequence[ImageRecord]) -> None:
        if not records:
            return
        with self._db.session() as conn:
            _executemany(
                conn,
                """
                INSERT INTO images (image_id, document_id, storage_path, page, bbox, caption)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (image_id) DO UPDATE SET
                    storage_path = excluded.storage_path,
                    page = excluded.page,
                    bbox = excluded.bbox,
                    caption = excluded.caption
                """,
                [
                    (
                        image.image_id,
                        image.document_id,
                        image.storage_path,
                        image.page,
                        image.bbox,
                        image.caption,
                    )
                    for image in records
                ],
            )

    def list_images(self, document_id: str) -> list[ImageRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM images WHERE document_id = %s ORDER BY page, image_id",
                (document_id,),
            ).fetchall()
        return [
            ImageRecord(
                image_id=row["image_id"],
                document_id=row["document_id"],
                storage_path=row["storage_path"],
                page=row["page"],
                bbox=row["bbox"],
                caption=row["caption"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 解析产物

    def save_parse_result(self, record: ParseResultRecord) -> None:
        """按 (document_id, part_id) 幂等写入：重跑解析覆盖旧产物而不留垃圾。"""
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO parse_results
                    (document_id, part_id, parser_name, markdown_path, probe_meta, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, part_id) DO UPDATE SET
                    parser_name = excluded.parser_name,
                    markdown_path = excluded.markdown_path,
                    probe_meta = excluded.probe_meta,
                    created_at = excluded.created_at
                """,
                (
                    record.document_id,
                    record.part_id or "",
                    record.parser_name,
                    record.markdown_path,
                    _json(record.probe_meta),
                    _dump(record.created_at),
                ),
            )

    def get_parse_result(self, document_id: str) -> ParseResultRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM parse_results WHERE document_id = %s ORDER BY part_id LIMIT 1",
                (document_id,),
            ).fetchone()
        if not row:
            return None
        return ParseResultRecord(
            document_id=row["document_id"],
            part_id=row["part_id"] or None,
            parser_name=row["parser_name"],
            markdown_path=row["markdown_path"],
            probe_meta=row["probe_meta"],
            created_at=_load(row["created_at"]),
        )

    def parser_page_usage(self, parser_name: str, *, since: datetime) -> tuple[int, int]:
        # 页数取下界 0：`page_count` 为 NULL（探测没测出页数）时整篇按 0 计，
        # 而不是 NULL 把 SUM 变成 NULL——"没测出页数"是"算不准"，不是"没消耗"，
        # 但编一个数字更糟：如实少报，用户看到的是下限。
        with self._db.read() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(
                        CASE WHEN p.id IS NOT NULL
                             THEN p.page_end - p.page_start + 1
                             ELSE COALESCE(d.page_count, 0)
                        END
                    ), 0) AS pages,
                    COUNT(*) AS calls
                FROM parse_results r
                JOIN documents d ON d.id = r.document_id
                LEFT JOIN document_parts p ON p.id = r.part_id
                WHERE r.parser_name = %s AND r.created_at >= %s
                """,
                (parser_name, _dump(since)),
            ).fetchone()
        if not row:
            return (0, 0)
        return (int(row["pages"]), int(row["calls"]))

    # ------------------------------------------------------------------ 任务队列

    def enqueue_task(self, record: TaskRecord) -> TaskRecord:
        stamp = _now()
        record.created_at = record.created_at or stamp
        record.updated_at = record.updated_at or stamp
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, kind, state, payload, document_id, part_id, attempts, max_attempts,
                     lease_owner, lease_expires_at, next_run_at, error, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.kind.value,
                    record.state.value,
                    _json(record.payload),
                    record.document_id,
                    record.part_id,
                    record.attempts,
                    record.max_attempts,
                    record.lease_owner,
                    _dump(record.lease_expires_at),
                    _dump(record.next_run_at),
                    record.error,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def claim_task(self, *, owner: str, lease_seconds: int) -> TaskRecord | None:
        """原子领取一个待执行任务。

        SQLite 版靠单条 ``UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING``
        在"单写者"下保证不双领；PG 是多写者，必须让子查询 ``FOR UPDATE SKIP LOCKED``：
        每个 worker 锁定自己看到的那一行，其余 worker 跳过它去领下一行，
        互不阻塞、也不会领到同一条。
        """
        now = _now()
        with self._db.session() as conn:
            row = conn.execute(
                """
                UPDATE tasks
                   SET state = %s,
                       lease_owner = %s,
                       lease_expires_at = %s,
                       attempts = attempts + 1,
                       updated_at = %s
                 WHERE id = (
                       SELECT id FROM tasks
                        WHERE state = %s
                          AND (next_run_at IS NULL OR next_run_at <= %s)
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                 )
                RETURNING *
                """,
                (
                    TaskState.RUNNING.value,
                    owner,
                    now + timedelta(seconds=lease_seconds),
                    now,
                    TaskState.PENDING.value,
                    now,
                ),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def heartbeat_task(self, task_id: str, *, owner: str, lease_seconds: int) -> bool:
        """续租。只有仍持有租约的消费者能续，返回 False 表示已被回收。"""
        now = _now()
        with self._db.session() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET lease_expires_at = %s, updated_at = %s
                 WHERE id = %s AND lease_owner = %s AND state = %s
                """,
                (
                    now + timedelta(seconds=lease_seconds),
                    now,
                    task_id,
                    owner,
                    TaskState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def finish_task(
        self, task_id: str, state: TaskState, *, owner: str, error: str | None = None
    ) -> bool:
        """落终态。条件更新 ``lease_owner = owner``：租约被回收后原消费者写不进来。"""
        with self._db.session() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET state = %s, error = %s, lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = %s
                 WHERE id = %s AND lease_owner = %s
                """,
                (state.value, error, _dump(_now()), task_id, owner),
            )
        return cursor.rowcount == 1

    def cancel_tasks_for_document(self, document_id: str) -> int:
        """把这个文档还没结束的任务标成 canceled（见 ``MetaStore`` 的说明）。"""
        with self._db.session() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET state = %s, error = '已取消', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = %s
                 WHERE document_id = %s AND state IN (%s, %s)
                """,
                (
                    TaskState.CANCELED.value,
                    _dump(_now()),
                    document_id,
                    TaskState.PENDING.value,
                    TaskState.RUNNING.value,
                ),
            )
        return int(cursor.rowcount)

    def cancel_tasks(self, task_ids: Sequence[str]) -> int:
        """按 id 撤销还没结束的任务（见 ``MetaStore`` 的说明）。"""
        wanted = list(dict.fromkeys(task_ids))
        if not wanted:
            return 0
        # 占位符按数量现拼：`IN (%s, %s, ...)` 没法用单个参数表达，而值仍然走参数绑定
        placeholders = ", ".join(["%s"] * len(wanted))
        sql = (
            "UPDATE tasks"  # noqa: S608
            " SET state = %s, error = '已取消', lease_owner = NULL,"
            " lease_expires_at = NULL, updated_at = %s"
            f" WHERE id IN ({placeholders}) AND state IN (%s, %s)"
        )
        with self._db.session() as conn:
            cursor = conn.execute(
                sql,
                (
                    TaskState.CANCELED.value,
                    _dump(_now()),
                    *wanted,
                    TaskState.PENDING.value,
                    TaskState.RUNNING.value,
                ),
            )
        return int(cursor.rowcount)

    def reclaim_expired_tasks(self, *, now: datetime | None = None) -> int:
        """回收超时任务：还有重试额度就回到 PENDING（断点续跑），否则判失败。"""
        moment = now or _now()
        with self._db.session() as conn:
            exhausted = conn.execute(
                """
                UPDATE tasks
                   SET state = %s, error = '超过最大重试次数，已放弃', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = %s
                 WHERE state = %s AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s
                   AND attempts >= max_attempts
                """,
                (TaskState.FAILED.value, moment, TaskState.RUNNING.value, moment),
            ).rowcount
            requeued = conn.execute(
                """
                UPDATE tasks
                   SET state = %s, lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
                 WHERE state = %s AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s
                   AND attempts < max_attempts
                """,
                (TaskState.PENDING.value, moment, TaskState.RUNNING.value, moment),
            ).rowcount
        return int(exhausted + requeued)

    def reschedule_task(
        self, task_id: str, *, owner: str, next_run_at: datetime, error: str | None
    ) -> bool:
        """退回待执行并设定下次可领时间，同时释放租约（指数退避的落地）。

        同样按 ``owner`` 条件更新：租约被回收后原消费者不能再把任务拽回队列，
        否则会把新消费者刚领走的任务改回 PENDING，造成同一份文档被两个人同时处理。
        """
        with self._db.session() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET state = %s, next_run_at = %s, error = %s, lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = %s
                 WHERE id = %s AND lease_owner = %s
                """,
                (
                    TaskState.PENDING.value,
                    _dump(next_run_at),
                    error,
                    _dump(_now()),
                    task_id,
                    owner,
                ),
            )
        return cursor.rowcount == 1

    def list_tasks(self, state: TaskState | None = None) -> list[TaskRecord]:
        with self._db.read() as conn:
            if state is None:
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE state = %s ORDER BY created_at",
                    (state.value,),
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def task_counts(self, *, now: datetime, overdue_before: datetime) -> TaskCounts:
        # 一条聚合拿到四个计数与最老入队时间（FILTER 子句让它们共享同一次扫描），
        # 再用一条只扫 PENDING 的 GROUP BY 拿类型分布。
        with self._db.read() as conn:
            row = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE state = %s) AS running,
                    count(*) FILTER (WHERE state = %s) AS pending,
                    count(*) FILTER (
                        WHERE state = %s AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= %s
                    ) AS stalled,
                    count(*) FILTER (
                        WHERE state = %s AND next_run_at IS NOT NULL AND next_run_at <= %s
                    ) AS overdue,
                    min(created_at) FILTER (WHERE state = %s) AS oldest_pending
                  FROM tasks
                """,
                (
                    TaskState.RUNNING.value,
                    TaskState.PENDING.value,
                    TaskState.RUNNING.value,
                    _dump(now),
                    TaskState.PENDING.value,
                    _dump(overdue_before),
                    TaskState.PENDING.value,
                ),
            ).fetchone()
            kinds = conn.execute(
                "SELECT kind, count(*) AS n FROM tasks WHERE state = %s GROUP BY kind",
                (TaskState.PENDING.value,),
            ).fetchall()
        if not row:
            return TaskCounts()
        return TaskCounts(
            running=int(row["running"] or 0),
            pending=int(row["pending"] or 0),
            stalled=int(row["stalled"] or 0),
            overdue=int(row["overdue"] or 0),
            oldest_pending_at=_load(row["oldest_pending"]),
            pending_by_kind={str(item["kind"]): int(item["n"]) for item in kinds},
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    # ------------------------------------------------------------------ 数据源 / 凭据 / webhook

    @staticmethod
    def _data_source_from_row(row: dict) -> DataSourceRecord:
        return DataSourceRecord(
            id=row["id"],
            knowledge_base_id=row["knowledge_base_id"],
            kind=DataSourceKind(row["kind"]),
            name=row["name"],
            config=row["config"],
            etag=row["etag"],
            last_pulled_at=_load(row["last_pulled_at"]),
            enabled=bool(row["enabled"]),
        )

    def get_data_source(self, source_id: str) -> DataSourceRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM data_sources WHERE id = %s", (source_id,)).fetchone()
        return self._data_source_from_row(row) if row else None

    def list_all_data_sources(self) -> list[DataSourceRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM data_sources ORDER BY id").fetchall()
        return [self._data_source_from_row(row) for row in rows]

    def update_data_source(self, record: DataSourceRecord) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE data_sources SET name = %s, config = %s, enabled = %s WHERE id = %s",
                (record.name, _json(record.config), record.enabled, record.id),
            )

    def delete_data_source(self, source_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM data_sources WHERE id = %s", (source_id,))

    def mark_data_source_pulled(self, source_id: str, *, etag: str | None) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE data_sources SET etag = %s, last_pulled_at = %s WHERE id = %s",
                (etag, _dump(_now()), source_id),
            )

    def create_data_source(self, record: DataSourceRecord) -> DataSourceRecord:
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO data_sources
                    (id, knowledge_base_id, kind, name, config, etag, last_pulled_at, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.knowledge_base_id,
                    record.kind.value,
                    record.name,
                    _json(record.config),
                    record.etag,
                    _dump(record.last_pulled_at),
                    record.enabled,
                ),
            )
        return record

    def list_data_sources(self, kb_id: str) -> list[DataSourceRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM data_sources WHERE knowledge_base_id = %s ORDER BY name",
                (kb_id,),
            ).fetchall()
        return [
            DataSourceRecord(
                id=row["id"],
                knowledge_base_id=row["knowledge_base_id"],
                kind=DataSourceKind(row["kind"]),
                name=row["name"],
                config=row["config"],
                etag=row["etag"],
                last_pulled_at=_load(row["last_pulled_at"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def create_api_key(self, record: ApiKeyRecord) -> ApiKeyRecord:
        """只落哈希：明文 key 永不进库（架构 §3.2）。"""
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, name, key_hash, permission, knowledge_base_ids, key_prefix,
                     created_by, created_at, last_used_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.name,
                    record.key_hash,
                    record.permission.value,
                    _json(list(record.knowledge_base_ids)),
                    record.key_prefix,
                    record.created_by,
                    _dump(record.created_at),
                    _dump(record.last_used_at),
                ),
            )
        return record

    @staticmethod
    def _api_key_from_row(row: dict) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=row["id"],
            name=row["name"],
            key_hash=row["key_hash"],
            permission=ApiKeyPermission(row["permission"]),
            knowledge_base_ids=tuple(row["knowledge_base_ids"]),
            key_prefix=row["key_prefix"],
            created_by=row["created_by"],
            created_at=_load(row["created_at"]),
            last_used_at=_load(row["last_used_at"]),
        )

    def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE key_hash = %s", (key_hash,)).fetchone()
        return self._api_key_from_row(row) if row else None

    def list_api_keys(self) -> list[ApiKeyRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [self._api_key_from_row(row) for row in rows]

    def delete_api_key(self, key_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))

    def touch_api_key(self, key_id: str, *, used_at: datetime | None = None) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                (_dump(used_at or _now()), key_id),
            )

    # ------------------------------------------------------------------ 幂等键

    def create_idempotency_key(self, record: IdempotencyRecord) -> IdempotencyRecord:
        """占住一个幂等键。

        用一个原子 INSERT 来"抢锁"：并发下只有一个会成功，另一个拿到唯一约束冲突。
        这比自己先 SELECT 再 INSERT 可靠——后者在两次调用之间有一个窗口，
        两个并发请求会同时看到"不存在"。
        """
        record.created_at = record.created_at or _now()
        try:
            with self._db.session() as conn:
                conn.execute(
                    "INSERT INTO idempotency_keys (key, request_hash, response, created_at)"
                    " VALUES (%s, %s, %s, %s)",
                    # response 写真正的 SQL NULL，不写 JSON 文本 "null"：
                    # 后者让"还没挂响应"这件事在 SQL 侧没法用 IS NULL 判定，
                    # 而释放键与"进行中"判定都要靠它（实测踩到：释放成了空操作）。
                    # response 列在 schema 里是 text，所以走 _json_text 而不是 Jsonb。
                    (
                        record.key,
                        record.request_hash,
                        _json_text(record.response) if record.response is not None else None,
                        _dump(record.created_at),
                    ),
                )
        except _psycopg().IntegrityError as exc:
            raise ConflictError(f"幂等键已被占用：{record.key}") from exc
        return record

    def get_idempotency_key(self, key: str) -> IdempotencyRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM idempotency_keys WHERE key = %s", (key,)).fetchone()
        if not row:
            return None
        raw = row["response"]
        return IdempotencyRecord(
            key=row["key"],
            request_hash=row["request_hash"],
            response=json.loads(raw) if raw else None,
            created_at=_load(row["created_at"]),
        )

    def save_idempotent_response(self, key: str, response: dict[str, object]) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE idempotency_keys SET response = %s WHERE key = %s",
                (_json_text(response), key),
            )

    def purge_expired_idempotency_keys(self, *, before: datetime) -> int:
        with self._db.session() as conn:
            cursor = conn.execute(
                "DELETE FROM idempotency_keys WHERE created_at < %s", (_dump(before),)
            )
        return cursor.rowcount or 0

    def purge_finished_tasks(self, *, before: datetime) -> int:
        # 只删终态：PENDING/RUNNING 是"当前状态"，不是历史——删掉会让正在排队的
        # 文档永远不再被处理（而且没有任何报错）
        with self._db.session() as conn:
            cursor = conn.execute(
                """
                DELETE FROM tasks
                 WHERE state IN (%s, %s, %s) AND updated_at < %s
                """,
                (
                    TaskState.SUCCEEDED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELED.value,
                    _dump(before),
                ),
            )
        return int(cursor.rowcount)

    def purge_stage_events(self, *, before: datetime) -> int:
        with self._db.session() as conn:
            cursor = conn.execute(
                "DELETE FROM document_stage_events WHERE entered_at < %s", (_dump(before),)
            )
        return int(cursor.rowcount)

    def release_idempotency_key(self, key: str) -> None:
        # 只删"还没挂上响应"的那种：已经成功过的键不能因为一次重放异常被放掉，
        # 那会让同一个键再被用来跑一遍业务。
        with self._db.session() as conn:
            conn.execute("DELETE FROM idempotency_keys WHERE key = %s AND response IS NULL", (key,))

    # ------------------------------------------------------------------ 使用者名册

    def set_user_avatar(self, user_id: str, avatar_key: str) -> None:
        with self._db.session() as conn:
            conn.execute("UPDATE users SET avatar_key = %s WHERE id = %s", (avatar_key, user_id))

    @staticmethod
    def _user_from_row(row: dict) -> UserRecord:
        return UserRecord(
            id=row["id"],
            name=row["name"],
            note=row["note"],
            username=row["username"],
            password_hash=row["password_hash"],
            role=UserRole(row["role"]),
            disabled=bool(row["disabled"]),
            created_at=_load(row["created_at"]),
            avatar_key=row["avatar_key"] or "",
        )

    def create_user(self, record: UserRecord) -> UserRecord:
        record.created_at = record.created_at or _now()
        try:
            with self._db.session() as conn:
                conn.execute(
                    """
                    INSERT INTO users (id, name, note, username, password_hash,
                                       role, disabled, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.id,
                        record.name,
                        record.note,
                        record.username,
                        record.password_hash,
                        record.role.value,
                        record.disabled,
                        _dump(record.created_at),
                    ),
                )
        except _psycopg().IntegrityError as exc:
            # 冲突可能来自 id 主键、name 或 username 两个唯一索引，查出是哪个再给文案：
            # 管理员开通账号时，"用户名被占"与"花名册里有同名的人"是两种不同的处理。
            # 注意：这里已经在 session() 之外（事务已回滚），下面的读走独立连接。
            with self._db.read() as conn:
                if conn.execute("SELECT 1 FROM users WHERE id = %s", (record.id,)).fetchone():
                    raise ConflictError(f"使用者 id「{record.id}」已存在") from exc
                if (
                    record.username
                    and conn.execute(
                        "SELECT 1 FROM users WHERE username = %s", (record.username,)
                    ).fetchone()
                ):
                    raise ConflictError(f"用户名「{record.username}」已被占用") from exc
            raise ConflictError(f"已经有叫「{record.name}」的使用者了") from exc
        return record

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return self._user_from_row(row) if row else None

    def find_user_by_name(self, name: str) -> UserRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE name = %s", (name,)).fetchone()
        return self._user_from_row(row) if row else None

    def list_users(self) -> list[UserRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at, id").fetchall()
        return [self._user_from_row(row) for row in rows]

    def delete_user(self, user_id: str) -> None:
        """删使用者，但**保留他传过的数据**。

        文档已经进了知识库、已经向量化、可能已被引用——把使用者删掉就顺手
        删掉他的文档，那是数据丢失而不是权限撤销。所有归属列（文档上传者、
        库 owner、会话 owner、API Key 创建者）一律置空，界面显示"未记录"。
        会话与分享记录不靠这里清：它们挂在 FK 级联上（PG 的外键始终强制），
        随 users 行一起消失。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE documents SET uploaded_by = NULL WHERE uploaded_by = %s", (user_id,)
            )
            conn.execute(
                "UPDATE knowledge_bases SET owner_id = NULL WHERE owner_id = %s", (user_id,)
            )
            conn.execute("UPDATE conversations SET owner_id = NULL WHERE owner_id = %s", (user_id,))
            conn.execute("UPDATE api_keys SET created_by = NULL WHERE created_by = %s", (user_id,))
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))

    def count_documents_by_user(self, user_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE uploaded_by = %s", (user_id,)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ 账号与会话

    def find_user_by_username(self, username: str) -> UserRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
        return self._user_from_row(row) if row else None

    def update_user_password(self, user_id: str, password_hash: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
            )

    def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        with self._db.session() as conn:
            conn.execute("UPDATE users SET disabled = %s WHERE id = %s", (disabled, user_id))

    def claim_legacy_ownership(self, owner_id: str) -> dict[str, int]:
        with self._db.session() as conn:
            kbs = conn.execute(
                "UPDATE knowledge_bases SET owner_id = %s WHERE owner_id IS NULL", (owner_id,)
            ).rowcount
            conversations = conn.execute(
                "UPDATE conversations SET owner_id = %s WHERE owner_id IS NULL", (owner_id,)
            ).rowcount
        return {"knowledge_bases": kbs or 0, "conversations": conversations or 0}

    # ------------------------------------------------------------------ 会话

    @staticmethod
    def _session_from_row(row: dict) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            user_id=row["user_id"],
            created_at=_load(row["created_at"]),
            expires_at=_load(row["expires_at"]),
            last_seen_at=_load(row["last_seen_at"]),
        )

    def create_session(self, record: SessionRecord) -> SessionRecord:
        stamp = _now()
        record.created_at = record.created_at or stamp
        record.last_seen_at = record.last_seen_at or stamp
        try:
            with self._db.session() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        record.id,
                        record.user_id,
                        _dump(record.created_at),
                        _dump(record.expires_at),
                        _dump(record.last_seen_at),
                    ),
                )
        except _psycopg().IntegrityError as exc:
            # 主键撞车（哈希碰撞，实际不可能）与外键违例（账号不存在）都落这里；
            # 对上层都是"这条会话建不成"，翻成领域错误而不是漏原生驱动异常
            raise InvalidRequestError("会话创建失败：账号不存在或会话标识冲突") from exc
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, expires_at: datetime
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = %s, expires_at = %s WHERE id = %s",
                (_dump(last_seen_at), _dump(expires_at), session_id),
            )

    def delete_session(self, session_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    def delete_sessions_for_user(
        self, user_id: str, *, except_session_id: str | None = None
    ) -> int:
        with self._db.session() as conn:
            if except_session_id is None:
                cursor = conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            else:
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE user_id = %s AND id != %s",
                    (user_id, except_session_id),
                )
        return cursor.rowcount or 0

    # ------------------------------------------------------------------ 知识库分享

    @staticmethod
    def _share_from_row(row: dict) -> ShareRecord:
        return ShareRecord(
            kb_id=row["kb_id"],
            user_id=row["user_id"],
            permission=SharePermission(row["permission"]),
            created_at=_load(row["created_at"]),
        )

    def put_share(self, record: ShareRecord) -> ShareRecord:
        record.created_at = record.created_at or _now()
        try:
            with self._db.session() as conn:
                conn.execute(
                    """
                    INSERT INTO kb_shares (kb_id, user_id, permission, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (kb_id, user_id) DO UPDATE SET permission = excluded.permission
                    """,
                    (
                        record.kb_id,
                        record.user_id,
                        record.permission.value,
                        _dump(record.created_at),
                    ),
                )
        except _psycopg().IntegrityError as exc:
            # 复合主键冲突已被 ON CONFLICT 接住，能落到这里的只剩外键违例
            raise InvalidRequestError("知识库或使用者不存在，无法分享") from exc
        # 重授只改档位：``created_at`` 要保留**首次授予**的时间，不随调整刷新——
        # 所以返回前回读一次，而不是把本地这份（带着新时间戳的）直接给出去
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM kb_shares WHERE kb_id = %s AND user_id = %s",
                (record.kb_id, record.user_id),
            ).fetchone()
        return self._share_from_row(row)

    def list_shares_for_kb(self, kb_id: str) -> list[ShareRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_shares WHERE kb_id = %s ORDER BY created_at", (kb_id,)
            ).fetchall()
        return [self._share_from_row(row) for row in rows]

    def list_shares_for_user(self, user_id: str) -> list[ShareRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_shares WHERE user_id = %s ORDER BY created_at", (user_id,)
            ).fetchall()
        return [self._share_from_row(row) for row in rows]

    def delete_share(self, kb_id: str, user_id: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "DELETE FROM kb_shares WHERE kb_id = %s AND user_id = %s", (kb_id, user_id)
            )

    # ------------------------------------------------------------------ 用量

    @staticmethod
    def _usage_from_row(row: dict) -> UsageEventRecord:
        return UsageEventRecord(
            id=row["id"],
            kind=row["kind"],
            provider=row["provider"],
            model_id=row["model_id"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            items=row["items"],
            duration_ms=row["duration_ms"],
            source=row["source"],
            created_at=_load(row["created_at"]),
        )

    def record_usage(self, record: UsageEventRecord) -> UsageEventRecord:
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO usage_events"
                " (id, kind, provider, model_id, prompt_tokens, completion_tokens,"
                "  items, duration_ms, source, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.kind,
                    record.provider,
                    record.model_id,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.items,
                    record.duration_ms,
                    record.source,
                    _dump(record.created_at),
                ),
            )
        return record

    def list_usage(self, *, since: datetime | None = None) -> list[UsageEventRecord]:
        sql = "SELECT * FROM usage_events"
        params: tuple[object, ...] = ()
        if since is not None:
            sql += " WHERE created_at >= %s"
            params = (_dump(since),)
        sql += " ORDER BY created_at"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._usage_from_row(row) for row in rows]

    def purge_usage_before(self, before: datetime) -> int:
        with self._db.session() as conn:
            cursor = conn.execute(
                "DELETE FROM usage_events WHERE created_at < %s", (_dump(before),)
            )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ 模型注册器

    @staticmethod
    def _provider_from_row(row: dict) -> ModelProviderRecord:
        return ModelProviderRecord(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            base_url=row["base_url"],
            api_key=row["api_key"],
            enabled=bool(row["enabled"]),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _model_from_row(row: dict) -> RegisteredModelRecord:
        return RegisteredModelRecord(
            id=row["id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            label=row["label"],
            dim=row["dim"],
            capabilities=tuple(row["capabilities"]),
            options=row["options"],
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    def create_model_provider(self, record: ModelProviderRecord) -> ModelProviderRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO model_providers"
                " (id, kind, name, base_url, api_key, enabled, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.kind,
                    record.name,
                    record.base_url,
                    record.api_key,
                    record.enabled,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_model_provider(self, provider_id: str) -> ModelProviderRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM model_providers WHERE id = %s", (provider_id,)
            ).fetchone()
        return self._provider_from_row(row) if row else None

    def list_model_providers(self) -> list[ModelProviderRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM model_providers ORDER BY created_at, id").fetchall()
        return [self._provider_from_row(row) for row in rows]

    def update_model_provider(self, record: ModelProviderRecord) -> None:
        record.updated_at = _now()
        with self._db.session() as conn:
            conn.execute(
                "UPDATE model_providers SET kind = %s, name = %s, base_url = %s,"
                " api_key = %s, enabled = %s, updated_at = %s WHERE id = %s",
                (
                    record.kind,
                    record.name,
                    record.base_url,
                    record.api_key,
                    record.enabled,
                    _dump(record.updated_at),
                    record.id,
                ),
            )

    def delete_model_provider(self, provider_id: str) -> None:
        # 显式删子行（虽然 PG 的外键级联也会删）：
        # 意图写在代码里比藏在 schema 里可读。只删供应商会留下指向不存在
        # 供应商的孤儿模型，而它还会出现在模型下拉里。
        with self._db.session() as conn:
            conn.execute("DELETE FROM model_registry WHERE provider_id = %s", (provider_id,))
            conn.execute("DELETE FROM model_providers WHERE id = %s", (provider_id,))

    def create_registered_model(self, record: RegisteredModelRecord) -> RegisteredModelRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        try:
            with self._db.session() as conn:
                conn.execute(
                    "INSERT INTO model_registry"
                    " (id, provider_id, model_id, label, dim, capabilities, options,"
                    "  created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.id,
                        record.provider_id,
                        record.model_id,
                        record.label,
                        record.dim,
                        _json(list(record.capabilities)),
                        _json(record.options),
                        _dump(record.created_at),
                        _dump(record.updated_at),
                    ),
                )
        except _psycopg().IntegrityError as exc:
            raise ConflictError(f"该供应商下已经登记过模型 {record.model_id}") from exc
        return record

    def get_registered_model(self, model_pk: str) -> RegisteredModelRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM model_registry WHERE id = %s", (model_pk,)).fetchone()
        return self._model_from_row(row) if row else None

    def list_registered_models(self, provider_id: str | None = None) -> list[RegisteredModelRecord]:
        sql = "SELECT * FROM model_registry"
        params: tuple[object, ...] = ()
        if provider_id is not None:
            sql += " WHERE provider_id = %s"
            params = (provider_id,)
        sql += " ORDER BY created_at, id"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._model_from_row(row) for row in rows]

    def update_registered_model(self, record: RegisteredModelRecord) -> None:
        record.updated_at = _now()
        with self._db.session() as conn:
            conn.execute(
                "UPDATE model_registry SET model_id = %s, label = %s, dim = %s,"
                " capabilities = %s, options = %s, updated_at = %s WHERE id = %s",
                (
                    record.model_id,
                    record.label,
                    record.dim,
                    _json(list(record.capabilities)),
                    _json(record.options),
                    _dump(record.updated_at),
                    record.id,
                ),
            )

    def delete_registered_model(self, model_pk: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM model_registry WHERE id = %s", (model_pk,))

    def resolve_model_binding(
        self, key: str
    ) -> tuple[ModelProviderRecord, RegisteredModelRecord] | None:
        """一条 JOIN 解出绑定（见 base.py 的同名方法：这是热路径）。"""
        with self._db.read() as conn:
            # `to_jsonb` 是关键：两张表都有 id / name / created_at，直接 JOIN 出来列名会打架，
            # 而转成 JSON 之后两个映射函数可以原样复用（它们只按键取值）。
            row = conn.execute(
                """
                SELECT to_jsonb(m) AS model, to_jsonb(p) AS provider
                  FROM app_settings s
                  JOIN model_registry m ON m.id = s.value
                  JOIN model_providers p ON p.id = m.provider_id
                 WHERE s.key = %s
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return (
            self._provider_from_row(row["provider"]),
            self._model_from_row(row["model"]),
        )

    # ------------------------------------------------------------------ 对话留存

    @staticmethod
    def _conversation_from_row(row: dict) -> ConversationRecord:
        return ConversationRecord(
            id=row["id"],
            title=row["title"],
            kb_ids=tuple(row["kb_ids"]),
            owner_id=row["owner_id"],
            model_pk=row["model_pk"],
            # 可空列在旧库里是 NULL：``None`` 表示"跟随全局默认"，不要折成 False
            thinking=None if row["thinking"] is None else bool(row["thinking"]),
            thinking_effort=row["thinking_effort"],
            pinned=bool(row["pinned"]),
            # 旧库/未归档的会话这一列是 NULL —— 保持 None，不要折成空串
            workspace_id=row.get("workspace_id"),
            archived_at=_load(row.get("archived_at")),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _message_from_row(row: dict) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"],
            sources=tuple(row["sources"]),
            # 老库里这两列刚补上，存量行是 '[]' / ''——与"这一轮没有过程"恰好同义
            steps=tuple(row.get("steps") or ()),
            thinking=row.get("thinking") or "",
            # 老库里这列刚补上，存量行是 '[]'——与"这条消息没有随发附件"恰好同义
            attachments=tuple(row.get("attachments") or ()),
            created_at=_load(row["created_at"]),
        )

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO conversations"
                " (id, title, kb_ids, owner_id, model_pk, thinking, thinking_effort,"
                "  workspace_id, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.title,
                    _json(list(record.kb_ids)),
                    record.owner_id,
                    record.model_pk,
                    record.thinking,
                    record.thinking_effort,
                    record.workspace_id,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = %s", (conversation_id,)
            ).fetchone()
        return self._conversation_from_row(row) if row else None

    def set_conversation_archived(self, conversation_id: str, archived: bool) -> None:
        # 不推 updated_at：归类动作不该改变"最近活动"的名次（与置顶/改名同理）
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at = CASE WHEN %s THEN now() ELSE NULL END"
                " WHERE id = %s",
                (archived, conversation_id),
            )

    def last_assistant_previews(self, conversation_ids: Sequence[str]) -> dict[str, str]:
        """一次查完每个会话最后一条回答。``DISTINCT ON`` 是 PG 的写法：
        按 ``conversation_id`` 分组取排序后的第一行。

        **不拼占位符**：ids 走 ``= ANY(%s)`` 传成数组参数（psycopg3 直接把 list
        适配成数组）。文件里其它批量查询是拼 ``"%s"`` 的，这里刻意不那么写——
        没有插值就没有 S608 那一类告警，也少一层字符串构造。
        """
        if not conversation_ids:
            return {}
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (conversation_id) conversation_id, content"
                " FROM chat_messages"
                " WHERE conversation_id = ANY(%s) AND role = 'assistant'"
                " ORDER BY conversation_id, created_at DESC",
                (list(conversation_ids),),
            ).fetchall()
        return {row["conversation_id"]: row["content"] for row in rows}

    def set_conversation_workspace(self, conversation_id: str, workspace_id: str | None) -> None:
        # 不推 updated_at：见协议里那段说明（整理动作不该改变"最近活动"的名次）
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET workspace_id = %s WHERE id = %s",
                (workspace_id, conversation_id),
            )

    # ---- 工作区 ----

    def create_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO workspaces"
                " (id, owner_id, name, root_path, description, kb_ids, device_id, device_name,"
                "  created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.owner_id,
                    record.name,
                    record.root_path,
                    record.description,
                    _json(list(record.kb_ids)),
                    # `None` 原样落库 = 服务器端（网页版/直连 API 看到的那些）
                    record.device_id,
                    record.device_name or "",
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = %s", (workspace_id,)).fetchone()
        return self._workspace_from_row(row) if row else None

    def list_workspaces(
        self, *, device_id: str | None = None, any_device: bool = False
    ) -> list[WorkspaceRecord]:
        # 设备过滤三态（见 base.py 协议）：`any_device` 不过滤；否则精确比 `device_id`，
        # 其中 `None` 是**服务器端**这一档（`IS NULL`，不是"没条件"）。
        # 归属过滤不在这里做——存储层不认识调用者身份（与会话列表同一取舍）。
        sql = "SELECT * FROM workspaces"
        params: list[object] = []
        if not any_device:
            if device_id is None:
                sql += " WHERE device_id IS NULL"
            else:
                sql += " WHERE device_id = %s"
                params.append(device_id)
        sql += " ORDER BY updated_at DESC"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._workspace_from_row(row) for row in rows]

    def update_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord:
        record.updated_at = _now()
        with self._db.session() as conn:
            conn.execute(
                "UPDATE workspaces SET name = %s, root_path = %s, description = %s,"
                " kb_ids = %s, updated_at = %s WHERE id = %s",
                (
                    record.name,
                    record.root_path,
                    record.description,
                    _json(list(record.kb_ids)),
                    _dump(record.updated_at),
                    record.id,
                ),
            )
        return record

    def set_workspace_archived(self, workspace_id: str, archived: bool) -> None:
        # 与 `set_conversation_archived` 逐字同一条：**不推 updated_at**（归档是整理动作）。
        # 时间戳而不是布尔：`now()` 记下"什么时候收起来的"，取消就置回 NULL。
        with self._db.session() as conn:
            conn.execute(
                "UPDATE workspaces SET archived_at = CASE WHEN %s THEN now() ELSE NULL END"
                " WHERE id = %s",
                (archived, workspace_id),
            )

    def delete_workspace(self, workspace_id: str) -> None:
        # 外键是 ON DELETE SET NULL：里面的会话**退回未归档**，不跟着删。
        # 这是刻意的，见 base.py 协议里那段说明。
        with self._db.session() as conn:
            conn.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))

    def count_workspace_conversations(self, workspace_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT count(*) AS total FROM conversations WHERE workspace_id = %s",
                (workspace_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    # ---- 定时任务（v0.33）----

    def create_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO scheduled_tasks"
                " (id, name, prompt, kind, cron, run_at, next_run_at, enabled, kb_ids,"
                "  model_pk, thinking, thinking_effort, conversation_id, owner_id,"
                "  last_run_at, last_status, last_error, run_count, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.name,
                    record.prompt,
                    record.kind,
                    record.cron,
                    _dump(record.run_at),
                    _dump(record.next_run_at),
                    record.enabled,
                    _json(list(record.kb_ids)),
                    record.model_pk,
                    record.thinking,
                    record.thinking_effort,
                    record.conversation_id,
                    record.owner_id,
                    _dump(record.last_run_at),
                    record.last_status,
                    record.last_error,
                    record.run_count,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_scheduled_task(self, scheduled_id: str) -> ScheduledTaskRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = %s", (scheduled_id,)
            ).fetchone()
        return self._scheduled_from_row(row) if row else None

    def list_scheduled_tasks(self) -> list[ScheduledTaskRecord]:
        # 待跑的排前面、按时间正序；跑完/停用的（``next_run_at IS NULL``）沉到最后，
        # 内部按最近更新倒序——用户找的多半是"下次什么时候跑"，而不是"去年跑过什么"
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY next_run_at ASC NULLS LAST, updated_at DESC"
            ).fetchall()
        return [self._scheduled_from_row(row) for row in rows]

    def update_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord:
        record.updated_at = _now()
        with self._db.session() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET name = %s, prompt = %s, kind = %s, cron = %s,"
                " run_at = %s, next_run_at = %s, enabled = %s, kb_ids = %s, model_pk = %s,"
                " thinking = %s, thinking_effort = %s, conversation_id = %s,"
                " last_run_at = %s, last_status = %s, last_error = %s, run_count = %s,"
                " updated_at = %s WHERE id = %s",
                (
                    record.name,
                    record.prompt,
                    record.kind,
                    record.cron,
                    _dump(record.run_at),
                    _dump(record.next_run_at),
                    record.enabled,
                    _json(list(record.kb_ids)),
                    record.model_pk,
                    record.thinking,
                    record.thinking_effort,
                    record.conversation_id,
                    _dump(record.last_run_at),
                    record.last_status,
                    record.last_error,
                    record.run_count,
                    _dump(record.updated_at),
                    record.id,
                ),
            )
        return record

    def delete_scheduled_task(self, scheduled_id: str) -> None:
        # 只删这条调度：**已经跑出来的会话不删**（那是用户问过的内容，
        # 与"删工作区不删会话"同一条纪律）
        with self._db.session() as conn:
            conn.execute("DELETE FROM scheduled_tasks WHERE id = %s", (scheduled_id,))

    def due_scheduled_tasks(self, *, now: datetime, limit: int = 10) -> list[ScheduledTaskRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks"
                " WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= %s"
                " ORDER BY next_run_at ASC LIMIT %s",
                (_dump(now), max(1, limit)),
            ).fetchall()
        return [self._scheduled_from_row(row) for row in rows]

    def arm_scheduled_task(
        self,
        scheduled_id: str,
        *,
        expected_next_run_at: datetime | None,
        next_run_at: datetime | None,
        enabled: bool,
    ) -> bool:
        """认领一次运行（CAS）。**判定写进 WHERE**，不靠"先读后写"——
        两个 worker 同时扫到同一条时，只有一条 UPDATE 能改到行（见协议里的说明）。"""
        with self._db.session() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_tasks SET next_run_at = %s, enabled = %s, updated_at = %s"
                " WHERE id = %s AND next_run_at IS NOT DISTINCT FROM %s",
                (
                    _dump(next_run_at),
                    enabled,
                    _dump(_now()),
                    scheduled_id,
                    # `IS NOT DISTINCT FROM`：``next_run_at`` 可能是 NULL，
                    # 而 SQL 里 `NULL = NULL` 不成立——用 `=` 的话
                    # 一次性任务的认领永远改不到行（那条路径正是从 NULL 认领）
                    _dump(expected_next_run_at),
                ),
            )
            return cursor.rowcount > 0

    def finish_scheduled_run(
        self,
        scheduled_id: str,
        *,
        status: str,
        error: str | None,
        last_run_at: datetime,
        conversation_id: str | None = None,
    ) -> None:
        """记一次运行的结果。

        ``conversation_id`` **用 COALESCE 而不是直接覆盖**：它只在第一次运行时为空，
        之后每次都传同一个值；万一某次调用忘了带，COALESCE 拦住的是"任务与它
        那条会话失联"（那种状态在界面上表现为"跑过但看不到结果"）。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at = %s, last_status = %s,"
                " last_error = %s, run_count = run_count + 1, updated_at = %s,"
                " conversation_id = COALESCE(%s, conversation_id)"
                " WHERE id = %s",
                (
                    _dump(last_run_at),
                    status,
                    error or "",
                    _dump(_now()),
                    conversation_id,
                    scheduled_id,
                ),
            )

    @staticmethod
    def _scheduled_from_row(row: dict) -> ScheduledTaskRecord:
        return ScheduledTaskRecord(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            kind=row["kind"],
            cron=row["cron"],
            run_at=_load(row["run_at"]),
            next_run_at=_load(row["next_run_at"]),
            enabled=bool(row["enabled"]),
            kb_ids=tuple(row["kb_ids"]),
            model_pk=row["model_pk"],
            # 可空列：``None`` = 跟随会话/全局默认，不要折成 False
            thinking=None if row["thinking"] is None else bool(row["thinking"]),
            thinking_effort=row["thinking_effort"],
            conversation_id=row["conversation_id"],
            owner_id=row["owner_id"],
            last_run_at=_load(row["last_run_at"]),
            last_status=row["last_status"],
            last_error=row["last_error"],
            run_count=int(row["run_count"]),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    # ---- MCP 服务 ----

    def create_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord:
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO mcp_servers"
                " (id, owner_id, name, transport, target, args, env, headers, policy,"
                "  enabled, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.owner_id,
                    record.name,
                    record.transport,
                    record.target,
                    _json(list(record.args)),
                    _json(dict(record.env)),
                    _json(dict(record.headers)),
                    record.policy,
                    record.enabled,
                    _dump(record.created_at),
                    _dump(record.updated_at),
                ),
            )
        return record

    def get_mcp_server(self, server_id: str) -> MCPServerRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM mcp_servers WHERE id = %s", (server_id,)).fetchone()
        return self._mcp_server_from_row(row) if row else None

    def list_mcp_servers(self) -> list[MCPServerRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY updated_at DESC").fetchall()
        return [self._mcp_server_from_row(row) for row in rows]

    def update_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord:
        record.updated_at = _now()
        with self._db.session() as conn:
            conn.execute(
                "UPDATE mcp_servers SET name = %s, transport = %s, target = %s, args = %s,"
                " env = %s, headers = %s, policy = %s, enabled = %s, updated_at = %s"
                " WHERE id = %s",
                (
                    record.name,
                    record.transport,
                    record.target,
                    _json(list(record.args)),
                    _json(dict(record.env)),
                    _json(dict(record.headers)),
                    record.policy,
                    record.enabled,
                    _dump(record.updated_at),
                    record.id,
                ),
            )
        return record

    def delete_mcp_server(self, server_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

    @staticmethod
    def _mcp_server_from_row(row: dict) -> MCPServerRecord:
        return MCPServerRecord(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            target=row["target"],
            args=tuple(row["args"]),
            env=dict(row["env"]),
            headers=dict(row["headers"]),
            policy=row["policy"],
            enabled=bool(row["enabled"]),
            owner_id=row["owner_id"],
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _workspace_from_row(row: dict) -> WorkspaceRecord:
        return WorkspaceRecord(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            owner_id=row["owner_id"],
            description=row["description"],
            kb_ids=tuple(row["kb_ids"]),
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
            archived_at=_load(row["archived_at"]),
            # 按列名取，不按位置：这一行的列序会随迁移增长（v0.59 刚加了两列）。
            # `device_name` 是**可空列**且没有默认值，存量记录读回来是 NULL——
            # 归一到空串，免得 `None` 从存储层漏进模型的 `str` 字段。
            device_id=row["device_id"],
            device_name=row["device_name"] or "",
        )

    def list_conversations(
        self, *, limit: int | None = None, q: str | None = None
    ) -> list[ConversationRecord]:
        """置顶优先，其次最近更新；``q`` 按**标题或消息正文**包含匹配（忽略大小写）。"""
        sql = "SELECT * FROM conversations"
        params: list[object] = []
        if q:
            # 与文档搜索同一套转义：用户搜 "a_b" 要字面匹配，而不是"a 后跟任意一字符"
            escaped = q.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
            # **标题或正文**（D12，2026-09-28 走查）：原先只比 `title`，于是
            # "搜一句我记得说过的话"永远搜不到——走查实测同一个词在正文里命中 3 行 /
            # 2 条会话，而列表接口 0 命中。
            #
            # 用 `EXISTS` 而不是 JOIN：一条会话可能有多条消息命中，JOIN 会把它复制成
            # 多行，而调用方的 LIMIT 语义是"前 N 条**会话**"，不是"前 N 条消息"。
            sql += (
                r" WHERE title ILIKE %s ESCAPE '\'"
                r" OR EXISTS (SELECT 1 FROM chat_messages m"
                r" WHERE m.conversation_id = conversations.id"
                r" AND m.content ILIKE %s ESCAPE '\')"
            )
            params.append(f"%{escaped}%")
            params.append(f"%{escaped}%")
        # **置顶的排最前**，其余按最近更新。PG 的 pinned 是 boolean，DESC 把 true 排前
        sql += " ORDER BY pinned DESC, updated_at DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._conversation_from_row(row) for row in rows]

    def set_conversation_pinned(self, conversation_id: str, pinned: bool) -> None:
        # 与改名同理：不推 updated_at
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET pinned = %s WHERE id = %s",
                (pinned, conversation_id),
            )

    def delete_chat_messages(self, message_ids: Sequence[str]) -> int:
        if not message_ids:
            return 0
        # 与上面的批量查询同一手法：placeholders 只由 "%s" 拼成（数量来自 len），
        # 真正的值走参数绑定。S608 在这里是误报。
        placeholders = _placeholders(len(message_ids))
        with self._db.session() as conn:
            cursor = conn.execute(
                f"DELETE FROM chat_messages WHERE id IN ({placeholders})",  # noqa: S608
                list(message_ids),
            )
        return int(cursor.rowcount or 0)

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        # 改名不推 updated_at：否则用户整理一遍标题列表，会话按"最近更新"的排序
        # 会全乱——他想按对话发生的时间找，不是按自己改标题的时间
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET title = %s WHERE id = %s", (title, conversation_id)
            )

    def set_conversation_model(self, conversation_id: str, model_pk: str | None) -> None:
        # 与改名同理：切模型不算"发生了对话"，不推 updated_at
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET model_pk = %s WHERE id = %s",
                (model_pk, conversation_id),
            )

    def set_conversation_thinking(
        self, conversation_id: str, thinking: bool | None, effort: str | None
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET thinking = %s, thinking_effort = %s WHERE id = %s",
                (thinking, effort, conversation_id),
            )

    def touch_conversation(self, conversation_id: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (_dump(_now()), conversation_id),
            )

    def get_conversation_summary(self, conversation_id: str) -> tuple[str, str | None]:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT context_summary, summary_upto FROM conversations WHERE id = %s",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return "", None
        return row["context_summary"] or "", row["summary_upto"]

    def set_conversation_summary(
        self, conversation_id: str, summary: str, upto_message_id: str | None
    ) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversations SET context_summary = %s, summary_upto = %s WHERE id = %s",
                (summary, upto_message_id, conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        """删会话及其消息。

        显式删消息而不是只靠外键级联（PG 的外键始终强制，级联也能删，
        但意图写在代码里比藏在 schema 里可读）。只删会话会留下一堆孤儿消息，
        而且它们会一直被 ``list_messages`` 之外的地方查到（例如按会话聚合的统计）。

        **产物记录也在这里显式删**（同一条道理）。它们的文件本体由服务层先处理：
        落在对象存储里的那份要删掉，落在工作区里的那些是用户项目里的真实文件，
        不能跟着会话一起消失。
        """
        with self._db.session() as conn:
            conn.execute("DELETE FROM chat_messages WHERE conversation_id = %s", (conversation_id,))
            conn.execute(
                "DELETE FROM conversation_artifacts WHERE conversation_id = %s",
                (conversation_id,),
            )
            conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))

    # ------------------------------------------------------------ 会话产物（v0.26）

    def create_artifact(self, record: ConversationArtifactRecord) -> ConversationArtifactRecord:
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO conversation_artifacts"
                " (id, conversation_id, name, format, size_bytes, storage, location,"
                "  workspace_id, owner_id, knowledge_base_id, document_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.conversation_id,
                    record.name,
                    record.format,
                    record.size_bytes,
                    record.storage,
                    record.location,
                    record.workspace_id,
                    record.owner_id,
                    record.knowledge_base_id,
                    record.document_id,
                    _dump(record.created_at),
                ),
            )
        return record

    def get_artifact(self, artifact_id: str) -> ConversationArtifactRecord | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_artifacts WHERE id = %s", (artifact_id,)
            ).fetchone()
        return None if row is None else self._artifact_from_row(row)

    def list_artifacts(self, conversation_id: str) -> list[ConversationArtifactRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_artifacts WHERE conversation_id = %s"
                " ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def mark_artifact_ingested(
        self, artifact_id: str, *, knowledge_base_id: str, document_id: str
    ) -> None:
        """记下"这份产物进了哪个库"。

        **只写这两个字段**，不动 ``location``：入库是**复制**一份进知识库
        （原文进对象存储、切块、建索引），产物本身还在原处——
        用户要的那份文件不该因为"顺便存了一份进库"而搬家。
        """
        with self._db.session() as conn:
            conn.execute(
                "UPDATE conversation_artifacts"
                " SET knowledge_base_id = %s, document_id = %s WHERE id = %s",
                (knowledge_base_id, document_id, artifact_id),
            )

    @staticmethod
    def _artifact_from_row(row: dict) -> ConversationArtifactRecord:
        return ConversationArtifactRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            name=row["name"],
            format=row["format"],
            size_bytes=int(row["size_bytes"] or 0),
            storage=row["storage"],
            location=row["location"] or "",
            workspace_id=row["workspace_id"],
            owner_id=row["owner_id"],
            knowledge_base_id=row["knowledge_base_id"],
            document_id=row["document_id"],
            created_at=row["created_at"],
        )

    def append_message(self, record: ChatMessageRecord) -> ChatMessageRecord:
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            self._insert_message(conn, record)
        return record

    @staticmethod
    def _insert_message(conn: Connection, record: ChatMessageRecord) -> None:
        """在**给定事务里**插一条消息（``append_message`` 与 ``append_turn`` 共用）。

        抽出来是为了"消息与事件同一事务"那条要求：各写一份 INSERT 的话，
        迟早有一处漏掉新列（这张表从 v0.25 起已经补过 ``steps`` / ``thinking``）。
        """
        conn.execute(
            "INSERT INTO chat_messages"
            " (id, conversation_id, role, content, sources, steps, thinking, attachments,"
            "  created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.id,
                record.conversation_id,
                record.role,
                record.content,
                _json([dict(item) for item in record.sources]),
                _json([dict(item) for item in record.steps]),
                record.thinking,
                _json([dict(item) for item in record.attachments]),
                _dump(record.created_at),
            ),
        )

    def append_turn(
        self,
        *,
        messages: Sequence[ChatMessageRecord],
        events: Sequence[SessionEventRecord],
    ) -> None:
        """一轮的消息与它的事件**同一个事务**（见接口处的说明）。

        顺序刻意是"先消息、后事件"？不——**两条都在一个事务里，先后无所谓**，
        唯一重要的是它们要么都在、要么都不在。这里先插消息只是阅读顺序顺一点。
        """
        if not messages:
            return
        now = _now()
        for message in messages:
            message.created_at = message.created_at or now
        with self._db.session() as conn:
            for message in messages:
                self._insert_message(conn, message)
            if events:
                self._insert_events(conn, events, now)

    def append_session_events(
        self, records: Sequence[SessionEventRecord]
    ) -> list[SessionEventRecord]:
        if not records:
            return []
        with self._db.session() as conn:
            return self._insert_events(conn, records, _now())

    def _insert_events(
        self,
        conn: Connection,
        records: Sequence[SessionEventRecord],
        now: datetime,
    ) -> list[SessionEventRecord]:
        """在给定事务里只追加一批事件，并把 ``seq`` / ``id`` 补回记录。

        ``seq`` 在**写这个事务里**算（``max(seq)+1`` 起逐个递增）：让数据库之外
        的任何一方来决定序号，都会在下一次并发写时撞上唯一约束。锁的顺序是先会话行
        （``FOR UPDATE``，同会话的写在这里排队）再取 max —— 反过来的话，
        两个事务会同时读到同一个 max。
        """
        conversation_id = records[0].conversation_id
        conn.execute(
            "SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,)
        )
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM session_events WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
        seq = int(row["s"]) if row is not None else 0
        for record in records:
            if record.conversation_id != conversation_id:
                # 一批事件必须同属一个会话：跨会话的那一批没法用一条 max 算 seq，
                # 硬要支持只会让这个函数变成两段几乎不重叠的代码。
                raise ValueError("同一批会话事件必须属于同一个会话")
            seq += 1
            record.seq = seq
            record.created_at = record.created_at or now
            inserted = conn.execute(
                "INSERT INTO session_events (conversation_id, seq, kind, payload, created_at)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    record.conversation_id,
                    record.seq,
                    record.kind,
                    _json(record.payload),
                    _dump(record.created_at),
                ),
            ).fetchone()
            record.id = int(inserted["id"])
        return list(records)

    def list_session_events(
        self, conversation_id: str, *, kinds: Sequence[str] | None = None
    ) -> list[SessionEventRecord]:
        sql = "SELECT * FROM session_events WHERE conversation_id = %s"
        params: list[object] = [conversation_id]
        if kinds:
            # `= ANY(%s)` 而不是拼 IN 占位符：kind 的个数由调用方给，
            # 拼字符串就又多一处"把外部值拼进 SQL"的机会。
            sql += " AND kind = ANY(%s)"
            params.append(list(kinds))
        # **按 seq 排序，不按 created_at**：同一毫秒里的一批并发工具调用
        # 靠时间戳分不出先后（见 `SessionEventRecord.seq`）。
        sql += " ORDER BY seq"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._session_event_from_row(row) for row in rows]

    @staticmethod
    def _session_event_from_row(row: dict) -> SessionEventRecord:
        return SessionEventRecord(
            id=int(row["id"]),
            conversation_id=row["conversation_id"],
            seq=int(row["seq"]),
            kind=row["kind"],
            payload=dict(row["payload"]),
            created_at=_load(row["created_at"]),
        )

    def list_messages(self, conversation_id: str) -> list[ChatMessageRecord]:
        # SQLite 用 `rowid` 做同 created_at 时的插入序兜底；PG 没有 rowid。
        # 这里用系统列 `ctid` 近似"物理插入顺序"——对话消息基本只追加
        # （「重新生成」会删掉尾部再追加），ctid 递增即插入序。
        # 不能完全等价于 rowid：UPDATE 会改变 ctid，但这张表不改消息内容。
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE conversation_id = %s ORDER BY created_at, ctid",
                (conversation_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return int(row["n"])

    def create_webhook(self, record: WebhookRecord) -> WebhookRecord:
        with self._db.session() as conn:
            conn.execute(
                "INSERT INTO webhooks (id, url, events, secret, enabled)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.url,
                    _json(list(record.events)),
                    record.secret,
                    record.enabled,
                ),
            )
        return record

    def list_webhooks(self) -> list[WebhookRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM webhooks ORDER BY id").fetchall()
        return [_webhook_of(row) for row in rows]

    def get_webhook(self, webhook_id: str) -> WebhookRecord | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM webhooks WHERE id = %s", (webhook_id,)).fetchone()
        return _webhook_of(row) if row else None

    def set_webhook_enabled(self, webhook_id: str, enabled: bool) -> WebhookRecord | None:
        with self._db.session() as conn:
            conn.execute("UPDATE webhooks SET enabled = %s WHERE id = %s", (enabled, webhook_id))
        return self.get_webhook(webhook_id)

    def delete_webhook(self, webhook_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM webhooks WHERE id = %s", (webhook_id,))

    # ------------------------------------------------------------------ 回收站

    def add_to_trash(self, record: TrashRecord) -> None:
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO trash (id, document_id, kind, storage_path, expires_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.document_id,
                    record.kind.value,
                    record.storage_path,
                    _dump(record.expires_at),
                    _dump(record.created_at),
                ),
            )

    def list_trash(self) -> list[TrashRecord]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM trash ORDER BY created_at").fetchall()
        return [
            TrashRecord(
                id=row["id"],
                document_id=row["document_id"],
                kind=TrashKind(row["kind"]),
                storage_path=row["storage_path"],
                expires_at=_load(row["expires_at"]),
                created_at=_load(row["created_at"]),
            )
            for row in rows
        ]

    def set_trash_expiry(self, trash_id: str, expires_at: datetime) -> None:
        with self._db.session() as conn:
            conn.execute(
                "UPDATE trash SET expires_at = %s WHERE id = %s",
                (_dump(expires_at), trash_id),
            )

    def delete_trash(self, trash_id: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM trash WHERE id = %s", (trash_id,))

    def purge_expired_trash(self, *, now: datetime | None = None) -> list[TrashRecord]:
        """到期清理：先取出待删清单（供调用方删文件），再在**同一事务**里删记录。"""
        moment = now or _now()
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM trash WHERE expires_at <= %s ORDER BY expires_at", (moment,)
            ).fetchall()
            conn.execute("DELETE FROM trash WHERE expires_at <= %s", (moment,))
        return [
            TrashRecord(
                id=row["id"],
                document_id=row["document_id"],
                kind=TrashKind(row["kind"]),
                storage_path=row["storage_path"],
                expires_at=_load(row["expires_at"]),
                created_at=_load(row["created_at"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 设置

    def get_setting(self, key: str) -> str | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else None

    def get_settings(self, keys: Sequence[str]) -> dict[str, str]:
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        sql = (
            "SELECT key, value FROM app_settings"  # noqa: S608
            f" WHERE key IN ({_placeholders(len(wanted))})"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, tuple(wanted)).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, value, _dump(_now())),
            )

    def delete_setting(self, key: str) -> None:
        with self._db.session() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = %s", (key,))

    # ------------------------------------------------------------------ 行映射

    @staticmethod
    def _images_by_chunk(conn: Connection, chunk_ids: Sequence[str]) -> dict[str, list[str]]:
        if not chunk_ids:
            return {}
        placeholders = _placeholders(len(chunk_ids))

        # 这是 PG 里 IN (%s, %s, ...) 的标准写法，不存在拼接注入面。
        rows = conn.execute(
            "SELECT chunk_id, image_id FROM chunk_images "  # noqa: S608
            f"WHERE chunk_id IN ({placeholders})",
            list(chunk_ids),
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["chunk_id"], []).append(row["image_id"])
        return grouped

    @staticmethod
    def _kb_from_row(row: dict) -> KnowledgeBaseRecord:
        return KnowledgeBaseRecord(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            embedding_model_id=row["embedding_model_id"],
            embedding_dim=row["embedding_dim"],
            embedding_base_url=row["embedding_base_url"],
            embedding_model_pk=row["embedding_model_pk"],
            chunk_strategy=row["chunk_strategy"],
            chunk_size=row["chunk_size"],
            chunk_overlap=row["chunk_overlap"],
            suggested_enabled=bool(row["suggested_enabled"]),
            suggested_count=row["suggested_count"],
            wiki_enabled=bool(row["wiki_enabled"]),
            suggested_model_pk=row["suggested_model_pk"],
            suggested_prompt=row["suggested_prompt"],
            system_prompt=row["system_prompt"],
            owner_id=row["owner_id"],
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _document_from_row(row: dict) -> DocumentRecord:
        return DocumentRecord(
            id=row["id"],
            knowledge_base_id=row["knowledge_base_id"],
            name=row["name"],
            source_kind=DataSourceKind(row["source_kind"]),
            content_hash=row["content_hash"],
            stage=DocumentStage(row["stage"]),
            size_bytes=row["size_bytes"],
            mime_type=row["mime_type"],
            page_count=row["page_count"],
            is_split=bool(row["is_split"]),
            error=row["error"],
            uploaded_by=row["uploaded_by"],
            folder_id=row["folder_id"],
            disabled=bool(row["disabled"]),
            summary=row.get("summary") or "",
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )

    @staticmethod
    def _task_from_row(row: dict) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            kind=TaskKind(row["kind"]),
            state=TaskState(row["state"]),
            payload=row["payload"],
            document_id=row["document_id"],
            part_id=row["part_id"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            lease_owner=row["lease_owner"],
            lease_expires_at=_load(row["lease_expires_at"]),
            next_run_at=_load(row["next_run_at"]),
            error=row["error"],
            created_at=_load(row["created_at"]),
            updated_at=_load(row["updated_at"]),
        )
