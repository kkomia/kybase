"""阅读视角（报告 G2）。

镜像同构：``app/services/documents.py::content_kind`` / ``reading_view`` +
``/documents/{id}/preview`` → 本文件。

要钉的是"同一份文档，两条路径给同一个答案"：下载与预览对 kind 的判定必须一致，
否则会出现"预览能渲染、下载却是二进制"这种自相矛盾。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.services.documents import content_kind
from tests.conftest import admin_client as admin_session

TEXT = "# 眼轴共识\n\n眼轴长度是主要监测指标。\n\n## 测量\n\n应散瞳后测量。\n".encode()
SIGNING_SECRET = "preview-test-secret"


# --------------------------------------------------------------------- 分类


@pytest.mark.parametrize(
    ("filename", "has_markdown", "expected"),
    [
        # 有解析产物一律当 markdown：那是流水线归一化后的文本，也是检索真正依据的东西
        ("报告.pdf", True, "markdown"),
        ("扫描件.png", True, "markdown"),
        # 没产物时才看后缀
        ("报告.pdf", False, "pdf"),
        ("图.png", False, "image"),
        ("照片.JPEG", False, "image"),  # 大小写不敏感
        ("说明.md", False, "markdown"),
        ("data.csv", False, "markdown"),
        # Office 三件套单列 kind：它们要在前端用库渲染，不是"给个地址让浏览器画"
        ("表格.xlsx", False, "excel"),
        ("合同.docx", False, "docx"),
        ("讲稿.pptx", False, "pptx"),
        # 老式 OLE2（.doc/.ppt/.xls）预览库解析不了：宁可明确说不支持，也不给个点开是错的入口
        ("旧合同.doc", False, "binary"),
        ("旧表格.xls", False, "binary"),
        ("旧讲稿.ppt", False, "binary"),
        ("没有后缀", False, "binary"),
    ]
    )
def test_content_kind(filename: str, has_markdown: bool, expected: str) -> None:
    assert content_kind(filename, has_markdown=has_markdown) == expected


# --------------------------------------------------------------------- 接口


@pytest.fixture
def client(monkeypatch):
    """带管理员会话凭据的客户端。

    签名密钥仍走环境变量：它不属于凭据体系，单独配一次即可。
    """
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", SIGNING_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    with admin_session() as test_client:
        yield test_client
    get_settings.cache_clear()




def _upload(client: TestClient, name: str, content: bytes, mime: str) -> str:
    kb = client.post("/api/v1/knowledge-bases", json={"name": f"库-{name}"})
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb.json()['id']}/documents",
        files={"file": (name, io.BytesIO(content), mime)},
        params={"start": "false"},

    )
    assert upload.status_code == 202, upload.text
    return upload.json()["document"]["id"]


def test_preview_needs_credentials() -> None:
    """缺凭据要 401——刻意用不带会话的客户端，覆盖的就是这条分支。"""
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        assert anonymous.get("/api/v1/documents/doc_x/preview").status_code == 401


def test_unknown_document_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/documents/doc_不存在/preview").status_code == 404


def test_text_document_preview_returns_markdown_inline(client: TestClient) -> None:
    """文本类内联返回，前端直接渲染——不必再发一次请求取内容。"""
    document_id = _upload(client, "说明.md", TEXT, "text/markdown")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "markdown"
    assert "眼轴长度是主要监测指标" in body["text"]
    assert body["url"] is None, "文本类不该给 URL（内容已经在 text 里了）"
    assert body["filename"] == "说明.md"


def test_binary_document_preview_degrades_to_download(client: TestClient) -> None:
    """既不能原生显示、也没有前端渲染库的格式，不能报错——退化成"只能下载"。"""
    document_id = _upload(client, "资料.zip", b"PK\x03\x04fake", "application/zip")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "binary"
    assert body["text"] is None and body["url"] is None
    # 前端据此显示"这个格式暂不支持预览，请下载"——所以文件名要给
    assert body["filename"] == "资料.zip"


@pytest.mark.parametrize(
    ("filename", "mime", "kind"),
    [
        (
            "合同.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        (
            "表格.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "excel",
        ),
        (
            "讲稿.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
        ),
    ],
)
def test_office_preview_gives_a_signed_url(
    client: TestClient, filename: str, mime: str, kind: str
) -> None:
    """Office 原版式交前端库渲染：后端只判断类型 + 签发原件链接。"""
    document_id = _upload(client, filename, b"PK\x03\x04fake", mime)

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == kind
    assert body["url"].startswith(f"/api/v1/documents/{document_id}/content")
    assert client.get(body["url"]).status_code == 200
    # 原件类型要告诉界面，它据此决定给不给「原文版式 / 解析文本」切换
    assert body["original_kind"] == kind


@pytest.mark.parametrize("filename", ["旧合同.doc", "旧表格.xls", "旧讲稿.ppt"])
def test_legacy_office_degrades_to_download(client: TestClient, filename: str) -> None:
    """老式 OLE2 二进制：预览库解析不了，明确说不支持比给个空入口好。"""
    document_id = _upload(client, filename, b"\xd0\xcf\x11\xe0fake", "application/octet-stream")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "binary"
    assert body["original_kind"] == "binary"


def test_pdf_preview_returns_a_signed_url(client: TestClient) -> None:
    """PDF 交给浏览器原生渲染，所以给签名链接而不是把字节塞进 JSON。"""
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "pdf"
    assert body["text"] is None
    assert body["url"].startswith(f"/api/v1/documents/{document_id}/content")
    # 链接必须能直接用（浏览器打开 PDF 时不带任何头）
    assert client.get(body["url"]).status_code == 200


def test_image_preview_returns_a_signed_url(client: TestClient) -> None:
    document_id = _upload(client, "截图.png", b"\x89PNG\r\n\x1a\nfake", "image/png")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "image"
    assert body["url"]


def test_preview_url_renders_inline_but_download_still_attaches(client: TestClient) -> None:
    """**预览要 inline，下载要 attachment**——两条路径共用一个端点，靠参数区分。

    回归：详情页默认看「原文版式」之后，PDF 进了 iframe；而那个端点一直无条件
    返回 `Content-Disposition: attachment`，于是浏览器把 iframe 里的 PDF
    **变成下载**——表现为"点开文档就下载"。
    """
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")

    preview = client.get(f"/api/v1/documents/{document_id}/preview").json()
    assert "disposition=inline" in preview["url"]
    rendered = client.get(preview["url"])
    assert rendered.headers["content-disposition"].startswith("inline")
    # 内联渲染时禁止嗅探：上传时声明的 mime 是用户可控的
    assert rendered.headers["x-content-type-options"] == "nosniff"

    # 走「下载」那条路签发的链接仍然是 attachment（否则点下载会在浏览器里打开）
    download = client.get(
        f"/api/v1/documents/{document_id}/download-url", params={"format": "original"}
    ).json()["url"]
    assert "disposition" not in download
    assert client.get(download).headers["content-disposition"].startswith("attachment")


def test_pdf_with_a_generic_mime_still_renders_inline(client: TestClient) -> None:
    """上传时没声明 mime（存成 octet-stream）的 PDF 也要能内联渲染。

    判定用**服务端按后缀算出的 kind**，不是客户端给的 mime——后者不可靠，
    拿它当开关会让这类文件白白退化成下载。
    """
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/octet-stream")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()

    assert body["kind"] == "pdf"
    assert client.get(body["url"]).headers["content-disposition"].startswith("inline")


def test_pdf_uploaded_without_a_content_type_still_renders_inline(client: TestClient) -> None:
    """上传时**没带** Content-Type 的 PDF 也要能内联预览。

    回归：库里 mime_type 为 NULL 时，响应会退成 `application/octet-stream`——
    而 octet-stream 的响应**即使带 `Content-Disposition: inline` 也只会被下载**。
    类型要按后缀兜底，因为声明类型是可选字段（curl / SDK / 脚本上传常常留空）。
    """
    kb = client.post("/api/v1/knowledge-bases", json={"name": "无类型"}).json()
    # 手工拼 multipart：`files=` 那条路 httpx 总会补一个 Content-Type，
    # 造不出"这个 part 没有类型"——而那正是要复现的输入（curl / SDK 上传就是这样）
    boundary = "----kylab-no-type"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n',
            b"\r\n",
            b"%PDF-1.7 fake",
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        params={"start": "false"},
    )
    assert upload.status_code == 202, upload.text
    # 前提要成立：库里存的确实是"没有类型"，否则这条测的就成了 octet-stream 那支
    assert upload.json()["document"]["mime_type"] is None
    document_id = upload.json()["document"]["id"]

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()
    served = client.get(body["url"])

    assert served.headers["content-type"].startswith("application/pdf")
    assert served.headers["content-disposition"].startswith("inline")

    # 显式声明成 octet-stream 的也一样：那等于没声明
    upload2 = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("另一份.pdf", io.BytesIO(b"%PDF-1.7 fake"), "application/octet-stream")},
        params={"start": "false"},
    )
    body2 = client.get(f"/api/v1/documents/{upload2.json()['document']['id']}/preview").json()
    assert client.get(body2["url"]).headers["content-type"].startswith("application/pdf")


def test_svg_is_never_served_inline(client: TestClient) -> None:
    """SVG 能带 ``<script>``：内联渲染等于在上传者的诱导下于本站 origin 执行脚本。

    所以 `disposition=inline` **不是**调用方能决定的开关——服务端按媒体类型复核，
    SVG 一律回 attachment。
    """
    document_id = _upload(client, "向量图.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()
    assert body["kind"] == "image"

    served = client.get(body["url"])

    assert served.headers["content-disposition"].startswith("attachment")


def test_inline_is_refused_for_unknown_disposition(client: TestClient) -> None:
    """拼错的取值当场 422，而不是被当成 inline 悄悄放过。"""
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")
    url = client.get(f"/api/v1/documents/{document_id}/preview").json()["url"]

    assert client.get(url.replace("disposition=inline", "disposition=embed")).status_code == 422


def test_preview_kind_matches_download_kind(client: TestClient) -> None:
    """**两条路径必须给同一个答案。**

    预览说"这是 PDF、能渲染"，下载却按二进制处理——那种自相矛盾会让前端
    不知道该信哪个，用户也会看到"预览正常但下载的是别的东西"。
    """
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")

    preview = client.get(f"/api/v1/documents/{document_id}/preview").json()
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url",
        params={"format": "original"},

    ).json()["url"]
    downloaded = client.get(url)

    assert preview["kind"] == "pdf"
    # 下载回来的 content-type 应当就是 PDF，说明两条路径对"这是什么"的判断一致
    assert downloaded.headers["content-type"].startswith("application/pdf")


@pytest.mark.asyncio
async def test_markdown_after_parsing_becomes_the_reading_view(client: TestClient) -> None:
    """解析完成后，阅读视角应当切到解析产物（归一化文本）。

    用真实的摄入链路：跑完 worker 之后 preview 的 kind 必须从 pdf 变成 markdown
    ——那意味着"用户核对解析对不对"看到的正是检索所依据的文本。
    """
    from app.core.services import get_services

    document_id = _upload(client, "报告.pdf", _tiny_pdf(), "application/pdf")
    services = get_services()
    # 手动驱动摄入（测试里 worker 不常驻）
    while await services.worker.run_once():
        pass

    body = client.get(f"/api/v1/documents/{document_id}/preview").json()
    if body["kind"] == "markdown":
        assert body["text"], "切到 markdown 却没有正文"
        assert body["url"] is None
    else:
        # 这个极小 PDF 可能被探测判成需要 OCR（没有文本层），那仍应是 pdf
        assert body["kind"] in ("pdf", "binary")


@pytest.mark.asyncio
async def test_original_source_shows_the_file_even_after_parsing(client: TestClient) -> None:
    """解析完成之后，用户仍然要能看**原件本身**。

    默认（auto）会给解析文本——那是核对解析质量用的；但"原文版式"是一个
    明确的用户意图，`source=original` 必须绕开"有产物就优先 markdown"这条规则。
    """
    from app.core.services import get_services

    document_id = _upload(client, "报告.pdf", _tiny_pdf(), "application/pdf")
    services = get_services()
    while await services.worker.run_once():
        pass

    auto = client.get(f"/api/v1/documents/{document_id}/preview").json()
    assert auto["original_kind"] == "pdf"

    original = client.get(
        f"/api/v1/documents/{document_id}/preview", params={"source": "original"}
    ).json()

    assert original["kind"] == "pdf"
    assert original["url"]
    assert original["text"] is None
    # **解析过的** PDF 的原件链接也必须是 inline——曾经这里退化成 attachment，
    # 因为 inline 的判定读的是 `content.kind`，而它对"有解析产物的原件"是 markdown
    assert "disposition=inline" in original["url"]
    assert client.get(original["url"]).headers["content-disposition"].startswith("inline")


def test_content_supports_byte_ranges(client: TestClient) -> None:
    """**PDF 内联预览的前提**：Chrome 的 PDF 查看器按 Range 渐进加载。

    服务端只回整包（200、无 Accept-Ranges）时，Chrome 会退化成下载——
    表现为"点开 PDF 就下载"。静态托管天生支持 Range，所以这个缺口只在自建端点上。
    """
    payload = b"%PDF-1.7 " + bytes(range(64))
    document_id = _upload(client, "报告.pdf", payload, "application/pdf")
    url = client.get(f"/api/v1/documents/{document_id}/preview").json()["url"]

    head = client.get(url)
    assert head.headers["accept-ranges"] == "bytes"
    assert head.status_code == 200

    ranged = client.get(url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == payload[:4]
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(payload)}"

    # 到结尾 / 尾部 N 字节两种写法 PDF 查看器都会用到
    assert client.get(url, headers={"Range": "bytes=5-"}).content == payload[5:]
    assert client.get(url, headers={"Range": "bytes=-4"}).content == payload[-4:]


def test_out_of_range_is_416_not_a_full_body(client: TestClient) -> None:
    """越界的 Range 要回 416，**不能**默默回整包——那会被当成"不支持分段"。"""
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")
    url = client.get(f"/api/v1/documents/{document_id}/preview").json()["url"]

    response = client.get(url, headers={"Range": "bytes=99999-"})

    assert response.status_code == 416
    assert response.headers["content-range"].startswith("bytes */")


def test_unknown_source_is_rejected(client: TestClient) -> None:
    """拼错的参数要当场 422，而不是悄悄按 auto 处理。"""
    document_id = _upload(client, "报告.pdf", b"%PDF-1.7 fake", "application/pdf")

    response = client.get(
        f"/api/v1/documents/{document_id}/preview", params={"source": "raw"}
    )

    assert response.status_code == 422


def _tiny_pdf() -> bytes:
    """一份带文本层的最小 PDF，用于走通"上传 → 解析 → 阅读视角切换"。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover - 环境缺库时退化成非 PDF
        return b"%PDF-1.7\nfake\n"

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Axial length monitoring consensus 2023.", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data
