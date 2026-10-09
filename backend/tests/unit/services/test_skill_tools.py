"""技能正文的工具名对齐（`app/services/skill_tools.py`）。

**为什么值得单独一个文件**：导进来的那批第三方技能全按 Claude Code 的工具名写
（`Skill` 1226 次 / `Read` 763 / `Bash` 691……），而我们叫 `read_skill` / `read_file` /
`run_command`。不改写的话，模型会去调不存在的工具——技能"装上了但调不动"。
"""

from __future__ import annotations

from app.services.skill_tools import (
    LEGACY_NAMES,
    TOOL_ALIASES,
    TOOL_SUBSTITUTES,
    catalog_note,
    translate_tool_names,
)
from app.services.skills import SkillService


def test_aliases_cover_the_tools_written_in_the_skills() -> None:
    """那张对照表就是契约：七个有对应物的、五个没有对应物的。

    `Write` / `Edit` **不给对应工具**（本环境刻意没有写文件工具），
    给的是替代做法——这一条是立场，写在表里而不是散在正文里。
    """
    assert TOOL_ALIASES == {
        "Skill": "read_skill / list_skills",
        "Read": "read_file",
        "Bash": "run_command",
        "Grep": "search_files",
        "Task": "spawn_subagent",
        "WebFetch": "web_fetch",
        "WebSearch": "web_search",
    }
    assert "Write" in TOOL_SUBSTITUTES and "Edit" in TOOL_SUBSTITUTES
    assert "export_*" in TOOL_SUBSTITUTES["Write"]
    assert "run_command" in TOOL_SUBSTITUTES["Edit"]


def test_backticked_names_are_rewritten() -> None:
    """技能里最常见的写法是 ``\\`Read\\``` 这种反引号。"""
    text = "1. Use `Read` to open the file.\n2. Then run `Bash` to build it."

    out = translate_tool_names(text)

    assert "`read_file`" in out and "`run_command`" in out
    assert "`Read`" not in out and "`Bash`" not in out


def test_call_form_and_tool_word_are_rewritten() -> None:
    assert translate_tool_names("Bash(npm test)") == "run_command(npm test)"
    assert translate_tool_names("the Read tool") == "the read_file 工具"
    assert translate_tool_names("use the Grep tool") == "use the search_files 工具"


def test_write_and_edit_become_substitutes_not_a_tool() -> None:
    """`Write` / `Edit` 换成**替代做法**：产出用导出、改动用命令行。"""
    out = translate_tool_names("Use `Write` to save it, `Edit` to patch it.")

    assert "export_*" in out
    assert "run_command" in out
    # 不许把它们换成"某个我们其实没有的工具名"
    assert "write_file" not in out and "edit_file" not in out


def test_plain_english_is_left_alone() -> None:
    """**不做裸词替换**：正文里 `Read the file` 这种普通英文不能被改坏。

    这不是洁癖——实测过 258 份导入技能的 657 处旧名里，**625 处是普通英文**
    （`Task Pattern`、`Read the paper/chapter`、`Edit/Write yourself`…），
    只有 32 处是明确的工具形状（反引号 / 调用形 / `…tool`）。
    所以这里只换那三种形状，其余交给目录里那张对照表去说明。
    """
    text = "Read the file carefully. Task the team with it. Bash scripts are fine."

    assert translate_tool_names(text) == text


def test_table_and_list_prose_is_left_alone() -> None:
    """表格/列表里的普通英文照样不动（实测里 625 处误伤都是这种）。"""
    text = "| Task Pattern | Read the paper | Edit/Write yourself |"

    assert translate_tool_names(text) == text


def test_code_fences_are_rewritten_too() -> None:
    """技能里的示例常常写在代码块里（调用形状），那也要换。"""
    text = "```\nRead({\"file_path\": \"a.md\"})\n```"

    assert translate_tool_names(text) == "```\nread_file({\"file_path\": \"a.md\"})\n```"


def test_catalog_note_lists_every_legacy_name() -> None:
    """注入的那段说明**必须点名每一个旧名字**：漏掉的那个，模型就会去调它。"""
    note = catalog_note()

    for name in LEGACY_NAMES:
        assert f"`{name}`" in note
    assert "没有 `Write` / `Edit`" in note
    assert "不要去调不存在的工具" in note


def _install(tmp_path, name: str, body: str, description: str = "把文字改得像人写的"):
    """往市场那条路装技能的位置（``<data>/skills/<名字>/SKILL.md``）写一份。"""
    directory = tmp_path / "data" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    return SkillService(tmp_path / "data", builtin_dir=tmp_path / "builtin")


def test_catalog_injects_the_note_once(tmp_path) -> None:
    """目录里要有那张表（每个请求都注入），且**只出现一次**。"""
    service = _install(tmp_path, "better-writing", "正文")

    catalog = service.catalog()

    assert catalog.count("【工具名对照】") == 1
    assert "`Read`→`read_file`" in catalog
    assert "- better-writing:" in catalog


def test_read_gives_the_model_translated_names_but_not_the_human(tmp_path) -> None:
    """同一份技能：**模型读到的是换过的**，人（详情页）读到的是上游原文。"""
    service = _install(
        tmp_path, "pptx", "Use `Read` to load the outline, then `Bash` to export."
    )

    _record, for_model = service.read("pptx")
    assert "`read_file`" in for_model and "`run_command`" in for_model

    _record, for_human = service.read("pptx", translate_names=False)
    assert "`Read`" in for_human and "`Bash`" in for_human
