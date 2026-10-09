r"""旧会话一次性导入与本机回滚（M2 阶段 5，方案 §3.1 与 §7）。

## 一条硬纪律：**不直连 PG**

导入走 HTTP（``GET {server}/conversations/export``，六型 NDJSON，见
``services/conversation_export.py``），不 dump 服务器库（v0.3 §4 的分界线、§8-3）。
所以这个模块不认识 psycopg，也不认识 sqlite3——它只用两样东西：

- ``StoreBundle`` 的接口（会话 / 消息 / 事件 / 产物 / 摘要的读写）；
- ``StoreBundle.ledger``（本机独有的导入台账，见 ``app/storage/base.py::ImportLedger``）。

因此 ``python -m app.services.legacy_import`` 这条 CLI 也**不违反 L6/L2**
（它不住在 ``app/`` 根、也不直连数据库），与边车那个入口是两码事。

## 来源是**注入**的（M5 阶段 5 抽出的接缝）

这个类原来自己写死了 HTTP 那一条路（``_pull`` / ``_fetch_page``）。M5 阶段 5 的按点恢复
要的是同一台写入机、同一套幂等键与覆盖规则、同一个台账，**只是来源换成一份解包出来的
快照库**——于是"来源"被抽成 :class:`TransferSource`（两个实现：
:class:`HttpExportSource` 与 ``services/backup_restore.SnapshotFileSource``）。

抽法守住三条（既有语义一个字节都没变）：

1. 协议只有一个动作——``transfers(since)`` 逐条吐 ``ConversationTransfer``，
   外加一个 ``source``（台账里那个来源标识 = 幂等键的第一段）；
2. ``HttpExportSource`` 的代码是**从本模块原样搬过去的**（分页、截断检查、状态码分档、
   逐行按字节切——一个字没改），所以既有用例（它们注入 ``transport`` 走真 HTTP 那条链）
   仍然覆盖着它；
3. 构造签名向后兼容：``source`` 仍可给一个**字符串基址**（原样，内部建
   ``HttpExportSource``），给一个对象（满足 :class:`TransferSource`）就是换来源。
   既有调用点（边车端点、CLI、全部用例）一个字不用改。

## 三条规则（写在这里，免得实现细节盖过它们）

1. **幂等**：键是 ``(source, conversation_id, source_updated_at_ms)``
   （``import_items`` 的主键）。重跑同一批 → 全部命中 → 报告里 ``skipped``，
   **一行不写**。台账里那条被回滚过的（批次状态 ``rolled_back``）不算命中——
   回滚之后想再导一次是正当需求，而"台账还在"不该把它挡住。
2. **覆盖策略 ``replace_if_local_untouched``**：本机这条会话存在、且它在
   **导入之后**又被改过（``updated_at`` 比台账里记的那个值大）→ **跳过**并在报告里
   如实列出（``local_newer``）。绝不静默覆盖用户在本机说过的话。反过来，本机这条
   如果根本不是我们导进来的（没有台账）→ 也跳过（``not_ours``）：**只有我们写进去的
   东西我们才敢覆盖**。
3. **可回滚**：导入前把被替换的那条会话按**同一套 NDJSON** 存进
   ``<data_dir>/import-rollback/<batch>/<会话>.ndjson``。``rollback`` 逐条按台账办：
   ``created`` 且本机没再动过 → 删；``replaced`` 且本机没再动过 → 用快照恢复；
   本机改过的一律保留并如实报数。两条规则都要写进 ``ImportReport`` 的计数里。

## 触发方式（两条，共用同一个类）

- CLI（边车开着也能跑，不依赖它）：
  ``python -m app.services.legacy_import --server http://nas:8000/api/v1 --token … --data-dir …``
  （``--since`` 增量、``--dry-run`` 只看不写、``--rollback <批次>`` 回滚）；
- 界面：``POST /api/v1/local/import``（本机后端）+ ``GET /local/import/{batch}`` 轮询。

## 读盘与内存上界

逐页拉取（默认一页 200 条会话）、页内**逐行**解析、攒完一条会话就落库：
内存里最多只有一条会话 + 一页的行缓冲。**页与页之间是顺序的**（不并发拉多页）——
见 ``HttpExportSource`` 的说明：并发拉页会把好几页的正文同时拽进内存，而上限恰恰是
最难估的那一项。换成快照库那个来源（``SnapshotFileSource``）时同一条上界也成立：
它是个生成器，一次只留一条会话。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.lazy_httpx import httpx
from app.services.conversation_export import (
    ARTIFACT,
    CONVERSATION,
    EVENT,
    FOOTER,
    HEADER,
    MESSAGE,
    PAGE_SIZE,
    ExportEvent,
    artifact_from_data,
    conversation_from_data,
    encode_conversation,
    event_from_data,
    footer_line,
    header_line,
    message_from_data,
    parse_line,
    read_transfer,
    summary_from_data,
)
from app.storage.base import (
    IMPORT_UNFINISHED_STATES,
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    ImportBatchRecord,
    ImportItemRecord,
    ImportLedger,
    SessionEventRecord,
    StoreBundle,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "ROLLBACK_DIRNAME",
    "UNFINISHED_STATES",
    "HttpExportSource",
    "ImportPlan",
    "ImportReport",
    "LegacyImportError",
    "LegacyImporter",
    "SnapshotError",
    "TransferSource",
    "main",
]

logger = logging.getLogger(__name__)

#: 拉一页导出流的超时（秒）。一页最多 200 条会话，NAS 那侧是本地库查询——
#: 给 60 秒足够宽（比检索那条链的 30 秒宽，因为这里要传的正文多得多）。
DEFAULT_TIMEOUT = 60.0

UNFINISHED_STATES = IMPORT_UNFINISHED_STATES
"""**没跑完**的那两个状态（``planned`` / ``running``）。

词表的家在存储契约（``base.IMPORT_UNFINISHED_STATES``），这里只是**转出来给协议层**用：
``api/`` 不许 import ``app.storage``（工程规范 §3.3 的 L1），而"还有几笔账没结"
这两处（``/local/status`` 与本模块的启动自检）看的是同一件事——两处各写一份必然漂。
"""

ROLLBACK_DIRNAME = "import-rollback"
"""回滚快照的落点：``<data_dir>/import-rollback/<batch>/<会话>.ndjson``（§1.1）。"""

#: 跳过与保留的**原因码**（写进报告，界面/CLI 按它分类：
#: 是"本来就不该导"还是"你动过所以没覆盖"，下一步动作完全不同）。
REASON_ALREADY_IMPORTED = "already_imported"
REASON_LOCAL_NEWER = "local_newer"
REASON_NOT_OURS = "not_ours"


class LegacyImportError(RuntimeError):
    """导入这条链上的可预期失败（连不上 / 被拒 / 流坏了 / 快照不在）。

    **不细分异常类**：调用方只有一种处置——把这句话记进 ``imports.error``
    并如实告诉用户。分类信息在句子本身（"连不上"还是"被拒"）已经写清楚了。
    """


class SnapshotError(LegacyImportError):
    """回滚快照不可用（缺失或读不出来）——回滚到这一条时如实报，不装作恢复过。"""


@dataclass(slots=True)
class PlanItem:
    """``plan()`` 里的一条会话（不含正文，只报**这条会怎么处理**）。"""

    conversation_id: str
    title: str
    source_updated_at_ms: int
    messages: int = 0
    events: int = 0
    artifacts: int = 0
    reason: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "source_updated_at_ms": self.source_updated_at_ms,
            "messages": self.messages,
            "events": self.events,
            "artifacts": self.artifacts,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ImportPlan:
    """``plan()`` 的结果：**会新建 / 会替换 / 会跳过**各是哪几条。

    ``--dry-run`` 与 ``POST /local/import {"dry_run": true}`` 都返回它——
    让用户在动手之前就看见"哪些会被跳过、为什么"，而不是导完才发现少了东西。
    """

    source: str
    since: datetime | None = None
    scanned: int = 0
    created: list[PlanItem] = field(default_factory=list)
    replaced: list[PlanItem] = field(default_factory=list)
    skipped: list[PlanItem] = field(default_factory=list)
    file_references: int = 0
    """这一批里**不随导入过来**的文件引用数（R4）：产物 + 消息附件。"""

    unresolved_workspaces: int = 0
    """这一批里**本机没有对应记录**的工作区引用数（会话那条外键）。

    工作区在本机档里的含义是"**这台机器上的一个路径**"（方案 §4.2），所以 NAS 上的
    工作区不随导入过来、也不该搬过来（一条指向不存在的目录的记录比没有更糟）。
    导入时把这种引用置空并在这里如实报数——真机验收抓到的第二个坑（见
    ``LegacyImporter._localize``）。
    """

    def counts(self) -> dict[str, Any]:
        """与 ``imports.counts_json`` 同一份形状（``state=planned`` 时它也是进度）。"""
        return {
            "scanned": self.scanned,
            "created": len(self.created),
            "replaced": len(self.replaced),
            "skipped": len(self.skipped),
            "skipped_items": [
                {
                    "conversation_id": item.conversation_id,
                    "reason": item.reason,
                    "detail": item.detail,
                }
                for item in self.skipped
            ],
            "file_references": self.file_references,
            "unresolved_workspaces": self.unresolved_workspaces,
        }


@dataclass(slots=True)
class ImportReport:
    """``run()`` / ``rollback()`` 的结果（``counts`` 与库里那份 ``counts_json`` 同源）。"""

    batch_id: str
    source: str
    state: str
    counts: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("done", "rolled_back")


class _Decision:
    """一条会话该怎么处理：``create`` / ``replace`` / ``skip``。"""

    __slots__ = ("detail", "outcome", "reason")

    def __init__(self, outcome: str, reason: str = "", detail: str = "") -> None:
        self.outcome = outcome
        self.reason = reason
        self.detail = detail

    @property
    def skipped(self) -> bool:
        return self.outcome == "skip"


class _ConversationBuilder:
    """攒一条会话：会话块开了之后，消息 / 事件 / 产物按信封归位。"""

    __slots__ = (
        "artifacts",
        "conversation",
        "conversation_id",
        "events",
        "messages",
        "summary",
        "summary_upto",
    )

    def __init__(self, data: dict[str, Any]) -> None:
        self.conversation = conversation_from_data(data)
        self.conversation_id = self.conversation.id
        self.summary, self.summary_upto = summary_from_data(data)
        self.messages: list[ChatMessageRecord] = []
        self.events: list[SessionEventRecord] = []
        self.artifacts: list[ConversationArtifactRecord] = []

    def add(self, event: ExportEvent) -> None:
        # 块串了（上一条消息挂到下一条会话上）是**流损坏**，当场报错：
        # 照着往下读只会把 A 的消息写进 B 里，而那种错回看时看不出来。
        if event.conversation_id != self.conversation_id:
            raise LegacyImportError(
                f"导出流的块对不上：会话 {self.conversation_id} 里出现了"
                f" {event.conversation_id} 的 {event.kind}"
            )
        if event.kind == MESSAGE:
            self.messages.append(message_from_data(event.data, self.conversation_id))
        elif event.kind == EVENT:
            self.events.append(event_from_data(event.data, self.conversation_id))
        elif event.kind == ARTIFACT:
            self.artifacts.append(artifact_from_data(event.data, self.conversation_id))

    def finish(self) -> ConversationTransfer:
        return ConversationTransfer(
            conversation=self.conversation,
            summary=self.summary,
            summary_upto=self.summary_upto,
            messages=self.messages,
            events=self.events,
            artifacts=self.artifacts,
        )


class _StreamReader:
    """把六型信封的**行**组装成一条条会话（``feed`` 完成一条就交出来）。

    它还把首末行**记下来**：``footer`` 见过没有是"这一页完整不完整"的唯一判据
    （连接被掐断时流会停在半路，而少了末行的半份数据必须当失败处理——
    这正是末行存在的理由）。
    """

    __slots__ = ("_builder", "footer", "header")

    def __init__(self) -> None:
        self.header: dict[str, Any] | None = None
        self.footer: dict[str, Any] | None = None
        self._builder: _ConversationBuilder | None = None

    def feed(self, raw: str) -> ConversationTransfer | None:
        event = parse_line(raw)
        if event is None:
            return None
        if event.kind == HEADER:
            self.header = dict(event.data)
            return None
        if event.kind == FOOTER:
            self.footer = dict(event.data)
            return self._close()
        if event.kind == CONVERSATION:
            previous = self._close()
            self._builder = _ConversationBuilder(event.data)
            return previous
        if self._builder is None:
            raise LegacyImportError(f"导出流的开头不是会话块：先遇到了 {event.kind}")
        self._builder.add(event)
        return None

    def finish(self) -> ConversationTransfer | None:
        """流读完了（无论有没有末行）：交出最后一条攒着的会话。"""
        return self._close()

    def _close(self) -> ConversationTransfer | None:
        if self._builder is None:
            return None
        finished = self._builder.finish()
        self._builder = None
        return finished


def _ms(value: datetime | None) -> int | None:
    """``datetime`` → UTC 毫秒（与导出侧、库里那一列同一个口径）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


@runtime_checkable
class TransferSource(Protocol):
    """**来源**：逐条吐出要导入的会话（M5 阶段 5 抽出的接缝）。

    导入器对来源的全部要求就两句话：

    - ``source``：这个来源的身份（写进台账，也是幂等键 ``(source, conversation_id,
      source_updated_at_ms)`` 的第一段）。**同一次恢复重跑必须给出同一个 source**，
      否则那批会话会被当成"另一批"，幂等就失效了；
    - ``transfers(since)``：逐条吐 ``ConversationTransfer``（会话 + 摘要 + 消息 + 事件 +
      产物）。**生成器**：调用方一条一条写库，来源一次只该在内存里留一条。

    两个实现：:class:`HttpExportSource`（NAS 的 NDJSON 导出流，M2 那一套原样）与
    ``services/backup_restore.SnapshotFileSource``（一份解包出来的快照库）。
    写进导入器的东西**一模一样**——覆盖规则 / 台账 / 回滚因此天然共用，不需要第二份。
    """

    @property
    def source(self) -> str:
        """台账里那个来源标识（HTTP 那条是 NAS 基址，快照那条是 ``backup://设备/快照``）。"""
        ...

    def transfers(self, *, since: datetime | None = None) -> Iterator[ConversationTransfer]:
        """逐条交出会话；``since`` 给了就只要那之后更新过的（与导出端点同一口径）。"""
        ...


class HttpExportSource:
    """M2 那条来源：``GET {server}/conversations/export`` 的六型 NDJSON 流。

    **代码是从 ``LegacyImporter`` 里原样搬过来的**（分页、末行检查、状态码分档、
    按字节切行）——搬的时候一个语义都没改，所以既有那一批用例（注入 ``transport``
    走真 HTTP 那条链）仍然在钉它。

    两条刻意的口径（搬过来时写在注释里，一并留着）：

    - **页与页之间不并发**（方案 §3.1 的"源端并发 4"没有照做）：并发拉页要么把好几页的
      正文同时拽进内存，要么得让 HTTP 流按序消费；真正决定吞吐的是本机写的那一侧；
    - **逐页检查末行**：连接被掐断时流会停在半路，少了末行的半份数据必须当失败处理
      （见 :class:`_StreamReader`）。
    """

    def __init__(
        self,
        *,
        url: str,
        token: str = "",
        page_size: int = PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._page_size = max(1, min(int(page_size), PAGE_SIZE))
        self._timeout = timeout
        #: 假传输的注入点（用例给 ``httpx.MockTransport``；生产这条是 ``None``）。
        self.transport = transport

    @property
    def source(self) -> str:
        return self._url

    def _headers(self) -> dict[str, str]:
        """鉴权头（**令牌只在内存与请求头上，绝不落库**：R6）。"""
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def transfers(self, *, since: datetime | None = None) -> Iterator[ConversationTransfer]:
        """逐页拉导出流、逐条交出会话（顺序拉取，理由见类说明）。"""
        offset = 0
        while True:
            reader = _StreamReader()
            received = 0
            for raw in self._fetch_page(offset, since=since):
                done = reader.feed(raw)
                if done is not None:
                    received += 1
                    yield done
            final = reader.finish()
            if final is not None:
                received += 1
                yield final
            if reader.footer is None:
                raise LegacyImportError(
                    "导出流没有末行（传输被截断）：这一页不完整，已写入的会话都有台账，可重跑续上"
                )
            if received < self._page_size:
                return
            offset += received

    def _fetch_page(self, offset: int, *, since: datetime | None) -> Iterator[str]:
        """拉一页（``limit`` / ``offset`` / ``since``），逐行吐出来。

        用 ``client.stream`` + ``_raw_lines``（按字节切行）：正文可能有几十 MB，
        整份读进内存再切行会白白翻一倍；而**逐行解码**与"整份读进内存"是两件事。
        状态码分档与 ``services/remote_clients.py`` 同一口径——
        "连不上"与"被拒"是两件事（前者重试有用，后者要先解决身份）。
        """
        url = f"{self._url}/conversations/export"
        params: dict[str, Any] = {"limit": self._page_size, "offset": offset}
        if since is not None:
            params["since"] = since.astimezone(UTC).isoformat()
        try:
            with (
                httpx.Client(
                    timeout=self._timeout, transport=self.transport, headers=self._headers()
                ) as client,
                client.stream("GET", url, params=params) as response,
            ):
                if response.status_code >= 500:
                    raise LegacyImportError(
                        f"导出端点出错了（HTTP {response.status_code}）：{self._url}"
                    )
                if response.status_code >= 400:
                    raise LegacyImportError(
                        f"导出请求被拒（HTTP {response.status_code}）："
                        f"{response.read()[:200]!r}；请检查 --token 与这个地址"
                    )
                yield from _raw_lines(response)
        except LegacyImportError:
            raise
        except Exception as exc:  # httpx 的各路异常 + 连接层
            raise LegacyImportError(f"连不上导出端点（{url}）：{exc}") from exc


def _raw_lines(response: httpx.Response) -> Iterator[str]:
    """把导出流按**行**吐出来：只在 ASCII ``\\n`` 上切，切完一行解一行。

    为什么不用 ``response.iter_lines()``（**2026-10-01 真机验收抓到的真 bug**）：
    httpx 的 ``LineDecoder`` 按 [W3C 的换行口径](https://www.w3.org/TR/newline)
    把 **``U+2028`` / ``U+2029``** 也当换行，而这两个字符在 JSON 字符串里
    **可以合法地原样出现**（``ensure_ascii=False`` 不转义它们；粘贴自网页的正文里
    一抓一大把）→ 一行被切成两半，两半都不是 JSON，导入当场失败
    「导出流里有一行不是 JSON」。

    现场：302 条真会话里那一条的正文带 ``U+2028``，24463 字符的行被读成
    12543 + 11920；同一条流按字节切（4846 + 3063 行）一行不差。

    逐字节切还顺手保住了内存上界：一次只在内存里留**一行**（外加跨块的半个多字节
    字符——``0x0A`` 不会出现在 UTF-8 的多字节序列里，所以整行解码永远安全）。
    """
    pending = b""
    for chunk in response.iter_bytes():
        pending += chunk
        while True:
            index = pending.find(b"\n")
            if index < 0:
                break
            yield pending[:index].decode("utf-8")
            pending = pending[index + 1 :]
    if pending:
        yield pending.decode("utf-8")


class LegacyImporter:
    """拉源端的导出流、写本机库、记账、回滚。

    **构造参数只有两样来源**：``stores``（本机库）与 ``source``+``token``（NAS）。
    用例把 ``transport`` 换成 ``httpx.MockTransport`` 就完全不碰网络（与
    ``tests/unit/services/test_remote_clients.py`` 同一套手法）。
    """

    def __init__(
        self,
        stores: StoreBundle,
        *,
        data_dir: Path,
        source: str | TransferSource,
        token: str = "",
        since: datetime | None = None,
        page_size: int = PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if stores.ledger is None:
            raise LegacyImportError(
                "这台机器没有导入台账（服务器档的会话就是权威，不存在"
                "「从别的部署导进来」这条动作）；导入只在本机档可用"
            )
        self._stores = stores
        self._ledger: ImportLedger = stores.ledger
        self._data_dir = Path(data_dir)
        # **来源**（M5 阶段 5 的接缝）：给字符串就是 NAS 基址（M2 那条 HTTP 路，原样），
        # 给一个对象就是换来源（按点恢复给的是 SnapshotFileSource）。对象自己带 source 标签
        # ——台账里的 ``source`` 与"从哪儿读的"因此永远是同一件事。
        if isinstance(source, str):
            self._reader: TransferSource = HttpExportSource(
                url=source,
                token=token,
                page_size=page_size,
                timeout=timeout,
                transport=transport,
            )
        elif isinstance(source, TransferSource):
            self._reader = source
        else:  # pragma: no cover - 类型上就到不了这儿
            raise LegacyImportError(
                "来源得是一个基址字符串或一个 TransferSource（逐条吐会话的那个协议）"
            )
        self._source = self._reader.source
        self._token = token
        self._since = since
        self._page_size = max(1, min(int(page_size), PAGE_SIZE))
        self._timeout = timeout
        #: 批次状态的小缓存（判断"这条台账是不是被回滚过"要用它）。
        #: **只在一次操作之内有效**：`plan` / `run` / `rollback` 一开始就清空它——
        #: 见 `_reset_batch_states` 那段（跨操作留着会静默漏数据）。
        self._batch_states: dict[str, str] = {}
        #: "这条工作区在本机有吗"的缓存（同一个工作区会被几百条会话引用，
        #: 每条问一次库是白问）。键是源端那个 id，值是本机有没有。
        self._workspaces: dict[str, bool] = {}

    # ------------------------------------------------------------------ 状态与身份

    @property
    def transport(self) -> httpx.BaseTransport | None:
        """假传输的注入点（用例给 ``httpx.MockTransport``；生产这条是 ``None``）。

        做成**公开属性**是为了让集成用例能在**装配好的 app** 上换掉它（见
        ``tests/integration/api/test_local_import_api.py``）——构造之后就换不了的东西
        在那种场景里没有别的入口。阶段 5 把发送那一层挪进了 ``HttpExportSource``，
        所以这一位现在**透传**给那个对象：写 `importer.transport = …` 与阶段 5 之前
        是同一个语义（这是接缝抽出来之后最容易悄悄坏掉的一处）。
        """
        return getattr(self._reader, "transport", None)

    @transport.setter
    def transport(self, value: httpx.BaseTransport | None) -> None:
        if not isinstance(self._reader, HttpExportSource):
            # 别的来源（``SnapshotFileSource``）本来就不发网络请求：换它没有意义，
            # 而"悄悄记下一个谁也不会看的字段"会让调用方以为换成功了。
            raise LegacyImportError("这个来源不走 HTTP（它不从 NAS 拉）：没有可换的传输层")
        self._reader.transport = value

    def _reset_batch_states(self) -> None:
        """开始一次操作：清掉批次状态缓存。

        **为什么要清**（不是"顺手"）：判断"这条台账算不算已经导过"要问它所属批次的
        状态（被回滚过的不算）。而"回滚"是一个**另一次请求**里发生的事——
        缓存跨操作留着，就会出现这条链：批次 A 导入 → 批次 B 重跑（把 A 记成 done）→
        回滚 A → 再导入（缓存里 A 还是 done）→ **全部 skipped，而本机一条会话都没有**。
        静默少数据是这条链上最坏的一种结果，而清缓存只是四个字典操作。

        一次操作之内它照旧有用：一批里几百条会话常常同属一两个批次。
        """
        self._batch_states.clear()

    @staticmethod
    def new_batch_id() -> str:
        """批次 id（``imp_<hex>``）。

        **交给调用方也能造**：端点要在后台线程开跑**之前**就把 id 返回给客户端
        （否则轮询的人不知道该查哪一个），所以生成这件事不能只藏在 ``run()`` 里。
        """
        return f"imp_{uuid.uuid4().hex[:12]}"

    def begin(self, *, batch_id: str, since: datetime | None = None) -> str:
        """把批次行**先**写进库（``state=planned``），返回批次 id。

        **为什么要单独一步**：端点要在后台线程开跑**之前**就把 id 交给客户端
        （否则轮询的人不知道该查哪一个），而"客户端立刻来查"必须查得到——
        查不到只会得到 404（那句话是"你给我的 id 是错的"，不是"还没开始"）。
        所以建行是**同步**的：这一行就是"这次导入存在"的凭证。

        ``run()`` 自己也会补上这一步（CLI 那条路没人替它建行），所以两处不冲突。
        """
        window = self._since if since is None else since
        self._ledger.start_import_batch(batch_id, source=self._source, since_ms=_ms(window))
        return batch_id

    @property
    def source(self) -> str:
        """来源部署的基址（空串 = 这一档没接 NAS：端点据此如实报"没得导"）。"""
        return self._source

    def batch(self, batch_id: str) -> ImportBatchRecord | None:
        """一个批次（轮询端点用它；没有就返回 ``None``，由协议层翻成 404）。"""
        return self._ledger.get_import_batch(batch_id)

    def recent_batches(self, *, limit: int = 5) -> list[ImportBatchRecord]:
        """最近的批次（``/local/status`` 用它与"没跑完的那笔账"对账）。"""
        return self._ledger.list_import_batches(limit=limit)

    # ------------------------------------------------------------------ 目标：计划

    def plan(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        since: datetime | None = None,
    ) -> ImportPlan:
        """拉源端清单、比对本机台账，回答"每条会怎么处理"。**一个字节都不写。**

        ``--dry-run`` 与端点上的 ``dry_run=true`` 走它。它会**完整读一遍源端**
        （要报消息/事件/产物的条数，就得读那些块）——这正是 dry-run 的意思。
        ``since`` 是本次的增量下界（不给就用构造时那个）。
        """
        self._reset_batch_states()
        window = self._since if since is None else since
        plan = ImportPlan(source=self._source, since=window)
        for transfer in self._reader.transfers(since=window):
            transfer, unresolved = self._localize(transfer)
            plan.unresolved_workspaces += unresolved
            decision = self._decision(transfer.conversation)
            plan.scanned += 1
            plan.file_references += transfer.file_references
            item = PlanItem(
                conversation_id=transfer.conversation.id,
                title=transfer.conversation.title,
                source_updated_at_ms=_ms(transfer.conversation.updated_at) or 0,
                messages=len(transfer.messages),
                events=len(transfer.events),
                artifacts=len(transfer.artifacts),
                reason=decision.reason,
                detail=decision.detail,
            )
            if decision.outcome == "create":
                plan.created.append(item)
            elif decision.outcome == "replace":
                plan.replaced.append(item)
            else:
                plan.skipped.append(item)
            if progress is not None:
                progress(plan.counts())
        return plan

    # ------------------------------------------------------------------ 目标：导入

    def run(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        batch_id: str | None = None,
        since: datetime | None = None,
    ) -> ImportReport:
        """按计划把源端的会话写进本机库；返回报告（失败也返回，状态里写着）。

        ``batch_id`` 可以**由调用方指定**（端点要先把 id 交给客户端再去后台跑）；
        ``since`` 是本次的增量下界（不给就用构造时那个）。

        **一个会话一个事务**（``write_imported_conversation``）：中途被杀时，库里
        要么有完整的一条会话，要么一条都没有——不会出现半条（方案 §5 R1 的第一句）。
        加上会话级幂等，重跑就从那条没写上的继续。

        失败**不往外抛**：把原因写进台账（``state=failed`` + ``error``）再返回报告——
        这条链的两个入口（后台线程 / CLI）都需要"失败也是一份记录"，
        而抛出去只会让失败消失在某个线程的栈里。
        """
        self._reset_batch_states()
        window = self._since if since is None else since
        batch_id = batch_id or self.new_batch_id()
        # 端点那条路已经同步建过行了（见 `begin`）；CLI 这条路在这里建。
        # 用"存在就不重建"而不是无条件插入：两个调用点都得能用，而重复插入会抛
        # IntegrityError（那会把一次无害的分工变成失败）。
        if self._ledger.get_import_batch(batch_id) is None:
            self.begin(batch_id=batch_id, since=window)
        counts: dict[str, Any] = {
            "scanned": 0,
            "created": 0,
            "replaced": 0,
            "skipped": 0,
            "skipped_items": [],
            "messages": 0,
            "events": 0,
            "artifacts": 0,
            "file_references": 0,
            "unresolved_workspaces": 0,
            "snapshots": 0,
        }
        self._ledger.set_import_state(batch_id, "running", counts=counts)
        if progress is not None:
            progress(counts)
        started = datetime.now(UTC)
        try:
            for transfer in self._reader.transfers(since=window):
                transfer, unresolved = self._localize(transfer)
                counts["unresolved_workspaces"] += unresolved
                decision = self._decision(transfer.conversation)
                counts["scanned"] += 1
                counts["file_references"] += transfer.file_references
                if decision.skipped:
                    counts["skipped"] += 1
                    counts["skipped_items"].append(
                        {
                            "conversation_id": transfer.conversation.id,
                            "reason": decision.reason,
                            "detail": decision.detail,
                        }
                    )
                else:
                    if decision.outcome == "replace":
                        # **导入前**存快照（§3.1 的顺序是刻意的：快照要在覆盖之前拍下）
                        self._snapshot(batch_id, transfer.conversation.id)
                        counts["snapshots"] += 1
                    self._write(batch_id, transfer, outcome=decision.outcome)
                    counts["created" if decision.outcome == "create" else "replaced"] += 1
                    counts["messages"] += len(transfer.messages)
                    counts["events"] += len(transfer.events)
                    counts["artifacts"] += len(transfer.artifacts)
                if progress is not None:
                    progress(counts)
                self._ledger.set_import_state(batch_id, "running", counts=counts)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            counts["error"] = message
            self._ledger.set_import_state(batch_id, "failed", counts=counts, error=message)
            logger.warning("旧会话导入失败（批次 %s）：%s", batch_id, message, exc_info=True)
            return ImportReport(
                batch_id=batch_id, source=self._source, state="failed", counts=counts, error=message
            )
        counts["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
        self._ledger.set_import_state(batch_id, "done", counts=counts)
        return ImportReport(batch_id=batch_id, source=self._source, state="done", counts=counts)

    # ------------------------------------------------------------------ 目标：回滚

    def rollback(
        self, batch_id: str, progress: Callable[[dict[str, Any]], None] | None = None
    ) -> ImportReport:
        """把一个批次撤销：**两条规则**逐条按台账办（方案 §3.1）。

        - ``outcome=created`` 且本机这条的 ``updated_at`` 仍等于导入时记下的值 → **删**；
        - ``outcome=replaced`` 且仍是那个值 → **用快照恢复**（回到导入前那一刻）；
        - 本机**改过**的一律**保留**，并如实记进 ``kept_items``（名字、原因、现在的值）。

        第三种情况是这条纪律的核心：回滚不是"把用户在本机说过的话也一起抹掉"。
        已不存在（``missing``）与快照缺失（``no_snapshot``）也如实记数——
        回滚跑完之后报告里该有"哪几条没按预想办、为什么"。
        """
        self._reset_batch_states()
        batch = self._ledger.get_import_batch(batch_id)
        if batch is None:
            raise LegacyImportError(f"导入批次不存在：{batch_id}")
        items = self._ledger.list_import_items(batch_id)
        counts: dict[str, Any] = {
            "scanned": len(items),
            "deleted": 0,
            "restored": 0,
            "kept": 0,
            "kept_items": [],
            "missing": 0,
            "no_snapshot": 0,
        }
        for item in items:
            local = self._stores.meta.get_conversation(item.conversation_id)
            local_ms = _ms(local.updated_at) if local is not None else None
            untouched = local_ms is not None and local_ms == item.local_updated_at_ms
            if item.outcome == "created":
                if local is None:
                    counts["missing"] += 1
                elif untouched:
                    self._stores.meta.delete_conversation(item.conversation_id)
                    counts["deleted"] += 1
                else:
                    self._keep(counts, item, local, "本机在导入之后又被改过")
            else:
                snapshot = self.snapshot_path(batch_id, item.conversation_id)
                if not snapshot.is_file():
                    counts["no_snapshot"] += 1
                    counts["kept_items"].append(
                        {
                            "conversation_id": item.conversation_id,
                            "reason": "no_snapshot",
                            "detail": f"回滚快照不在：{snapshot}",
                        }
                    )
                elif not untouched:
                    self._keep(counts, item, local, "本机在导入之后又被改过")
                else:
                    restore = self._read_snapshot(snapshot)
                    self._ledger.write_imported_conversation(restore)
                    counts["restored"] += 1
            if progress is not None:
                progress(counts)
        self._ledger.set_import_state(batch_id, "rolled_back", counts=counts)
        return ImportReport(
            batch_id=batch_id, source=batch.source, state="rolled_back", counts=counts
        )

    @staticmethod
    def _keep(
        counts: dict[str, Any],
        item: ImportItemRecord,
        local: ConversationRecord | None,
        why: str,
    ) -> None:
        """本机改过的那条**保留**，并把"为什么没动它"记进报告。"""
        counts["kept"] += 1
        counts["kept_items"].append(
            {
                "conversation_id": item.conversation_id,
                "reason": REASON_LOCAL_NEWER,
                "detail": f"{why}（台账记 {item.local_updated_at_ms}，本机现在 "
                f"{_ms(local.updated_at) if local is not None else None}）：保留",
            }
        )

    # ------------------------------------------------------------------ 快照

    def snapshot_path(self, batch_id: str, conversation_id: str) -> Path:
        """``<data_dir>/import-rollback/<batch>/<会话>.ndjson``（§1.1）。

        会话 id 是 ``conv_<hex>``（我们自己生成的），但这里仍然只取文件名部分：
        拿一个外部值去拼路径的代码，等哪天 id 变了形状就是一条路径穿越。
        """
        return self._data_dir / ROLLBACK_DIRNAME / batch_id / f"{Path(conversation_id).name}.ndjson"

    def _snapshot(self, batch_id: str, conversation_id: str) -> None:
        """把**要被替换掉的**那条本机会话按同一套 NDJSON 存下来。

        写文件用"先写 ``.part`` 再 ``replace``"：导入中途被杀时，半份快照会被
        **原子替换**挡住——而半份快照比没有快照更坏（回滚会"成功恢复"成半条会话）。
        """
        record = self._stores.meta.get_conversation(conversation_id)
        if record is None:  # pragma: no cover - 调用方刚刚读到过它
            raise LegacyImportError(f"要快照的会话不在了：{conversation_id}")
        transfer = read_transfer(self._stores, record)
        target = self.snapshot_path(batch_id, conversation_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_suffix(".ndjson.part")
        with scratch.open("w", encoding="utf-8", newline="\n") as sink:
            sink.write(header_line(count=1))
            for line in encode_conversation(transfer):
                sink.write(line)
            sink.write(
                footer_line(
                    conversations=1,
                    messages=len(transfer.messages),
                    events=len(transfer.events),
                    artifacts=len(transfer.artifacts),
                )
            )
        scratch.replace(target)

    def _read_snapshot(self, path: Path) -> ConversationTransfer:
        """读回一份快照（**与导入走同一个解析器**：一物两用的那一半）。"""
        reader = _StreamReader()
        transfers: list[ConversationTransfer] = []
        try:
            with path.open("r", encoding="utf-8") as source:
                for raw in source:
                    done = reader.feed(raw)
                    if done is not None:
                        transfers.append(done)
                final = reader.finish()
        except OSError as exc:
            raise SnapshotError(f"回滚快照读不出来：{path}（{exc}）") from exc
        if final is not None:
            transfers.append(final)
        if len(transfers) != 1 or reader.footer is None:
            raise SnapshotError(
                f"回滚快照不完整或不只有一条会话（读到 {len(transfers)} 条，"
                f"末行{'在' if reader.footer else '不在'}）：{path}"
            )
        return transfers[0]

    # ------------------------------------------------------------------ 判定与写入

    def _batch_state(self, batch_id: str) -> str:
        """批次状态（缓存）：判断一条台账"是不是被回滚过"要用它。"""
        state = self._batch_states.get(batch_id)
        if state is None:
            record = self._ledger.get_import_batch(batch_id)
            state = record.state if record is not None else ""
            self._batch_states[batch_id] = state
        return state

    def _decision(self, conversation: ConversationRecord) -> _Decision:
        """这条会话该怎么处理（三条规则里的前两条，逐档写在下面）。"""
        source_ms = _ms(conversation.updated_at) or 0
        item = self._ledger.get_import_item(source=self._source, conversation_id=conversation.id)
        # ① 幂等：这一版已经导过 → 一行不写。被回滚过的批次不算命中（那是历史，
        #    而"回滚之后想再导一次"是正当需求）。
        if (
            item is not None
            and item.source_updated_at_ms == source_ms
            and self._batch_state(item.batch_id) != "rolled_back"
        ):
            return _Decision("skip", REASON_ALREADY_IMPORTED, "这一版已经导过一次（幂等键命中）")
        local = self._stores.meta.get_conversation(conversation.id)
        if local is None:
            return _Decision("create")
        # ② 本机有这条会话，但它不是我们导进来的 → 不覆盖（只有我们写的东西我们才敢覆盖）
        if item is None:
            return _Decision(
                "skip",
                REASON_NOT_OURS,
                f"本机已有这条会话（{local.title or '未命名'}），不是从 {self._source} 导进来的",
            )
        # ③ 覆盖策略 `replace_if_local_untouched`：本机在导入之后又被改过 → 跳过并如实列出
        local_ms = _ms(local.updated_at)
        if (
            local_ms is not None
            and item.local_updated_at_ms is not None
            and local_ms > item.local_updated_at_ms
        ):
            return _Decision(
                "skip",
                REASON_LOCAL_NEWER,
                f"本机有更新的改动（本机 {local_ms} > 导入时 {item.local_updated_at_ms}），没覆盖",
            )
        return _Decision("replace")

    def _localize(self, transfer: ConversationTransfer) -> tuple[ConversationTransfer, int]:
        """本机没有的工作区引用 → 置空，并回一个计数（**真机验收抓到的第二个坑**）。

        `conversations.workspace_id` 在本机库上是**外键**（`ON DELETE SET NULL`），
        而 NAS 上的会话挂的是 NAS 上的工作区（用户的那些"项目"），工作区**不随导入过来**。
        不处理的话：真机实测 302 条导到第 3 条就整批红 ——
        `FOREIGN KEY constraint failed`，批次 `failed`。

        为什么不把工作区也搬过来（两条都否掉）：

        - **不照搬行**：本机档里工作区的含义是"**这台机器上的一个路径**"（方案 §4.2），
          `workspaces.root_path` 又是 NOT NULL —— 把 NAS 的路径原样建一条，
          就是界面里多出一个点开必然报错的项目 ✗；
        - **不静默丢**：丢成 NULL 是"这条会话落到未归档"，用户看得见的是**会话还在**；
          但"有几条掉了归属"必须报数（`counts.unresolved_workspaces`），
          否则"我的会话怎么都不在项目里了"没人解释得了。

        幂等性与它无关：置空只发生在这里，台账记的仍是源端那一条的身份
        （`(source, conversation_id, source_updated_at_ms)`）。
        """
        workspace_id = transfer.conversation.workspace_id
        if workspace_id is None:
            return transfer, 0
        known = self._workspaces.get(workspace_id)
        if known is None:
            known = self._stores.meta.get_workspace(workspace_id) is not None
            self._workspaces[workspace_id] = known
        if known:
            return transfer, 0
        return replace(transfer, conversation=replace(transfer.conversation, workspace_id=None)), 1

    def _write(self, batch_id: str, transfer: ConversationTransfer, *, outcome: str) -> None:
        """写一条会话 + 记一条台账。

        ``local_updated_at_ms`` **写完之后再读回来**（不是拿源端那个值凑）：
        台账里记的必须是"本机这条会话在导入完成那一刻的 ``updated_at``"，
        两条规则（跳过 / 回滚）全靠它比较。源端没给时间的那些边角情形由存储层补
        ``now``，读回来才拿得到真值。
        """
        self._ledger.write_imported_conversation(transfer)
        stored = self._stores.meta.get_conversation(transfer.conversation.id)
        if stored is None:  # pragma: no cover - 刚写进去就读不到，说明存储层坏了
            raise LegacyImportError(f"会话写进去之后读不回来：{transfer.conversation.id}")
        self._ledger.record_import_item(
            ImportItemRecord(
                conversation_id=transfer.conversation.id,
                source=self._source,
                source_updated_at_ms=_ms(transfer.conversation.updated_at) or 0,
                outcome="created" if outcome == "create" else "replaced",
                batch_id=batch_id,
                local_updated_at_ms=_ms(stored.updated_at),
            )
        )


# ------------------------------------------------------------------ CLI


def _local_stores(data_dir: Path) -> StoreBundle:
    """CLI 这条路的装配：**按本机档**建库（服务器档的库不该被 CLI 碰到）。

    环境变量四个与 ``sidecar.pin_local_deployment`` 同一手法（`KYLAB_DATABASE_URL`
    **设空串**而不是删掉——本机 `.env` 里真的配了它，删掉会让它"复活"），
    但**不 import 边车模块**：CLI 没必要把整个边车进程拖进导入图里。

    函数内 import ``app.core.storage`` 是刻意的：装配点只在 CLI 这一条路上用一次
    （`-m` 跑本模块时用不着建库的东西——比如用例注入自己的 StoreBundle）。
    """
    import os

    from app.core.config import get_settings
    from app.core.storage import build_stores

    os.environ["KYLAB_DEPLOYMENT"] = "local"
    os.environ["KYLAB_DATABASE_URL"] = ""
    os.environ["KYLAB_DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()
    return build_stores(get_settings())


def _parse_since(raw: str) -> datetime | None:
    """``--since`` 的解析：ISO 8601（``2026-10-01`` 这种只有日期的也认，按 UTC 当天零点）。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"--since 不是一个 ISO 8601 时刻：{raw}") from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.legacy_import",
        description=(
            "把 NAS 上的旧会话一次性导进本机库（也可以回滚一个批次）。"
            "**不直连服务器库**：走 GET {server}/conversations/export 的 NDJSON 流。"
        ),
    )
    parser.add_argument("--server", required=True, help="NAS 的 API 基址（含 /api/v1）")
    parser.add_argument("--token", default="", help="NAS 的用户会话令牌（只在内存与请求头上）")
    parser.add_argument("--data-dir", required=True, help="本机数据目录（库与回滚快照都在它下面）")
    parser.add_argument(
        "--since", default="", help="只导**严格晚于**这个时刻更新过的会话（ISO 8601）"
    )
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE, help="一次拉几条会话")
    parser.add_argument("--dry-run", action="store_true", help="只报会怎么处理，一个字节都不写")
    parser.add_argument("--rollback", default="", help="回滚这个批次（给批次 id，如 imp_xxxxxxxx）")
    parser.add_argument("--quiet", action="store_true", help="不逐条打进度（只留最后那份报告）")
    return parser


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """CLI 入口：``--dry-run`` 只看不写、``--rollback`` 撤销一个批次。

    ``transport`` 是给用例的注入点（假 NAS，绝不打真网络）；命令行上没有它——
    真实的 CLI 只能连真的 NAS。**失败给一句话 + 退出码 1**，不甩 traceback：
    这个命令是给排障的人用的，栈顶那三行对他没有信息量。
    """
    # 输出编码兜底（与 `scripts/*.py` 同一手法）：Windows 的 GBK 控制台遇到码表外的
    # 字符（标题里的 emoji、罕用汉字）会当场 `UnicodeEncodeError`——这条命令是给排障
    # 的人用的，不该因为某个会话标题里的一个字符而崩在半路。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = _parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    stores = _local_stores(data_dir)
    importer = LegacyImporter(
        stores,
        data_dir=data_dir,
        source=args.server,
        token=args.token,
        since=_parse_since(args.since),
        page_size=args.page_size,
        transport=transport,
    )

    def _progress(counts: dict[str, Any]) -> None:
        if not args.quiet:
            print(json.dumps(counts, ensure_ascii=False), file=sys.stderr, flush=True)

    try:
        return _run_command(args, importer, _progress)
    except LegacyImportError as exc:
        print(f"导入没能进行：{exc}", file=sys.stderr)
        return 1


def _run_command(
    args: argparse.Namespace,
    importer: LegacyImporter,
    progress: Callable[[dict[str, Any]], None],
) -> int:
    """按参数走哪一条（dry-run / rollback / 真导入）——分开写是为了 ``main`` 的
    try/except 只包一件事：**把可预期失败翻成一句话与退出码**。"""
    if args.rollback:
        report = importer.rollback(args.rollback, progress=progress)
        print(json.dumps(report.counts, ensure_ascii=False, indent=2))
        if report.counts.get("no_snapshot"):
            print(
                f"批次 {report.batch_id}：已回滚，但有 {report.counts['no_snapshot']} 条"
                "没有快照可恢复（见上面那几条详细的）",
                file=sys.stderr,
            )
            return 1
        print(f"批次 {report.batch_id}：{report.state}")
        return 0
    if args.dry_run:
        plan = importer.plan(progress=progress)
        print(json.dumps(plan.counts(), ensure_ascii=False, indent=2))
        print(
            f"只看不写：新增 {len(plan.created)} / 替换 {len(plan.replaced)} / "
            f"跳过 {len(plan.skipped)}；"
            f"未随导入的文件引用 {plan.file_references} 条（文件本体在 NAS 上）"
        )
        return 0
    report = importer.run(progress=progress)
    print(json.dumps(report.counts, ensure_ascii=False, indent=2))
    if report.ok:
        print(f"批次 {report.batch_id}：{report.state}")
        return 0
    print(f"批次 {report.batch_id} 失败：{report.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
