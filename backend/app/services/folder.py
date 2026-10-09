"""知识库内的目录（v13）。

用户要求"知识库里要能新建目录"。两处设计取舍：

1. **单层、不嵌套**：个人知识库的规模下，一层分类就够把不同用途的文件分开；
   嵌套会立刻带来拖拽跨层、路径拼接、删除策略一串复杂度，收益却要等库里
   真的分了几十类才出现。要嵌套时再加 ``parent_id`` 迁移即可。
2. **非空目录拒绝删**：把里面的文件悄悄挪回根，用户会以为文件丢了；
   拒绝时把"还有几篇"说清楚，比删掉再解释好。
"""

from __future__ import annotations

import uuid

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.storage.base import FolderRecord, StoreBundle

__all__ = ["FOLDER_NAME_MAX_CHARS", "FolderService"]

#: 目录名长度上限。够长到能写清"2026 年 Q1 合同"，又不至于把树撑爆。
FOLDER_NAME_MAX_CHARS = 64


class FolderService:
    """目录的建、列、改名、删，以及"把文档放进目录"。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    def create(self, kb_id: str, name: str) -> FolderRecord:
        self._require_kb(kb_id)
        cleaned = _clean_name(name)
        if any(item.name == cleaned for item in self._stores.meta.list_folders(kb_id)):
            # 先查一次给出可读文案（UNIQUE 约束是最后一道，报的是英文约束名）
            raise ConflictError(f"目录已存在：{cleaned}")
        return self._stores.meta.create_folder(
            FolderRecord(id=f"fld_{uuid.uuid4().hex[:12]}", kb_id=kb_id, name=cleaned)
        )

    def list(self, kb_id: str) -> list[FolderRecord]:
        self._require_kb(kb_id)
        return self._stores.meta.list_folders(kb_id)

    def counts(self, kb_id: str) -> dict[str, int]:
        """``{目录 id: 文档数}``。**一次 GROUP BY 取全**，不是逐个目录查。"""
        return self._stores.meta.count_documents_by_folders(kb_id)

    def get(self, folder_id: str) -> FolderRecord:
        record = self._stores.meta.get_folder(folder_id)
        if record is None:
            raise NotFoundError(f"目录不存在：{folder_id}")
        return record

    def rename(self, folder_id: str, name: str) -> FolderRecord:
        record = self.get(folder_id)
        cleaned = _clean_name(name)
        if cleaned != record.name and any(
            item.name == cleaned for item in self._stores.meta.list_folders(record.kb_id)
        ):
            raise ConflictError(f"目录已存在：{cleaned}")
        self._stores.meta.rename_folder(folder_id, cleaned)
        return self.get(folder_id)

    def delete(self, folder_id: str) -> None:
        """删目录；**非空则拒绝**（理由见模块说明）。"""
        record = self.get(folder_id)
        count = self._stores.meta.count_documents_by_folders(record.kb_id).get(folder_id, 0)
        if count:
            raise ConflictError(
                f"「{record.name}」里还有 {count} 篇文档；先把它们移走或删除，再删目录"
            )
        self._stores.meta.delete_folder(folder_id)

    def move_document(self, document_id: str, folder_id: str | None) -> None:
        """把文档移进目录；``folder_id=None`` 表示移回根目录。"""
        document = self._stores.meta.get_document(document_id)
        if document is None:
            raise NotFoundError(f"文档不存在：{document_id}")
        if folder_id is not None:
            folder = self.get(folder_id)
            if folder.kb_id != document.knowledge_base_id:
                # 不校验的话会把文档挂到别的库的目录上——列表按目录查时就"消失"了
                raise InvalidRequestError("目标目录不属于这份文档所在的知识库")
        self._stores.meta.set_document_folder(document_id, folder_id)

    # ------------------------------------------------------------------ 内部

    def _require_kb(self, kb_id: str) -> None:
        if self._stores.meta.get_knowledge_base(kb_id) is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")


def _clean_name(name: str) -> str:
    """压平空白并校验长度：目录名会进 URL 查询与列表，带换行会很难看。"""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise InvalidRequestError("目录名不能为空")
    if len(cleaned) > FOLDER_NAME_MAX_CHARS:
        raise InvalidRequestError(f"目录名不能超过 {FOLDER_NAME_MAX_CHARS} 个字符")
    return cleaned
