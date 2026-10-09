"""PaddleOCR-VL 云端解析（M2 T2.5）。

接口来自官方示例（``paddleocr.aistudio-app.com/api/v2/ocr/jobs``），是**异步任务**模式：

1. ``POST {JOB_URL}`` —— 本地文件走 ``multipart``（``data`` + ``files``），
   远端 URL 走 JSON（``fileUrl``）；
2. ``GET {JOB_URL}/{jobId}`` 轮询，``data.state`` 依次是
   ``pending`` → ``running`` → ``done`` / ``failed``；
3. ``done`` 时取 ``data.resultUrl.jsonUrl``，那是一个 **JSONL**：每行一页，
   ``result.layoutParsingResults[].markdown.text`` 是这一页的 Markdown，
   ``markdown.images`` 是"相对路径 → 图片 URL"的映射，需要自己下载。

为什么两个云端解析器都留着：MinerU 强在版面还原与公式表格，PaddleOCR 是**另一条独立通道**——
架构 §4.1 要求"云端失败可降级备选节点"，同一份文件换一个引擎成功率会明显不同。

关键提醒（官方示例里踩过的坑）：``multipart`` 的 ``optionalPayload`` 必须是 **JSON 字符串**，
传 dict 会被当成字段展开而报错。

实现只依赖 ``parsers/base.py`` 的 ``ParseResult``（工程规范 §3.3 的 L3 规则），
外加 ``core/page_markers.py`` 这个纯文本工具（core 是共享层，不违反插件层纪律）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.core.page_markers import page_marker
from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeKind, ProbeResult
from app.parsers.probe import IMAGE_EXTENSIONS, PDF_EXTENSIONS, suffix_of

#: 与 `probe.py` 同源的支持范围（解释见 mineru_cloud.py 的同名常量）。
#: PaddleOCR 只吃 pdf 与常见位图，tiff/gif/jp2 **显式排除**。
_SUPPORTED_IMAGES = IMAGE_EXTENSIONS - {".gif", ".jp2", ".tif", ".tiff"}

__all__ = ["PaddleOCRApiParser", "PaddleOCRConfig"]

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
"""轮询间隔：5 秒一次，与 MinerU 通道保持一致。"""

POLL_DEADLINE_SECONDS = 1800.0
"""轮询总时限（30 分钟）。与 MinerU 通道同一口径，理由见该模块的同名常量：
无界轮询会让摄入线程永久阻塞，任务占着队列、worker 看似活着却不再消费。"""
TERMINAL_STATES = frozenset({"done", "failed"})


@dataclass(frozen=True, slots=True)
class PaddleOCRConfig:
    """运行期配置快照（来自设置页）。"""

    token: str
    endpoint: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    model: str = "PaddleOCR-VL-1.6"
    timeout_seconds: float = 120.0
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.token)


@dataclass(slots=True)
class _PageImage:
    """一页里的一张图：本地文件名 + 内容。"""

    name: str
    data: bytes


@dataclass(slots=True)
class _Page:
    markdown: str
    images: list[_PageImage] = field(default_factory=list)


class PaddleOCRApiParser(ParserProvider):
    """PaddleOCR-VL：扫描件与混合型文档的第二条云端通道。"""

    name = "PaddleOCRApiParser"

    #: 纯文本类直读文件不必上云
    NATIVE_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".csv", ".json", ".log")

    def __init__(
        self,
        config: PaddleOCRConfig,
        *,
        client: httpx.Client | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._config = config
        self._client = client
        self._poll_interval = poll_interval

    @property
    def description(self) -> str:
        return f"PaddleOCR（{self._config.model}）"

    # ------------------------------------------------------------------ 路由

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        if not self._config.is_configured:
            return False

        lowered = (filename or "").lower()
        if lowered.endswith(self.NATIVE_SUFFIXES):
            return False

        # 它是"扫描件的第二选择"：文字型 PDF 交给 MinerU 更快更准
        suffix = suffix_of(filename)
        if suffix in PDF_EXTENSIONS or suffix in _SUPPORTED_IMAGES:
            return probe.kind in {ProbeKind.SCANNED, ProbeKind.MIXED}
        return False

    # ------------------------------------------------------------------ 解析

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        if not self._config.is_configured:
            raise ParseError("PaddleOCR 未配置 Token，请到设置页填写后再试", stage="parsing")

        client = self._client or httpx.Client(timeout=self._config.timeout_seconds)
        owns_client = self._client is None
        try:
            job_id = self._submit(client, content, filename)
            jsonl_url = self._poll(client, job_id)
            pages = self._collect(client, jsonl_url)
        finally:
            if owns_client:
                client.close()

        # 逐页产出时就插页标记：PaddleOCR 本来就是按页给结果的，这是拿到**准确页码**
        # 的唯一机会——合并成一整段之后再想拆回来只能靠猜。
        # 页码用 enumerate 的位置（真实页序号），空页也占一个页号，不做压缩。
        blocks: list[str] = []
        for page_number, page in enumerate(pages, start=1):
            body = page.markdown.strip()
            if not body:
                continue
            blocks.append(page_marker(page_number))
            blocks.append(body)
        markdown = "\n\n".join(blocks)
        if not markdown:
            raise ParseError("PaddleOCR 返回的 Markdown 为空", stage="parsing")

        images = [image for page in pages for image in page.images]
        return ParseResult(
            markdown=markdown,
            parser_name=self.name,
            page_count=len(pages) or (probe.page_count if probe else None),
            probe=probe,
            image_ids=[image.name for image in images],
        )

    # ------------------------------------------------------------------ 步骤

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self._config.token}"}

    def _optional_payload(self) -> str:
        return json.dumps(
            {
                "useDocOrientationClassify": self._config.use_doc_orientation_classify,
                "useDocUnwarping": self._config.use_doc_unwarping,
            }
        )

    def _submit(self, client: httpx.Client, content: bytes, filename: str) -> str:
        # multipart：optionalPayload 必须是字符串，传 dict 会报错（官方示例即如此）
        response = client.post(
            self._config.endpoint,
            headers=self._headers(),
            data={"model": self._config.model, "optionalPayload": self._optional_payload()},
            files={"file": (filename or "document.pdf", content, "application/octet-stream")},
        )
        data = _unwrap(response, action="提交解析任务")
        job_id = data.get("jobId")
        if not job_id:
            raise ParseError("PaddleOCR 未返回 jobId", stage="parsing")
        return str(job_id)

    def _poll(self, client: httpx.Client, job_id: str) -> str:
        url = f"{self._config.endpoint}/{job_id}"
        deadline = time.monotonic() + POLL_DEADLINE_SECONDS
        while True:
            response = client.get(url, headers=self._headers())
            data = _unwrap(response, action="查询解析进度")
            state = data.get("state")

            if state == "done":
                result_url = (data.get("resultUrl") or {}).get("jsonUrl")
                if not result_url:
                    raise ParseError("PaddleOCR 标记完成但未给出结果地址", stage="parsing")
                return str(result_url)
            if state == "failed":
                raise ParseError(
                    f"PaddleOCR 解析失败：{data.get('errorMsg') or '未提供原因'}", stage="parsing"
                )

            progress = data.get("extractProgress") or {}
            if state == "running" and progress:
                logger.info(
                    "PaddleOCR 解析中：%s/%s 页",
                    progress.get("extractedPages"),
                    progress.get("totalPages"),
                )
            if time.monotonic() >= deadline:
                raise ParseError(
                    f"PaddleOCR 解析超过 {POLL_DEADLINE_SECONDS / 60:.0f} 分钟仍未完成"
                    f"（当前状态：{state or '未知'}），已放弃等待",
                    stage="parsing",
                )
            time.sleep(self._poll_interval)

    def _collect(self, client: httpx.Client, jsonl_url: str) -> list[_Page]:
        """下载 JSONL 并逐页收集 Markdown 与图片。

        图片在 JSONL 里是 URL（不是 zip 里的文件），所以下载也必须在这一层做：
        解析器的产物是"Markdown + 图片字节"，写盘由调用方负责。
        """
        response = client.get(jsonl_url)
        if response.status_code != 200:
            raise ParseError(
                f"PaddleOCR 结果下载失败（HTTP {response.status_code}）", stage="parsing"
            )

        pages: list[_Page] = []
        for line in response.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results = json.loads(line)["result"]["layoutParsingResults"]
            except (KeyError, ValueError) as exc:
                raise ParseError(f"PaddleOCR 结果格式不符合预期：{exc}", stage="parsing") from exc

            for entry in results:
                markdown_block = entry.get("markdown") or {}
                text = markdown_block.get("text") or ""
                images = self._download_images(
                    client, markdown_block.get("images") or {}, len(pages)
                )
                pages.append(_Page(markdown=text, images=images))
        return pages

    def _download_images(
        self, client: httpx.Client, mapping: dict[str, str], page_index: int
    ) -> list[_PageImage]:
        """下载这一页的图片，并改成可预测的扁平文件名。

        JSONL 给的键是形如 ``images/xxx.jpg`` 的相对路径，Markdown 里引用的也是它；
        这里统一压成 ``p0001_img01.jpg`` 并要求调用方替换引用，避免同名覆盖。
        """
        collected: list[_PageImage] = []
        for order, (raw_name, url) in enumerate(sorted(mapping.items()), start=1):
            suffix = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else "jpg"
            flat = f"p{page_index + 1:04d}_img{order:02d}.{suffix}"
            try:
                download = client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("PaddleOCR 图片下载失败（%s）：%s", raw_name, exc)
                continue
            if download.status_code != 200:
                logger.warning(
                    "PaddleOCR 图片下载失败（%s）：HTTP %s", raw_name, download.status_code
                )
                continue
            collected.append(_PageImage(name=flat, data=download.content))
        return collected


def _unwrap(response: httpx.Response, *, action: str) -> dict:
    """PaddleOCR 的响应信封是 ``{"data": {...}}``，失败时 HTTP 非 200 或 data 缺失。"""
    if response.status_code == 401:
        raise ParseError(
            f"PaddleOCR {action}失败：Token 无效或已过期，请到设置页重新填写", stage="parsing"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ParseError(
            f"PaddleOCR {action}失败（HTTP {response.status_code}，响应不是 JSON）", stage="parsing"
        ) from exc

    if response.status_code != 200:
        message = body.get("message") or body.get("msg") or body.get("errorMsg") or ""
        raise ParseError(
            f"PaddleOCR {action}失败（HTTP {response.status_code}）：{message or '未提供原因'}",
            stage="parsing",
        )

    data = body.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"PaddleOCR {action}响应缺少 data 字段", stage="parsing")
    return data
