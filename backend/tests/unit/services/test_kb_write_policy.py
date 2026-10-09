"""用户点名的两条（2026-09-29 走查）：**别往知识库塞东西** / **压缩包先解压再读**。

两条都在执行链路上，所以都钉**机制或顺序**，不只钉文案：

1. `ingest_file` 是有副作用的写操作（往**用户的长期资产**里写）→ 必须走审批闸
   （`tool_meta.needs_approval`），模型自己无权直接做；
2. 读不了二进制时的提示**顺序**：① 压缩包用 `run_command` 解开再读 → ② 看元信息
   → ③ **只有用户明确要求时**才入库。顺序反了，用户看到的就是"缘木求鱼"。
"""

from __future__ import annotations

from app.services.agent_tools import _binary_file_outcome, tool_specs
from app.services.tool_meta import meta_of


def _description(name: str) -> str:
    """工具描述（`tool_specs()` 给的是 `ToolSpec`，也容忍 dict 形状）。"""
    for spec in tool_specs():
        spec_name = getattr(spec, "name", None) or (
            spec.get("name") if isinstance(spec, dict) else None
        )
        if spec_name != name:
            continue
        text = getattr(spec, "description", None)
        if text is None and isinstance(spec, dict):
            text = spec.get("description")
        return str(text or "")
    raise AssertionError(f"没有这个工具：{name}")


def test_ingest_file_goes_through_the_approval_gate() -> None:
    """**机制**：往知识库写要问一次（用户原话："不得往知识库里面塞东西"）。"""
    meta = meta_of("ingest_file")

    assert meta.needs_approval is True
    # 它与"写文件"那些是同一档：有副作用、动的是用户的数据
    assert meta.side_effect_scope == "workspace"


def test_binary_hint_tells_you_to_unzip_first() -> None:
    """**顺序**：压缩包 → 先解压再读；入库排最后，而且限定"用户明确要求时"。"""
    content = _binary_file_outcome("神经内科临床指南.zip", "file_1").content

    at_zip = content.index("unzip")
    at_meta = content.index("file` / `ls -l")
    at_ingest = content.index("ingest_file")
    assert at_zip < at_meta < at_ingest, "顺序必须是 解压 → 元信息 → 入库"
    assert "用户明确要求" in content
    assert "不要替用户决定" in content


def test_read_file_description_keeps_the_same_order() -> None:
    """工具描述也照这个顺序（模型先看的是它，不只是失败后的那句话）。"""
    text = _description("read_file")

    assert text.index("unzip") < text.index("ingest_file")
    assert "只有在用户明确要求" in text


def test_read_conversation_file_description_keeps_the_same_order() -> None:
    text = _description("read_conversation_file")

    assert text.index("unzip") < text.index("ingest_file")
    assert "只有在用户明确要求" in text
