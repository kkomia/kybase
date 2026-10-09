"""``VectorStore`` 的 PostgreSQL + pgvector 实现（v0.12）。

按知识库分区：每个库一张 ``vec_<kb_id>`` 表，**维度在分区创建时确定**——
与 SQLite 版同一模型（架构 §8.3 / D4）；pgvector 的列维度是固定的，
一列装不了多种维度，所以"一库一表"在这里不是妥协而是必然。

**分区即真相，不建登记表**：维度读 ``pg_attribute.atttypmod``、
分区列表读 ``pg_class``。另建一张登记表会引入第二处事实来源——
建表成功而登记失败、drop 了却忘了删行，两种漂移都要额外代码兜。
（这也是 sqlite_impl 里"不另建分区表、只认建表语句"的同一个判断。）

**与 sqlite-vec 的机制差异**：

- sqlite-vec 是**暴力扫描**（无 ANN 索引），这里是 **HNSW 近似索引**——
  百万级下这是数量级的差别，代价是结果为近似（``ef_search`` 越大越准越慢）。
- ``vec0`` 不支持 ``INSERT OR REPLACE``（原实现只能先删后插），
  PG 一条 ``ON CONFLICT DO UPDATE`` 就够，批内不会有中间空洞。
- 距离一律用余弦（``<=>``），与 embedding 的常规用法一致；
  索引也必须按 ``vector_cosine_ops`` 建，否则算子与索引不匹配、退化成顺序扫描。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 只在注解里用到（``_declared_dim(conn: Connection, …)`` ✓）：`from __future__ import
    # annotations` 之下注解不求值 ✓，所以这一句**不进运行时**、也就不进客户端的导入闭包 ✓
    # （边车不连 PostgreSQL ✓）。
    from psycopg import Connection

from app.storage.base import VectorDimensionMismatch, VectorMatch, VectorStore
from app.storage.postgres_impl.connection import Database

logger = logging.getLogger(__name__)

_TABLE_PREFIX = "vec_"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""知识库 ID 会拼进表名（SQL 无法参数化标识符），因此必须白名单校验。"""

HNSW_M = 16
"""HNSW 每节点连边数。索引构建参数，建索引时定死。"""

HNSW_EF_CONSTRUCTION = 64
"""HNSW 构建期的候选队列长度。越大索引质量越好、构建越慢。"""

EF_SEARCH = 100
"""查询期候选队列长度。**每次检索逐条设置**（``SET LOCAL``），
因为它直接决定"召回率 ↔ 延迟"这组权衡，且不同查询可以不同——
把它钉死在索引里反而失去了按查询调节的余地。"""

HNSW_MAX_DIM = 2000
"""HNSW 索引能建的最大维度（pgvector 的硬限制）。

超过它时 ``CREATE INDEX ... USING hnsw`` 直接失败——实测 2048 维的原文是
``column cannot have more than 2000 dimensions for hnsw index``。
**这不代表那个库不能用**：分区照建、写入与检索照常，只是没有索引、走精确扫描；
小库完全没问题，大库会明显变慢。所以这里不报错、只如实说清
（见 `ensure_partition` 的返回值：那句话必须让用户看到）。
"""

_DIM_SQL = """
select a.atttypmod as dim
  from pg_attribute a
  join pg_class c on c.oid = a.attrelid
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = current_schema()
   and c.relname = %s
   and a.attname = 'embedding'
   and a.attnum > 0
   and not a.attisdropped
"""


def _literal(vector: Sequence[float]) -> str:
    """把向量渲染成 pgvector 的文本输入（``[1,2,3]``）。

    用 ``repr`` 而不是 ``%.6f``：量化前多丢一位精度都会影响召回，
    而文本解析这点开销在批量写入里占比很低（pgvector 的文本解析是 C 实现）。
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class PostgresVectorStore(VectorStore):
    """向量仓储的 pgvector 实现。"""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------ 分区

    def ensure_partition(self, kb_id: str, *, dim: int) -> str | None:
        """建分区；**维度超过 HNSW 上限时只建表、不建索引**，并回一句要告诉用户的话。

        为什么不是报错：那个库仍然完全可用（写入、检索、余弦排序都对），只是**精确扫描**。
        把它拦下来等于"因为建不了索引就不许用 2048 维"——那是我们的限制，不是用户的问题。

        为什么必须说清：不说的话用户会以为索引建好了、只是这次慢，然后在几十万条之后
        才发现每一次检索都在全表扫。返回值与日志**两处都给**：返回给调用方去落（界面那面
        迟早要显示），日志给当时就在看服务端的人。
        """
        if dim <= 0:
            raise ValueError("向量维度必须为正整数")
        table = self._table(kb_id)

        with self._db.session() as conn:
            existing = self._declared_dim(conn, table)
            if existing is None:
                conn.execute(
                    f"CREATE TABLE {table} ("
                    f"  chunk_id text PRIMARY KEY,"
                    f"  embedding vector({int(dim)}) NOT NULL)"
                )
                if dim > HNSW_MAX_DIM:
                    advisory = (
                        f"知识库 {kb_id} 的向量维度是 {dim}，超过向量索引上限 "
                        f"（pgvector 的 HNSW 最多 {HNSW_MAX_DIM} 维）：这个库**没有向量索引**，"
                        "检索走精确扫描——数据量小时没差别，数据量大时会明显变慢。"
                        "想用索引就把嵌入模型的维度改成 1024（支持 Matryoshka 的模型会取前 "
                        "1024 维并重新归一化，检索结果仍然正确），然后新建一个库。"
                    )
                    logger.warning("%s", advisory)
                    return advisory
                # 索引名由表名派生；kb_id 白名单限长 64，极端长度下 PG 会截断
                # 标识符（63 字节），实际 ID 远短于此
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} "
                    f"USING hnsw (embedding vector_cosine_ops) "
                    f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
                )
            elif existing != dim:
                raise VectorDimensionMismatch(
                    f"知识库 {kb_id} 的向量分区已是 {existing} 维，"
                    f"不能按 {dim} 维写入（架构 §6.4：维度不同不可混用）"
                )
        return None

    def has_hnsw_index(self, kb_id: str) -> bool:
        """这个库到底有没有 HNSW 索引。

        "没有索引"是**要告诉用户的事**（见 `ensure_partition` 的返回值），所以它得能被
        查证：用例据此钉住">2000 维不建索引"，将来界面/运维页也可以直接显示"检索方式"。
        分区不存在时返回 False。
        """
        with self._db.read() as conn:
            row = conn.execute(
                "select 1 from pg_indexes"
                " where schemaname = current_schema() and tablename = %s"
                "   and indexdef ilike %s",
                (self._table(kb_id), "%using hnsw%"),
            ).fetchone()
        return row is not None

    def drop_partition(self, kb_id: str) -> None:
        with self._db.session() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {self._table(kb_id)}")

    def list_partitions(self) -> list[str]:
        """列出已有的向量分区（返回知识库 id）。

        **维护页据此识别孤儿分区**：删库时若没跟着 drop，表会永远留着，
        而它只能从系统目录发现（``knowledge_bases`` 里已经没有它了）。

        用 ``starts_with`` 而不是 ``LIKE 'vec_%'``：``_`` 在 LIKE 里是通配符，
        会顺带匹配 ``vecX…`` 这类不相干的表名。
        """
        with self._db.read() as conn:
            rows = conn.execute(
                "select c.relname as name from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = current_schema() and c.relkind = 'r' "
                "and starts_with(c.relname, %s)",
                (_TABLE_PREFIX,),
            ).fetchall()
        return [row["name"][len(_TABLE_PREFIX) :] for row in rows]

    def declared_dim(self, kb_id: str) -> int | None:
        """读取分区实际维度；未建分区返回 None。供上层做模型/维度校验。"""
        with self._db.read() as conn:
            return self._declared_dim(conn, self._table(kb_id))

    # ------------------------------------------------------------------ 写入

    def upsert_vectors(
        self, kb_id: str, *, items: Sequence[tuple[str, Sequence[float]]]
    ) -> None:
        if not items:
            return
        table = self._table(kb_id)
        with self._db.session() as conn:
            dim = self._declared_dim(conn, table)
            if dim is None:
                raise VectorDimensionMismatch(f"知识库 {kb_id} 尚未建立向量分区")

            for chunk_id, vector in items:
                if len(vector) != dim:
                    raise VectorDimensionMismatch(
                        f"chunk {chunk_id} 的向量是 {len(vector)} 维，分区是 {dim} 维"
                    )

            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} (chunk_id, embedding) VALUES (%s, %s::vector) "  # noqa: S608
                    "ON CONFLICT (chunk_id) DO UPDATE SET embedding = excluded.embedding",
                    [(chunk_id, _literal(vector)) for chunk_id, vector in items],
                )

    def delete_vectors(self, kb_id: str, *, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        table = self._table(kb_id)
        with self._db.session() as conn:
            if self._declared_dim(conn, table) is None:
                return 0
            cur = conn.execute(
                f"DELETE FROM {table} WHERE chunk_id = ANY(%s)",  # noqa: S608
                (list(chunk_ids),),
            )
            deleted = cur.rowcount
        return deleted if deleted and deleted > 0 else 0

    # ------------------------------------------------------------------ 检索

    def search(
        self, kb_id: str, *, query_vector: Sequence[float], top_k: int
    ) -> list[VectorMatch]:
        if top_k <= 0:
            return []
        table = self._table(kb_id)
        # 用 session 而不是 read：SET LOCAL 只作用于当前事务，需要显式事务边界
        with self._db.session() as conn:
            dim = self._declared_dim(conn, table)
            if dim is None:
                return []
            if len(query_vector) != dim:
                raise VectorDimensionMismatch(
                    f"查询向量 {len(query_vector)} 维，与知识库 {kb_id} 的 {dim} 维分区不符"
                )
            # SET 不接受绑定参数（psycopg 用扩展协议发过去会直接语法错），只能内联；
            # EF_SEARCH 是本模块常量、不是外部输入，内联没有注入面
            conn.execute(f"SET LOCAL hnsw.ef_search = {int(EF_SEARCH)}")
            literal = _literal(query_vector)
            rows = conn.execute(
                f"SELECT chunk_id, embedding <=> %s::vector AS distance "  # noqa: S608
                f"FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s",
                (literal, literal, top_k),
            ).fetchall()
        return [VectorMatch(chunk_id=row["chunk_id"], distance=row["distance"]) for row in rows]

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _table(kb_id: str) -> str:
        if not _SAFE_ID.match(kb_id):
            raise ValueError(f"非法知识库 ID：{kb_id!r}（仅允许字母数字下划线连字符）")
        return f"{_TABLE_PREFIX}{kb_id}"

    @staticmethod
    def _declared_dim(conn: Connection, table: str) -> int | None:
        """从系统目录读分区声明的维度。

        pgvector 把维度存在列的 ``atttypmod`` 里，所以无需解析建表语句、
        也无需另建登记表——分区本身就是唯一的事实来源。
        """
        row = conn.execute(_DIM_SQL, (table,)).fetchone()
        if row is None or row["dim"] is None or row["dim"] < 0:
            return None
        return int(row["dim"])
