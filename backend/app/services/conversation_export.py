r"""会话**搬运**的线格式与服务：六型 NDJSON 信封（M2 阶段 5，方案 §3.1）。

同一个格式，三个地方在用（方案 §1.1 那句"与导出同一套格式，一物两用"）：

| 用途 | 方向 | 落点 |
| --- | --- | --- |
| NAS 那侧的导出端点 | 编 | `GET /api/v1/conversations/export`（服务器档照常跑） |
| 本机导入器 | 解 | `services/legacy_import.py` |
| 回滚快照 | 编 → 存 → 解 | `<data_dir>/import-rollback/<batch>/<会话>.ndjson` |

## 六型信封（首行 / 末行是**契约的一部分**，不是注释）

```
{"type":"header",     "export_version":1, "generated_at":"…", "count":N}
{"type":"conversation","data":{…会话那一行…}}
{"type":"message",     "conversation_id":"…", "data":{…}}
{"type":"event",       "conversation_id":"…", "data":{…}}
{"type":"artifact",    "conversation_id":"…", "data":{…}}
{"type":"footer",     "conversations":N, "messages":M, "events":E, "artifacts":A}
```

首行的 ``count`` 是"这一页扫过几条会话"，末行的四个数是"这一页实际发了多少"——
两者不同就说明这一页里有会话在读取过程中变了/没了（读的人一眼看得出来，
而不是拿到半份数据还以为导完了）。

## 为什么 ``data`` 块是**库列原样**（毫秒整数），不是 API 那套 ISO 口径

这份流的用途是**搬运**，不是给人读的（给人读的那份是 `GET /conversations`）：

- 会话那一块就是 ``conversations`` 表的那 14 列（含**不进 API** 的
  ``context_summary`` / ``summary_upto``——§1.3：丢了它们，导进来的长期会话就失去了
  "已经被压缩到哪里"，续聊会从头重算）；
- 时间列一律是 **UTC 毫秒整数**，与库里那一列逐字同名（``created_at_ms``）。
  换成 ISO 会引入一次"格式化再解析"的往返，而幂等键要的正是**一个不会漂的整数**：
  ``import_items.source_updated_at_ms`` 就是源端的 ``updated_at_ms`` 原样。

这与《API 接口规范》§1.2 的"一律 ISO 8601"**不冲突**：那条管的是 REST 的 JSON 响应，
而本文件是给搬运用的行格式（导出端点的 ``media_type`` 是 ``application/x-ndjson``，
而且它不进前端的类型生成——OpenAPI 里它只是一个 200 响应）。

## 内存上界：一条会话

编（``ConversationExportService.stream``）与解（``legacy_import``）都是**流式**的：
编的时候逐条会话现读现发，解的时候攒完一条就落库。所以几个 GB 的导出流也不需要
几个 GB 的内存——R3"大 transcript 导入慢"的缓解里，一半靠的是这个形状。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import InvalidRequestError
from app.services.artifacts import ArtifactService
from app.services.conversation import ConversationService
from app.storage.base import (
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    SessionEventRecord,
    StoreBundle,
)

__all__ = [
    "ARTIFACT",
    "CONVERSATION",
    "EVENT",
    "EXPORT_VERSION",
    "FOOTER",
    "HEADER",
    "MEDIA_TYPE",
    "MESSAGE",
    "PAGE_SIZE",
    "ConversationExportService",
    "ExportEvent",
    "artifact_from_data",
    "conversation_from_data",
    "encode_conversation",
    "event_from_data",
    "footer_line",
    "header_line",
    "message_from_data",
    "parse_line",
    "read_transfer",
    "summary_from_data",
]

#: 线格式的版本号（首行那个 ``export_version``）。
#:
#: 它是**唯一的兼容判据**：导入端不认识某个版本时**当场拒绝**，而不是"尽力读一读"
#: ——猜着读一份改了形状的流，结果是半截数据进了用户库里。
EXPORT_VERSION = 1

#: 响应的媒体类型（导出端点用它；不是 ``application/json``，因为一行一个 JSON）。
MEDIA_TYPE = "application/x-ndjson"

#: 一页最多导几条会话。与导出端点的 ``limit`` 上限同一个数（那边也是一个常量，
#: 两处写同一个数字是因为它们说的是同一件事：一次最多扫多少条）。
PAGE_SIZE = 200

#: 六型信封的 ``type`` 取值。写成常量而不是散在各处的字面量：编与解各写一份字符串，
#: 迟早有一处拼错——而拼错的表现是"导入静默少了几百条消息"。
HEADER = "header"
CONVERSATION = "conversation"
MESSAGE = "message"
EVENT = "event"
ARTIFACT = "artifact"
FOOTER = "footer"

_KINDS = frozenset({HEADER, CONVERSATION, MESSAGE, EVENT, ARTIFACT, FOOTER})

#: 一行的字符上限：单条消息的正文可能很长（几十万字的附件正文），给足空间。
#: 它的用途是挡住"把二进制/整份文件当成一行读进来"这种离谱输入，不是限制正文长度。
MAX_LINE_CHARS = 32 * 1024 * 1024


class ExportEvent:
    """解出来的一行（``kind`` + ``data``；只有消息/事件/产物带 ``conversation_id``）。

    刻意是一个轻量容器而不是 pydantic 模型：它是**进程内**的解码结果，两端都在我们
    自己手里；给它套一层校验框架只会让"这一行到底怎么来的"更难读。
    """

    __slots__ = ("conversation_id", "data", "kind")

    def __init__(self, kind: str, data: dict[str, Any], conversation_id: str = "") -> None:
        self.kind = kind
        self.data = data
        self.conversation_id = conversation_id

    def __repr__(self) -> str:  # pragma: no cover - 只为排障时可读
        return f"ExportEvent({self.kind!r}, conversation_id={self.conversation_id!r})"


# ------------------------------------------------------------------ 编


def _line(payload: dict[str, Any]) -> str:
    """一行 JSON（``ensure_ascii=False``：正文里的中文原样过线，体积也小一截）。"""
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _ms(value: datetime | None) -> int | None:
    """``datetime`` → UTC 毫秒（与 ``sqlite_impl`` 的 ``_dump`` 同一个口径）。

    naive 一律按 UTC 解释：这个格式只存毫秒，而"没有时区的 datetime 算哪个时区"
    只能有一个答案（按本机时区会让同一份数据在两台机器上落成两个值）。
    截断而不是四舍五入，理由同 ``_dump``。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def header_line(*, count: int, generated_at: datetime | None = None) -> str:
    """首行。``count`` = 这一页**扫过**几条会话（末行那四个数是实际发出去的）。"""
    moment = generated_at or datetime.now(UTC)
    return _line(
        {
            "type": HEADER,
            "export_version": EXPORT_VERSION,
            "generated_at": moment.astimezone(UTC).isoformat(),
            "count": int(count),
        }
    )


def conversation_line(transfer: ConversationTransfer) -> str:
    """会话那一行——**表列原样**，含两个不进 API 的摘要列。"""
    item = transfer.conversation
    return _line(
        {
            "type": CONVERSATION,
            "data": {
                "id": item.id,
                "title": item.title,
                "kb_ids": list(item.kb_ids),
                "owner_id": item.owner_id,
                "model_pk": item.model_pk,
                # 三态列（None = 跟随全局默认）原样过线：折成 false 就丢了一档
                "thinking": item.thinking,
                "thinking_effort": item.thinking_effort,
                "pinned": bool(item.pinned),
                "workspace_id": item.workspace_id,
                "archived_at_ms": _ms(item.archived_at),
                "created_at_ms": _ms(item.created_at),
                "updated_at_ms": _ms(item.updated_at),
                "context_summary": transfer.summary,
                "summary_upto": transfer.summary_upto,
            },
        }
    )


def message_line(conversation_id: str, message: ChatMessageRecord) -> str:
    return _line(
        {
            "type": MESSAGE,
            "conversation_id": conversation_id,
            "data": {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "sources": [dict(item) for item in message.sources],
                "steps": [dict(item) for item in message.steps],
                "thinking": message.thinking,
                "attachments": [dict(item) for item in message.attachments],
                "created_at_ms": _ms(message.created_at),
            },
        }
    )


def event_line(conversation_id: str, event: SessionEventRecord) -> str:
    """``seq`` **必须过线**：它是事件顺序的唯一真相（同一毫秒里的并发工具调用靠它）。"""
    return _line(
        {
            "type": EVENT,
            "conversation_id": conversation_id,
            "data": {
                "seq": int(event.seq),
                "kind": event.kind,
                "payload": dict(event.payload),
                "created_at_ms": _ms(event.created_at),
            },
        }
    )


def artifact_line(conversation_id: str, artifact: ConversationArtifactRecord) -> str:
    """产物记录照发，``location`` **保原样**（R4：文件本体不随导入过来）。"""
    return _line(
        {
            "type": ARTIFACT,
            "conversation_id": conversation_id,
            "data": {
                "id": artifact.id,
                "name": artifact.name,
                "format": artifact.format,
                "size_bytes": int(artifact.size_bytes),
                "storage": artifact.storage,
                "location": artifact.location,
                "workspace_id": artifact.workspace_id,
                "owner_id": artifact.owner_id,
                "knowledge_base_id": artifact.knowledge_base_id,
                "document_id": artifact.document_id,
                "created_at_ms": _ms(artifact.created_at),
            },
        }
    )


def footer_line(*, conversations: int, messages: int, events: int, artifacts: int) -> str:
    return _line(
        {
            "type": FOOTER,
            "conversations": int(conversations),
            "messages": int(messages),
            "events": int(events),
            "artifacts": int(artifacts),
        }
    )


def encode_conversation(transfer: ConversationTransfer) -> Iterator[str]:
    """一条会话的全部行（会话 → 消息 → 事件 → 产物，**顺序固定**）。

    顺序写死在这里是有用的：解的人可以一路攒到"下一条会话"才落库，
    而不必先读完整个流。
    """
    conversation_id = transfer.conversation.id
    yield conversation_line(transfer)
    for message in transfer.messages:
        yield message_line(conversation_id, message)
    for event in transfer.events:
        yield event_line(conversation_id, event)
    for artifact in transfer.artifacts:
        yield artifact_line(conversation_id, artifact)


# ------------------------------------------------------------------ 解


def _require(data: Any, key: str, *, kind: str) -> Any:
    """取一个**必填**字段；缺了就报清楚（含是哪个类型的哪一块）。"""
    if not isinstance(data, dict) or key not in data:
        raise InvalidRequestError(f"导出流里 {kind} 那一块缺字段：{key}")
    return data[key]


def parse_line(raw: str) -> ExportEvent | None:
    """一行 → :class:`ExportEvent`；空行给 ``None``，坏行**报错**（不猜）。

    坏行的处置与"版本不认识"同一条纪律：这是搬运，猜错的代价是用户库里的会话
    少了半截，而那种损坏事后极难发现（所以宁可当场失败，让台账记下 ``failed``）。
    """
    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_LINE_CHARS:
        raise InvalidRequestError(f"导出流里有一行超过 {MAX_LINE_CHARS} 字符：这不像是本格式")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(f"导出流里有一行不是 JSON：{text[:120]!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidRequestError("导出流的每一行都必须是一个 JSON 对象")
    kind = str(payload.get("type") or "")
    if kind not in _KINDS:
        raise InvalidRequestError(f"导出流里出现了不认识的块类型：{kind or '(空)'}")
    if kind == HEADER:
        version = payload.get("export_version")
        if version != EXPORT_VERSION:
            raise InvalidRequestError(
                f"导出流的格式版本是 {version!r}，本机只认识 {EXPORT_VERSION}；"
                "请升级本机应用（不认识的版本**不猜着读**）"
            )
        return ExportEvent(kind, payload)
    if kind == FOOTER:
        return ExportEvent(kind, payload)
    data = payload.get("data")
    conversation_id = str(payload.get("conversation_id") or "")
    if not isinstance(data, dict):
        raise InvalidRequestError(f"导出流里 {kind} 那一块缺 data")
    if kind != CONVERSATION and not conversation_id:
        raise InvalidRequestError(f"导出流里 {kind} 那一块缺 conversation_id")
    return ExportEvent(kind, data, conversation_id)


def _load_ms(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def conversation_from_data(data: dict[str, Any]) -> ConversationRecord:
    """会话那一块 → 记录。**表列原样**，没有任何推导。"""
    return ConversationRecord(
        id=str(_require(data, "id", kind=CONVERSATION)),
        title=str(data.get("title") or ""),
        kb_ids=tuple(str(item) for item in data.get("kb_ids") or ()),
        owner_id=data.get("owner_id"),
        model_pk=data.get("model_pk"),
        # 三态列：None 保持 None（"跟随全局默认"是一个真实状态，不折成 False）
        thinking=None if data.get("thinking") is None else bool(data["thinking"]),
        thinking_effort=data.get("thinking_effort"),
        pinned=bool(data.get("pinned")),
        workspace_id=data.get("workspace_id"),
        archived_at=_load_ms(data.get("archived_at_ms")),
        created_at=_load_ms(data.get("created_at_ms")),
        updated_at=_load_ms(data.get("updated_at_ms")),
    )


def message_from_data(data: dict[str, Any], conversation_id: str) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=str(_require(data, "id", kind=MESSAGE)),
        conversation_id=conversation_id,
        role=str(_require(data, "role", kind=MESSAGE)),
        content=str(data.get("content") or ""),
        sources=tuple(dict(item) for item in data.get("sources") or ()),
        steps=tuple(dict(item) for item in data.get("steps") or ()),
        thinking=str(data.get("thinking") or ""),
        attachments=tuple(dict(item) for item in data.get("attachments") or ()),
        created_at=_load_ms(data.get("created_at_ms")),
    )


def event_from_data(data: dict[str, Any], conversation_id: str) -> SessionEventRecord:
    return SessionEventRecord(
        conversation_id=conversation_id,
        kind=str(_require(data, "kind", kind=EVENT)),
        payload=dict(data.get("payload") or {}),
        seq=int(_require(data, "seq", kind=EVENT)),
        created_at=_load_ms(data.get("created_at_ms")),
    )


def artifact_from_data(data: dict[str, Any], conversation_id: str) -> ConversationArtifactRecord:
    return ConversationArtifactRecord(
        id=str(_require(data, "id", kind=ARTIFACT)),
        conversation_id=conversation_id,
        name=str(data.get("name") or ""),
        format=str(data.get("format") or ""),
        size_bytes=int(data.get("size_bytes") or 0),
        storage=str(data.get("storage") or "object"),
        # **location 保原样**：它指向 NAS 上那个 key，本机没有那份文件（R4）
        location=str(data.get("location") or ""),
        workspace_id=data.get("workspace_id"),
        owner_id=data.get("owner_id"),
        knowledge_base_id=data.get("knowledge_base_id"),
        document_id=data.get("document_id"),
        created_at=_load_ms(data.get("created_at_ms")),
    )


def summary_from_data(data: dict[str, Any]) -> tuple[str, str | None]:
    """会话那一块里那两个**不进 API 的摘要列**（§1.3）。"""
    return str(data.get("context_summary") or ""), data.get("summary_upto")


# ------------------------------------------------------------------ 一条会话的读法（两端共用）


def read_transfer(stores: StoreBundle, conversation: ConversationRecord) -> ConversationTransfer:
    """从仓储里读全一条会话（会话行 + 摘要 + 消息 + 事件 + 产物）。

    **导出、回滚快照两处共用这一份读法**：两处各写一遍的话，迟早有一处漏掉新出现的
    东西（这个项目上已经发生过——摘要那两列就是这么容易被人忘掉），而快照少一列
    意味着"回滚之后会话回不到原样"。

    用现有的 ``list_*`` 与 ``get_conversation_summary``，**不新增存储方法**（§3.1）。
    """
    summary, upto = stores.meta.get_conversation_summary(conversation.id)
    return ConversationTransfer(
        conversation=conversation,
        summary=summary,
        summary_upto=upto,
        messages=stores.meta.list_messages(conversation.id),
        events=stores.meta.list_session_events(conversation.id),
        artifacts=stores.meta.list_artifacts(conversation.id),
    )


# ------------------------------------------------------------------ 导出服务（服务器那侧）


class ConversationExportService:
    """导出端点背后那一段：挑出可见的会话，逐条编成 NDJSON 行。

    **装配点**是 ``core/services.py``（与其它服务一样），API 层只调 ``stream``——
    协议层不许碰存储（工程规范 §3.3 的 L1：``api/`` 里出现 ``app.storage`` 直接红），
    而"哪些会话可见""归档的算不算""since 怎么比"这些判断都压在存储之上，
    只能在服务层做。
    """

    def __init__(
        self,
        stores: StoreBundle,
        *,
        conversations: ConversationService,
        artifacts: ArtifactService,
    ) -> None:
        self._stores = stores
        self._conversations = conversations
        self._artifacts = artifacts

    # ---- 可见性

    def visible(
        self, *, owner_id: str | None, since: datetime | None = None
    ) -> list[ConversationRecord]:
        """这次导出看得到哪些会话：**含归档的**，按归属过滤，再按 ``since`` 取增量。

        三处刻意的取舍：

        - **归档的会话也要导**：归档不是删除（§1.3 的语义）。漏掉它们等于用户导完之后
          发现"我归档过的那批没了"。两条列表都取（未归档在前、归档在后）——
          `ConversationService.list` 一次只给一档，那是它既有的口径；
        - **只看不是自己的那条**：``owner_id`` 就是 ``conversations.py::_caller_owner``
          给出来的（成员 = 自己，管理员/本机主人 = ``None`` = 不过滤）；
        - ``since`` 用**严格大于**：既导过一次再导增量时，把上次那批的时间点原样传进来
          不该把同一批再导一遍（幂等键兜得住重复，但报告会变得没法读）。

        过滤发生在 Python 侧而不是 SQL：会话量级是几百条，而为这一个场景加一条
        带 ``since`` 的仓储方法不值（与 `ConversationService.list` 里那段说明同一取舍）。
        """
        active = self._conversations.list(limit=None, owner_id=owner_id, archived=False)
        archived = self._conversations.list(limit=None, owner_id=owner_id, archived=True)
        records = [*active, *archived]
        if since is None:
            return records
        return [item for item in records if item.updated_at is not None and item.updated_at > since]

    def transfer(self, conversation_id: str) -> ConversationTransfer:
        """读全一条会话（与回滚快照用的是**同一个**读法，见 ``read_transfer``）。"""
        record = self._conversations.get(conversation_id)
        return read_transfer(self._stores, record)

    # ---- 流

    def stream(
        self,
        *,
        owner_id: str | None,
        limit: int = PAGE_SIZE,
        offset: int = 0,
        since: datetime | None = None,
        generated_at: datetime | None = None,
    ) -> Iterator[str]:
        """把一页会话编成 NDJSON 行——**流式**，一次只有一条会话在内存里。

        ``offset`` / ``limit`` 是"扫过的会话数"游标（含归档那一批）：导入端按会话写库，
        不关心谁先谁后，所以游标只要**稳定**就行（同一份数据、同一个 offset 给同一批）。
        断点重跑也靠它（会话级幂等，见 `legacy_import`）。
        """
        records = self.visible(owner_id=owner_id, since=since)
        page = records[max(0, offset) : max(0, offset) + max(1, limit)]
        yield header_line(count=len(page), generated_at=generated_at)

        sent = messages = events = artifact_count = 0
        for record in page:
            # **逐条现读现发**（不是先攒一页）：一条会话的正文可能很长，
            # 攒一页再发等于把整页搬进内存——而导出正是最可能遇到长会话的那条路。
            transfer = read_transfer(self._stores, record)
            yield from encode_conversation(transfer)
            sent += 1
            messages += len(transfer.messages)
            events += len(transfer.events)
            artifact_count += len(transfer.artifacts)
        yield footer_line(
            conversations=sent, messages=messages, events=events, artifacts=artifact_count
        )
