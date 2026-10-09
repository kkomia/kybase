"""摄入里"向量从哪来"的那一处：``vectors_for`` 与 ``IngestService._media_blob``。

镜像同构：``app/services/ingest.py`` → 本文件。这里只测**这一处判据**——它是
"文本向量 / 媒体向量"唯一的分岔点，错了会表现成"算的是媒体向量、记的是文本通道"那种
只在检索结果里显形的错位。整条摄入链路的集成用例在
``tests/integration/services/test_ingest.py``（那边的夹具要 PostgreSQL）。

**为什么不用夹具**：本机没有 ``KYLAB_TEST_DATABASE_URL`` 时 conftest 的 autouse 夹具会把
整个套件 skip 掉，而这两条判据值得在没有库的机器上也能验。这里的替身只回答两个事实：
"这份文档是谁解析的"与"当前实现支不支持媒体"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.enums import DataSourceKind, DocumentStage
from app.parsers.base import ProbeResult
from app.parsers.media_direct import MediaDirectParser
from app.parsers.plain_text import PlainTextParser
from app.services.embedding.base import EmbeddingError, EmbeddingProvider
from app.services.ingest import IngestService, vectors_for
from app.services.parser_router import ParserRouter
from app.storage.base import DocumentRecord

_ORIGINAL = b"\x00\x00\x00 ftypmp42-fake-video-bytes"


class _FakeMeta:
    """只回答两件事：这份文档的解析产物是谁、原文路径在哪。"""

    def __init__(
        self, *, parser_name: str | None, original_path: str | None = "/objects/x"
    ) -> None:
        self._parser_name = parser_name
        self._path = original_path

    def get_parse_result(self, document_id: str) -> object | None:
        if self._parser_name is None:
            return None
        return SimpleNamespace(document_id=document_id, parser_name=self._parser_name)

    def get_setting(self, key: str) -> str | None:
        return self._path


class _FakeObjects:
    def read(self, path: str) -> bytes:
        return _ORIGINAL


class _FakeEmbedder(EmbeddingProvider):
    """记下"走的哪一条路"，并给出可辨认的向量。"""

    def __init__(self, *, dim: int = 4, supports_media: bool = False) -> None:
        self.model_id = "fake-embedder"
        self.dim = dim
        self.supports_media = supports_media
        self.text_calls: list[list[str]] = []
        self.media_calls: list[bytes] = []

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.text_calls.append(list(texts))
        return [[0.25] * self.dim for _ in texts]

    def embed_media(self, data: bytes) -> list[float]:
        self.media_calls.append(data)
        return [0.5] * self.dim


def _document() -> DocumentRecord:
    return DocumentRecord(
        id="doc_media_1",
        knowledge_base_id="kb_1",
        name="花.mp4",
        source_kind=DataSourceKind.UPLOAD,
        content_hash="hash",
        stage=DocumentStage.EMBEDDING,
    )


def _service(
    *, parser_name: str | None, supports_media: bool
) -> tuple[IngestService, _FakeEmbedder]:
    stores = SimpleNamespace(meta=_FakeMeta(parser_name=parser_name), objects=_FakeObjects())
    embedder = _FakeEmbedder(supports_media=supports_media)
    service = IngestService(
        stores,  # type: ignore[arg-type]
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
    )
    return service, embedder


# ---------------------------------------------------------------------- vectors_for


def test_vectors_for_without_media_uses_the_text_batch() -> None:
    embedder = _FakeEmbedder(dim=3)
    vectors = vectors_for(embedder, texts=["甲", "乙"], media=None)

    assert embedder.text_calls == [["甲", "乙"]]
    assert embedder.media_calls == []
    assert len(vectors) == 2
    assert all(len(vector) == 3 for vector in vectors)


def test_vectors_for_with_media_uses_the_media_endpoint_once() -> None:
    """一份媒体向量**给每个分段各一份**：媒体向量描述的是整份文件。"""
    embedder = _FakeEmbedder(dim=3, supports_media=True)
    vectors = vectors_for(embedder, texts=["甲", "乙", "丙"], media=_ORIGINAL)

    assert embedder.text_calls == []
    assert embedder.media_calls == [_ORIGINAL]
    assert len(vectors) == 3
    assert all(vector == [0.5, 0.5, 0.5] for vector in vectors)


class _TextOnlyEmbedder(EmbeddingProvider):
    """只实现 ``embed`` 的最小实现：用来验协议**默认**的媒体行为（拒绝）。"""

    model_id = "text-only"
    dim = 2

    def embed(self, texts):  # type: ignore[no-untyped-def]
        return [[0.1, 0.2] for _ in texts]


def test_base_embedder_refuses_media_by_default() -> None:
    """只认文本的实现拿到媒体字节时必须**说清楚做不到**，不能拿文件名当文本嵌。"""
    with pytest.raises(EmbeddingError, match="不支持"):
        _TextOnlyEmbedder().embed_media(b"bytes")


# ---------------------------------------------------------------------- 维度上限


def test_dimension_limit_matches_the_storage_layer() -> None:
    """2048 维（WeMM 的原生维度）**不是错误**：库照建照用，只是没有向量索引。

    处置是"不建索引 + 如实告诉用户"（判据与那句话的措辞在
    `PostgresVectorStore.ensure_partition`，用例见
    `tests/unit/storage/postgres_impl/test_vector_store.py` 与
    `tests/integration/storage/test_vector_store.py`）。
    摄入这一层只把仓储返回的那句话写进日志——所以两处那个数字必须一致：
    它就是"什么时候不建索引"的唯一判据。
    """
    from app.services.ingest import HNSW_MAX_DIM
    from app.storage.postgres_impl.vector_store import HNSW_MAX_DIM as STORE_LIMIT

    assert HNSW_MAX_DIM == STORE_LIMIT == 2000


# ---------------------------------------------------------------------- _media_blob


def test_media_blob_is_used_only_for_media_direct_documents() -> None:
    """配了 OCR 的图片走的是 OCR 文本，它的向量必须还是文本向量。

    判据是**解析器是谁**，不是文件后缀：只看后缀的话，同一张图"配没配 OCR"会落进
    两个语义空间，而界面上完全看不出来。
    """
    service, _ = _service(parser_name=MediaDirectParser.name, supports_media=True)
    assert service._media_blob(_document(), _FakeEmbedder(supports_media=True)) == _ORIGINAL

    for other in ("MinerUCloudParser", "PaddleOCRApiParser", "PlainTextParser", None):
        service, _ = _service(parser_name=other, supports_media=True)
        assert service._media_blob(_document(), _FakeEmbedder(supports_media=True)) is None


def test_media_blob_refuses_instead_of_silently_using_a_text_vector() -> None:
    """媒体直通文档碰上"不支持媒体"的嵌入实现时**报错**，不退回文本向量。

    退回文本的后果不是失败而是**说假话**：那份最小 Markdown 里写着"本文件由媒体向量
    索引"，而向量其实来自这段文件名文字。用户点开出处看到的就是一句被自己骗了的话。
    """
    service, _ = _service(parser_name=MediaDirectParser.name, supports_media=False)

    with pytest.raises(EmbeddingError, match="不支持媒体向量"):
        service._media_blob(_document(), _FakeEmbedder(supports_media=False))


def test_media_blob_reads_the_original_not_the_markdown() -> None:
    """媒体向量必须来自**原文**（服务端自己解码），不是我们转出来的那份 Markdown。"""
    service, _ = _service(parser_name=MediaDirectParser.name, supports_media=True)

    blob = service._media_blob(_document(), _FakeEmbedder(supports_media=True))

    assert blob == _ORIGINAL
    assert b"#" not in (blob or b"")  # 不是 Markdown 产物


def test_probe_result_is_not_consulted_by_the_media_judgement() -> None:
    """判据只有"谁解析的 + 实现支持媒体"两条：探测结论不该在这里再判一遍。"""
    service, _ = _service(parser_name=MediaDirectParser.name, supports_media=True)
    probe = ProbeResult(kind="scanned", text_coverage=0.0)

    # 探测结论（这里刻意给一个与媒体无关的）不影响结果
    assert probe.kind == "scanned"
    assert service._media_blob(_document(), _FakeEmbedder(supports_media=True)) == _ORIGINAL
