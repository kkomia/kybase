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
    # 只在注解里用到（``_executemany(conn: Connection, …)`` / ``_json(value) -> Jsonb`` √）：
    # `from __future__ import annotations` 之下注解不求值 √，所以这两句**不进运行时**、
    # 也就不进客户端的导入闭包 √。
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
    ChunkRecord,
    DataSourceRecord,
    DocumentPartRecord,
    DocumentRecord,
    DocumentStageEventRecord,
    DocumentStatRow,
    FolderRecord,
    IdempotencyRecord,
    ImageRecord,
    KnowledgeBaseRecord,
    MetaStore,
    ModelProviderRecord,
    ParseResultRecord,
    RegisteredModelRecord,
    SearchEventRecord,
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
)
from app.storage.postgres_impl.connection import Database

#: 惰性拿到的 psycopg（**故意不是模块级导入** ×）。
_PSYCOPG: Any = None


def _psycopg() -> Any:
    """第一次真用 psycopg 时才导入它（模块级导入会让**客户端运行时**凭空多背一份 ×）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 √，而"连 PostgreSQL"只有
    **服务器**那条链会做 √ —— 旧版客户端（客户端运行时）只跑循环 + 工具 + 沙箱 √，
    它的会话权威与库都在**远端**（走 HTTP √，不走 psycopg）。

    **行为一个字没变** ×：第一次调用时才导入 √，psycopg 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` √（只是从导入期挪到了调用期 √）。
    """
    global _PSYCOPG
    if _PSYCOPG is None:
        import psycopg

        _PSYCOPG = psycopg
    return _PSYCOPG


def _jsonb() -> Any:
    """``psycopg.types.json.Jsonb``（每写一个 jsonb 列都要用它包一层 √）。"""
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
        """架构 6.3 的文件级去重：同库同 hash 只应有一份。"""
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

    def delete_parse_result(self, document_id: str) -> int:
        # 按 document_id 一刀删（含分段产物）：大文件切分时每段一行 part_id，
        # 只删 part_id 为空那行会让下一轮的 `_parse_in_parts` 撞上旧段产物。
        with self._db.session() as conn:
            cursor = conn.execute(
                "DELETE FROM parse_results WHERE document_id = %s", (document_id,)
            )
        return int(cursor.rowcount or 0)

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
        """只落哈希：明文 key 永不进库（架构 3.2）。"""
        record.created_at = record.created_at or _now()
        with self._db.session() as conn:
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, name, key_hash, permission, knowledge_base_ids, allowed_tool_names,
                     key_prefix, created_by, created_at, last_used_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.name,
                    record.key_hash,
                    record.permission.value,
                    _json(list(record.knowledge_base_ids)),
                    _json(list(record.allowed_tool_names)),
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
            allowed_tool_names=tuple(row["allowed_tool_names"]),
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
        库 owner、API Key 创建者）一律置空，界面显示"未记录"。
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

    def claim_legacy_ownership(self, owner_id: str) -> int:
        with self._db.session() as conn:
            claimed = conn.execute(
                "UPDATE knowledge_bases SET owner_id = %s WHERE owner_id IS NULL", (owner_id,)
            ).rowcount
        return claimed or 0

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

    @staticmethod
    def _search_event_from_row(row: dict) -> SearchEventRecord:
        return SearchEventRecord(
            id=row["id"],
            kb_id=row["kb_id"],
            candidates=row["candidates"],
            kept=row["kept"],
            fit=row["fit"],
            created_at=_load(row["created_at"]),
        )

    def record_search_events(self, records: Sequence[SearchEventRecord]) -> None:
        if not records:
            return
        with self._db.session() as conn:
            _executemany(
                conn,
                "INSERT INTO search_events (id, kb_id, candidates, kept, fit, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (
                        record.id,
                        record.kb_id,
                        record.candidates,
                        record.kept,
                        record.fit,
                        _dump(record.created_at or _now()),
                    )
                    for record in records
                ],
            )

    def list_search_events(self, *, since: datetime | None = None) -> list[SearchEventRecord]:
        sql = "SELECT * FROM search_events"
        params: tuple[object, ...] = ()
        if since is not None:
            sql += " WHERE created_at >= %s"
            params = (_dump(since),)
        sql += " ORDER BY created_at"
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._search_event_from_row(row) for row in rows]

    def purge_search_events_before(self, before: datetime) -> int:
        with self._db.session() as conn:
            cursor = conn.execute(
                "DELETE FROM search_events WHERE created_at < %s", (_dump(before),)
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
