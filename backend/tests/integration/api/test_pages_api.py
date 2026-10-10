"""按页取原文的端点：文字 + 页图（``services/document_pages.py`` + ``api/v1/pages.py``）。

三条要钉住的性质：

1. **按页切得准**：页范围回来的是那几页的正文，且没有页码信息时**如实说没有**
   （不是 500，也不是一片空白让人以为文档是空的）；
2. **页图那条路只认签名**：``<img src>`` 带不了 Authorization 头，所以它的凭据是
   URL 里的签名——而签名必须绑死"哪个文档、哪一页"（换一页或换一份文档都不行）；
3. **范围闸照旧**：别的成员够不到不属于他的库里的文档，页文字与页图都够不到。

页图是**从原始 PDF 现场栅格化**的，所以这里上传的是真 PDF（pymupdf 现写），
而不是 ``%PDF-1.7 fake`` 那种字符串——假的渲染不出来，测出来的"绿"没有意义。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from app.core.signing import sign_resource
from app.services.document_pages import page_image_key, page_image_resource
from tests.conftest import admin_client as admin_session

SIGNING_SECRET = "pages-test-secret"

#: 带页标记的解析产物（形状与真实产物一致：标记独占一行，第 3 页是空的）
MARKED = """<!-- page:1 -->
# 眼轴共识

第一页正文。

<!-- page:2 -->
## 测量

第二页正文。

<!-- page:3 -->

<!-- page:4 -->
第四页正文。
"""

PLAIN = "# 一份没有页标记的产物\n\n正文只有一段。\n"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def client(monkeypatch):
    """带管理员会话凭据的客户端。

    签名密钥走环境变量：它不属于凭据体系，单独配一次即可（同其它下载类用例）。
    """
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", SIGNING_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    with admin_session() as test_client:
        yield test_client
    get_settings.cache_clear()


# --------------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    """一份两页、每页有文本层的真 PDF。"""
    pymupdf = pytest.importorskip("pymupdf")

    document = pymupdf.open()
    for index in range(2):
        page = document.new_page()
        page.insert_text((72, 100), f"page {index + 1} body text", fontsize=12)
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture(scope="module")
def png_bytes(pdf_bytes: bytes) -> bytes:
    """一张真 PNG（拿上面那份 PDF 的第 1 页渲出来）。"""
    pymupdf = pytest.importorskip("pymupdf")

    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    data = document.load_page(0).get_pixmap(dpi=72).tobytes("png")
    document.close()
    return data


def _upload(
    client: TestClient, name: str, content: bytes, mime: str, *, kb_name: str = "页测试库"
) -> tuple[str, str]:
    """传一份文档（不启动摄入），返回 ``(知识库 id, 文档 id)``。"""
    kb = client.post("/api/v1/knowledge-bases", json={"name": kb_name}).json()
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": (name, io.BytesIO(content), mime)},
        params={"start": "false"},
    )
    assert upload.status_code == 202, upload.text
    return kb["id"], upload.json()["document"]["id"]


def _attach_markdown(document_id: str, markdown: str, *, page_count: int | None) -> None:
    """把一份解析产物挂到文档上（测试与 app 同进程，直达存储造状态）。

    走真实摄入链路也能造出来，但那样造不出"第 3 页是空的""产物里没有标记"这些
    边角输入——而那几条恰恰是本文件要钉的。
    """
    from app.core.services import get_services
    from app.storage.base import ParseResultRecord

    stores = get_services().auth._stores
    path = stores.objects.write(
        f"markdown/{document_id}.md", markdown.encode("utf-8")
    )
    stores.meta.save_parse_result(
        ParseResultRecord(
            document_id=document_id,
            part_id=None,
            parser_name="fake-parser",
            markdown_path=path,
        )
    )
    if page_count is not None:
        stores.meta.update_document_page_count(document_id, page_count)


def _sign(
    document_id: str, page: int, *, ttl_seconds: int = 600, now: int | None = None
) -> tuple[str, int]:
    """按**服务端此刻真正在用的密钥**签一条页图链接，返回 ``(签名, 到期时间戳)``。

    刻意不用上面那个环境变量常量：首次 ``POST /auth/setup`` 会生成一条密钥并落库，
    而 ``signing_secret()`` 优先取库里那条——两处不一致时手写的签名会被判"无效"，
    于是"以为在测 409，拿到的是 401"（实测踩到）。
    """
    from app.core.services import get_services
    from app.services.auth import URL_SIGNING_SECRET_SETTING

    secret = get_services().runtime.get(URL_SIGNING_SECRET_SETTING) or SIGNING_SECRET
    return sign_resource(
        page_image_resource(document_id, page),
        secret,
        ttl_seconds=ttl_seconds,
        now=now,
    )


def _signed_image_url(document_id: str, page: int) -> str:
    signature, expires = _sign(document_id, page)
    return (
        f"/api/v1/documents/{document_id}/pages/{page}/image"
        f"?expires={expires}&signature={signature}"
    )


@pytest.fixture
def marked_document(client: TestClient) -> str:
    """一份四页、带页标记的文档（第 3 页没有正文）。"""
    _, document_id = _upload(client, "指南.pdf", b"%PDF-1.7 fake", "application/pdf")
    _attach_markdown(document_id, MARKED, page_count=4)
    return document_id


# --------------------------------------------------------------------- 鉴权与 404


def test_pages_need_credentials(marked_document: str) -> None:
    """页文字与读文档正文是同一件事，所以它要凭据——刻意用不带会话的客户端。"""
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        response = anonymous.get(f"/api/v1/documents/{marked_document}/pages")

    assert response.status_code == 401


def test_unknown_document_is_404(client: TestClient) -> None:
    """不存在要 404 而不是 403：403 等于告诉越权探测者"这个 id 在本机上存在"。"""
    response = client.get(
        "/api/v1/documents/doc_不存在/pages", params={"from_page": 1, "to_page": 1}
    )

    assert response.status_code == 404


def test_member_cannot_read_pages_of_another_kb(client: TestClient, marked_document: str) -> None:
    """范围闸与其它文档端点同款：别人的库里的文档，页文字够不到。"""
    from app.core.security import hash_password
    from app.core.services import get_services
    from app.models.enums import UserRole
    from app.storage.base import UserRecord

    get_services().auth._stores.meta.create_user(
        UserRecord(
            id="user_member",
            name="成员",
            username="member",
            password_hash=hash_password("member pass 123"),
            role=UserRole.MEMBER,
        )
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "member", "password": "member pass 123"}
    ).json()["token"]

    response = client.get(
        f"/api/v1/documents/{marked_document}/pages",
        params={"from_page": 1, "to_page": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


# --------------------------------------------------------------------- 页文字


def test_text_is_sliced_by_the_requested_range(client: TestClient, marked_document: str) -> None:
    body = client.get(
        f"/api/v1/documents/{marked_document}/pages",
        params={"from_page": 1, "to_page": 2},
    ).json()

    assert body["document_id"] == marked_document
    assert (body["from_page"], body["to_page"]) == (1, 2)
    assert body["total_pages"] == 4
    assert body["page_text_available"] is True
    assert body["truncated"] is False
    assert [item["page"] for item in body["pages"]] == [1, 2]
    assert "第一页正文" in body["pages"][0]["text"]
    assert "第二页正文" not in body["pages"][0]["text"]
    assert "第二页正文" in body["pages"][1]["text"]
    # 内部页标记不能漏给用户（与 /preview 同一条口径）
    assert all("<!--" not in item["text"] for item in body["pages"])
    # 没要图就不签链接：三个 image 字段应当一起是 null
    assert all(
        item["image_url"] is None
        and item["image_expires_at"] is None
        and item["image_kind"] is None
        for item in body["pages"]
    )


def test_an_empty_page_in_the_middle_is_listed(client: TestClient, marked_document: str) -> None:
    """第 3 页在两个标记之间什么都没有：仍然出条目，文字为空串。"""
    body = client.get(
        f"/api/v1/documents/{marked_document}/pages",
        params={"from_page": 3, "to_page": 4},
    ).json()

    assert [item["page"] for item in body["pages"]] == [3, 4]
    assert body["pages"][0]["text"] == ""
    assert "第四页正文" in body["pages"][1]["text"]


def test_a_range_beyond_the_document_is_flagged(client: TestClient, marked_document: str) -> None:
    """超出总页数：**条目照给**（界面按请求的页数铺格子），但要标出这是被截住的。"""
    body = client.get(
        f"/api/v1/documents/{marked_document}/pages",
        params={"from_page": 1, "to_page": 9},
    ).json()

    assert len(body["pages"]) == 9
    assert body["truncated"] is True
    assert [item["text"] for item in body["pages"][4:]] == ["", "", "", "", ""]


def test_a_document_without_page_markers_says_so(client: TestClient) -> None:
    """没有页码信息**不是错误**：如实回 ``page_text_available=false``，
    界面才好说"这份文档没有页码信息"，而不是显示成文档是空的。
    """
    _, document_id = _upload(client, "说明.md", b"# x", "text/markdown")
    _attach_markdown(document_id, PLAIN, page_count=None)

    response = client.get(
        f"/api/v1/documents/{document_id}/pages", params={"from_page": 1, "to_page": 2}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["page_text_available"] is False
    assert body["total_pages"] is None
    assert body["truncated"] is False
    # 条目仍然在（每页一条），只是文字为空
    assert [item["page"] for item in body["pages"]] == [1, 2]
    assert [item["text"] for item in body["pages"]] == ["", ""]


def test_a_document_that_is_not_parsed_yet_says_so(client: TestClient) -> None:
    """还没解析完（没有产物）与"产物里没有标记"对调用方是同一件事：现在没有页码信息。"""
    _, document_id = _upload(client, "还没解析.pdf", b"%PDF-1.7 fake", "application/pdf")

    body = client.get(
        f"/api/v1/documents/{document_id}/pages", params={"from_page": 1, "to_page": 1}
    ).json()

    assert body["page_text_available"] is False
    assert body["pages"][0]["text"] == ""


# --------------------------------------------------------------------- 范围校验


def test_a_range_starting_below_one_is_rejected(client: TestClient, marked_document: str) -> None:
    response = client.get(
        f"/api/v1/documents/{marked_document}/pages", params={"from_page": 0, "to_page": 2}
    )

    assert response.status_code == 422
    # 中文文案：调用方要照着它把页号改对
    assert "从 1 开始" in response.json()["message"]


def test_a_reversed_range_is_rejected(client: TestClient, marked_document: str) -> None:
    response = client.get(
        f"/api/v1/documents/{marked_document}/pages", params={"from_page": 5, "to_page": 3}
    )

    assert response.status_code == 422
    assert "不能小于起始页" in response.json()["message"]


def test_too_many_pages_at_once_is_rejected(client: TestClient, marked_document: str) -> None:
    """一次 51 页要被挡住：页图是现场渲染的，一次点击不该把解析机按住。"""
    response = client.get(
        f"/api/v1/documents/{marked_document}/pages", params={"from_page": 1, "to_page": 51}
    )

    assert response.status_code == 422
    assert "50" in response.json()["message"]


# --------------------------------------------------------------------- 页图


def test_pdf_pages_get_signed_image_urls(client: TestClient, pdf_bytes: bytes) -> None:
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")

    body = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 2, "images": "true"},
    ).json()

    for item in body["pages"]:
        assert item["image_kind"] == "pdf"
        assert item["image_url"].startswith(
            f"/api/v1/documents/{document_id}/pages/{item['page']}/image"
        )
        assert "signature=" in item["image_url"] and "expires=" in item["image_url"]
        assert 0 < item["image_expires_at"] - int(time.time()) <= 900


def test_page_image_is_a_png(client: TestClient, pdf_bytes: bytes) -> None:
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    url = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 1, "images": "true"},
    ).json()["pages"][0]["image_url"]

    # **不带 Authorization 头**：`<img src>` 就是这样发请求的，这个端点也不该要它
    response = client.get(url, headers={"Authorization": ""})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(_PNG_MAGIC)
    assert response.headers["x-content-type-options"] == "nosniff"


def test_each_page_renders_its_own_image(client: TestClient, pdf_bytes: bytes) -> None:
    """两页的内容不同，图就必须不同——同一条签名只能换到它签的那一页。"""
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    pages = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 2, "images": "true"},
    ).json()["pages"]

    first = client.get(pages[0]["image_url"]).content
    second = client.get(pages[1]["image_url"]).content

    assert first.startswith(_PNG_MAGIC) and second.startswith(_PNG_MAGIC)
    assert first != second


def test_the_rendered_page_is_cached(client: TestClient, pdf_bytes: bytes) -> None:
    """渲染结果进对象存储：翻页是高频动作，同一页不该被画第二遍。"""
    from app.core.services import get_services

    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    url = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 1, "images": "true"},
    ).json()["pages"][0]["image_url"]

    stores = get_services().auth._stores
    key = page_image_key(document_id, 1)
    assert stores.objects.exists(key) is False

    first = client.get(url)
    assert first.status_code == 200
    assert stores.objects.exists(key) is True
    assert stores.objects.read(key) == first.content

    # 第二次走缓存，字节一致（内容一样才说明缓存没被写坏）
    assert client.get(url).content == first.content


def test_an_image_document_serves_the_original_for_page_one(
    client: TestClient, png_bytes: bytes
) -> None:
    """原件本身就是一张图：第 1 页就是它自己（不必栅格化，也不该重编码）。"""
    # 刻意声明成一个**不该信**的类型：服务端按后缀认种类，不看上传方那句话
    _, document_id = _upload(client, "扫描件.png", png_bytes, "text/html")
    body = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 1, "images": "true"},
    ).json()

    assert body["pages"][0]["image_kind"] == "image"
    response = client.get(body["pages"][0]["image_url"])

    assert response.status_code == 200
    assert response.content == png_bytes
    # 声明成 text/html 也要按后缀回 image/png：内联渲染时那是安全边界
    assert response.headers["content-type"].startswith("image/png")


def test_a_document_without_real_pages_signs_nothing(client: TestClient) -> None:
    """纯文本 / Office 没有"页"可切：**不签链接**（否则界面会多一片必然失败的图）。"""
    _, document_id = _upload(client, "说明.md", b"# x", "text/markdown")

    body = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 2, "images": "true"},
    ).json()

    assert all(item["image_url"] is None and item["image_kind"] is None for item in body["pages"])


def test_a_second_page_of_a_single_page_image_is_404(client: TestClient, png_bytes: bytes) -> None:
    """一张图只有第 1 页：要第 2 页是 404（这份文件没有那一页），不是 500。"""
    _, document_id = _upload(client, "扫描件.png", png_bytes, "image/png")

    response = client.get(_signed_image_url(document_id, 2))

    assert response.status_code == 404


def test_a_page_beyond_the_pdf_is_404(client: TestClient, pdf_bytes: bytes) -> None:
    """PDF 只有两页，要第 3 页给 404，并且文案里带上真实页数。"""
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")

    response = client.get(_signed_image_url(document_id, 3))

    assert response.status_code == 404
    assert "2 页" in response.json()["message"]


def test_a_format_without_page_images_is_rejected(client: TestClient) -> None:
    """清单之外的种类：签名是对的也取不到图——409（文件在，只是这份格式没有页图）。"""
    _, document_id = _upload(client, "说明.md", b"# x", "text/markdown")

    response = client.get(_signed_image_url(document_id, 1))

    assert response.status_code == 409
    assert "不能按页出图" in response.json()["message"]


def test_images_beyond_the_last_page_are_not_signed(
    client: TestClient, pdf_bytes: bytes
) -> None:
    """要的页超出总页数时**不签链接**：签出来必然 404，界面上就是一个加载失败的图。

    这条与 ``truncated`` 配着看：界面拿到 `truncated=true` 与后面那些 null，
    就知道"后面本来就没了"，而不是"后端图挂了"。
    """
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    from app.core.services import get_services

    get_services().auth._stores.meta.update_document_page_count(document_id, 2)

    body = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 4, "images": "true"},
    ).json()

    assert body["total_pages"] == 2
    assert body["truncated"] is True
    assert body["pages"][0]["image_url"] is not None
    assert body["pages"][1]["image_url"] is not None
    assert body["pages"][2]["image_url"] is None
    assert body["pages"][3]["image_url"] is None
    assert body["pages"][2]["image_kind"] is None


def test_the_image_endpoint_requires_a_signature(client: TestClient, pdf_bytes: bytes) -> None:
    """没有签名参数直接 422（缺必填查询参数），而不是把图发出去。"""
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")

    assert client.get(f"/api/v1/documents/{document_id}/pages/1/image").status_code == 422


def test_a_signature_cannot_be_switched_to_another_page(
    client: TestClient, pdf_bytes: bytes
) -> None:
    """**本文件最重要的一条之一**：签名绑死页号。

    不绑的话，一条"第 1 页"的链接改个页号就能换到任意一页——而"哪一页"正是用户
    要控制的东西。
    """
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    url = client.get(
        f"/api/v1/documents/{document_id}/pages",
        params={"from_page": 1, "to_page": 1, "images": "true"},
    ).json()["pages"][0]["image_url"]

    swapped = url.replace("/pages/1/image", "/pages/2/image")

    assert client.get(swapped).status_code == 401


def test_a_signature_cannot_be_switched_to_another_document(
    client: TestClient, pdf_bytes: bytes
) -> None:
    """签名同样绑死文档：拿 A 的签名去够 B 的第 1 页必须失败。"""
    _, first = _upload(client, "报告.pdf", pdf_bytes, "application/pdf", kb_name="甲的库")
    _, second = _upload(client, "另一份.pdf", pdf_bytes, "application/pdf", kb_name="乙的库")
    url = client.get(
        f"/api/v1/documents/{first}/pages",
        params={"from_page": 1, "to_page": 1, "images": "true"},
    ).json()["pages"][0]["image_url"]

    assert client.get(url.replace(first, second)).status_code == 401


def test_an_expired_image_link_is_rejected(client: TestClient, pdf_bytes: bytes) -> None:
    """过期即失效，而不是"还能用"（同下载那条路）。"""
    _, document_id = _upload(client, "报告.pdf", pdf_bytes, "application/pdf")
    past = int(time.time()) - 10
    signature, _ = _sign(document_id, 1, ttl_seconds=0, now=past)

    response = client.get(
        f"/api/v1/documents/{document_id}/pages/1/image",
        params={"expires": past, "signature": signature},
    )

    assert response.status_code == 401
