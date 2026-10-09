"""知识库创建：嵌入模型随库冻结（v11 设计调整，v0.8 收紧）。

原先建库是把**全局**解析出的模型冻进记录，所有库共用一个 embedder；
现在嵌入模型是知识库属性——建库时从注册表挑，随库冻结。这个文件钉住四条：
选了就用选的、没选就用注册表里的默认、两个都没有要**当场拒绝**（v0.8 取消哈希兜底）、
选了个不能用的也要当场报错而不是摄入到一半才炸。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.embedding import NOT_CONFIGURED_HINT
from app.services.embedding.base import EmbeddingProvider
from app.services.knowledge_base import KnowledgeBaseService
from app.services.model_registry import ModelRegistryService


class _FakeEmbedder(EmbeddingProvider):
    def __init__(self, *, model_id: str, dim: int, is_development: bool = True) -> None:
        self.model_id = model_id
        self.dim = dim
        self.is_development = is_development

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


#: 没配默认模型时组合根给出的 embedder：model_id 与 dim 都是空的
_UNCONFIGURED = _FakeEmbedder(model_id="", dim=0)


@pytest.fixture
def registry(bundle) -> ModelRegistryService:  # type: ignore[no-untyped-def]
    return ModelRegistryService(bundle)


def _register(registry: ModelRegistryService, *, model_id: str, dim: int | None) -> str:
    provider = registry.create_provider(
        kind="embedding", name="嵌入供应商", base_url="https://api.example.com", api_key="sk-x"
    )
    model = registry.register_model(
        provider_id=provider.id, model_id=model_id, capabilities=["embedding"], dim=dim
    )
    return model.id


def _service(
    bundle,  # type: ignore[no-untyped-def]
    registry: ModelRegistryService,
    embedder: EmbeddingProvider | None = None,
) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        bundle,
        embedder=embedder or _FakeEmbedder(model_id="global-default", dim=256),
        models=registry,
    )


def test_create_freezes_the_chosen_model(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    pk = _register(registry, model_id="bge-m3", dim=1024)

    kb = _service(bundle, registry).create(
        kb_id="kb_1", name="高精度小库", embedding_model_pk=pk
    )

    assert kb.embedding_model_id == "bge-m3"
    assert kb.embedding_dim == 1024
    assert kb.embedding_model_pk == pk
    # 落库之后再读一次，确认字段真的持久化了（不是只留在内存记录里）
    assert bundle.meta.get_knowledge_base("kb_1").embedding_model_pk == pk


def test_create_without_a_choice_uses_the_global_default(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    kb = _service(bundle, registry).create(kb_id="kb_2", name="不挑模型")

    assert kb.embedding_model_id == "global-default"
    assert kb.embedding_dim == 256
    assert kb.embedding_model_pk is None


def test_create_without_any_embedding_model_is_rejected(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """没有嵌入模型就不许建库（v0.8 取消哈希兜底）。

    以前这种情况会静默退回无语义的词面哈希，界面上还标个"开发兜底"，
    用户会以为检索是有效的。现在当场拒绝，并说清去哪儿配。
    """
    service = _service(bundle, registry, embedder=_UNCONFIGURED)

    with pytest.raises(InvalidRequestError) as excinfo:
        service.create(kb_id="kb_5", name="无模型库")

    assert str(excinfo.value) == NOT_CONFIGURED_HINT
    assert bundle.meta.get_knowledge_base("kb_5") is None


def test_create_with_an_explicit_model_needs_no_default(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """建库时挑了一个模型就够了，全局默认没配也不该拦——这才是"每库可自选"的意义。"""
    pk = _register(registry, model_id="tiny-small", dim=256)
    service = _service(bundle, registry, embedder=_UNCONFIGURED)

    kb = service.create(kb_id="kb_6", name="自带模型", embedding_model_pk=pk)

    assert kb.embedding_model_id == "tiny-small"
    assert kb.embedding_dim == 256


def test_create_rejects_a_model_without_a_dimension(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    pk = _register(registry, model_id="no-dim", dim=None)

    with pytest.raises(InvalidRequestError, match="维度"):
        _service(bundle, registry).create(kb_id="kb_3", name="坏模型", embedding_model_pk=pk)

    # 建库失败不该留下半成品
    assert bundle.meta.get_knowledge_base("kb_3") is None


def test_create_rejects_a_non_embedding_model(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    provider = registry.create_provider(
        kind="llm", name="对话供应商", base_url="https://api.example.com", api_key="sk-x"
    )
    model = registry.register_model(
        provider_id=provider.id, model_id="chat-only", capabilities=["chat"]
    )

    with pytest.raises(InvalidRequestError, match="embedding"):
        _service(bundle, registry).create(
            kb_id="kb_4", name="用错模型", embedding_model_pk=model.id
        )


# ------------------------------------------------------- 切分参数可调（v17）

from app.services.chunking import CHUNK_SIZE_MAX, CHUNK_SIZE_MIN  # noqa: E402
from app.services.knowledge_base import validate_chunking  # noqa: E402


def _plain(bundle, registry) -> KnowledgeBaseService:  # type: ignore[no-untyped-def]
    return _service(bundle, registry, _FakeEmbedder(model_id="m", dim=8))


def test_create_validates_chunking_bounds(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """建库与改配置共用一套校验：越界要当场拒绝，并说清该填多少。"""
    service = _plain(bundle, registry)
    with pytest.raises(InvalidRequestError, match=str(CHUNK_SIZE_MIN)):
        service.create(kb_id="kb_a", name="太小", chunk_size=CHUNK_SIZE_MIN - 1)
    with pytest.raises(InvalidRequestError, match="一半"):
        service.create(kb_id="kb_b", name="重叠过头", chunk_size=512, chunk_overlap=300)
    # 边界值合法：重叠恰为块长的一半
    record = service.create(kb_id="kb_c", name="边界", chunk_size=512, chunk_overlap=256)
    assert (record.chunk_size, record.chunk_overlap) == (512, 256)


def test_set_chunking_persists_and_merges_partial_updates(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """只传一个字段时，另一个用库里已有的值——所以"重叠<块长"必须合并后再判。"""
    service = _plain(bundle, registry)
    service.create(kb_id="kb_1", name="库", chunk_size=512, chunk_overlap=64)

    updated = service.set_chunking("kb_1", chunk_size=256, chunk_overlap=None)

    assert (updated.chunk_size, updated.chunk_overlap) == (256, 64)
    # 真的落库了，不只是改了内存里的记录
    assert (bundle.meta.get_knowledge_base("kb_1").chunk_size) == 256
    # 再只改重叠
    again = service.set_chunking("kb_1", chunk_size=None, chunk_overlap=32)
    assert (again.chunk_size, again.chunk_overlap) == (256, 32)


def test_set_chunking_rejects_pair_that_only_service_can_see(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """块长调小之后，旧的重叠可能就"太大"了——单独传一个字段也必须拦住。"""
    service = _plain(bundle, registry)
    service.create(kb_id="kb_1", name="库", chunk_size=512, chunk_overlap=200)

    with pytest.raises(InvalidRequestError, match="一半"):
        service.set_chunking("kb_1", chunk_size=256, chunk_overlap=None)
    # 拒绝之后原值不变
    assert bundle.meta.get_knowledge_base("kb_1").chunk_size == 512


def test_validate_chunking_returns_normalized_pair() -> None:
    assert validate_chunking(512, 64) == (512, 64)
    assert validate_chunking(CHUNK_SIZE_MIN, 0) == (CHUNK_SIZE_MIN, 0)
    assert validate_chunking(CHUNK_SIZE_MAX, CHUNK_SIZE_MAX // 2) == (
        CHUNK_SIZE_MAX,
        CHUNK_SIZE_MAX // 2,
    )


# ------------------------------------------------------- 推荐问题设置（v19）

from app.services.knowledge_base import validate_suggested  # noqa: E402
from app.services.suggested_questions import (  # noqa: E402
    DEFAULT_QUESTIONS_PER_CHUNK,
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    PROMPT_MAX_CHARS,
)


def test_create_persists_suggested_settings(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    service = _plain(bundle, registry)

    record = service.create(
        kb_id="kb_s",
        name="库",
        suggested_enabled=False,
        suggested_count=4,
        suggested_prompt="  按诊断标准出题  ",
    )

    assert record.suggested_enabled is False
    assert record.suggested_count == 4
    # 提示词只 trim，不做别的加工
    assert record.suggested_prompt == "按诊断标准出题"
    # 真的落库了：重新读一遍也是这几个值（列名与记录字段不能对不上）
    saved = bundle.meta.get_knowledge_base("kb_s")
    assert (
        saved.suggested_enabled,
        saved.suggested_count,
        saved.suggested_prompt,
    ) == (False, 4, "按诊断标准出题")


def test_create_defaults_match_the_service_constants(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    """默认值在两个地方各写了一份（storage 不许 import services），用用例钉住一致。"""
    service = _plain(bundle, registry)

    record = service.create(kb_id="kb_d", name="库")

    assert record.suggested_enabled is False
    assert record.suggested_count == DEFAULT_QUESTIONS_PER_CHUNK


def test_set_suggested_merges_and_persists(bundle, registry) -> None:  # type: ignore[no-untyped-def]
    service = _plain(bundle, registry)
    service.create(kb_id="kb_1", name="库", suggested_count=2)

    updated = service.set_suggested("kb_1", enabled=True, count=3, model_pk=None, prompt="出题")

    assert (updated.suggested_enabled, updated.suggested_count, updated.suggested_prompt) == (
        True,
        3,
        "出题",
    )
    saved = bundle.meta.get_knowledge_base("kb_1")
    assert (saved.suggested_enabled, saved.suggested_count) == (True, 3)


def test_validate_suggested_bounds_and_trim() -> None:
    assert validate_suggested(MIN_QUESTIONS, "  x  ") == (MIN_QUESTIONS, "x")
    assert validate_suggested(MAX_QUESTIONS, "") == (MAX_QUESTIONS, "")
    with pytest.raises(InvalidRequestError, match=str(MIN_QUESTIONS)):
        validate_suggested(MIN_QUESTIONS - 1, "")
    with pytest.raises(InvalidRequestError, match=str(MAX_QUESTIONS)):
        validate_suggested(MAX_QUESTIONS + 1, "")
    with pytest.raises(InvalidRequestError, match=str(PROMPT_MAX_CHARS)):
        validate_suggested(3, "长" * (PROMPT_MAX_CHARS + 1))
