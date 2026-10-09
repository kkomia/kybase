"""知识库服务（M2）。

知识库是模型锁定的载体：创建时把当前 embedding 实现写入 ``embedding_model_id`` 与
``embedding_dim``，此后**库内一旦有向量就不允许更换模型**（架构 §6.4）。

v0.8：嵌入模型是知识库的必备属性。既没在建库时挑一个注册模型、全局默认也没配，
就**拒绝建库**并给出下一步动作——不退回无语义的哈希实现（那会让用户以为检索有效）。
"""

from __future__ import annotations

from datetime import datetime

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.chunking import (
    CHUNK_OVERLAP_RATIO_MAX,
    CHUNK_SIZE_MAX,
    CHUNK_SIZE_MIN,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
)
from app.services.embedding import NOT_CONFIGURED_HINT
from app.services.embedding.base import EmbeddingProvider
from app.services.model_registry import ModelRegistryService
from app.services.suggested_questions import (
    DEFAULT_QUESTIONS_PER_CHUNK as DEFAULT_SUGGESTED_COUNT,
)
from app.services.suggested_questions import (
    MAX_QUESTIONS as SUGGESTED_COUNT_MAX,
)
from app.services.suggested_questions import (
    MIN_QUESTIONS as SUGGESTED_COUNT_MIN,
)
from app.services.suggested_questions import (
    PROMPT_MAX_CHARS as SUGGESTED_PROMPT_MAX_CHARS,
)
from app.storage.base import KnowledgeBaseRecord, StoreBundle

__all__ = [
    "DEFAULT_CHUNK_STRATEGY",
    "KnowledgeBaseService",
    "validate_chunking",
    "validate_suggested",
]

DEFAULT_CHUNK_STRATEGY = "fixed"

KB_NAME_MAX_CHARS = 120
"""与建库时的 schema 上限一致（``KnowledgeBaseCreate.name``）。"""

KB_DESCRIPTION_MAX_CHARS = 200

#: 库级提示词的长度上限（v0.19）。比出题提示词宽：它要写口径、术语、回答结构，
#: 几句话装不下；但也得有个头——它会进每一轮的 system prompt，是按轮计费的。
SYSTEM_PROMPT_MAX_CHARS = 4000
"""库简介上限。卡片上只显示两行，200 字足够写清"这个库是干什么的"。"""


def validate_chunking(size: int, overlap: int) -> tuple[int, int]:
    """校验切分参数，返回规范化后的 ``(size, overlap)``。

    **为什么放在服务层而不是只靠 pydantic**：PATCH 可能只传其中一个字段，
    必须与库里已有的那个合并之后再判"重叠 < 块长"——单个字段的 ``ge/le``
    校验看不到另一个字段，做不到这件事。两个字段一起传时也走这里，
    保证建库与改配置**用同一套口径**，不会出现"建库能过、改配置不过"。

    报错文案写给用户看：说清该填多少，而不是回一句"参数非法"。
    """
    if not (CHUNK_SIZE_MIN <= size <= CHUNK_SIZE_MAX):
        raise InvalidRequestError(
            f"块长需要在 {CHUNK_SIZE_MIN}–{CHUNK_SIZE_MAX} 之间（当前 {size}）"
        )
    overlap_max = max(1, int(size * CHUNK_OVERLAP_RATIO_MAX))
    if not (0 <= overlap <= overlap_max):
        raise InvalidRequestError(
            f"块重叠需要在 0–{overlap_max} 之间（当前 {overlap}）；"
            "上限是块长的一半——重叠等于块长会让切分原地打转"
        )
    return size, overlap


def validate_suggested(count: int, prompt: str) -> tuple[int, str]:
    """校验"分段问题生成"设置，返回规范化后的 ``(count, prompt)``。

    ``count`` 是**每个分段生成几条**（v23 起；v22 时曾是"空状态显示几条"）。
    ``prompt`` 是出题提示词，空串 = 用内置的。

    与 ``validate_chunking`` 同一套理由：建库与改配置必须共用一份口径，
    否则会出现"建库能过、改设置不过"这种说不清的差别。提示词只做 trim——
    它是给模型的自然语言，除了长度没有别的可判定标准。
    """
    if not (SUGGESTED_COUNT_MIN <= count <= SUGGESTED_COUNT_MAX):
        raise InvalidRequestError(
            f"每个分段生成的问题条数需要在 {SUGGESTED_COUNT_MIN}–{SUGGESTED_COUNT_MAX} 之间"
            f"（当前 {count}）"
        )
    cleaned = (prompt or "").strip()
    if len(cleaned) > SUGGESTED_PROMPT_MAX_CHARS:
        raise InvalidRequestError(f"出题提示词最多 {SUGGESTED_PROMPT_MAX_CHARS} 个字符")
    return count, cleaned


def validate_system_prompt(prompt: str) -> str:
    """校验库级提示词，返回规范化后的文本（v0.19）。

    与出题提示词同一套口径：只 trim + 长度上限。它是给模型的自然语言，
    除了长度没有别的可判定标准——**不要在这里做"像不像提示词"的启发式判断**，
    那只会把某些写法正常的库挡在门外。

    上限比出题提示词宽：库级提示词要写口径、术语、回答结构，几句话是不够的。
    """
    cleaned = (prompt or "").strip()
    if len(cleaned) > SYSTEM_PROMPT_MAX_CHARS:
        raise InvalidRequestError(f"库提示词最多 {SYSTEM_PROMPT_MAX_CHARS} 个字符")
    return cleaned


class KnowledgeBaseService:
    """知识库的创建与查询。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        embedder: EmbeddingProvider,
        models: ModelRegistryService | None = None,
    ) -> None:
        self._stores = stores
        self._embedder = embedder
        # 建库时要按选中的注册模型取"模型 ID + 维度"，所以需要注册器。
        # 允许为 None 只为老测试方便——生产由组合根传进来
        self._models = models

    def create(
        self,
        *,
        kb_id: str,
        name: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_OVERLAP,
        chunk_strategy: str = DEFAULT_CHUNK_STRATEGY,
        owner_id: str | None = None,
        embedding_model_pk: str | None = None,
        suggested_enabled: bool = False,
        suggested_count: int = DEFAULT_SUGGESTED_COUNT,
        suggested_model_pk: str | None = None,
        suggested_prompt: str = "",
        wiki_enabled: bool = False,
    ) -> KnowledgeBaseRecord:
        """建库并**冻结嵌入模型**（架构 §6.4）。

        嵌入模型是知识库属性（v11 设计调整）：``embedding_model_pk`` 传了就用注册表里
        那个模型（凭据运行时按 pk 解析）；没传就用注册表里绑定的**默认嵌入模型**，
        两者都没有则拒绝建库——向量空间是库的地基，没有它就建不出能检索的库。

        ``owner_id``（v10）：登录成员建的库归自己；API Key 通道
        没有账号概念，传 None 即无主（对管理员全可见）。

        切分参数（v17）在这里就落库并校验，**摄入时按库读取**——之前它只是被存下来
        没人用（真实切分永远是默认 512/64），那是一个不成立的承诺。
        """
        chunk_size, chunk_overlap = validate_chunking(chunk_size, chunk_overlap)
        suggested_count, suggested_prompt = validate_suggested(suggested_count, suggested_prompt)
        if suggested_model_pk:
            # 与"嵌入模型必须能真用"同一套：出题也是真发一次模型调用，
            # 存一个不能对话的 pk 只会让这个库的空状态永远出不了题（还查不出原因）
            self._require_chat_model(suggested_model_pk)
        if embedding_model_pk:
            if self._models is None:
                raise InvalidRequestError("未接入模型注册器，无法按所选模型建库")
            _, model = self._models.embedding_target(embedding_model_pk)
            model_id, dim = model.model_id, model.dim or 0
        else:
            model_id, dim = self._embedder.model_id, self._embedder.dim
            if not model_id or dim <= 0:
                raise InvalidRequestError(NOT_CONFIGURED_HINT)

        return self._stores.meta.create_knowledge_base(
            KnowledgeBaseRecord(
                id=kb_id,
                name=name,
                embedding_model_id=model_id,
                embedding_dim=dim,
                embedding_model_pk=embedding_model_pk,
                chunk_strategy=chunk_strategy,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                suggested_enabled=suggested_enabled,
                suggested_count=suggested_count,
                suggested_model_pk=suggested_model_pk or None,
                suggested_prompt=suggested_prompt,
                owner_id=owner_id,
                wiki_enabled=wiki_enabled,
            )
        )

    def set_wiki(self, kb_id: str, enabled: bool) -> KnowledgeBaseRecord:
        """改库形态：要不要生成 Wiki 页面（v24）。

        **只动开关**：已有页面留着（关掉只是不再生成/不再展示，用户可能只是
        暂时不想看；要清空有单独的删除接口）。这与 `set_suggested` 同一套写法。
        """
        record = self.get(kb_id)
        if record.wiki_enabled == enabled:
            return record
        self._stores.meta.set_knowledge_base_wiki(kb_id, enabled=enabled)
        record.wiki_enabled = enabled
        return record

    def set_system_prompt(self, kb_id: str, prompt: str) -> KnowledgeBaseRecord:
        """改这个库的**库级提示词**（v0.19）。

        只动这一段文本，不碰任何检索/切分参数：提示词影响的是"怎么答"，
        与"检索出什么"是两件事，改一个不该顺带动另一个。
        （对应地，它也不需要重新摄入——下一轮问答就会用上。）
        """
        record = self.get(kb_id)
        cleaned = validate_system_prompt(prompt)
        if cleaned == record.system_prompt:
            return record
        self._stores.meta.set_knowledge_base_prompt(kb_id, prompt=cleaned)
        record.system_prompt = cleaned
        return record

    def _require_chat_model(self, model_pk: str) -> None:
        """校验"这个 pk 真能用来对话"。没有注册器（老测试）时跳过。"""
        if self._models is None:
            return
        self._models.chat_target(model_pk)

    def get(self, kb_id: str) -> KnowledgeBaseRecord:
        record = self._stores.meta.get_knowledge_base(kb_id)
        if record is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")
        return record

    def list_all(self) -> list[KnowledgeBaseRecord]:
        return self._stores.meta.list_knowledge_bases()

    def document_stats(self) -> dict[str, tuple[int, datetime | None]]:
        """每个库的 ``(文档数, 最近更新时间)``——**一次聚合查询**，不逐库列文档。"""
        return self._stores.meta.document_stats_by_kbs()

    def set_description(self, kb_id: str, description: str) -> KnowledgeBaseRecord:
        """改库简介。空串是合法值（= 清空），不做"必须填"的要求。"""
        record = self.get(kb_id)
        cleaned = " ".join((description or "").split())
        if len(cleaned) > KB_DESCRIPTION_MAX_CHARS:
            raise InvalidRequestError(f"简介最多 {KB_DESCRIPTION_MAX_CHARS} 个字符")
        if cleaned == record.description:
            return record
        self._stores.meta.set_knowledge_base_description(kb_id, cleaned)
        record.description = cleaned
        return record

    def rename(self, kb_id: str, name: str) -> KnowledgeBaseRecord:
        """改显示名。**不碰嵌入模型**——那是库的地基，改名只是标签。

        切分参数另走 ``set_chunking``：它与改名不是一回事，改错了要重跑摄入。
        """
        record = self.get(kb_id)
        cleaned = name.strip()
        if not cleaned:
            raise InvalidRequestError("知识库名称不能为空")
        if len(cleaned) > KB_NAME_MAX_CHARS:
            raise InvalidRequestError(f"知识库名称最多 {KB_NAME_MAX_CHARS} 个字符")
        if cleaned == record.name:
            return record
        self._stores.meta.rename_knowledge_base(kb_id, cleaned)
        record.name = cleaned
        return record

    def set_chunking(
        self, kb_id: str, *, chunk_size: int | None, chunk_overlap: int | None
    ) -> KnowledgeBaseRecord:
        """改切分参数（块长 / 块重叠）。两个都可选，只传要改的那个。

        **改动只对之后摄入的文档生效**——切块是解析阶段写进库的，
        已经切好的文档不会自己跟着变。所以接口不假装"改完就生效"：
        调用方（界面）负责提示"已有文档需要重新摄入"，并把
        `documents/batch` 的 reprocess 入口摆在那里。

        这里**不做自动重跑**是刻意的：一个几万文档的库被一次参数微调
        静默全量重跑（要钱、要时间、还要临时占用云端配额）比"没生效"更糟。
        用户自己点那一下，才知道代价。
        """
        record = self.get(kb_id)
        size = record.chunk_size if chunk_size is None else chunk_size
        overlap = record.chunk_overlap if chunk_overlap is None else chunk_overlap
        size, overlap = validate_chunking(size, overlap)
        if (size, overlap) == (record.chunk_size, record.chunk_overlap):
            return record
        self._stores.meta.set_knowledge_base_chunking(kb_id, size, overlap)
        record.chunk_size = size
        record.chunk_overlap = overlap
        return record

    def set_suggested(
        self,
        kb_id: str,
        *,
        enabled: bool,
        count: int,
        model_pk: str | None,
        prompt: str,
    ) -> KnowledgeBaseRecord:
        """改这个库的推荐问题设置（v19）。

        与切块参数不同，这组设置**立刻生效**：出题发生在对话页的空状态，读的就是
        库里的这份设置，没有"只对之后入库的文档生效"那种滞后。

        ``model_pk`` 为空 = 跟随对话页当前选的模型；这是默认，也是多数人该用的档
        （单独指定一般是为了"出题用便宜的小模型"）。
        """
        record = self.get(kb_id)
        count, prompt = validate_suggested(count, prompt)
        normalized = model_pk or None
        if normalized:
            self._require_chat_model(normalized)
        if (
            enabled,
            count,
            normalized,
            prompt,
        ) == (
            record.suggested_enabled,
            record.suggested_count,
            record.suggested_model_pk,
            record.suggested_prompt,
        ):
            return record
        self._stores.meta.set_knowledge_base_suggested(
            kb_id, enabled=enabled, count=count, model_pk=normalized, prompt=prompt
        )
        record.suggested_enabled = enabled
        record.suggested_count = count
        record.suggested_model_pk = normalized
        record.suggested_prompt = prompt
        return record
