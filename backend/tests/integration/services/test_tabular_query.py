"""表格的 SQL 查询（v0.33）：范围、聚合、上限。

镜像同构：``app/services/tabular.py`` 的 ``tables`` / ``query_sql`` + DuckDB 的
``list_tables`` / ``run_select`` → 本文件（闸本身在 ``test_tabular_sql.py``）。

这一层要回答的是**聚合类问题**——"这个月一共花了多少"检索答不了它
（检索给最相关的几行，而正确答案要求所有行）。用例钉四件事：

1. **表就是文档**：``list_tables`` 给的表名是 ``document_id``，范围按库过滤；
2. **聚合真的算得对**（这是这条通路存在的理由，算错等于白做）；
3. **范围外的表查不到**（越权读表是这条通路最大的风险，判定在服务层）；
4. **结果有上限**（它会原样进模型上下文：几百行明细既贵又没用）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.tabular import TabularService
from app.storage.base import DataSourceKind, DocumentRecord, DocumentStage, KnowledgeBaseRecord

DEFAULT_MODEL_ID = "m_default_512"
DEFAULT_DIM = 512


@pytest.fixture
def service(bundle) -> TabularService:  # type: ignore[no-untyped-def]
    return TabularService(bundle)


def _doc(store, kb_id: str, doc_id: str, name: str):  # type: ignore[no-untyped-def]
    return store.create_document(
        DocumentRecord(
            id=doc_id,
            knowledge_base_id=kb_id,
            name=name,
            source_kind=DataSourceKind.UPLOAD,
            content_hash=f"hash-{doc_id}",
            stage=DocumentStage.UPLOADED,
            size_bytes=10,
        )
    )


def _expenses(bundle, service: TabularService, *, doc_id: str, kb_id: str, name: str):  # type: ignore[no-untyped-def]
    """一份"记账"表：金额是字符串（**副本里一切都是 VARCHAR**，与真实链路一致）。"""
    _doc(bundle.meta, kb_id, doc_id, name)
    bundle.tabular.write_table(
        table=doc_id,
        columns=["月份", "科目", "金额"],
        rows=[["1月", "餐饮", "120"], ["1月", "交通", "80"], ["2月", "餐饮", "200"]],
    )


@pytest.fixture
def scoped(bundle, kb, service: TabularService):  # type: ignore[no-untyped-def]
    """一个库里的两张表（都用真实链路写进 DuckDB）。"""
    _expenses(bundle, service, doc_id="doc_a", kb_id=kb.id, name="记账.csv")
    bundle.tabular.write_table(
        table="doc_b", columns=["科目", "预算"], rows=[["餐饮", "500"], ["交通", "300"]]
    )
    _doc(bundle.meta, kb.id, "doc_b", "预算.csv")
    return kb.id


# ------------------------------------------------------------------ 列表


def test_tables_lists_documents_with_a_structured_copy(
    bundle, service, scoped
) -> None:  # type: ignore[no-untyped-def]
    items = service.tables(kb_ids=[scoped])
    names = {item["document_id"]: item["name"] for item in items}
    assert names == {"doc_a": "记账.csv", "doc_b": "预算.csv"}
    first = next(item for item in items if item["document_id"] == "doc_a")
    assert first["columns"] == ["月份", "科目", "金额"]
    assert first["rows"] == 3


def test_tables_respects_the_kb_scope(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    other = bundle.meta.create_knowledge_base(
        KnowledgeBaseRecord(
            id="kb_other",
            name="别的库",
            embedding_model_id=DEFAULT_MODEL_ID,
            embedding_dim=DEFAULT_DIM,
        )
    )
    assert service.tables(kb_ids=[other.id]) == []


def test_tables_ignores_tables_without_a_document(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    """副本在、文档没了（摄入被取消/文档被删）→ **不进列表**：
    模型查它只会得到一份没有来源的数据。"""
    bundle.tabular.write_table(table="doc_gone", columns=["a"], rows=[["1"]])
    assert [item["document_id"] for item in service.tables(kb_ids=[scoped])] == ["doc_a", "doc_b"]


# ------------------------------------------------------------------ 查询


def test_aggregation_is_the_point(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    """**这条通路的理由**：按科目汇总，检索答不了这种问题。"""
    payload = service.query_sql(
        sql=(
            "SELECT 科目, sum(CAST(金额 AS INTEGER)) AS 合计 FROM doc_a"
            " GROUP BY 科目 ORDER BY 合计 DESC"
        ),
        kb_ids=[scoped],
    )
    assert payload["columns"] == ["科目", "合计"]
    assert payload["rows"] == [["餐饮", "320"], ["交通", "80"]]


def test_count_across_two_tables(
    bundle, service, scoped
) -> None:  # type: ignore[no-untyped-def]
    payload = service.query_sql(sql="SELECT count(*) FROM doc_b", kb_ids=[scoped])
    assert payload["rows"] == [["2"]]


def test_query_outside_the_scope_is_refused(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    """**真实的表、但不在这一轮范围里**——这是越权读表，必须被我们拦下。"""
    bundle.tabular.write_table(table="doc_secret", columns=["a"], rows=[["x"]])
    with pytest.raises(InvalidRequestError, match="不在这一轮能查的范围内"):
        service.query_sql(sql="SELECT * FROM doc_secret", kb_ids=[scoped])


def test_a_table_that_does_not_exist_falls_through_to_the_engine(service, scoped) -> None:  # type: ignore[no-untyped-def]
    """拼错的表名交给引擎报错：它那句 "表不存在，是不是想说 doc_a？" 比我们能编的更精确。"""
    with pytest.raises(Exception, match="does not exist"):
        service.query_sql(sql="SELECT * FROM doc_typo", kb_ids=[scoped])


def test_query_without_any_scope_explains_itself(service) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError, match="没有可查的表格"):
        service.query_sql(sql="SELECT 1", kb_ids=[])


def test_results_are_capped_and_say_so(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    bundle.tabular.write_table(
        table="doc_big",
        columns=["n"],
        rows=[[str(index)] for index in range(1000)],
    )
    _doc(bundle.meta, scoped, "doc_big", "很多行.csv")
    payload = service.query_sql(sql="SELECT * FROM doc_big", kb_ids=[scoped], limit=10)
    assert len(payload["rows"]) == 10
    assert payload["truncated"] is True
    assert "最多回 10 行" in str(payload["note"])


def test_the_response_lists_what_it_was_allowed_to_read(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    """把"能查哪些表"回给调用方：模型下一步要自己拼 SQL，它得知道可用范围。"""
    payload = service.query_sql(sql="SELECT 1 FROM doc_a", kb_ids=[scoped])
    assert {item["document_id"] for item in payload["tables"]} == {"doc_a", "doc_b"}


def test_a_written_looking_sql_never_reaches_the_engine(bundle, service, scoped) -> None:  # type: ignore[no-untyped-def]
    """写语句在**服务层**就被拒（不是靠 DuckDB 报错），而且要拒得干净。"""
    with pytest.raises(InvalidRequestError):
        service.query_sql(sql="DELETE FROM doc_a", kb_ids=[scoped])
    assert bundle.tabular.row_count("doc_a") == 3
