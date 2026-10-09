"""表格文件的**纯格式处理**：解码、解析行列、渲染检索文本。

**为什么它住在 `parsers/` 而不是 `services/`**（v0.12 review 的分层修正）：
`parsers/tabular.py`（CSV/Excel 的 ParserProvider）与 `parsers/plain_text.py`
（判断"这不是纯文本、别直读"）都需要这些函数，而它们**只依赖 csv/io/openpyxl**、
不碰任何仓储。原先它们住在 `app/services/tabular.py`，于是解析器要 `import app.services`
——插件层反向依赖业务层，正是分层纪律要禁的方向（而且那时的门禁只查 parser→parser，
拦不住）。放到这里之后依赖方向变成 `parsers ← services`，与 `probe.py` 同一性质：
**共享的格式契约，不是某个 ParserProvider 实现**。

`services/tabular.py` 仍然保留 `TabularService`（编排：解析 + 写 DuckDB 结构化副本）
并从这里重新导出这些名字，所以调用点无需改动。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from app.core.exceptions import InvalidRequestError

__all__ = [
    "MAX_COLUMNS",
    "MAX_ROWS",
    "TABULAR_EXTENSIONS",
    "TabularParse",
    "parse_tabular",
    "rows_to_text",
]

#: 走表格通道的后缀。TSV 也在这里——它只是另一个分隔符。
TABULAR_EXTENSIONS = frozenset({".csv", ".tsv", ".xlsx", ".xls"})

#: 单表行数上限。**超了截断而不是拒绝**：前几万行已经能回答绝大多数问题，
#: 而一份百万行的表进库会把摄入时间拖到不可接受，还会挤掉别的文档。
MAX_ROWS = 50_000

#: 列数上限。到这个量级的多半是导出错的文件（比如整行被当成了列）。
MAX_COLUMNS = 512


@dataclass(slots=True)
class TabularParse:
    """一次表格解析的产物。"""

    columns: list[str]
    rows: list[list[str]]
    truncated: bool = False
    """是否因为超过 ``MAX_ROWS`` 被截断。要在文档里写明——否则用户
    以为库里有全部数据，而精确定位到某一行时会找不到。"""

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _decode(content: bytes) -> str:
    """解码阶梯：UTF-8（含 BOM）→ GB18030 → 兜底不丢字节。

    与 ``parsers/plain_text.py`` 同一套顺序：中文环境导出的 CSV 常常是 GB18030，
    而 UTF-8 解它不会抛错（会解成乱码），所以顺序不能反。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _suffix(filename: str) -> str:
    lowered = filename.lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else ""


def _normalize(raw_rows: list[list[str]]) -> TabularParse:
    """把原始行整理成"列名 + 数据行"。

    三件事：第一行当列名、补齐参差的行、超限截断。
    """
    rows = [[("" if cell is None else str(cell)).strip() for cell in row] for row in raw_rows]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return TabularParse(columns=[], rows=[])

    header = rows[0]
    body = rows[1:]

    # 列名去重与补空：空列名会让"列名=值"渲染出 `=张三` 这种没法读的东西
    columns: list[str] = []
    seen: dict[str, int] = {}
    for index, name in enumerate(header[:MAX_COLUMNS]):
        label = name or f"列{index + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label}_{seen[label]}"
        else:
            seen[label] = 0
        columns.append(label)

    # 没有表头的表（第一行就是数据）会被当表头用掉——这是 CSV 的固有歧义，
    # 无法可靠区分，所以按约定取第一行为列名，并在渲染时保证列名一定出现。
    truncated = len(body) > MAX_ROWS
    body = body[:MAX_ROWS]

    width = len(columns)
    padded = [(row + [""] * width)[:width] for row in body]
    return TabularParse(columns=columns, rows=padded, truncated=truncated)


def parse_tabular(*, filename: str, content: bytes) -> TabularParse:
    """把表格文件读成行列。

    **一律按字符串处理**：让 pandas/openpyxl 推断类型会把 ``007`` 变成 ``7``、
    把长数字转成科学计数法、把日期格式改掉——而这些都是**数据本身的损失**，
    且用户很难发现。表格进知识库是为了能被检索和核对，不是做统计计算，
    所以保真比类型重要。
    """
    suffix = _suffix(filename)
    if suffix not in TABULAR_EXTENSIONS:
        raise InvalidRequestError(f"不是表格文件：{filename}")
    if suffix in (".xlsx", ".xls"):
        return _parse_excel(content)
    return _parse_delimited(content, delimiter="\t" if suffix == ".tsv" else ",")


def _parse_delimited(content: bytes, *, delimiter: str) -> TabularParse:
    text = _decode(content)
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        raw_rows = list(reader)
    except csv.Error as exc:
        raise InvalidRequestError(f"CSV 格式有误：{exc}") from exc
    return _normalize(raw_rows)


def _parse_excel(content: bytes) -> TabularParse:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 依赖在 extras 里
        raise InvalidRequestError("解析 Excel 需要 openpyxl（pip install '.[parse]'）") from exc

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise InvalidRequestError(f"Excel 打不开：{exc}") from exc

    try:
        # **只读第一个工作表**：多表进一个知识库会让"这一行属于哪张表"
        # 无从分辨。需要别的表就先导出成 CSV 再传——那样至少表名是清楚的。
        sheet = workbook.worksheets[0]
        raw_rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()

    return _normalize(raw_rows)


def rows_to_text(parsed: TabularParse, *, sheet_name: str = "") -> str:
    """渲染成**每行一条**的可读文本，作为向量化的单位。

    刻意用"列名=值"而不是保留表格形状：切块器按行切，而
    ``张三 | 30 | 北京`` 这样的行进库后，检索"张三的年龄"要能同时命中
    姓名与年龄——**列名必须在每一行里出现**，否则那一列的值失去了它的语义
    （一个孤零零的 ``30`` 什么也说明不了）。

    这也解释了为什么不直接存 CSV 原文：原文里列名只在第一行出现一次，
    切块之后大部分行的列名就不在同一块里了。
    """
    lines: list[str] = []
    if sheet_name:
        lines.append(f"# {sheet_name}")
        lines.append("")
    for index, row in enumerate(parsed.rows, start=1):
        pairs = [
            f"{column}={value}"
            for column, value in zip(parsed.columns, row, strict=False)
            if str(value).strip()
        ]
        if pairs:
            lines.append(f"第 {index} 行：" + "，".join(pairs))
    if parsed.truncated:
        lines.append("")
        lines.append(f"（该表超过 {MAX_ROWS} 行，此处只包含前 {MAX_ROWS} 行）")
    return "\n".join(lines) + "\n"
