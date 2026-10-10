"""``SearchFilter`` → SQL 条件片段的共用渲染（向量与全文两个仓储同用）。

**为什么单独一个模块**：两条通道必须筛得一模一样。口径写两遍的结局一定是一遍先漂，
而"全文搜不到、向量搜得到"这种静默不一致最难查——``fulltext_store`` 里"文档级停用
要在 SQL 里裁"那条纪律是同一个道理。

别名是**写死的常量**（``c`` = chunks、``d`` = documents）：这两个仓储的 SQL 都用这一对
别名，于是同一份片段可以原样拼进两边；也因为没有来自外部的标识符，拼接没有注入面
（值与列名都走绑定参数/固定字面量）。
"""

from __future__ import annotations

from app.storage.base import SearchFilter

#: 块表的别名。两个仓储的 SQL 都用它，片段才可以共用。
CHUNK_ALIAS = "c"

#: 文档表的别名（``chunks.document_id`` JOIN ``documents.id``）。
DOCUMENT_ALIAS = "d"


def conditions(search_filter: SearchFilter) -> tuple[list[str], list[object]]:
    """渲染成 ``(WHERE 片段, 绑定参数)``；片段之间用 ``AND`` 连接。

    只把**真的给了的条件**写进 SQL（``None`` / 空 = 不设限），不在这一层做
    "值看起来没意义就不加"的二次判断——那会让"传了却没生效"无从解释：调用方
    读到的应该是"我给的每一个条件都在 SQL 里"。

    **没有页码 / 标题路径的块被整体排除**（见 ``SearchFilter`` 的说明）：
    ``NULL >= 40`` 与 ``position(… in NULL) > 0`` 的结果都是 ``NULL`` 而不是真，
    所以 ``page_is_null`` 的块两种条件都不满足。这不是疏漏，是"我只第 40–60 页"
    这句话的应有之义。

    ``heading_contains`` 用 ``position(lower(x) in lower(heading_path))`` 而不是
    ``LIKE '%' || x || '%'``：后者的模式里 ``%`` 与 ``_`` 是通配符，用户给的
    "第 3 章" 若含这两个字符就会意外地宽匹配（而它是**子串**，不是模式）。
    """
    clauses: list[str] = []
    params: list[object] = []

    if search_filter.document_ids:
        clauses.append(f"{CHUNK_ALIAS}.document_id = ANY(%s)")
        params.append(list(search_filter.document_ids))
    if search_filter.source_kinds:
        clauses.append(f"{DOCUMENT_ALIAS}.source_kind = ANY(%s)")
        # 枚举落库是文本（``DataSourceKind`` 就是 ``str`` 子类，见 schema.sql 的
        # ``source_kind text``）：显式转一次，免得参数适配随驱动版本变行为
        params.append([str(kind) for kind in search_filter.source_kinds])
    if search_filter.created_after is not None:
        # 与 ``_passes_filters`` 的边界口径一致：条件本身**含**边界值
        # （那一侧写的是 ``created_at < after`` 才丢掉）
        clauses.append(f"{DOCUMENT_ALIAS}.created_at >= %s")
        params.append(search_filter.created_after)
    if search_filter.created_before is not None:
        clauses.append(f"{DOCUMENT_ALIAS}.created_at <= %s")
        params.append(search_filter.created_before)
    if search_filter.page_from is not None:
        clauses.append(f"{CHUNK_ALIAS}.page >= %s")
        params.append(int(search_filter.page_from))
    if search_filter.page_to is not None:
        clauses.append(f"{CHUNK_ALIAS}.page <= %s")
        params.append(int(search_filter.page_to))
    if search_filter.heading_contains:
        clauses.append(f"position(lower(%s) in lower({CHUNK_ALIAS}.heading_path)) > 0")
        params.append(search_filter.heading_contains)

    return clauses, params
