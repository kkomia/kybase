"""《API 接口规范》与真实 OpenAPI 的一致性（T4.9）。

镜像同构：``scripts/gen_api_spec.py`` + ``docs/规范/API-接口规范-v0.1.md`` → 本文件。

**T4.9 的验收条件就是"与 OpenAPI 自动生成结果一致"**，所以这条测试就是验收本身：
它从真实应用抽一遍 OpenAPI，再与文档里的端点表逐条比对。

**为什么要有它**：手抄一份 69 行的路径表必然会在某次加端点时漂掉——
而那正是这类文档最常见的死法：文档看起来还在、内容已经过期，
读的人照着它对接口，然后发现字段对不上。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SPEC = ROOT / "docs" / "规范" / "API-接口规范-v0.1.md"

#: 文档里端点表的一行：| `GET` | `/api/v1/health` | 说明 |
_ROW = re.compile(r"(?m)^\|\s*`([A-Z]+)`\s*\|\s*`([^`]+)`\s*\|")


def _documented() -> set[tuple[str, str]]:
    """从文档里抽出端点表。

    **用 ``_ROW.findall(text)``，不能写 ``re.findall(_ROW, text, re.M)``**：
    后者对已编译的模式再传 flags 会抛
    ``ValueError: cannot process flags argument with a compiled pattern``。
    多行匹配靠的是 ``(?m)`` 内联标志（见 ``_ROW`` 的定义），不是调用处传参。
    """
    text = SPEC.read_text(encoding="utf-8")
    return {(method, path) for method, path in _ROW.findall(text)}


def _actual() -> set[tuple[str, str]]:
    from app.main import create_app

    spec = create_app().openapi()
    found: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        for method in operations:
            if method in ("get", "post", "put", "patch", "delete"):
                found.add((method.upper(), path))
    return found


def test_spec_file_exists() -> None:
    assert SPEC.is_file(), "《API 接口规范》缺失；跑 `python scripts/gen_api_spec.py` 生成"


def test_documented_endpoints_match_openapi() -> None:
    """**这条就是 T4.9 的验收。**

    两个方向都查：漏记（OpenAPI 有、文档没有）与多记（文档有、OpenAPI 没有）。
    只查一个方向会让"删掉的端点仍在文档里"永远发现不了。
    """
    documented = _documented()
    actual = _actual()

    missing = sorted(actual - documented)
    extra = sorted(documented - actual)

    assert not missing, f"这些端点没写进规范：{missing}"
    assert not extra, f"规范里记着但已不存在的端点：{extra}"


def test_endpoint_count_is_stated() -> None:
    """文档开头写了总数——它也得对，否则改完后总数会留在旧值上。"""
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r"共 \*\*(\d+)\*\* 条端点", text)
    assert match, "文档里没有端点总数"
    assert int(match.group(1)) == len(_actual())


def test_every_endpoint_has_a_summary() -> None:
    """每个端点都要有一句说明。

    端点表是按 tag 与路径排的，没有说明时读者只知道"有这么个地址"，
    不知道它做什么——那等于没写。
    """
    from app.main import create_app

    spec = create_app().openapi()
    bare: list[str] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method in ("get", "post", "put", "patch", "delete") and not (
                operation.get("summary") or ""
            ).strip():
                bare.append(f"{method.upper()} {path}")
    assert not bare, f"这些端点缺 summary：{bare}"


# --------------------------------------------------------------------- 约定部分


@pytest.mark.parametrize(
    "section",
    [
        "错误信封",  # 非 2xx 的统一形状
        "鉴权：两种凭据",  # 登录会话 / 读写密钥 / 只读密钥
        "幂等键",  # 四种结果
        "签名下载 URL",  # 无状态 HMAC + 过期
        "时间格式",  # ISO 8601 带时区
        "分页",  # limit + offset + total
        "技能列表的分类与精选",  # v0.61：分类是算出来的、精选只给界面、都不进提示词
    ]
    )
def test_conventions_are_documented(section: str) -> None:
    """**约定部分是手写的，而它才是这份文档的价值所在。**

    端点清单能生成，但"错误信封长什么样""两种凭据各能做什么""幂等键有几种结果"
    这些机器读不出来——而接 API 的人踩的正是这些坑。
    所以它必须真的写在文档里，而不是只存在于代码里。
    """
    text = SPEC.read_text(encoding="utf-8")
    assert section in text, f"《API 接口规范》缺少约定章节：{section}"


def test_error_codes_are_enumerated() -> None:
    """错误码要列全，且与后端实际用的那套对得上。"""
    from app.core.exceptions import (
        ConflictError,
        ForbiddenError,
        InvalidRequestError,
        NotFoundError,
        PayloadTooLargeError,
        UnauthorizedError,
        UnsupportedContentError,
        UpstreamError,
    )

    text = SPEC.read_text(encoding="utf-8")
    for error in (
        ConflictError,
        ForbiddenError,
        InvalidRequestError,
        NotFoundError,
        PayloadTooLargeError,
        UnauthorizedError,
        UnsupportedContentError,
        UpstreamError,
    ):
        assert error.code in text, f"错误码 {error.code} 没写进规范"


def test_spec_points_at_the_machine_readable_source() -> None:
    """文档要明确指向 `/docs` 与 `/openapi.json`——
    否则读者会以为这份是权威 schema，然后照着它猜字段类型。
    """
    text = SPEC.read_text(encoding="utf-8")

    assert "/api/v1/openapi.json" in text
    assert "/api/v1/docs" in text
    assert "openapi.json" in text
