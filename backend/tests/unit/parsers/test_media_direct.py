"""媒体直通解析器（图片 / 视频）：支持范围、产物形状、以及**它什么时候才会被挂上**。

镜像同构：``app/parsers/media_direct.py`` → 本文件。

三条边界各有用例：产物如实标注向量来源、支持范围与 probe 口径一致、以及
解析路由里"最低优先级 + 配置门控"（配了 OCR 的图片照旧走 OCR）。

**不依赖夹具**：路由那几条用一个只回答 `mineru()/paddleocr()/embedding()` 的替身
（`build_parsers` 要的就是这三件事），本机没有测试库时也能真跑。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.parsers.base import ParseError, ProbeKind, ProbeResult
from app.parsers.media_direct import MEDIA_IMAGE_EXTENSIONS, MediaDirectParser
from app.parsers.mineru_cloud import MinerUConfig
from app.parsers.paddleocr_api import PaddleOCRConfig
from app.parsers.plain_text import PlainTextParser
from app.parsers.probe import BINARY_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, probe
from app.services.parser_router import ParserRouter, build_parsers
from app.services.runtime_config import EmbeddingSettings


@dataclass
class _FakeRuntime:
    """`build_parsers` 只问几件事：两个云端凭据 + "这台机器上有没有媒体能力"。"""

    protocol: str = "openai"
    mineru_token: str = ""
    paddleocr_token: str = ""
    media_declared: bool = False
    """模拟"某个**已登记模型**声明了 wemm"（API/配置层按模型设的那一栏）。"""

    def mineru(self) -> MinerUConfig:
        return MinerUConfig(token=self.mineru_token)

    def paddleocr(self) -> PaddleOCRConfig:
        return PaddleOCRConfig(token=self.paddleocr_token, endpoint="https://example.com")

    def embedding(self) -> EmbeddingSettings:
        return EmbeddingSettings(
            base_url="http://127.0.0.1:8234",
            api_key="",
            model_id="WeMM-Embedding-2B-Q4_K_M.gguf",
            dim=2048,
            batch_size=32,
            protocol=self.protocol,
        )

    def embedding_supports_media(self) -> bool:
        """与 `RuntimeConfigService.embedding_supports_media` 同一个契约。

        真实实现是"全局默认支持媒体 **或** 任一登记模型声明了支持媒体的协议"；
        这里把那个"或"的两半拆成两个显式开关，好让两条路各有一条用例。
        """
        return self.media_declared or self.protocol == "wemm"


def _scanned() -> ProbeResult:
    return ProbeResult(kind=ProbeKind.SCANNED, text_coverage=0.0)


def _parser() -> MediaDirectParser:
    return MediaDirectParser()


# ---------------------------------------------------------------------- 支持范围


def test_supports_common_image_and_video_suffixes() -> None:
    parser = _parser()
    for name in ("图.png", "照.jpg", "照.jpeg", "动.webp", "图.bmp", "动.gif"):
        assert parser.supports(filename=name, mime_type=None, probe=_scanned()), name
    for name in ("片.mp4", "片.mov", "片.mkv", "片.webm", "片.avi", "片.m4v"):
        assert parser.supports(filename=name, mime_type=None, probe=_scanned()), name


def test_mime_is_only_a_fallback() -> None:
    """浏览器之外的上传器常常只给 MIME：那种情况也要认得出来。"""
    parser = _parser()
    assert parser.supports(filename="没后缀", mime_type="image/png", probe=_scanned())
    assert parser.supports(
        filename="没后缀", mime_type="image/jpeg; charset=binary", probe=_scanned()
    )
    assert parser.supports(filename="没后缀", mime_type="video/quicktime", probe=_scanned())
    assert not parser.supports(filename="没后缀", mime_type="text/plain", probe=_scanned())


def test_mime_whitelist_excludes_what_the_decoder_cannot_read() -> None:
    """``image/*`` 太宽：svg 是文本、avif/jxl 多半没编进解码器——放进来只会得到
    "服务端报错"或"一条没有意义的向量"，两者都比早点说这个格式走不通糟。"""
    parser = _parser()
    for mime in ("image/svg+xml", "image/avif", "image/jxl", "image/tiff"):
        assert not parser.supports(filename="没后缀", mime_type=mime, probe=_scanned()), mime


def test_tiff_and_jp2_are_not_media_passthrough() -> None:
    """``probe`` 认它们"是图片"，但多模态编码器不一定解得开。

    宁可在这里拦住（用户看到的是"这个格式走不通"），也不要让一张读不出来的图
    **悄悄**占一条没有任何意义的向量。
    """
    parser = _parser()
    assert not parser.supports(filename="扫描.tif", mime_type=None, probe=_scanned())
    assert not parser.supports(filename="扫描.tiff", mime_type=None, probe=_scanned())
    assert not parser.supports(filename="老图.jp2", mime_type=None, probe=_scanned())


def test_media_image_vocabulary_stays_inside_the_probe_vocabulary() -> None:
    """扩展名口径只有一处（probe）：这里只允许**收窄**，不允许自己发明一种"图片"。"""
    assert MEDIA_IMAGE_EXTENSIONS <= IMAGE_EXTENSIONS


def test_videos_are_binary_containers_not_text() -> None:
    """视频进 `BINARY_EXTENSIONS`：否则纯文本直通拿到 `video/mp4` 声明的字节流会当文本收下。"""
    assert VIDEO_EXTENSIONS <= BINARY_EXTENSIONS
    assert not PlainTextParser().supports(
        filename="片.mp4", mime_type="video/mp4", probe=_scanned()
    )


def test_probe_reports_video_as_needing_media() -> None:
    """探测对视频的结论是"需要外部能力"，而不是"这是个二进制垃圾"——理由要能落进任务记录。"""
    result = probe(b"\x00\x00\x00 ftypmp42", filename="片.mp4", mime_type="video/mp4")

    assert result.kind == ProbeKind.SCANNED
    assert "媒体向量" in result.detail["reason"]


# ---------------------------------------------------------------------- 产物


def test_markdown_states_where_the_vector_came_from() -> None:
    """产物必须**如实标注**：出处/引用看到这段文字时不能以为它是解析出来的正文。"""
    result = _parser().parse(content=b"x" * 2048, filename="花.mp4", probe=_scanned())

    assert result.parser_name == MediaDirectParser.name
    assert result.markdown.startswith("# 花.mp4")
    assert "媒体向量索引" in result.markdown
    assert "视频直通" in result.markdown
    assert "不是" in result.markdown  # "不是从这段文字解析出来的内容"


def test_markdown_says_image_for_images() -> None:
    result = _parser().parse(content=b"x", filename="图.png", probe=_scanned())
    assert "图片直通" in result.markdown


def test_markdown_keeps_the_filename_as_the_citation_anchor() -> None:
    """检索命中时用户点开出处，至少要看到这是哪份文件（标题 + 文件名都在）。"""
    result = _parser().parse(content=b"x", filename="dir/花 花.mp4", probe=_scanned())

    assert "# 花 花.mp4" in result.markdown  # 只取文件名，不带目录


def test_size_is_readable_for_small_files() -> None:
    """18 KB 写成 "0.0 MB" 等于没写：这一行是用户点开出处时唯一的体量线索。"""
    from app.parsers.media_direct import human_size

    assert human_size(18122) == "17.7 KB"
    assert human_size(1128375) == "1.1 MB"


def test_page_count_is_absent_and_probe_is_carried_through() -> None:
    scanned = _scanned()
    result = _parser().parse(content=b"x", filename="图.png", probe=scanned)

    assert result.page_count is None
    assert result.probe is scanned


# ---------------------------------------------------------------------- 路由：最低优先级 + 配置门控


def test_gate_is_off_by_default() -> None:
    """默认（OpenAI 兼容）那一档下这个解析器**根本不存在**：既有部署行为一位不变。"""
    names = [parser.name for parser in build_parsers(_FakeRuntime())]  # type: ignore[arg-type]

    assert MediaDirectParser.name not in names


def test_gate_turns_it_on_and_keeps_it_last() -> None:
    names = [
        parser.name
        for parser in build_parsers(_FakeRuntime(protocol="wemm"))  # type: ignore[arg-type]
    ]

    assert names[-1] == MediaDirectParser.name
    # 只挂一个：排在最后意味着"前面谁都不认"才轮到它
    assert names.count(MediaDirectParser.name) == 1


def test_gate_also_opens_when_a_registered_model_declares_media() -> None:
    """全局默认还是 openai，但**某个库绑的模型**声明了 wemm：门控也要开。

    协议是按模型的（`protocols.protocol_for_model`），所以只看全局默认会把这种组合下
    的图片 / 视频挡在"暂不支持"上——那个库的媒体文件连进都进不来。
    """
    names = [
        parser.name
        for parser in build_parsers(_FakeRuntime(media_declared=True))  # type: ignore[arg-type]
    ]

    assert names[-1] == MediaDirectParser.name


def test_ocr_still_wins_for_images_when_configured() -> None:
    """**行为不变的那一条**：配了 OCR 的图片照旧走 OCR，媒体直通只兜"没人接"的。"""
    runtime = _FakeRuntime(protocol="wemm", mineru_token="token")
    router = ParserRouter(build_parsers(runtime))  # type: ignore[arg-type]

    decision = router.decide(filename="扫描.png", mime_type="image/png", probe=_scanned())

    assert decision.parser_name == "MinerUCloudParser"


def test_video_is_routed_to_media_passthrough_when_enabled() -> None:
    """今天视频**进不了库**（没有任何解析器支持）；开了媒体直通它才有去处。"""
    runtime = _FakeRuntime(protocol="wemm")
    router = ParserRouter(build_parsers(runtime))  # type: ignore[arg-type]

    decision = router.decide(filename="片.mp4", mime_type="video/mp4", probe=_scanned())

    assert decision.parser_name == MediaDirectParser.name


def test_video_without_the_feature_still_fails_with_an_actionable_hint() -> None:
    """没开这一档时视频仍然"暂不支持"，但报错要指出唯一的出路（多模态嵌入）。"""
    router = ParserRouter(build_parsers(_FakeRuntime()))  # type: ignore[arg-type]

    with pytest.raises(ParseError) as excinfo:
        router.decide(filename="片.mp4", mime_type="video/mp4", probe=_scanned())

    assert "暂不支持" in str(excinfo.value)
    assert "WeMM" in str(excinfo.value)
