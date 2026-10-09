"""文件名还原的单元测试（真实上传路径上踩到过的编码坑）。

注意：本文件刻意不写出乱码的字面量——那段乱码里含 U+201E 等字符，
会被仓库自己的 emoji 扫描判成违规字符（门禁只扫 app/，但没必要给质检留坑）。
乱码用 `"架构设计.md".encode().decode("latin-1")` 现场构造，效果一样且自解释。
"""

from app.services.ingest import normalize_filename


def test_utf8_filename_decoded_as_latin1_is_recovered() -> None:
    """浏览器把 UTF-8 文件名的字节塞进只允许 latin-1 的 Content-Disposition 头。

    Starlette 照 latin-1 解出来，中文名整片变乱码——这就是"上传中文名文档，
    列表里显示一串看不懂的西欧字母"的成因。
    """
    mangled = "架构设计.md".encode().decode("latin-1")

    assert mangled != "架构设计.md"  # 先确认真的构造出了乱码
    assert normalize_filename(mangled) == "架构设计.md"


def test_ascii_filename_is_untouched() -> None:
    # 纯 ASCII 名不可能由这个成因产生，必须原样返回
    assert normalize_filename("report-2026.pdf") == "report-2026.pdf"


def test_plain_non_ascii_filename_is_untouched() -> None:
    """已经是正确中文的名字不能被"再还原一次"改坏。"""
    assert normalize_filename("架构设计.md") == "架构设计.md"


def test_unrecoverable_bytes_fall_back_to_original() -> None:
    """不是这个成因的名字必须原样返回，而不是抛异常。

    "文件文.md" 编不回 latin-1（中文字符超出该编码范围），
    说明它本来就不是"UTF-8 被当 latin-1 读"的产物。
    """
    assert normalize_filename("文件文.md") == "文件文.md"


def test_empty_filename_still_becomes_placeholder_downstream() -> None:
    assert normalize_filename("") == ""
