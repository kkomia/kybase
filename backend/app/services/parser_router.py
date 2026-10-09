"""解析路由决策器（M2 T2.6）。

架构 §4.1 的差异化能力：**逐文件探测、逐文件路由**，并把决策连同理由一起写进任务记录，
让控制台能回答"这个文件为什么走了 OCR / 为什么用了直提"。

放在 ``services/`` 而不是 ``parsers/``：路由必然要 import 多个解析器实现，
而工程规范 §3.3 禁止解析器实现之间互相引用（`scripts/check_layering.py` 的 L3 规则）。

**解析器清单每次决策时现建**：凭据与模型来自设置页（运行期配置），
用户在网页上填完 token 就该立刻生效，而不是重启进程才认。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.parsers.base import ParseError, ParserProvider, ProbeKind, ProbeResult
from app.parsers.html_upload import HtmlUploadParser
from app.parsers.local_office import LOCAL_OFFICE_EXTENSIONS, LocalOfficeParser
from app.parsers.local_pdf import LocalPdfTextParser
from app.parsers.media_direct import MediaDirectParser
from app.parsers.mineru_cloud import MinerUCloudParser
from app.parsers.paddleocr_api import PaddleOCRApiParser
from app.parsers.plain_text import PlainTextParser
from app.parsers.probe import (
    IMAGE_EXTENSIONS,
    OFFICE_EXTENSIONS,
    PDF_EXTENSIONS,
    VIDEO_EXTENSIONS,
    suffix_of,
)
from app.parsers.tabular import TabularParser
from app.services.runtime_config import RuntimeConfigService

__all__ = ["ParserRouter", "RoutingDecision", "build_parsers"]


def build_parsers(runtime: RuntimeConfigService) -> list[ParserProvider]:
    """按当前运行期配置构造解析器清单，**顺序即优先级**。

    顺序理由（前面优先）：
    1. **表格解析器**——它与纯文本直通都认 ``.csv``，而纯文本直通会把 CSV 原文当
       Markdown（列名只在第一行出现一次，切块后大部分行的列名就不在同一块里了）；
    2. **HTML 解析器**——同理，纯文本直通会把 ``.html`` 原样收下，脚本与导航直接进库；
    3. **纯文本直通**——最便宜，兜住所有 text/* 与已知文本后缀；
    4. **MinerU**（版面还原更强，扫描件的第一选择）、**PaddleOCR**（备选通道）；
    5. **本地 PDF/Office 直提**——垫在最后是有意的：有云端凭据就用还原更好的那份，
       没凭据时文字型 PDF 与 docx/pptx 仍然能用（v17 之前它们直接"暂不支持"）。
    云端解析器都会在 ``supports()`` 里排除纯文本类文件，所以顺序不会误伤本地直读；
    未配置 token 时它们的 ``supports()`` 恒为 False —— 没凭据也能跑通整条链路。

    **6（条件项）媒体直通**（图片 / 视频）：只有当前嵌入协议**支持媒体**时才追加，
    而且永远排最后——它接的是"前面谁都不认"的那一类（视频从来没人认，没配 OCR 的图片
    也没人认）。配了 OCR 的图片照旧被 MinerU/PaddleOCR 接走，行为一位不变；
    默认的 OpenAI 兼容那一档下这个解析器根本不存在（见 `parsers/media_direct.py`）。
    """
    parsers: list[ParserProvider] = [
        TabularParser(),
        HtmlUploadParser(),
        PlainTextParser(),
        MinerUCloudParser(runtime.mineru()),
        PaddleOCRApiParser(runtime.paddleocr()),
        # 本地兜底：扫描件不接（本地没有 OCR），文字型 PDF 与 OOXML 接
        LocalPdfTextParser(),
        LocalOfficeParser(),
    ]
    if runtime.embedding_supports_media():
        parsers.append(MediaDirectParser())
    return parsers


def hint_for(probe: ProbeResult, filename: str) -> str:
    """说不支持之后，再告诉用户**下一步做什么**。

    最需要这句的是"扫描件 + 没配云端引擎"：`暂不支持` 本身没给任何出路，
    而用户手上就有一个打不开的 PDF。这里直接指向设置页那两栏。

    **两个地方都用它**（所以是公开函数）：一个是"一个候选都没有"（`decide`），
    另一个是"候选全失败了"（`ingest._probe_and_parse`）——实测 .webm 上传正是后者：
    MinerU 拒收它，而那时没配多模态、媒体直通压根不在候选里，用户看到的
    就只有"MinerU 不支持该文件格式"这一句，手上那份视频其实一配就能进库。
    """
    suffix = suffix_of(filename)
    needs_ocr = suffix in PDF_EXTENSIONS or suffix in IMAGE_EXTENSIONS
    if suffix in VIDEO_EXTENSIONS:
        # 视频走不了 OCR（云端两家都不接），唯一的出路就是多模态嵌入——不说这一句，
        # 用户手上那份视频的报错里没有任何可执行的下一步
        return (
            "。视频只能走多模态嵌入：请到「设置 → 向量化 → 嵌入协议」选 WeMM 多模态，"
            "并在「设置 → 模型注册」登记那台服务的地址"
        )
    if needs_ocr and probe.kind in {ProbeKind.SCANNED, ProbeKind.MIXED}:
        return (
            "。这个文件需要 OCR：请到「设置 → 服务配置」配置 MinerU 或 PaddleOCR；"
            "或者到「设置 → 向量化 → 嵌入协议」选 WeMM 多模态，让图片本身进向量空间"
        )
    if suffix in OFFICE_EXTENSIONS and suffix not in LOCAL_OFFICE_EXTENSIONS:
        return "。旧的二进制 Office 格式只能走云端：请配置 MinerU，或先另存为 .docx / .pptx"
    return ""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """一次路由决策：选了谁、为什么、探测到了什么。"""

    parser: ParserProvider
    reason: str
    probe: ProbeResult

    @property
    def parser_name(self) -> str:
        return self.parser.name


class ParserRouter:
    """按注册顺序挑选第一个 ``supports`` 的解析器（顺序即优先级）。"""

    def __init__(self, parsers: Sequence[ParserProvider] | RuntimeConfigService) -> None:
        if isinstance(parsers, RuntimeConfigService):
            self._runtime: RuntimeConfigService | None = parsers
            self._fixed: tuple[ParserProvider, ...] | None = None
        else:
            if not parsers:
                raise ValueError("至少需要注册一个解析器")
            self._runtime = None
            self._fixed = tuple(parsers)

    @property
    def parsers(self) -> tuple[ParserProvider, ...]:
        if self._fixed is not None:
            return self._fixed
        if self._runtime is None:  # pragma: no cover - 构造时二者必有其一
            raise RuntimeError("ParserRouter 既没有固定清单也没有运行期配置")
        return tuple(build_parsers(self._runtime))

    @property
    def parser_names(self) -> tuple[str, ...]:
        return tuple(parser.name for parser in self.parsers)

    def candidates(
        self, *, filename: str, mime_type: str | None, probe: ProbeResult
    ) -> list[RoutingDecision]:
        """**所有**能处理该文件的解析器，按优先级排列（v17 起用于失败降级）。

        为什么要有这个而不是只给一个：云端引擎会失败（额度用尽、接口抖动、
        返回半截结果），而"这个文件谁都能解"的情况其实很常见——
        扫描件有 MinerU 与 PaddleOCR 两条路，文字型 PDF 还有本地直提。
        原先只挑第一个，第一个失败就整份文档失败，白白浪费了后面的通道。
        """
        decisions: list[RoutingDecision] = []
        for parser in self.parsers:
            if parser.supports(filename=filename, mime_type=mime_type, probe=probe):
                decisions.append(
                    RoutingDecision(
                        parser=parser,
                        reason=f"{parser.name} 支持该类型（探测结论：{probe.kind}）",
                        probe=probe,
                    )
                )
        return decisions

    def decide(
        self, *, filename: str, mime_type: str | None, probe: ProbeResult
    ) -> RoutingDecision:
        """挑第一个能处理的解析器，**不降级**（单次决策，给调用方自己编排用）。"""
        decisions = self.candidates(filename=filename, mime_type=mime_type, probe=probe)
        if decisions:
            return decisions[0]
        raise ParseError(
            f"暂不支持的文件类型：{filename or '(未命名)'}（探测结论：{probe.kind}）"
            + hint_for(probe, filename),
            stage="probing",
        )
