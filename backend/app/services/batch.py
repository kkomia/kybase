"""文档批量动作（多选之后的删除 / 重新摄入）。

**为什么单列一个服务而不是让接口层循环调单文档端点**：
批量动作的语义核心是"**部分失败**"——10 篇里 2 篇没删掉要如实说清是哪两篇、
为什么。把这段逻辑摊在 API 层，等于让协议适配层承担业务决策；放在这里还能被
单测直接覆盖。服务层对外只回一张逐条结果表，接口层原样转发。

**逐条成败而不是全有全无**：批量删除里有一篇已经被别人删掉了，不该让另外 9 篇
也跟着失败；反过来，为了"原子性"把 10 篇包进一个事务，会让一次误选变成整体回滚，
用户还得重新勾一遍。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.exceptions import InvalidRequestError, KylabError
from app.models.enums import DocumentStage
from app.services.documents import DocumentService
from app.services.folder import FolderService
from app.services.lifecycle import LifecycleService
from app.storage.base import StoreBundle

__all__ = ["BATCH_ALL_LIMIT", "BatchItem", "DocumentBatchService"]

BATCH_ACTIONS = ("delete", "reprocess", "move", "enable", "disable", "questions")
"""支持的批量动作。加动作时同步改 API 的 ``Literal`` 与前端类型。"""

BATCH_ALL_LIMIT = 2000
"""``all=True`` 一次最多覆盖多少篇。

**为什么要有个上限**：``all`` 服务的是"改完切分参数整库重跑"——真跑起来每一篇
都要走一次云端解析（要钱、要配额、要几十分钟），而它对用户只是一个点击。
超过上限就拒绝并说清怎么办（分批，或走 API 自己控节奏），
比"静默排了五万个任务"负责。这个数字是产品判断，不是技术限制。
"""


@dataclass(frozen=True, slots=True)
class BatchItem:
    """一条结果。``error`` 为空即成功。"""

    document_id: str
    ok: bool
    error: str | None = None


class DocumentBatchService:
    def __init__(
        self,
        stores: StoreBundle,
        documents: DocumentService,
        lifecycle: LifecycleService,
        folders: FolderService,
    ) -> None:
        self._stores = stores
        self._documents = documents
        self._lifecycle = lifecycle
        self._folders = folders

    def run(
        self,
        kb_id: str,
        action: str,
        document_ids: list[str],
        *,
        folder_id: str | None = None,
        all_documents: bool = False,
    ) -> list[BatchItem]:
        """对一批文档执行同一个动作，逐条返回结果。

        **先校验归属再动手**：请求里的 id 可能来自另一个库（多标签页、手工构造），
        直接删就是越权。不属于本库的一律记成失败，不进入动作分支。

        ``folder_id`` 只对 ``move`` 有意义：给 id 就是移进那个目录，给 ``None``
        是移回根目录。目录归属由 ``FolderService.move_document`` 校验。

        ``all_documents=True``（v17）时由服务端解析全集，忽略传进来的 ids：
        "改了切分参数要整库重跑"是库级动作，让界面先取一遍文档 id 再回传
        纯属绕路。上限见 ``BATCH_ALL_LIMIT``。
        """
        if action not in BATCH_ACTIONS:
            raise ValueError(f"不支持的批量动作：{action}")

        if all_documents:
            document_ids = self._all_document_ids(kb_id)
        elif not document_ids:
            raise InvalidRequestError("没有指定要处理的文档")

        results: list[BatchItem] = []
        for document_id in document_ids:
            document = self._stores.meta.get_document(document_id)
            if document is None or document.knowledge_base_id != kb_id:
                results.append(BatchItem(document_id, False, "文档不存在或不属于这个知识库"))
                continue
            try:
                self._apply(action, document_id, folder_id=folder_id)
            except KylabError as exc:
                # 领域异常带可读文案（"已索引完成"之类），原样透给用户
                results.append(BatchItem(document_id, False, str(exc)))
            else:
                results.append(BatchItem(document_id, True))
        return results

    def _all_document_ids(self, kb_id: str) -> list[str]:
        """整库的目标集合。**排除正在跑的**：给已经在队列里的文档再排一次
        只会让同一篇被解析两遍（第二遍还会撞上"已在处理中"），白花钱。

        判据用 ``DocumentStage`` 而不是字符串：阶段是 StrEnum，拿字符串集合比
        会永远不命中（枚举成员不等于它的字符串值），那样这个过滤器就成了摆设。
        """
        pending = {
            DocumentStage.UPLOADED,
            DocumentStage.PROBING,
            DocumentStage.PARSING,
            DocumentStage.CHUNKING,
            DocumentStage.EMBEDDING,
        }
        records = self._stores.meta.list_documents(kb_id)
        if not records:
            raise InvalidRequestError("这个库还没有文档")
        ids = [record.id for record in records if record.stage not in pending]
        if not ids:
            # 有文档但全在跑：与"空库"是两件事，文案分开——否则用户会以为文档丢了
            raise InvalidRequestError("这个库的文档都还在处理中，请等它们跑完再操作")
        if len(ids) > BATCH_ALL_LIMIT:
            raise InvalidRequestError(
                f"这个库有 {len(ids)} 篇文档，一次最多处理 {BATCH_ALL_LIMIT} 篇；"
                "请分批操作，或通过 API 自行控制节奏"
            )
        return ids

    def _apply(self, action: str, document_id: str, *, folder_id: str | None = None) -> None:
        if action == "delete":
            self._lifecycle.delete_document(document_id)
            return
        if action == "move":
            # `folder_id=None` 是"移回根目录"；目标目录的归属由 folder 服务校验
            self._folders.move_document(document_id, folder_id)
            return
        if action in ("enable", "disable"):
            # 与切块级禁用同一套语义：只动标记，恢复零成本
            self._documents.set_disabled(document_id, action == "disable")
            return
        if action == "questions":
            # 补生成分段问题（v24）：只有已索引的文档收得下，非索引进来的那条
            # 会抛 ConflictError，被 run() 记成这条的失败原因（部分失败是正常结果）
            self._documents.enqueue_questions(document_id)
            return
        # 重跑：force=True 才允许对已索引的文档重新入队（见 DocumentService.enqueue_ingest）
        self._documents.enqueue_ingest(document_id, force=True)
