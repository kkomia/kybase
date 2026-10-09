"""媒体直通解析器（图片 / 视频 → 一份"这是什么文件"的最小 Markdown）。

## 为什么需要它

今天这两类文件在库里是这样：

- **视频**（``.mp4`` 等）**根本进不了库**：探测成二进制之后没有任何解析器支持它，
  ``ParserRouter.decide()`` 直接抛"暂不支持的文件类型"。用户看到的是一个上传失败，
  而他手上那份视频本来是可以被检索的；
- **图片**必须先配云端 OCR（MinerU / PaddleOCR）才能入库，而它进库的是 **OCR 出来的
  文字**——图本身（"这张图里是什么"）从来没有变成过向量。

配了多模态嵌入（``WeMM``，三种模态共享同一向量空间）之后，这两类文件的价值恰恰在
"它自己那份向量"上：一句文字就能搜到这张图 / 这段视频。这个解析器就是那条路的第一步——
让它们**能进库**；向量由 ``IngestService`` 走媒体接口算（见 ``_media_blob``）。

## 三条边界（都是刻意的）

1. **最低优先级**：它由 ``ParserRouter.build_parsers`` **追加在清单最后**，只有别的解析器
   都不支持时才轮到它。所以"配了 OCR 的图片照旧走 OCR、走 OCR 文本向量"这条既有行为
   一位不变——包括降级链：MinerU 失败时它会成为下一个候选（这是好事：OCR 抖动不该让
   一份图片白失败）；
2. **由配置门控**：只有当前嵌入协议支持媒体时它才会被挂上（``protocols.supports_media``）。
   默认的 OpenAI 兼容那一档下它**根本不存在**，既有部署的行为一个字都没变；
3. **产物如实标注向量来源**：Markdown 里写明"本文件由媒体向量索引"。
   这一行是**引用的红线**：出处必须说的是它真正是什么——用户点开出处看到这段文字时，
   不能以为"这就是解析出来的正文"（那段正文并不存在，我们只有文件名与那份向量）。
"""

from __future__ import annotations

from app.parsers.base import ParseResult, ParserProvider, ProbeResult
from app.parsers.probe import VIDEO_EXTENSIONS, suffix_of

__all__ = ["MEDIA_IMAGE_EXTENSIONS", "MEDIA_IMAGE_MIME", "MediaDirectParser", "human_size"]

#: 能交给多模态编码器的图片格式（``stb_image`` 那一族：png / jpg / webp / bmp / gif）。
#: **刻意不照抄 ``probe.IMAGE_EXTENSIONS``**：那份表里还有 tif / tiff / jp2，它们"是图片"
#: 没错，但解码器认不认是另一回事——认不出来时服务端要么报错、要么给回一个没有意义的
#: 向量。宁可在这里拦住（用户看到的是"这个格式走不通，请转成 png/jpg"），
#: 也不要让一张读不出来的图悄悄占一条向量。
MEDIA_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})

#: 认的图片 MIME 白名单——**与后缀白名单是同一份事实**（解码器认得的那些）。
#: 刻意不写 ``mime.startswith("image/")``：那会把 svg / avif / jxl 一起放进来，
#: 而它们解不开（svg 是文本、avif 多半没编进解码器）。放进来之后只有两种下场：
#: 服务端报错，或者给回一条没有意义的向量——两者都比"早点说这个格式走不通"糟。
MEDIA_IMAGE_MIME = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif"}
)


def human_size(size: int) -> str:
    """人读的体积：不足 1 MB 就给 KB。

    18 KB 的一张图写成 "0.0 MB" 等于没写；这一行是用户点开出处时唯一的体量线索。
    """
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class MediaDirectParser(ParserProvider):
    """图片 / 视频直通：不解析正文，只如实说"这是一份媒体文件"。"""

    name = "MediaDirectParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        """认后缀（白名单），也认 MIME（**同一份白名单**）。

        **不做优先级判断**（"有没有配 OCR"之类）：那是解析路由的事，这里判了就有两处
        真相。它挂在链尾，能被问到就说明前面没人接。
        """
        suffix = suffix_of(filename)
        if suffix in MEDIA_IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS:
            return True
        # 浏览器之外的上传器常常只给 MIME（甚至留空），所以 MIME 也要认；
        # 但认的范围与后缀是同一份（见 `MEDIA_IMAGE_MIME` 的说明）
        mime = (mime_type or "").lower().split(";")[0].strip()
        if mime in MEDIA_IMAGE_MIME:
            return True
        return mime.startswith("video/")

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        """产出一份最小 Markdown：标题是文件名，正文只有一句**向量来源的说明**。

        为什么正文不写别的东西：写任何像"内容摘要"的话都是在编——我们手里只有文件名与
        那份向量。这一段的用途是**出处的落点**（检索命中时用户点开看得到它是什么文件），
        而不是假装它是被解析出来的正文。
        """
        title = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or "未命名媒体"
        kind = "视频" if suffix_of(filename) in VIDEO_EXTENSIONS else "图片"
        size = human_size(len(content))
        return ParseResult(
            markdown=(
                f"# {title}\n\n"
                f"本文件由**媒体向量索引**（{kind}直通，{size}）：它的向量来自多模态嵌入"
                f"接口对{kind}本身的编码，**不是**从这段文字解析出来的内容。\n"
                f"这里只留下文件名，作为检索命中时的出处落点。\n"
            ),
            parser_name=self.name,
            page_count=None,
            probe=probe,
        )
