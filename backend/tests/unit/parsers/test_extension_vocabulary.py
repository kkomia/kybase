"""扩展名口径只允许有一处：`probe.py` 的分类集合。

**为什么单列一条**：曾经 probe 认 `.tiff`（算图片）、云端解析器却在各自的
`supports()` 里内联了一份不含 tiff 的字面量——同一件事两处定义，必然漂。
现在解析器的支持范围**从 probe 的集合派生**，这个文件把"哪些是有意不收的"
钉住：以后有人往 probe 的 IMAGE/OFFICE 里加后缀（比如 `.heic`），这里会红，
逼他明确"这个格式谁解析"。
"""

from __future__ import annotations

from app.parsers import mineru_cloud, paddleocr_api, probe


def test_parser_support_is_derived_from_the_probe_vocabulary() -> None:
    """解析器的支持集合必须是 probe 分类集合的子集（不可能支持一种"不是该类型"的后缀）。"""
    assert mineru_cloud._SUPPORTED_IMAGES <= probe.IMAGE_EXTENSIONS
    assert mineru_cloud._SUPPORTED_OFFICE <= probe.OFFICE_EXTENSIONS
    assert paddleocr_api._SUPPORTED_IMAGES <= probe.IMAGE_EXTENSIONS


def test_unclaimed_image_suffixes_are_the_known_intentional_gaps() -> None:
    """probe 认成图片、但两个云端解析器都不收的后缀，只能是这两个。

    它们确实会落到"暂不支持的文件类型"（明确报错，不是静默失败）。
    如果有人放宽了支持范围，这里会红——那时请一并更新这条断言与文档。
    """
    claimed = mineru_cloud._SUPPORTED_IMAGES | paddleocr_api._SUPPORTED_IMAGES
    assert probe.IMAGE_EXTENSIONS - claimed == {".tif", ".tiff"}


def test_unclaimed_office_suffixes_are_the_known_intentional_gaps() -> None:
    assert {
        ".odt",
        ".ods",
        ".odp",
    } == probe.OFFICE_EXTENSIONS - mineru_cloud._SUPPORTED_OFFICE
