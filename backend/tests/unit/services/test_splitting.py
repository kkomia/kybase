"""大文件强制切分（M2 / T2.7）的单元测试。

三层各测各的，因为它们的失败方式完全不同：

- :func:`plan_split` 是**纯决策**——切错页范围是永久的、不可恢复的数据损坏
  （用户拿到的是内容缺失却"成功"的文档），所以边界要逐个钉住；
- :func:`extract_page_range` 碰真实 PDF 字节；
- :class:`PageRangeSplitter` 是编排——它决定"一段失败要不要放弃全部"，
  这条判断直接决定用户要不要重跑 1500 页。

**为什么要造真 PDF 而不是造假字节**：`extract_page_range` 的整个价值就在
"真的切出了那几页"，用 mock 会把这一条变成同义反复。pymupdf 已是依赖，
造一份 6 页的 PDF 只花几毫秒。
"""

from __future__ import annotations

import pytest

from app.models.enums import DocumentStage
from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeResult
from app.services.splitting import (
    MINERU_PAGE_LIMIT,
    PageRange,
    PageRangeSplitter,
    build_result,
    extract_page_range,
    merge_parts,
    plan_split,
    render_page_marker,
)


def make_pdf(page_count: int) -> bytes:
    """造一份带文本层的 PDF，每页写一个可识别的页号。"""
    import pymupdf

    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        page.insert_text((72, 100), f"MARKER_PAGE_{index + 1}", fontsize=12)
    data = document.tobytes()
    document.close()
    return data


def page_texts(pdf: bytes) -> list[str]:
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        return [page.get_text().strip() for page in document]


class FakeParser(ParserProvider):
    """按调用顺序编号的假解析器：只关心"被调了几次、拿到什么"。"""

    name = "FakeParser"

    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self._fail_on = fail_on or set()

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        return True

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        index = len(self.calls)
        self.calls.append((len(content), filename))
        if index in self._fail_on:
            raise ParseError(f"第 {index} 段故意失败", stage="parsing")
        return ParseResult(markdown=f"内容{index}", parser_name=self.name)



#: 编排用例每段的页数。**取 6 而不是默认的 200**：编排要验的是
#: "按计划切了几段、顺序对不对、失败怎么办"，与每段多长无关。
#: 用默认值会让每个用例去造 600+ 页的真 PDF（实测整套跑 23 秒），
#: 而它们全都在测同一段逻辑——测试慢下来之后就没人愿意跑了。
#:
#: 注意 play 时 `part_pages` 与 `limit` 要**一起**传：段长取两者较小值，
#: 只传 part_pages 的话 6 > 默认 limit(200)？不是——是 min(6, 200)=6，能work；
#: 但若只传 limit=6 而 part_pages 还是 200，段长也会是 6。两个都传最不容易误解。
_TEST_PART_PAGES = 6


def _run_splitter(
    parser, page_count: int = 30, *, filename: str = "大书.pdf", expected_parts: int = 3, **kwargs
):
    """用**真的** PDF 跑切分编排。

    第一版这里传的是 `b"x"`，结果每个用例都在断言 `extract_page_range` 的
    "打不开 PDF"错误路径上——7 个用例红成一片，看着像编排坏了，
    实际是夹具喂了假字节。编排要真跑就得给真文件。

    段落数由 `expected_parts` 反推页数，保证**恰好**切出那么多段。
    """
    page_count = expected_parts * _TEST_PART_PAGES
    return PageRangeSplitter(parser, **kwargs).run(
        content=make_pdf(page_count),
        filename=filename,
        plan=plan_split(
            page_count, part_pages=_TEST_PART_PAGES, limit=_TEST_PART_PAGES
        ),
    )


# ---------------------------------------------------------------- plan_split


class TestPlanSplit:
    def test_不超过上限就不切(self) -> None:
        plan = plan_split(150)
        assert plan.needed is False
        assert plan.parts == ()

    def test_刚好等于上限也不切(self) -> None:
        # 边界上的 `>` 与 `>=` 差一页，而 `-60006` 的判定就是"超过 200 页"。
        # 写成 >= 的话一份正好 200 页的文档会被无谓地切成两段
        assert plan_split(MINERU_PAGE_LIMIT).needed is False
        assert plan_split(MINERU_PAGE_LIMIT + 1).needed is True

    def test_1500_页切八段且页范围连续无重叠(self) -> None:
        # 段长 200（= MinerU 上限，因为默认 part_pages=500 > limit=200）
        plan = plan_split(1500)
        assert len(plan.parts) == 8
        assert plan.parts[0] == PageRange(1, 200)
        assert plan.parts[-1] == PageRange(1401, 1500)
        assert all(part.page_count <= MINERU_PAGE_LIMIT for part in plan.parts)
        # 连续性：上一段的 end + 1 必须等于下一段的 start，
        # 否则会漏页（内容静默丢失）或重页（同一段内容进两次索引）
        for previous, following in zip(plan.parts, plan.parts[1:], strict=False):
            assert following.start == previous.end + 1

    def test_最后一段收在总页数上而不是补满(self) -> None:
        plan = plan_split(1201)
        assert plan.parts[-1] == PageRange(1201, 1201)
        assert sum(part.page_count for part in plan.parts) == 1201

    def test_所有段加起来正好等于总页数(self) -> None:
        # 这条是"内容不丢"的总闸：任何切法下都必须成立
        for total in (201, 500, 501, 999, 1000, 1001, 5000):
            plan = plan_split(total)
            assert sum(part.page_count for part in plan.parts) == total, total

    def test_页数未知不切(self) -> None:
        # 纯文本 / 表格 / 探测拿不到页数。按猜测切一个 Markdown 只会把内容截断
        plan = plan_split(None)
        assert plan.needed is False
        assert "无法确定页数" in plan.reason

    def test_零页不切(self) -> None:
        assert plan_split(0).needed is False
        # 负数是数据损坏，同样不切——而不是算出负的页范围
        assert plan_split(-5).needed is False

    def test_每段页数可覆盖(self) -> None:
        # part_pages=250 比 limit(200) 大 → 段长服从上限，取 200
        plan = plan_split(1000, part_pages=250)
        assert plan.parts[0] == PageRange(1, 200)
        assert len(plan.parts) == 5
        # 只有**超过上限**时才切；1200 > 1000 所以切，段长按 part_pages=250
        assert plan_split(1200, part_pages=250, limit=1000).parts[0] == PageRange(1, 250)

    def test_limit_取更严的一档判断要不要切(self) -> None:
        # 默认 limit 是 MinerU 的 200 页；按 PaddleOCR 的 1000 页判时 500 页就不必切
        assert plan_split(500).needed is True
        assert plan_split(500, limit=1000).needed is False

    def test_每段页数小于渠道上限时按每段页数切(self) -> None:
        # part_pages 比 limit 小时以 part_pages 为准（想切碎一点）
        plan = plan_split(300, part_pages=50)
        assert all(part.page_count <= 50 for part in plan.parts)
        assert len(plan.parts) == 6

    def test_段长必须服从渠道上限(self) -> None:
        """这一条是切分的**全部意义**：切出来的每一段都不能被渠道拒。

        早先的实现把 limit 既用来判"要不要切"、又当段长用，于是一份 201 页的
        文档（超过 MinerU 的 200 页上限）被切成 ``(1, 500)``，实际钳到 ``(1, 201)``
        ——**一段 201 页**，提交上去仍然拿 ``-60006``，等于没切。
        """
        for total in (201, 250, 500, 999, 1000, 1001, 5000):
            plan = plan_split(total)  # 默认 part_pages=500 > limit=200
            for part in plan.parts:
                assert part.page_count <= MINERU_PAGE_LIMIT, f"{total} 页切出了 {part}"

    def test_201_页切成两段(self) -> None:
        # 贴着边界的那个数：一段 200、一段 1
        plan = plan_split(201)
        assert plan.parts == (PageRange(1, 200), PageRange(201, 201))

    def test_非法参数直接抛(self) -> None:
        with pytest.raises(ValueError):
            plan_split(100, part_pages=0)
        with pytest.raises(ValueError):
            plan_split(100, limit=0)

    def test_默认段长不会越过默认上限(self) -> None:
        """默认值组合必须自洽：判"要不要切"与"切多大"用的是同一把尺子。

        早先 limit 既当判据又当段长，导致 201 页的文档被切成**一段 201 页**——
        判为"超限"却切出超限的段，等于没切。这条守住"切出来的段一定合规"。
        """
        for total in (201, 500, 501, 1000, 1001):
            plan = plan_split(total)
            assert all(part.page_count <= MINERU_PAGE_LIMIT for part in plan.parts), total


# ------------------------------------------------------- extract_page_range


class TestExtractPageRange:
    def test_切出的正是那几页(self) -> None:
        pdf = make_pdf(6)
        chunk = extract_page_range(pdf, PageRange(3, 5))
        assert page_texts(chunk) == ["MARKER_PAGE_3", "MARKER_PAGE_4", "MARKER_PAGE_5"]

    def test_第一段从第一页开始(self) -> None:
        chunk = extract_page_range(make_pdf(6), PageRange(1, 2))
        assert page_texts(chunk) == ["MARKER_PAGE_1", "MARKER_PAGE_2"]

    def test_单页范围(self) -> None:
        assert page_texts(extract_page_range(make_pdf(3), PageRange(2, 2))) == ["MARKER_PAGE_2"]

    def test_页范围超出实际页数时钳制而不是整个失败(self) -> None:
        # 探测报的页数与实际不一致（PDF 元数据损坏）时，能救回几页就救几页。
        # 直接抛会让这个文件永远传不上去，而它其实只差最后两页
        chunk = extract_page_range(make_pdf(4), PageRange(3, 10))
        assert page_texts(chunk) == ["MARKER_PAGE_3", "MARKER_PAGE_4"]

    def test_起点已超出总页数才算错(self) -> None:
        with pytest.raises(ParseError, match="超出实际页数"):
            extract_page_range(make_pdf(4), PageRange(9, 12))

    def test_不是_PDF_时报错而不是崩(self) -> None:
        with pytest.raises(ParseError, match="无法打开"):
            extract_page_range(b"this is not a pdf at all", PageRange(1, 1))


# -------------------------------------------------------------- merge_parts


class TestMergeParts:
    def test_每段前面插入起始页标记(self) -> None:
        merged = merge_parts(["甲", "乙"], [PageRange(1, 500), PageRange(501, 1000)])
        assert render_page_marker(1) in merged
        assert render_page_marker(501) in merged
        assert merged.index(render_page_marker(1)) < merged.index("甲")
        assert merged.index(render_page_marker(501)) < merged.index("乙")

    def test_保持段落顺序(self) -> None:
        merged = merge_parts(
            ["甲", "乙", "丙"], [PageRange(1, 1), PageRange(2, 2), PageRange(3, 3)]
        )
        assert merged.index("甲") < merged.index("乙") < merged.index("丙")

    def test_数量不一致直接抛(self) -> None:
        # 长度不一致说明调用方弄丢了一段产物。猜一个对应关系会把内容拼错页，
        # 而拼错页的文档看起来是"成功"的
        with pytest.raises(ValueError, match="不一致"):
            merge_parts(["甲"], [PageRange(1, 1), PageRange(2, 2)])


# ------------------------------------------------------ PageRangeSplitter


class TestPageRangeSplitter:
    def test_逐段调用解析器并保持顺序(self) -> None:
        parser = FakeParser()
        outcome = _run_splitter(parser)

        assert len(parser.calls) == 3
        assert outcome.succeeded and len(outcome.succeeded) == 3
        assert outcome.markdown.index("内容0") < outcome.markdown.index("内容1")

    def test_每段的文件名不同(self) -> None:
        # 云端解析服务按文件名登记任务，同名提交在部分渠道会被当成同一个任务去重
        parser = FakeParser()
        _run_splitter(parser)
        names = [name for _, name in parser.calls]
        assert len(set(names)) == len(names)
        assert names[0] == "大书_P1-6.pdf"

    def test_一段失败不影响其余段(self) -> None:
        # 架构 §4.2：单个子文件失败只需重跑该子文件。
        # 放弃全部会让用户重跑整个 1500 页
        parser = FakeParser(fail_on={1})
        outcome = _run_splitter(parser)

        assert len(outcome.outcomes) == 3
        assert len(outcome.failed) == 1
        assert outcome.failed[0].part == PageRange(7, 12)
        assert "故意失败" in (outcome.failed[0].error or "")
        # 失败段的正文不进合并结果，但成功的两段都要在
        assert "内容0" in outcome.markdown and "内容2" in outcome.markdown

    def test_失败的段不插入页标记(self) -> None:
        # 插了的话会得到一个指向空白的页标记，chunker 会把"第 501 页"变成一个空 chunk
        parser = FakeParser(fail_on={1})
        outcome = _run_splitter(parser)
        assert render_page_marker(1) in outcome.markdown
        assert render_page_marker(13) in outcome.markdown
        assert outcome.markdown.count("<!-- page:") == 2

    def test_全部失败时产物为空(self) -> None:
        parser = FakeParser(fail_on={0, 1, 2})
        outcome = _run_splitter(parser)
        assert outcome.markdown == ""
        assert len(outcome.failed) == 3

    def test_回调按段报告进展(self) -> None:
        # 界面靠这两个回调显示"第 1 段在跑 / 第 1 段成功"，
        # 没有它们整条切分期间界面只有一个转圈
        started: list[PageRange] = []
        finished: list[tuple[str, str | None]] = []
        parser = FakeParser(fail_on={1})
        _run_splitter(
            parser,
            part_id_of=lambda index: f"p{index}",
            on_part=lambda part, part_id: started.append(part),
            on_part_done=lambda outcome: finished.append((outcome.part_id, outcome.error)),
        )

        assert len(started) == 3
        assert finished[0] == ("p0", None)
        assert finished[1][0] == "p1" and finished[1][1]

    def test_解析器的意外异常也被兜住(self) -> None:
        # 云端解析器可能抛出非 ParseError 的东西（httpx 超时、JSON 解码…）。
        # 让它冒出去会变成任务级失败，整份文档重来
        class Exploding(FakeParser):
            def parse(self, **kwargs):  # type: ignore[override]
                raise RuntimeError("网络断了")

        outcome = _run_splitter(Exploding())
        assert len(outcome.failed) == 3
        assert "RuntimeError" in (outcome.failed[0].error or "")

    def test_真的按页切了而不是整份提交(self) -> None:
        """编排到底有没有用上 `extract_page_range`。

        这一条是**唯一能证明切分真的发生**的断言：假解析器拿到的是切好的字节，
        如果编排把原始 PDF 整份传下去，三次调用的字节数会完全相同。
        """
        parser = FakeParser()
        source = make_pdf(3 * _TEST_PART_PAGES)
        _run_splitter(parser)
        sizes = [size for size, _ in parser.calls]
        assert len(set(sizes)) == 3, f"三段拿到的字节数应当各不相同，实际 {sizes}"
        # 且每段都明显小于原文——整份传下去的话三段的大小会都等于原文
        assert all(size < len(source) for size in sizes), f"原文 {len(source)} 字节，各段 {sizes}"


class TestBuildResult:
    def test_页数是各段之和(self) -> None:
        outcome = _run_splitter(FakeParser())
        result = build_result(outcome, parser_name="FakeParser")
        assert result.page_count == 3 * _TEST_PART_PAGES
        assert result.parser_name == "FakeParser"

    def test_有段失败时页数仍是原文件总页数(self) -> None:
        # 页数是原文件的属性，不是"解析成功了多少页"。
        # 改小会让界面显示一个错误的页数，而真正的坏消息由抛出的错误承担
        outcome = _run_splitter(FakeParser(fail_on={1}))
        assert build_result(outcome, parser_name="FakeParser").page_count == 3 * _TEST_PART_PAGES


# ------------------------------------------- 摄入链路上的落库（T2.7 的收口）


class _EchoPdf:
    """把切片里的文字原样吐回来。

    单测里没有云端凭据，而这里要验的是**切分编排**而不是解析质量，
    所以一个只做"读回来"的解析器就够了——但它必须真的读，
    否则"按页切了"这条断言又会退化成同义反复。
    """

    name = "EchoPdf"

    def supports(self, *, filename: str, mime_type: str | None, probe) -> bool:
        return filename.lower().endswith(".pdf")

    def parse(self, *, content: bytes, filename: str, mime_type=None, probe=None) -> ParseResult:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as document:
            text = "\n".join(page.get_text().strip() for page in document)
        return ParseResult(markdown=text, parser_name=self.name)


class TestIngestRecordsPageCountAndParts:
    """切分不只要算出结果，还要**在库里留下痕迹**。

    这里测三件界面直接依赖的事，缺任何一件用户都看不到切分发生过：

    - ``documents.page_count`` —— 界面上"共 N 页"的唯一来源（此前恒为 null）；
    - ``documents.is_split`` —— 决定这一行渲染成可展开的父行还是普通单行；
    - ``document_parts`` —— 子文件树的内容，以及"哪一段失败了"。

    用**真的** 201 页 PDF：断言"按页切了"却喂假字节的话，
    测的就只是 ``extract_page_range`` 的报错分支（第一版已经这么错过一次）。
    """

    @staticmethod
    def _service(bundle):
        from app.services.embedding.deterministic import DeterministicEmbedder
        from app.services.ingest import IngestService
        from app.services.parser_router import ParserRouter

        return IngestService(
            bundle,
            router=ParserRouter([_EchoPdf()]),
            embedder=DeterministicEmbedder(dim=8),
        )

    def test_超过上限的_PDF_被切分并留下子文件记录(self, bundle, kb) -> None:
        service = self._service(bundle)
        pdf = make_pdf(MINERU_PAGE_LIMIT + 1)
        submitted = service.submit(
            knowledge_base_id=kb.id, filename="大部头.pdf", content=pdf, mime_type="application/pdf"
        )

        outcome = service._probe_and_parse(submitted.document)

        assert outcome.page_count == MINERU_PAGE_LIMIT + 1
        # 切分出来的产物要带页标记，否则 201 页文档的检索结果定位不到原页。
        # 201 页 → 两段（1-200、201-201），两段的起始页标记都要在
        assert render_page_marker(1) in outcome.markdown
        assert render_page_marker(201) in outcome.markdown
        assert outcome.markdown.count("<!-- page:") == 2

        refreshed = bundle.meta.get_document(submitted.document.id)
        assert refreshed is not None
        assert refreshed.page_count == MINERU_PAGE_LIMIT + 1
        assert refreshed.is_split is True

        parts = bundle.meta.list_document_parts(submitted.document.id)
        assert len(parts) == 2
        assert (parts[0].page_start, parts[0].page_end) == (1, 200)
        assert (parts[1].page_start, parts[1].page_end) == (201, 201)
        assert all(part.stage is DocumentStage.PARSED for part in parts)

    def test_未超上限时也记页数但不切(self, bundle, kb) -> None:
        service = self._service(bundle)
        submitted = service.submit(
            knowledge_base_id=kb.id,
            filename="小册子.pdf",
            content=make_pdf(3),
            mime_type="application/pdf",
        )
        service._probe_and_parse(submitted.document)

        refreshed = bundle.meta.get_document(submitted.document.id)
        assert refreshed is not None
        # 页数照记：界面上"共 N 页"与"是否被切分"是两件事，
        # 没切分的文档同样要能显示页数（这一列此前一直是 null）
        assert refreshed.page_count == 3
        assert refreshed.is_split is False
        assert bundle.meta.list_document_parts(submitted.document.id) == []

    def test_重跑不会把子文件记录翻倍(self, bundle, kb) -> None:
        # 失败重跑是设计内的路径：`ingest()` 从当前阶段续跑，已完成的步骤跳过。
        # 子文件 id 若用随机值，界面上子文件树会越跑越长——而重跑是**正常操作**
        # （改了切块大小之后重跑是常见动作），不是异常路径。
        service = self._service(bundle)
        submitted = service.submit(
            knowledge_base_id=kb.id,
            filename="大部头.pdf",
            content=make_pdf(MINERU_PAGE_LIMIT + 1),
            mime_type="application/pdf",
        )

        service.ingest(submitted.document.id)
        first = bundle.meta.list_document_parts(submitted.document.id)
        service.ingest(submitted.document.id)
        second = bundle.meta.list_document_parts(submitted.document.id)

        assert len(first) == 2
        assert [part.id for part in first] == [part.id for part in second]
        assert len(second) == 2
