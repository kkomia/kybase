"""档案文件层（期一）。

镜像同构：``app/services/archive_files.py`` → 本文件。

这一层的错法都很安静：解析漏一条、渲染多一个空行、写文件翻成 CRLF——每一样都要到
"用户拿别的编辑器打开、发现内容变了"才暴露。所以这里钉三件事：

1. **解析与渲染往返**：四个固定分区、项目组、未知分区都要原样回来；
2. **写字节纪律**（§3.5 第 3 条）：文件里不能出现 ``\\r``，往返不增殖；
3. **变更流逐字节**：旧值里的任意标点（含分隔符本身）都要能还原。
"""

from __future__ import annotations

from pathlib import Path

from app.services import archive_files as af


def test_parse_and_render_round_trip() -> None:
    text = """---
updated: 2026-10-03
---

# 用户档案

## 身份与称呼

- 用户叫小又，称呼「小又」即可。

## 长期偏好与风格

- 用户要求回答先给结论，再列依据。

## 进行中的项目

### 内网知识库

- 用户的目标是把知识库与 Agent 都放在内网，不出公网。
- 用户已决定用 PostgreSQL + pgvector。

## 工具与环境

- 用户的内网有一台 Windows Server 2019 配一张 L20。
"""
    archive = af.parse_archive(text)
    assert archive.updated == "2026-10-03"
    assert [entry.section for entry in archive.entries] == [
        af.SECTION_IDENTITY,
        af.SECTION_PREFERENCES,
        af.SECTION_PROJECTS,
        af.SECTION_PROJECTS,
        af.SECTION_TOOLS,
    ]
    assert archive.entries[2].group == "内网知识库"
    assert archive.unknown_sections == ()
    assert af.parse_archive(af.render_archive(archive)) == archive


def test_unknown_section_is_parsed_and_marked() -> None:
    text = """---
updated: 2026-10-03
---

# 用户档案

## 身份与称呼

- 用户叫小又。

## 兴趣

- 用户喜欢钓鱼。
"""
    archive = af.parse_archive(text)
    assert archive.unknown_sections == ("兴趣",)
    assert [entry.text for entry in archive.entries] == ["用户叫小又。", "用户喜欢钓鱼。"]
    # 未知分区在渲染时排在四个已知分区之后，内容一字不动（§3.5 第 4 条）
    rendered = af.render_archive(archive)
    assert "## 兴趣" in rendered
    assert "- 用户喜欢钓鱼。" in rendered
    assert rendered.index(af.SECTION_TOOLS) < rendered.index("## 兴趣")


def test_write_bytes_never_introduces_crlf(tmp_path: Path) -> None:
    path = tmp_path / "PROFILE.md"
    af.write_bytes(path, "第一行\r\n第二行\r\n")
    assert b"\r" not in path.read_bytes()
    # 往返两次不再增殖（旧实现踩过的实测坑：每存一次长一个 \r）
    once = path.read_bytes()
    af.write_bytes(path, af.read_text(path))
    af.write_bytes(path, af.read_text(path))
    assert path.read_bytes() == once


def test_changes_round_trip_keeps_punctuation_byte_for_byte(tmp_path: Path) -> None:
    # 旧值里带分隔符本身、全角冒号、书名号：拼成一行就还原不回去了（§3.4）
    old = "用户要求 A · B：不要「这个」。"
    new = "用户要求先给结论。"
    af.append_changes(
        tmp_path,
        [
            af.ChangeRecord(
                at="2026-10-03 14:22",
                action=af.ACTION_REPLACED,
                section=af.SECTION_PREFERENCES,
                source="显式",
                old=old,
                new=new,
            )
        ],
        keep=300,
    )
    records = af.read_changes(tmp_path)
    assert len(records) == 1
    assert records[0].old == old
    assert records[0].new == new
    assert records[0].action == af.ACTION_REPLACED
    assert records[0].section == af.SECTION_PREFERENCES


def test_append_changes_keeps_only_recent(tmp_path: Path) -> None:
    for index in range(5):
        af.append_changes(
            tmp_path,
            [
                af.ChangeRecord(
                    at="2026-10-03 14:22",
                    action=af.ACTION_ADDED,
                    section=af.SECTION_PREFERENCES,
                    new=f"第 {index} 条。",
                )
            ],
            keep=3,
        )
    records = af.read_changes(tmp_path)
    assert [record.new for record in records] == ["第 2 条。", "第 3 条。", "第 4 条。"]


def test_bullet_lines_carry_heading_context() -> None:
    text = """---
summary: m
---

## 核心长期记忆

- 用户的机器是 Windows Server 2019。

## 工具设置

- 用户的设备名是 nas。
"""
    assert af.bullet_lines(text) == [
        ("核心长期记忆", "用户的机器是 Windows Server 2019。"),
        ("工具设置", "用户的设备名是 nas。"),
    ]
