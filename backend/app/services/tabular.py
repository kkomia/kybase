"""表格数据：结构化副本与检索用文本（M2 / T2.11）。

**为什么表格要单独处理**：CSV/Excel 走纯文本直通时会被当成一坨文本切块，
于是"张三的年龄是多少"这类问题只能靠关键词撞运气——**表的结构信息（哪一列是什么）
在切块时就丢了**。

T2.11 的做法是**双写**：

- **行文本进向量库**：每行渲染成可读文本，成为可检索的块；
- **结构化副本进 DuckDB**：原始行列原样保留，供精确查询与导出。

**关于 D5（SQL 旁路是否进 MVP）**——计划里记的是"架构说缓做，但 T2.11 要写结构化副本"，
两者其实不矛盾，本模块按这个区分落地：

- **结构化副本进 MVP**（T2.11 明文要求）：它是入库时的顺手产物，几乎零成本；
- **完整的 SQL 旁路不进 MVP**（架构 §5.2 的"缓做"）：不提供用户自定义 SQL 的开放入口；
- 但**提供一条受控的只读查询**（`query_rows`）——因为"写进 DuckDB 却查不到"
  等于没写；而受控查询的代价仅是白名单校验，不是一整套 SQL 服务。

于是 D5 消解为：**副本写、查询受控、开放 SQL 旁路仍缓做**。

**格式解析已搬到 `app/parsers/tabular_format.py`**（v0.12 review 的分层修正）：
那几个纯函数（解码 / 解析行列 / 渲染检索文本）同时被 `parsers/tabular.py` 与
`parsers/plain_text.py` 需要，留在这里会让解析器反向 import 业务层。
本模块保留 `TabularService`（编排：解析 + 写结构化副本），
并从新位置**重新导出**那些名字，既有调用点无需改动。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.core.exceptions import InvalidRequestError
from app.parsers.tabular_format import (
    MAX_COLUMNS,
    MAX_ROWS,
    TABULAR_EXTENSIONS,
    TabularParse,
    parse_tabular,
    rows_to_text,
)
from app.services.tabular_sql import check_tables, validate_select
from app.storage.base import StoreBundle

__all__ = [
    "MAX_COLUMNS",
    "MAX_ROWS",
    "MAX_SQL_ROWS",
    "TABULAR_EXTENSIONS",
    "TabularParse",
    "TabularService",
    "parse_tabular",
    "rows_to_text",
]

logger = logging.getLogger(__name__)

#: SQL 一次最多回多少行。**这不是"能取多少"的上限，而是"进上下文"的上限**：
#: 结果会原样进模型的上下文，几百行明细既贵又没用——要精确的答案该让 SQL
#: 自己算出来（sum / count / group by），而不是把明细拉回来让模型数。
MAX_SQL_ROWS = 500


class TabularService:
    """读表格、渲染检索文本、写结构化副本。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 读

    def parse(self, *, filename: str, content: bytes) -> TabularParse:
        """把上传的表格文件读成行列（转发到解析器层的纯函数，见其说明）。"""
        return parse_tabular(filename=filename, content=content)

    # ------------------------------------------------------------------ 写

    def store(self, *, document_id: str, parsed: TabularParse) -> int:
        """把结构化副本写进 DuckDB，返回写入行数。

        表名用 ``document_id``：一份文档一张表，互不干扰，删文档时也容易连带清理。
        """
        if not parsed.rows:
            return 0
        return self._stores.tabular.write_table(
            table=document_id, columns=parsed.columns, rows=parsed.rows
        )

    def drop(self, *, document_id: str) -> None:
        """删掉一份文档的结构化副本（删文档时调用）。"""
        self._stores.tabular.drop_table(document_id)

    # ------------------------------------------------------------------ 查询

    def query_rows(
        self, *, document_id: str, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        """读一份文档的结构化副本。

        **这是"受控查询"，不是 SQL 旁路**：调用方给的是文档 id + 分页参数，
        不是 SQL。架构 §5.2 把开放 SQL 列为缓做，这里遵守；
        但"写进 DuckDB 却查不到"等于没写，所以留这一条只读通路。
        """
        table = document_id
        if not self._stores.tabular.table_exists(table):
            raise InvalidRequestError(
                "这份文档没有结构化副本（只有 CSV/Excel 才会生成）。"
                "它可能是 PDF、Markdown 等非表格文档"
            )
        safe_limit = max(1, min(500, limit))
        return {
            "document_id": document_id,
            "columns": self._stores.tabular.columns(table),
            "rows": self._stores.tabular.read_rows(table, limit=safe_limit, offset=max(0, offset)),
            "total": self._stores.tabular.row_count(table),
        }

    # ------------------------------------------------------- SQL（v0.33，给 Agent）

    def tables(self, *, kb_ids: Sequence[str] | None = None) -> list[dict[str, object]]:
        """有结构化副本的表格文档（可按库范围过滤）。

        表名就是 ``document_id``，所以"库里有哪些表"这件事只能靠
        **两份数据对起来**：副本库里的表名列表 ∩ 这些文档的所属库。反过来
        （先列文档再逐个问 table_exists）是 N 次查询，而这一份是两次。
        """
        names = self._stores.tabular.list_tables()
        if not names:
            return []
        documents = self._stores.meta.get_documents_by_ids(names)
        scope = {str(item) for item in (kb_ids or [])} if kb_ids is not None else None
        items: list[dict[str, object]] = []
        for name in names:
            record = documents.get(name)
            if record is None:
                # 表在、文档没了：摄入被中途取消/文档被删而副本还没清掉。
                # 这种表**不进列表**——模型看到它、查它，会得到一份没有来源的数据
                continue
            if scope is not None and record.knowledge_base_id not in scope:
                continue
            items.append(
                {
                    "document_id": record.id,
                    "name": record.name,
                    "knowledge_base_id": record.knowledge_base_id,
                    "columns": self._stores.tabular.columns(name),
                    "rows": self._stores.tabular.row_count(name),
                }
            )
        return items

    def query_sql(
        self, *, sql: str, kb_ids: Sequence[str] | None = None, limit: int = 100
    ) -> dict[str, object]:
        """跑一条**只读** SQL（校验与范围判定见 ``services/tabular_sql.py``）。

        返回形状与 ``query_rows`` 保持一致（columns / rows / total），
        界面与调用方不必区分"按文档读"与"按 SQL 查"。
        """
        allowed_items = self.tables(kb_ids=kb_ids)
        allowed = {str(item["document_id"]) for item in allowed_items}
        if not allowed:
            raise InvalidRequestError(
                "这一轮没有可查的表格（知识库关着、没选库，或者那些库里没有 CSV / Excel）。"
                "表格文档要先入库处理完才能被查询"
            )
        text = validate_select(sql)
        check_tables(text, known=self._stores.tabular.list_tables(), allowed=allowed)
        safe_limit = max(1, min(MAX_SQL_ROWS, limit))
        columns, rows = self._stores.tabular.run_select(text, max_rows=safe_limit)
        return {
            "sql": text,
            "columns": columns,
            "rows": rows,
            "total": len(rows),
            "truncated": len(rows) >= safe_limit,
            "tables": [
                {"document_id": item["document_id"], "name": item["name"]} for item in allowed_items
            ],
            "note": (
                f"最多回 {safe_limit} 行，可能还有更多——要精确的值请让 SQL 自己算出"
                "一行（sum / count / group by），而不是把明细拉回来再数"
            ),
        }
