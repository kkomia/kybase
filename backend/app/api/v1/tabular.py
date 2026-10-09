"""表格结构化副本端点（M2 / T2.11）。

**为什么要有这个端点**：把数据写进 DuckDB 却不提供任何读取通路，
等于没写——存储本身不是价值，"能精确查到某一行"才是。

**它是受控查询，不是 SQL 旁路。** 调用方给的是文档 id + 分页参数，
不是 SQL 字符串。架构 §5.2 把"开放的 SQL 旁路"列为缓做，这里遵守；
两者的区别是**攻击面**：接受 SQL 文本就要面对注入、资源耗尽、越权读表一整套问题，
而"按文档读第 N 行"没有这些问题。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.auth import require_read
from app.api.v1.schemas import TableRowsOut
from app.core.services import Services, get_services
from app.services.api_key import Caller

router = APIRouter(tags=["tabular"])


@router.get(
    "/documents/{document_id}/table",
    response_model=TableRowsOut,
    summary="读表格文档的结构化副本（分页）",
)
def read_table(
    document_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> TableRowsOut:
    """读一份表格文档的行列。

    **只对 CSV/Excel 有效**：其它文档没有结构化副本，会得到一条
    说明原因的错误，而不是空结果——后者会让人以为数据丢了。
    """
    # 与其它文档端点同一个范围判定：先读文档再判范围，
    # 反了会给出 403 而不是 404，等于告诉越权探测者"这个 id 存在"
    from app.api.auth import check_kb_scope

    document = services.documents.get(document_id)
    check_kb_scope(services, caller, [document.knowledge_base_id])

    return TableRowsOut.model_validate(
        services.tabular.query_rows(document_id=document_id, limit=limit, offset=offset)
    )
