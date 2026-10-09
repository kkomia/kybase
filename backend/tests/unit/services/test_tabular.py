"""表格双写：CSV / Excel 的结构化副本与检索文本（M2 / T2.11）。

镜像同构：``app/services/tabular.py`` + ``app/parsers/tabular.py`` + DuckDB 实现 → 本文件。

**这一层要解决的问题**：CSV 走纯文本直通时会被当成一坨文本切块，
于是"张三的年龄是多少"只能靠关键词撞运气——**表的结构信息（哪一列是什么）
在切块时就丢了**。所以这里的核心断言只有一条：
**列名必须出现在每一行里**，否则那一列的值失去了语义
（一个孤零零的 ``30`` 什么也说明不了）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.parsers.tabular import TabularParser
from app.services.tabular import (
    MAX_ROWS,
    TabularService,
    parse_tabular,
    rows_to_text,
)

CSV = "姓名,年龄,城市\n张三,30,北京\n李四,25,上海\n".encode()


@pytest.fixture
def service(bundle) -> TabularService:  # type: ignore[no-untyped-def]
    return TabularService(bundle)


# --------------------------------------------------------------------- 解析


def test_parses_csv_with_header() -> None:
    parsed = parse_tabular(filename="人员.csv", content=CSV)

    assert parsed.columns == ["姓名", "年龄", "城市"]
    assert parsed.rows == [["张三", "30", "北京"], ["李四", "25", "上海"]]
    assert parsed.row_count == 2


def test_parses_tsv() -> None:
    parsed = parse_tabular(filename="人员.tsv", content="姓名\t年龄\n张三\t30\n".encode())

    assert parsed.columns == ["姓名", "年龄"]
    assert parsed.rows == [["张三", "30"]]


def test_values_stay_strings() -> None:
    """**一律按字符串处理**：推断类型会把 ``007`` 变成 ``7``、
    把长数字转成科学计数法——那都是数据本身的损失，且用户很难发现。
    表格进知识库是为了能被检索和核对，不是做统计计算。
    """
    csv = "编号,备注\n007,前导零\n1234567890123456789,长数字\n"
    parsed = parse_tabular(filename="编号.csv", content=csv.encode())

    assert parsed.rows[0][0] == "007"
    assert parsed.rows[1][0] == "1234567890123456789"


def test_ragged_rows_are_padded() -> None:
    """参差的行要补齐——否则渲染时后面的值会与错误的列名配成一对。"""
    parsed = parse_tabular(filename="a.csv", content="甲,乙,丙\n1,2\n3,4,5\n".encode())

    assert parsed.rows == [["1", "2", ""], ["3", "4", "5"]]


def test_blank_lines_are_dropped() -> None:
    parsed = parse_tabular(filename="a.csv", content="甲,乙\n\n1,2\n\n".encode())

    assert parsed.rows == [["1", "2"]]


def test_duplicate_headers_get_suffixes() -> None:
    """重名列会让"列名=值"里出现两个同名的键，读取方无法区分是哪一个。
    加后缀是为了让它们各自可指认。
    """
    parsed = parse_tabular(filename="a.csv", content="值,值\n甲,乙\n".encode())

    assert parsed.columns == ["值", "值_1"]


def test_empty_header_cells_get_placeholders() -> None:
    """空列名会渲染出 `=张三` 这种没法读的东西，所以补占位名。"""
    parsed = parse_tabular(filename="a.csv", content="姓名,,城市\n张三,x,北京\n".encode())

    assert parsed.columns == ["姓名", "列2", "城市"]


def test_rejects_non_tabular_extension() -> None:
    with pytest.raises(InvalidRequestError):
        parse_tabular(filename="a.pdf", content=b"%PDF")


def test_empty_file_is_not_an_error() -> None:
    """空表返回空结果而不是抛错：上传一个空 CSV 是人之常情，
    而"抛错让摄入失败"会把一件小事升格成任务失败。
    """
    parsed = parse_tabular(filename="空.csv", content=b"")

    assert parsed.columns == []
    assert parsed.row_count == 0


def test_gb18030_csv_is_decoded() -> None:
    """中文环境导出的 CSV 常常是 GB18030。**顺序不能反**：
    UTF-8 解 GB18030 不会抛错（会解成乱码），所以必须先试 UTF-8，
    但也不能只试 UTF-8。
    """
    content = "姓名,城市\n张三,北京\n".encode("gb18030")

    parsed = parse_tabular(filename="gb.csv", content=content)

    assert parsed.columns == ["姓名", "城市"]
    assert parsed.rows == [["张三", "北京"]]


def test_row_cap_truncates_and_flags() -> None:
    """超限**截断而不是拒绝**，但必须标记——否则用户以为库里有全部数据，
    而精确定位到某一行时会找不到。
    """
    rows = "\n".join(f"值{i}" for i in range(MAX_ROWS + 5))
    parsed = parse_tabular(filename="大.csv", content=f"列\n{rows}\n".encode())

    assert parsed.row_count == MAX_ROWS
    assert parsed.truncated is True


# --------------------------------------------------------------------- 渲染


def test_every_row_carries_its_column_names() -> None:
    """**这是本模块存在的理由。**

    纯文本直通给出的是 ``张三,30,北京``，列名只在第一行出现一次；
    切块之后大部分行的列名就不在同一块里了，检索"年龄"命中不到 ``30``。
    """
    text = rows_to_text(parse_tabular(filename="人员.csv", content=CSV))

    assert "姓名=张三" in text
    assert "年龄=30" in text
    assert "城市=北京" in text
    # 第二行同样带列名
    assert "姓名=李四" in text
    assert "年龄=25" in text


def test_empty_values_are_skipped() -> None:
    """空值渲染成 `城市=` 只是噪声，还会稀释那一段的检索信号。"""
    parsed = parse_tabular(filename="a.csv", content="姓名,备注\n张三,\n".encode())

    text = rows_to_text(parsed)

    assert "姓名=张三" in text
    assert "备注=" not in text


def test_row_numbers_start_at_one() -> None:
    """行号是用户能对照原文的说法，从 1 开始——不是从 0。"""
    text = rows_to_text(parse_tabular(filename="a.csv", content="列\n甲\n乙\n".encode()))

    assert "第 1 行：" in text
    assert "第 2 行：" in text
    assert "第 0 行：" not in text


def test_truncation_is_stated_in_the_text() -> None:
    """截断了就要写出来，否则检索到的内容会被当成完整数据。"""
    body = "列\n" + "\n".join(f"v{i}" for i in range(MAX_ROWS + 1))
    parsed = parse_tabular(filename="a.csv", content=body.encode())

    assert str(MAX_ROWS) in rows_to_text(parsed)


def test_sheet_name_becomes_a_heading() -> None:
    text = rows_to_text(parse_tabular(filename="人员.csv", content=CSV), sheet_name="人员.csv")

    assert text.startswith("# 人员.csv")


# --------------------------------------------------------------------- 结构化副本


def test_store_and_read_back(service: TabularService, bundle) -> None:  # type: ignore[no-untyped-def]
    parsed = parse_tabular(filename="人员.csv", content=CSV)

    assert service.store(document_id="doc_t1", parsed=parsed) == 2

    result = service.query_rows(document_id="doc_t1")
    assert result["columns"] == ["姓名", "年龄", "城市"]
    assert result["rows"] == [["张三", "30", "北京"], ["李四", "25", "上海"]]
    assert result["total"] == 2


def test_rows_come_back_in_source_order(service: TabularService) -> None:
    """**按写入顺序返回**：DuckDB 不保证无 ORDER BY 时的行序，
    而"第 3 行"是用户能对照原文的说法。
    """
    from app.services.tabular import TabularParse

    rows = [[f"值{i}"] for i in range(30)]
    service.store(document_id="doc_order", parsed=TabularParse(columns=["列"], rows=rows))

    read = service.query_rows(document_id="doc_order", limit=30)["rows"]
    assert read == [[f"值{i}"] for i in range(30)]


def test_rewrite_replaces_instead_of_appending(service: TabularService) -> None:
    """重新摄入要给一份干净的副本。

    追加会让行数翻倍，而用户看到"这份表有两倍的行"时，
    很难联想到是自己点了一次重跑。
    """
    service.store(
        document_id="doc_rw", parsed=parse_tabular(filename="a.csv", content=CSV)
    )
    service.store(
        document_id="doc_rw",
        parsed=parse_tabular(filename="a.csv", content="姓名\n赵六\n".encode()),
    )

    result = service.query_rows(document_id="doc_rw")
    assert result["total"] == 1
    assert result["columns"] == ["姓名"]


def test_pagination(service: TabularService) -> None:
    service.store(
        document_id="doc_page",
        parsed=parse_tabular(
            filename="a.csv", content=("列\n" + "\n".join(f"v{i}" for i in range(10))).encode()
        ),
    )

    page = service.query_rows(document_id="doc_page", limit=3, offset=2)

    assert page["rows"] == [["v2"], ["v3"], ["v4"]]
    assert page["total"] == 10


def test_querying_a_non_tabular_document_explains_itself(
    service: TabularService,
) -> None:
    """**说清"只有 CSV/Excel 才有结构化副本"**，而不是笼统的"找不到"——
    后者会让用户以为数据丢了。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        service.query_rows(document_id="doc_pdf")

    assert "CSV" in str(excinfo.value)


def test_drop_removes_the_table(service: TabularService, bundle) -> None:  # type: ignore[no-untyped-def]
    service.store(document_id="doc_drop", parsed=parse_tabular(filename="a.csv", content=CSV))

    service.drop(document_id="doc_drop")

    assert bundle.tabular.table_exists("doc_drop") is False


def test_drop_is_idempotent(service: TabularService) -> None:
    """删两次不报错：删文档的路径不该因为"表本来就不存在"而失败。"""
    service.drop(document_id="doc_never_existed")


# --------------------------------------------------------------------- 解析器


def test_parser_claims_tabular_files() -> None:
    parser = TabularParser()

    assert parser.supports(filename="a.csv", mime_type=None, probe=None)  # type: ignore[arg-type]
    assert parser.supports(filename="a.xlsx", mime_type=None, probe=None)  # type: ignore[arg-type]
    assert not parser.supports(filename="a.md", mime_type=None, probe=None)  # type: ignore[arg-type]


def test_parser_output_is_row_text() -> None:
    result = TabularParser().parse(filename="人员.csv", content=CSV)

    assert isinstance(result.markdown, str), "ParseResult.markdown 是 str，不是 bytes"
    assert result.parser_name == "TabularParser"
    assert "姓名=张三" in result.markdown


def test_parser_runs_before_plain_text(bundle) -> None:  # type: ignore[no-untyped-def]
    """**顺序是关键**：两者都认 ``.csv``，纯文本直通先接走就丢列名了。"""
    from app.core.services import get_services
    from app.services.parser_router import build_parsers

    services = get_services()
    names = [parser.name for parser in build_parsers(services.runtime)]

    assert names.index("TabularParser") < names.index("PlainTextParser")


def test_plain_text_parser_yields_tabular_files() -> None:
    """纯文本解析器要显式让路，否则它仍然会在路由里抢走 CSV。"""
    from app.parsers.plain_text import PlainTextParser

    parser = PlainTextParser()

    assert not parser.supports(filename="a.csv", mime_type=None, probe=None)  # type: ignore[arg-type]
    # 其它文本照旧
    assert parser.supports(filename="a.md", mime_type=None, probe=None)  # type: ignore[arg-type]


# --------------------------------------------------------------------- Excel


def test_parses_xlsx(service: TabularService) -> None:
    """Excel 与 CSV 走同一条渲染与副本通路——只是读法不同。"""
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["姓名", "年龄"])
    sheet.append(["张三", 30])
    sheet.append(["李四", 25])
    import io

    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_tabular(filename="人员.xlsx", content=buffer.getvalue())

    assert parsed.columns == ["姓名", "年龄"]
    assert parsed.rows == [["张三", "30"], ["李四", "25"]]
    assert "年龄=30" in rows_to_text(parsed)


def test_broken_excel_gives_a_readable_error() -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        parse_tabular(filename="坏.xlsx", content="这不是 xlsx".encode())

    assert "Excel" in str(excinfo.value)


def test_the_duckdb_connection_is_never_used_from_two_threads_at_once(
    tabular_store,  # type: ignore[no-untyped-def]
) -> None:
    """表格副本的 DuckDB 连接**不是线程安全的**，所以它必须串行用（v0.27）。

    在此之前它只在"一次请求一个连接"的假设下被调用；工具循环现在会把同一批里的
    几次调用**并发**跑（比如一次导出表格 + 一次读表），两个线程同时用同一个连接
    轻则报错、重则串了结果。这条用例同时钉两件事：

    1. **不炸**：几个线程反复读写同一张表，一个异常都不该有；
    2. **不自锁**：`read_rows` 内部要调 `columns` → `table_exists`，几个公开方法
       互相嵌套——用普通 `Lock` 会在这里把自己锁死（所以实现里用的是 `RLock`）。
       真锁死了这条用例会**挂住**而不是红，这正是不用等它红的原因。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = tabular_store
    rows = [[f"值{i}"] for i in range(50)]
    store.write_table(table="doc_conc", columns=["列"], rows=rows)

    start = threading.Barrier(4)
    failures: list[BaseException] = []

    def hammer() -> None:
        try:
            start.wait(timeout=10)
            for _ in range(15):
                assert store.row_count("doc_conc") == 50
                assert store.columns("doc_conc") == ["列"]
                assert store.read_rows("doc_conc", limit=50)[0] == ["值0"]
        except BaseException as exc:
            failures.append(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: hammer(), range(4)))

    assert failures == []
