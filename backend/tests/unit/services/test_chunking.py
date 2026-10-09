"""切分器的单元测试。

镜像同构：``app/services/chunking.py`` → ``tests/unit/services/test_chunking.py``。
"""

import pytest

from app.services.chunking import ChunkingConfig, chunk_markdown, content_hash_of

DOC = "doc_1"
KB = "kb_1"


def _chunk(markdown: str, **kwargs):
    return chunk_markdown(markdown, document_id=DOC, knowledge_base_id=KB, **kwargs)


# --------------------------------------------------------------------- 配置校验


def test_default_config_matches_architecture() -> None:
    config = ChunkingConfig()
    assert (config.size, config.overlap) == (512, 64)


@pytest.mark.parametrize(
    "kwargs", [{"size": 0}, {"size": -1}, {"overlap": -1}, {"size": 100, "overlap": 100}]
)
def test_invalid_config_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(**kwargs)


# --------------------------------------------------------------------- 基本切分


def test_empty_markdown_produces_no_chunks() -> None:
    assert _chunk("") == []
    assert _chunk("   \n\n  ") == []


def test_single_short_paragraph_is_one_chunk() -> None:
    chunks = _chunk("只有一段话。")
    assert len(chunks) == 1
    assert chunks[0].text == "只有一段话。"
    assert chunks[0].ordinal == 0


def test_ordinals_are_continuous_and_ordered() -> None:
    markdown = "\n\n".join(f"第{i}段内容。" * 20 for i in range(6))
    chunks = _chunk(markdown, config=ChunkingConfig(size=120, overlap=20))

    assert len(chunks) > 1
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.text) <= 120 for chunk in chunks)


def test_chunks_respect_size_limit() -> None:
    """一个超长段落必须被硬切成不超过块长的片段。"""
    long_text = "甲" * 1000
    chunks = _chunk(long_text, config=ChunkingConfig(size=100, overlap=10))
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert len(chunks) > 5


def test_overlap_carries_context_across_chunks() -> None:
    """重叠的意义：跨块语义不被切断，所以相邻块应有重复内容。"""
    long_text = "".join(f"{i:04d}" for i in range(200))  # 800 字符
    chunks = _chunk(long_text, config=ChunkingConfig(size=100, overlap=20))

    first, second = chunks[0].text, chunks[1].text
    assert first[-20:] in second


def test_all_whitespace_slice_is_skipped() -> None:
    """硬切时可能出现整片空白，不能产出只有空白的 chunk。"""
    text = "甲" * 10 + " " * 100 + "乙" * 10
    chunks = _chunk(text, config=ChunkingConfig(size=50, overlap=0))

    assert chunks
    assert all(chunk.text.strip() for chunk in chunks)


def test_zero_overlap_produces_disjoint_chunks() -> None:
    long_text = "".join(f"{i:04d}" for i in range(100))
    chunks = _chunk(long_text, config=ChunkingConfig(size=100, overlap=0))
    assert chunks[0].text[-4:] not in chunks[1].text


def test_whitespace_between_blocks_is_normalized() -> None:
    chunks = _chunk("第一段\n\n\n\n第二段")
    assert "\n\n\n\n" not in chunks[0].text


# --------------------------------------------------------------------- 标题路径


def test_heading_path_is_injected() -> None:
    """架构 §5.1：chunk 要带标题路径，检索结果里能看出命中的是哪一节。

    两节各自 20 字、块长 30，装不进同一块，因此每节各自成块。
    """
    markdown = f"# 第1章\n\n{'引' * 20}\n\n## 1.1 背景\n\n{'背' * 20}\n"
    chunks = _chunk(markdown, config=ChunkingConfig(size=30, overlap=0))

    paths = [chunk.heading_path for chunk in chunks]
    assert "第1章" in paths
    assert "第1章 > 1.1 背景" in paths


def test_heading_path_is_taken_from_first_block_of_a_chunk() -> None:
    """合并成一块时，路径取该块首个内容所属的标题。"""
    chunks = _chunk("# 第1章\n\n引言内容。\n\n## 1.1 背景\n\n背景内容。\n")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "第1章"


def test_heading_path_resets_after_higher_level_heading() -> None:
    markdown = f"# 第一章\n\n## 小节\n\n{'甲' * 20}\n\n# 第二章\n\n{'乙' * 20}\n"
    chunks = _chunk(markdown, config=ChunkingConfig(size=30, overlap=0))

    by_text = {chunk.text: chunk.heading_path for chunk in chunks}
    assert by_text["甲" * 20] == "第一章 > 小节"
    assert by_text["乙" * 20] == "第二章"


def test_content_without_headings_has_no_path() -> None:
    chunks = _chunk("没有标题的正文。")
    assert chunks[0].heading_path is None


def test_seven_hashes_is_not_a_heading() -> None:
    """Markdown 的 ATX 标题最多 6 级；7 个 # 属于段落，不该被当成标题。"""
    chunks = _chunk("####### 不是标题\n\n内容。\n", config=ChunkingConfig(size=20, overlap=0))
    assert chunks[0].heading_path is None
    assert "#######" in chunks[0].text


# --------------------------------------------------------------------- 稳定 ID 与 hash


def test_chunk_ids_are_stable_across_runs() -> None:
    """重跑解析后 ID 不变，增量更新才能按位置对齐（架构 §6.1）。"""
    markdown = "# 标题\n\n段落一。\n\n段落二。\n"
    first = _chunk(markdown)
    second = _chunk(markdown)

    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


def test_chunk_id_encodes_document_and_ordinal() -> None:
    chunks = _chunk("段落。" * 400, config=ChunkingConfig(size=100, overlap=0))
    assert chunks[0].chunk_id == f"{DOC}#00000"
    assert chunks[1].chunk_id == f"{DOC}#00001"


def test_part_id_joins_the_chunk_id_scope() -> None:
    chunks = _chunk("内容。", part_id="part_1")
    assert chunks[0].chunk_id == f"{DOC}/part_1#00000"
    assert chunks[0].part_id == "part_1"


def test_content_hash_changes_with_content() -> None:
    assert content_hash_of("甲") != content_hash_of("乙")
    assert content_hash_of("甲") == content_hash_of("甲")


def test_content_hash_is_recorded_on_each_chunk() -> None:
    chunks = _chunk("# 标题\n\n正文。\n")
    assert all(chunk.content_hash == content_hash_of(chunk.text) for chunk in chunks)


def test_metadata_is_filled_for_every_chunk() -> None:
    chunks = _chunk("# 标题\n\n正文。\n")
    for chunk in chunks:
        assert chunk.document_id == DOC
        assert chunk.knowledge_base_id == KB
        assert tuple(chunk.image_ids) == ()


# ------------------------------------------------- 大文件切分后的页码（T2.7）


class TestPageMapping:
    """页标记 → ``chunk.page``。

    大文件切分器把 1500 页的文档切成 8 段分别解析，产物是拼起来的。
    没有这条映射，「这条命中在第几页」就永远答不出来——
    而引文页码是"答案可核查"的前提（架构 §5）。

    这一组同时钉住**两边共用的标记格式**：``splitting.PAGE_MARKER_TEMPLATE``
    写、``chunking._PAGE_MARKER`` 读。格式改成一边就只剩另一边，测试会红。
    """

    def test_标记之后的正文带上那一页的页码(self) -> None:
        from app.services.splitting import render_page_marker

        # 段长给 10，让两段必然分开成两块——否则它们会被贪心装进同一个桶，
        # 而那测的就不是"页码对不对"而是"桶怎么装"了
        markdown = (
            f"{render_page_marker(1)}\n\n第一段正文。\n\n"
            f"{render_page_marker(501)}\n\n第二段正文。"
        )
        chunks = _chunk(markdown, config=ChunkingConfig(size=10, overlap=0))

        assert [chunk.page for chunk in chunks] == [1, 501]

    def test_标记本身不进_chunk_正文(self) -> None:
        # 留在正文里会污染检索文本，也会让嵌入向量被一串 HTML 注释干扰
        from app.services.splitting import render_page_marker

        chunks = _chunk(f"{render_page_marker(7)}\n\n正文。\n")
        assert all("<!--" not in chunk.text for chunk in chunks)
        assert all("page:" not in chunk.text for chunk in chunks)

    def test_没有标记的文档页码为空(self) -> None:
        # 没被切分的文档本就没有页信息。编一个 1 出来会让界面显示假页码，
        # 而 None 会被渲染成"—"，后者才是诚实的
        chunks = _chunk("# 标题\n\n正文。\n")
        assert all(chunk.page is None for chunk in chunks)

    def test_一块横跨两段时标的是起始页(self) -> None:
        """**已知的近似，不是 bug**：一块只带一个页码。

        段长足够大时两段会被装进同一个 chunk，它只能报一个页码——
        报的是**起始页**（1）。这与"PDF 未经切分时整篇报 None"是同一种取舍：
        页码是**定位辅助**，不是精确映射；想要精确就得把页标记做成绝对分块边界，
        而那会把块切得很碎，不值得。

        这条测试是**记录这个决定**：将来若有人把页标记改成强制分块，
        它会红，然后他必须回来读这段注释而不是默默改掉行为。
        """
        from app.services.splitting import render_page_marker

        markdown = f"{render_page_marker(1)}\n\n第一页。\n\n{render_page_marker(2)}\n\n第二页。"
        chunks = _chunk(markdown, config=ChunkingConfig(size=100, overlap=0))
        assert len(chunks) == 1
        assert chunks[0].page == 1
        # 段长小时就会分成两块，各自带自己的页码
        split = _chunk(markdown, config=ChunkingConfig(size=8, overlap=0))
        assert [chunk.page for chunk in split] == [1, 2]

    def test_同一页的每一块都带这一页的页码(self) -> None:
        """**页码是页的属性，不是"紧跟标记那一段"的属性。**

        真实回归：一份 2 页 PDF 曾拿到 ``[1, null, null, null, 2, null, null, null]``——
        页码用掉一次就被清掉，同一页后面的段落全成了 None。八块里六块没有页码，
        引用照样写不出"第几页"。这里钉住"标记之后的每一块都带这一页"。
        """
        from app.services.splitting import render_page_marker

        markdown = (
            f"{render_page_marker(1)}\n\n第一页第一段。\n\n第一页第二段。\n\n"
            f"{render_page_marker(2)}\n\n第二页第一段。\n\n第二页第二段。"
        )
        # 段长调小，让每个自然段各自成块（否则测的是桶怎么装，不是页码）
        chunks = _chunk(markdown, config=ChunkingConfig(size=8, overlap=0))

        assert [chunk.page for chunk in chunks] == [1, 1, 2, 2]

    def test_有标记时页码不会丢(self) -> None:
        # 与"没有标记就为空"相反的方向：有标记就必须带上，不能因为分块而丢掉
        from app.services.splitting import render_page_marker

        chunks = _chunk(
            f"{render_page_marker(3)}\n\n有页码。\n", config=ChunkingConfig(size=100, overlap=0)
        )
        assert chunks[0].page == 3


class TestConfigFromRecord:
    """从知识库记录读切分参数（v17）：正常值原样、越界值钳位。"""

    def test_正常值原样通过(self) -> None:
        from app.services.chunking import config_from_record

        record = type("R", (), {"chunk_size": 300, "chunk_overlap": 20})()
        config = config_from_record(record)

        assert (config.size, config.overlap) == (300, 20)

    def test_历史脏数据被钳位而不是抛错(self) -> None:
        """旧版允许 size=512 & overlap=4000 入库；直接构造 ChunkingConfig 会抛错，
        那会让"一条老配置"把之后所有文档的摄入打挂，且看起来像文件坏了。"""
        from app.services.chunking import config_from_record

        record = type("R", (), {"chunk_size": 512, "chunk_overlap": 4000})()
        config = config_from_record(record)

        assert config.size == 512
        assert config.overlap == 256  # 钳到一半

    def test_块长越界同样钳位(self) -> None:
        from app.services.chunking import CHUNK_SIZE_MAX, CHUNK_SIZE_MIN, config_from_record

        assert config_from_record(type("R", (), {"chunk_size": 1, "chunk_overlap": 0})()).size == (
            CHUNK_SIZE_MIN
        )
        assert config_from_record(
            type("R", (), {"chunk_size": 99999, "chunk_overlap": 10})()
        ).size == CHUNK_SIZE_MAX

    def test_字段缺失时退回默认(self) -> None:
        from app.services.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, config_from_record

        config = config_from_record(type("R", (), {})())

        assert (config.size, config.overlap) == (DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP)
