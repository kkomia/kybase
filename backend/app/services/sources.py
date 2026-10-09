"""数据源服务：登记、拉取、逐条入库（M6 / T6.1–T6.3）。

把「连接器」（怎么抓）与「摄入」（抓来之后怎么办）接起来。

**为什么拉取要排队而不是同步做**：一个 RSS 源可能有几十条，每条都要解析、
切块、向量化——同步做会让 HTTP 请求挂几分钟。所以 ``sync_async`` 只入队一个
``FETCH_SOURCE`` 任务，真正的活在 worker 里干。而 ``sync_now`` 保留同步版本
给测试与"我就想立刻看到结果"的场景。
"""

from __future__ import annotations

import logging
import uuid

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.models.enums import DataSourceKind, TaskKind, TaskState
from app.services.connectors.base import build_connector
from app.storage.base import DataSourceRecord, StoreBundle, TaskRecord

__all__ = ["SourceService", "SyncOutcome"]

logger = logging.getLogger(__name__)

#: 允许创建的数据源类型。WebDAV 明确排除（架构 §14 缓做）。
SUPPORTED_KINDS = (DataSourceKind.HTML, DataSourceKind.RSS)


class SyncOutcome:
    """一次拉取的结果。"""

    __slots__ = ("created", "duplicates", "errors", "fetched", "not_modified", "source_id")

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.fetched = 0
        """连接器取回几条。"""
        self.created = 0
        """真正新入库几条。"""
        self.duplicates = 0
        """判定为重复而跳过的条数（内容 hash 命中已有文档）。"""
        self.not_modified = False
        self.errors: list[str] = []

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "fetched": self.fetched,
            "created": self.created,
            "duplicates": self.duplicates,
            "not_modified": self.not_modified,
            "errors": self.errors,
        }


class SourceService:
    """数据源的登记、启停与拉取。"""

    def __init__(self, stores: StoreBundle, ingest, documents) -> None:  # type: ignore[no-untyped-def]
        self._stores = stores
        self._ingest = ingest
        # 入队摄入在 DocumentService 上（它管文档生命周期），不在 IngestService 上。
        # IngestService 只负责"把一份已登记的文档跑到索引完成"
        self._documents = documents

    # ------------------------------------------------------------------ 登记

    def create(
        self,
        *,
        knowledge_base_id: str,
        kind: DataSourceKind,
        name: str,
        url: str,
        max_items: int | None = None,
    ) -> DataSourceRecord:
        if kind not in SUPPORTED_KINDS:
            raise InvalidRequestError(
                f"暂不支持的数据源类型：{kind}（当前支持 HTML 与 RSS）"
            )
        cleaned_url = url.strip()
        if not cleaned_url.startswith(("http://", "https://")):
            # 只允许 http(s)：file:// 之类会变成任意文件读取
            raise InvalidRequestError("数据源地址必须是 http 或 https")
        if self._stores.meta.get_knowledge_base(knowledge_base_id) is None:
            raise NotFoundError(f"知识库不存在：{knowledge_base_id}")

        config: dict[str, object] = {"url": cleaned_url}
        if max_items is not None:
            config["max_items"] = max_items

        return self._stores.meta.create_data_source(
            DataSourceRecord(
                id=f"ds_{uuid.uuid4().hex[:12]}",
                knowledge_base_id=knowledge_base_id,
                kind=kind,
                name=name.strip() or cleaned_url,
                config=config,
            )
        )

    def get(self, source_id: str) -> DataSourceRecord:
        record = self._stores.meta.get_data_source(source_id)
        if record is None:
            raise NotFoundError(f"数据源不存在：{source_id}")
        return record

    def list(self, knowledge_base_id: str) -> list[DataSourceRecord]:
        return self._stores.meta.list_data_sources(knowledge_base_id)

    def list_all(self) -> list[DataSourceRecord]:
        return self._stores.meta.list_all_data_sources()

    def set_enabled(self, source_id: str, *, enabled: bool) -> DataSourceRecord:
        record = self.get(source_id)
        record.enabled = enabled
        self._stores.meta.update_data_source(record)
        return record

    def delete(self, source_id: str) -> None:
        """删数据源。

        **已经抓进来的文档不删。** 它们是知识库的正式内容，可能已被引用；
        停掉订阅不等于要撤销已经收集的资料。用户真要清掉，去文档列表删。
        """
        self.get(source_id)
        self._stores.meta.delete_data_source(source_id)

    # ------------------------------------------------------------------ 拉取

    def sync_async(self, source_id: str) -> str:
        """入队一次拉取，返回任务 id。"""
        source = self.get(source_id)
        task = self._stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=TaskKind.FETCH_SOURCE,
                state=TaskState.PENDING,
                payload={"source_id": source.id},
            )
        )
        logger.info("数据源「%s」已入队拉取（任务 %s）", source.name, task.id)
        return task.id

    def sync_now(self, source_id: str) -> SyncOutcome:
        """立刻拉一次（worker 与测试用）。

        失败**向上抛**：由 worker 决定重试还是记错。这里吞掉的话，
        "拉取失败"就只剩日志里一行，界面上永远显示上次成功的样子。
        """
        source = self.get(source_id)
        outcome = SyncOutcome(source.id)

        connector = build_connector(source.kind)
        result = connector.fetch(source)

        if result.not_modified:
            outcome.not_modified = True
            # 304 也要记时间：否则界面上"上次拉取"会一直停在很久以前，
            # 用户以为定时任务坏了
            self._stores.meta.mark_data_source_pulled(source.id, etag=source.etag)
            return outcome

        outcome.fetched = len(result.items)
        for item in result.items:
            try:
                submitted = self._ingest.submit(
                    knowledge_base_id=source.knowledge_base_id,
                    filename=item.filename,
                    content=item.content,
                    mime_type=item.mime_type,
                )
            except Exception as exc:
                # 单条失败不放弃整批：其余条目照样入库，失败的记进 errors
                logger.warning("数据源「%s」的条目「%s」入库失败：%s", source.name, item.title, exc)
                outcome.errors.append(f"{item.title}: {exc}")
                continue

            if submitted.is_duplicate:
                outcome.duplicates += 1
                continue

            outcome.created += 1
            # 新入库的文档立刻排队摄入，不必等下一次拉取
            self._documents.enqueue_ingest(submitted.document.id)

        self._stores.meta.mark_data_source_pulled(source.id, etag=result.etag)
        logger.info(
            "数据源「%s」拉取完成：取回 %d、新入库 %d、重复 %d、失败 %d",
            source.name,
            outcome.fetched,
            outcome.created,
            outcome.duplicates,
            len(outcome.errors),
        )
        return outcome
