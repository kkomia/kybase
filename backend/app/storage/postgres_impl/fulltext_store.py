"""``FullTextStore`` 的 PostgreSQL 实现（v0.12）。

与 SQLite 版的两处结构差异，都是 PG 让设计变简单的地方：

1. **没有独立的全文表**。原实现是 ``chunks_fts`` 虚拟表 + ``chunks`` 两张表、
   查询要 JOIN；这里 ``tokens`` 就是 ``chunks`` 上的一个**生成列**
   （``to_tsvector('simple', tokens_text)``，见 schema.sql），
   写入只更新 ``tokens_text``，索引由数据库自己维护。
2. **中文仍然不需要数据库扩展**。沿用同一套 jieba 预切词
   （``storage/text.py``，两个实现共用，避免切法漂移），把切好的词元串写进
   ``tokens_text``，生成列再把它变成 tsvector。所以不必装 zhparser。

排序函数换了，方向没换：SQLite 用 ``-bm25``（取负让"越大越相关"），
这里用 ``ts_rank_cd``（本身就是越大越相关）。RRF 融合只看名次
（``services/retrieval/fusion.py`` 按位置计分），所以分数量纲变化不影响融合——
但**方向必须一致**，否则召回顺序会整体反过来。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from app.storage.base import ChunkRecord, FullTextStore, SearchHit
from app.storage.postgres_impl.connection import Database
from app.storage.text import cut, tokenize

#: 惰性拿到的 jieba（**故意不是模块级导入** ✗）。
_JIEBA: Any = None


def _jieba_cache_file() -> str | None:
    """jieba 的缓存该落在**数据目录**，而不是进程的 cwd。

    为什么必须显式设：jieba 的 ``cache_file`` 默认是 ``None``，此时它用
    ``os.path.join(tmp_dir or tempfile.gettempdir(), "jieba.cache")`` ——
    而本机沙箱**拒写 %TEMP%**，``tempfile.gettempdir()`` 会静默回退到 **cwd**，
    于是跑一次检索用例，仓库根就冒出一个 ``jieba.cache``（约 9 MB，实测过）。

    **行为不变**：缓存里只是前缀词典的概率表，谁写都一样 —— 切词结果一字不差，
    只是它不再落在 cwd。取不到数据目录时返回 ``None``（退回 jieba 自己的默认）：
    别为了一个缓存路径让切词挂掉。
    """
    try:
        from pathlib import Path

        from app.core.config import get_settings

        data_dir = Path(get_settings().data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / "jieba.cache")
    except Exception:
        return None


def _jieba() -> Any:
    """第一次用到时才导入 jieba（模块级导入会让**客户端运行时**多背 40.9 MB ✗）。

    与 ``app/storage/text.py::_jieba``、``app/services/retrieval/coverage.py::_jieba``、
    ``app/services/embedding/deterministic.py::_jieba`` 同一个手法 ✓：这个模块挂在
    ``app.storage`` 的导入链上 ✓，而"预热词典"只有**服务器**那条全文检索链会做 ✓ ——
    边车（客户端运行时）只跑循环 + 工具 + 沙箱 ✓，从不建全文索引。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，jieba 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _JIEBA
    if _JIEBA is None:
        import jieba

        # 缓存落哪儿：**数据目录**，不是 cwd（见 `_jieba_cache_file()` 的说明）
        cache = _jieba_cache_file()
        if cache:
            jieba.dt.cache_file = cache

        _JIEBA = jieba
    return _JIEBA

logger = logging.getLogger(__name__)

DEFAULT_SLOW_QUERY_MS = 500
"""超过该耗时的查询留一条 WARNING（架构 §12 可观测性）。构造时可覆盖，便于测试。"""


def to_tsquery_text(text: str) -> str:
    """把用户输入转成 tsquery 表达式：词元加单引号、用 ``|`` 连接。

    与 FTS5 那版同样是"OR 连接、召回优先"（精度交给 RRF 与可选 rerank），
    但语法不同：tsquery 里的引号词元写成 ``'词'``，``|`` 才是 OR（``&`` 是 AND）。

    **必须加引号**：词元里可能出现 ``&``、``!``、``(``、``:`` 这些 tsquery 的
    语法字符（jieba 会把它们切出来），不加引号就是一句语法错误——
    用户的查询内容不该有能力让检索崩掉。词元内的单引号按 SQL 惯例双写转义。

    **不能用 plainto_tsquery / websearch_to_tsquery**：它们按空格做 AND，
    而我们的词元是 jieba 切出来的、本就同属一个语义单元，AND 会把召回掐得太死。
    """
    tokens = cut(text)
    if not tokens:
        return ""
    quoted = ["'" + token.replace("'", "''") + "'" for token in tokens]
    return " | ".join(quoted)


class PostgresFullTextStore(FullTextStore):
    """全文检索的 PostgreSQL 实现。"""

    def __init__(self, database: Database, *, slow_query_ms: int = DEFAULT_SLOW_QUERY_MS) -> None:
        self._db = database
        self._slow_query_ms = slow_query_ms
        self._jieba_ready = False

    def _ensure_jieba(self) -> None:
        """jieba 首次调用要加载词典（约 1 秒），预热一次后续都快。"""
        if not self._jieba_ready:
            _jieba().initialize()
            self._jieba_ready = True

    # ------------------------------------------------------------------ 写入

    def index_chunks(self, chunks: Sequence[ChunkRecord]) -> None:
        if not chunks:
            return
        self._ensure_jieba()
        with self._db.session() as conn, conn.cursor() as cur:
            # tokens_text 就是 chunks 上的普通列，直接覆盖即可（生成列随之更新），
            # 没有"先删后插"的必要
            #
            # 索引的是 **index_text**（原文 + 该段生成的问题，见 ChunkRecord）：
            # 问题的用词因此也进了全文索引，"换个问法"能靠相关性排序命中原段。
            # 这是"分段问题提升召回"的全文那一半，向量那一半在 ingest 用同一属性。
            cur.executemany(
                "UPDATE chunks SET tokens_text = %s WHERE chunk_id = %s",
                [(tokenize(chunk.index_text), chunk.chunk_id) for chunk in chunks],
            )

    def delete_chunks(self, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        with self._db.session() as conn:
            # 把词元清空而不是删行：chunks 的行归 MetaStore 管，
            # 全文索引只是它的一列。删行会让"全文先删、元数据后删"的顺序里
            # 中间态变成"整段没了"，而清空词元只是"搜不到"，语义更窄。
            cur = conn.execute(
                "UPDATE chunks SET tokens_text = '' WHERE chunk_id = ANY(%s)",
                (list(chunk_ids),),
            )
            removed = cur.rowcount
        return removed if removed and removed > 0 else 0

    # ------------------------------------------------------------------ 检索

    def search(self, *, query: str, top_k: int, kb_id: str | None = None) -> list[SearchHit]:
        """全文检索。

        **JOIN documents 并要求 ``disabled = false``**：文档级停用在 SQL 里就裁掉，
        不浪费 top_k 名额——与 SQLite 版同一条纪律，否则会出现"全文搜不到、
        向量搜得到"的静默不一致。

        ``chunk`` 级的 disabled 不在这一层过滤（与 SQLite 版一致）：
        留给下游统一处理，两个通道的口径必须一样。
        """
        if top_k <= 0:
            return []
        self._ensure_jieba()
        tsquery = to_tsquery_text(query)
        if not tsquery:
            return []

        started = time.perf_counter()
        sql = [
            "SELECT c.chunk_id, c.document_id, c.knowledge_base_id, c.text,",
            "       c.heading_path, c.page,",
            "       ts_rank_cd(c.tokens, to_tsquery('simple', %s)) AS rank",
            "  FROM chunks AS c",
            "  JOIN documents AS d ON d.id = c.document_id AND d.disabled = false",
            " WHERE c.tokens @@ to_tsquery('simple', %s)",
        ]
        params: list[object] = [tsquery, tsquery]
        if kb_id is not None:
            sql.append("   AND c.knowledge_base_id = %s")
            params.append(kb_id)
        sql.append(" ORDER BY rank DESC, c.chunk_id LIMIT %s")
        params.append(top_k)

        with self._db.read() as conn:
            rows = conn.execute("\n".join(sql), params).fetchall()
            images = self._images_by_chunk(conn, [row["chunk_id"] for row in rows])
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > self._slow_query_ms:
            logger.warning("全文检索慢查询 %.0fms：%r", elapsed_ms, query)

        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                text=row["text"],
                score=float(row["rank"]),  # ts_rank_cd 本身就越大越相关
                source="fulltext",
                page=row["page"],
                heading_path=row["heading_path"],
                image_ids=tuple(images.get(row["chunk_id"], ())),
            )
            for row in rows
        ]

    @staticmethod
    def _images_by_chunk(conn, chunk_ids: Sequence[str]) -> dict[str, list[str]]:
        if not chunk_ids:
            return {}
        rows = conn.execute(
            "SELECT chunk_id, image_id FROM chunk_images WHERE chunk_id = ANY(%s)",
            (list(chunk_ids),),
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["chunk_id"], []).append(row["image_id"])
        return grouped
