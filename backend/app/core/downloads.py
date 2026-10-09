"""文件下载的两个 HTTP 契约：媒体类型判定 + ``Content-Disposition``。

**为什么住在共享底座**：这两件被两侧同时用来"把一份内容作为文件回给客户端"——
KB 侧下文档（`api/v1/documents.py`）、Agent 侧导出会话与附件
（`api/v1/conversations.py`）。它们不含任何业务判断：

- 媒体类型是"上传时声明得具体就用它，笼统或缺失就按后缀猜"；
- ``Content-Disposition`` 是按 RFC 6266 拼头（中文名走 ``filename*=UTF-8''`` 那一支）。

两边各写一遍的后果很实在：`Content-Type` 错了会让浏览器把 PDF 变成下载
（见下面 ``_FALLBACK_MEDIA_TYPES`` 的说明），而中文文件名那一段更细——只改一边
就是要靠人记得。

**2026-10-08 剥离阶段 0 从 `services/documents.py` 与 `services/ingest.py` 上移到这里**：
两处原位置仍 import 进来（`__all__` 里照旧导出），调用点一行没改；Agent 侧的
`api/v1/conversations.py` 改为直接从这里取，于是它不再 import KB 域模块。
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["content_disposition", "media_type_of", "suffix_of"]


def suffix_of(name: str) -> str:
    """文件名的后缀（小写，含点）。没有后缀时返回空串。

    ``rfind(".") > 0`` 是刻意的：``.gitignore`` 这类"点开头"的名字整串就是名字，
    不该被当成后缀 ``.gitignore``。
    """
    lowered = name.lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else ""


#: 后缀 → 媒体类型，**只在上传时声明的类型缺失或过于笼统时兜底**。
#:
#: 为什么必须兜底：`Content-Type` 错了的后果很实在——一个
#: ``application/octet-stream`` 的响应，**即使带 ``Content-Disposition: inline``，
#: 浏览器也只会下载、不会渲染**。而上传时声明的类型是可选字段，浏览器之外的上传器
#: （curl、SDK、脚本）常常留空，于是库里的 PDF 一预览就变下载（实测踩到）。
#: 判后缀是服务端自己算的，比客户端声明的类型可靠。
_FALLBACK_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

#: 「太笼统、等于没说」的媒体类型：留着它们还不如按后缀猜。
_VAGUE_MEDIA_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def media_type_of(filename: str, stored: str | None) -> str:
    """一份内容该用哪个媒体类型：声明得具体就用它，笼统/缺失就按后缀兜底。"""
    if stored and stored.strip().lower() not in _VAGUE_MEDIA_TYPES:
        return stored
    return _FALLBACK_MEDIA_TYPES.get(suffix_of(filename), stored or "application/octet-stream")


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """按 RFC 6266 拼 ``Content-Disposition``。

    中文文件名必须走 ``filename*=UTF-8''`` 那一支：HTTP 头是 latin-1，
    直接把中文塞进 ``filename="..."`` 会被上游编码器拒掉（或变成乱码落盘）。

    同时给两个参数是刻意的，不是冗余：
    - ``filename=`` 是 ASCII 回退，给不认识 ``filename*`` 的老客户端；
    - ``filename*=`` 是标准写法，现代浏览器优先用它。
    只给后者，老客户端会拿到一个没名字的文件；只给前者，中文名就保不住。

    ``%`` 与换行要转义/剔除：换行进头部就是响应拆分（response splitting），
    而文件名是用户可控的输入。
    """
    safe = filename.replace("\r", "").replace("\n", "").replace('"', "")
    ascii_fallback = safe.encode("ascii", "replace").decode("ascii") or "download"
    quoted = quote(safe, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"
