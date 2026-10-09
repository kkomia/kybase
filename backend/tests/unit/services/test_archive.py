"""档案服务层（期一，验收判据见 ``docs/设计/记忆档案-设计-v0.1.md`` §9.2）。

镜像同构：``app/services/archive.py`` → 本文件。

钉的是"判错会静默丢事实或让用户失去控制"的那几条：预算闸、顶替进变更流、
还原往返、忘掉留痕、敏感信息否决、未知分区照收。用例名直接沿用 §9.2 的 kebab 名，
方便验收时逐条对上。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.exceptions import InvalidRequestError
from app.services import archive_files as af
from app.services.archive import (
    SECTION_ENTRY_LIMITS,
    SECTION_IDENTITY,
    SECTION_PREFERENCES,
    SECTION_PROJECTS,
    SECTION_TOOLS,
    ArchiveService,
    classify_action,
    is_sensitive,
)

PROFILE = af.ARCHIVE_FILENAME

#: 一条能触发**包含关系顶替**的对子：新值多两个字、数字集合相同、旧值是它的前缀
#: （§4.3 第二行）。两个都不是对方的整行，故"档案里查不到旧值"可逐行断言。
OLD = "用户要求回答先给结论、再列依据"
NEW = "用户要求回答先给结论、再列依据，简短"


def _service(tmp_path: Path) -> ArchiveService:
    return ArchiveService(tmp_path, now=lambda: datetime(2026, 10, 3, 14, 22))


def _lines(tmp_path: Path) -> list[str]:
    return (tmp_path / PROFILE).read_bytes().decode("utf-8").splitlines()


#: 判据 §9.2 第 3 条
def test_over_budget_append_is_rejected_not_truncated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    limit = SECTION_ENTRY_LIMITS[SECTION_IDENTITY]
    for index in range(limit):
        assert service.add(f"用户是第 {index} 号测试身份。", SECTION_IDENTITY).action == "added"
    before = (tmp_path / PROFILE).read_bytes()

    result = service.add("用户是第 99 号测试身份。", SECTION_IDENTITY)

    assert result.action == "rejected"
    assert result.reason == "section"
    # 回执必须给两条出路（§4.4），且读数是"现在就到顶了"（10/10）
    assert "满了" in result.receipt
    assert f"{limit}/{limit}" in result.receipt
    assert "删一条" in result.receipt and "顶替" in result.receipt
    # **档案逐字节不变**
    assert (tmp_path / PROFILE).read_bytes() == before
    # 被拒的写入在变更流里留一条"撤回"（§3.4）
    assert any(record.action == af.ACTION_REVERTED for record in service.changes())


#: 判据 §9.2 第 4 条
def test_replace_moves_old_value_to_changelog(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.add(OLD, SECTION_PREFERENCES).action == "added"

    result = service.add(NEW, SECTION_PREFERENCES)

    assert result.action == "replaced"
    assert result.replaced == OLD
    lines = _lines(tmp_path)
    assert f"- {NEW}" in lines
    assert f"- {OLD}" not in lines
    records = [record for record in service.changes() if record.action == af.ACTION_REPLACED]
    assert records and records[-1].old == OLD and records[-1].new == NEW


#: 判据 §9.2 第 5 条
def test_restore_from_changelog_round_trips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add(OLD, SECTION_PREFERENCES)
    before = (tmp_path / PROFILE).read_bytes()
    service.add(NEW, SECTION_PREFERENCES)
    assert (tmp_path / PROFILE).read_bytes() != before

    result = service.restore(OLD)

    assert result.action == "restored"
    assert (tmp_path / PROFILE).read_bytes() == before
    records = [record for record in service.changes() if record.action == af.ACTION_RESTORED]
    assert records and records[-1].new == OLD and records[-1].old == NEW


#: 判据 §9.2 第 6 条
def test_forget_removes_entry_and_keeps_history(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add(OLD, SECTION_PREFERENCES)

    result = service.forget(OLD)

    assert result.action == "forgotten"
    assert f"- {OLD}" not in _lines(tmp_path)
    records = [record for record in service.changes() if record.action == af.ACTION_FORGOTTEN]
    assert records and records[-1].old == OLD
    # 留痕可还原
    assert service.restore(OLD).action == "restored"
    assert f"- {OLD}" in _lines(tmp_path)


#: 判据 §9.2 第 11 条（写入侧）
def test_sensitive_entries_never_reach_archive(tmp_path: Path) -> None:
    service = _service(tmp_path)
    secret = "用户的数据库密码是 hunter2，令牌是 sk-abcdefghijklmnop"

    result = service.add(secret, SECTION_PREFERENCES)

    assert result.action == "rejected"
    assert result.reason == "sensitive"
    assert secret not in af.read_text(tmp_path / PROFILE)
    # **变更流里也不能有**：留痕本身就是把凭据换个地方写下来
    assert secret not in af.read_text(tmp_path / af.CHANGES_FILENAME)
    assert is_sensitive(secret)


#: 判据 §9.2 第 13 条（**解析侧**：注入本体是期二，这里钉"照收、计入预算、如实标记"）
def test_unknown_section_is_injected_and_reported(tmp_path: Path) -> None:
    (tmp_path / PROFILE).write_bytes(
        (
            "---\nupdated: 2026-10-03\n---\n\n# 用户档案\n\n"
            "## 身份与称呼\n\n- 用户叫小又。\n\n"
            "## 兴趣\n\n- 用户喜欢钓鱼。\n"
        ).encode()
    )
    service = _service(tmp_path)

    archive = service.read()
    assert archive.unknown_sections == ("兴趣",)
    assert [entry.text for entry in archive.entries] == ["用户叫小又。", "用户喜欢钓鱼。"]
    # 计入预算：未知分区也出现在读数里
    report = service.budget(archive)
    assert report.entries == 2
    interests = [section for section in report.sections if section.name == "兴趣"]
    assert interests and not interests[0].known and interests[0].entries == 1
    # 渲染（注入块将来照它拼）里也在
    assert "- 用户喜欢钓鱼。" in af.render_archive(archive)


# ------------------------------------------------------------ 判据细节（机械表）


def test_completely_identical_entry_is_not_written() -> None:
    existing = [af.ArchiveEntry(text="用户要求回答先给结论。", section=SECTION_PREFERENCES)]
    # 只差标点空白 → 已存在
    assert classify_action("用户要求 回答先给结论", existing).action == "existing"


def test_digits_differ_never_replaces() -> None:
    existing = [af.ArchiveEntry(text="用户的服务器地址是 192.168.1.10", section=SECTION_TOOLS)]
    # 数字不同就是另一件事（§4.3 那条否决）——即使字面高度相似也只新增
    match = classify_action("用户的服务器地址是 192.168.1.11", existing)
    assert match.action == "add"


def test_containment_replacement_keeps_more_complete_one() -> None:
    existing = [af.ArchiveEntry(text=OLD, section=SECTION_PREFERENCES)]
    match = classify_action(NEW, existing)
    assert match.action == "replace"
    assert match.kept == NEW
    # 反过来：现有那条更完整时不动它（回执"已经有了"）
    more_complete = [af.ArchiveEntry(text=NEW, section=SECTION_PREFERENCES)]
    assert classify_action(OLD, more_complete).action == "existing"


def test_cross_section_duplicate_is_replaced_into_new_section(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add(OLD, SECTION_PROJECTS, group="内网知识库")
    result = service.add(NEW, SECTION_PREFERENCES)
    assert result.action == "replaced"
    archive = service.read()
    assert [(entry.section, entry.text) for entry in archive.entries] == [
        (SECTION_PREFERENCES, NEW)
    ]


def test_explicit_replaces_wins_over_mechanical(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add("用户使用 PostgreSQL。", SECTION_TOOLS)
    service.add("用户使用 MySQL。", SECTION_TOOLS)
    # 模型在同一次调用里直接给出顶替目标（§4.3 第 1 条）：机械判据兜底，不是唯一入口
    result = service.add("用户已改用 SQLite。", SECTION_TOOLS, replaces="用户使用 MySQL。")
    assert result.action == "replaced"
    assert result.replaced == "用户使用 MySQL。"
    texts = [entry.text for entry in service.read().entries]
    assert "用户使用 PostgreSQL。" in texts
    assert "用户使用 MySQL。" not in texts
    # 指认的目标不存在时退回机械判据，而不是整次写入失败
    fallback = service.add(NEW, SECTION_PREFERENCES, replaces="档案里根本没有这一条")
    assert fallback.action == "added"


def test_group_and_section_limits_are_enforced(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for index in range(8):
        text = f"项目 {index} 的目标是内网部署。"
        result = service.add(text, SECTION_PROJECTS, group=f"项目{index}")
        assert result.action == "added"
    ninth = service.add("项目 9 的目标是内网部署。", SECTION_PROJECTS, group="项目9")
    assert ninth.action == "rejected"
    assert "组" in ninth.receipt


def test_single_entry_over_limit_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.add("长" * 121, SECTION_PREFERENCES)
    assert result.action == "rejected"
    assert result.reason == "single"
    assert not (tmp_path / PROFILE).exists()


def test_empty_text_is_rejected_by_the_api(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(InvalidRequestError):
        service.add("   ", SECTION_PREFERENCES)


# ----------------------------------------------------------------- 变更流查证
#
# §5.3：`recall` 的池子只剩变更流，排序是**纯字面**判据（子串命中数 + 二元组覆盖率）。
# 这一组钉三件：该命中的命中并带回行号、不该命中的返回空、以及"没搜到"只有一个含义。


def _seed_changes(tmp_path: Path, service: ArchiveService) -> None:
    service.add("用户要求回答简短", SECTION_PREFERENCES)
    service.add("用户要求回答先给结论、再列依据", SECTION_PREFERENCES, replaces="用户要求回答简短")
    service.add("项目代号叫 kylab", SECTION_PROJECTS, group="内网部署")


def test_search_changes_finds_the_replacement_with_its_line_range(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_changes(tmp_path, service)

    hits = service.changes_hits("先给结论")

    assert hits, "那条替换就在变更流里"
    top = hits[0]
    assert top.path == af.CHANGES_FILENAME
    assert top.start_line >= 1 and top.end_line >= top.start_line
    assert "用户要求回答简短" in top.text, "旧值也要在结果里（它回答的是'以前是什么'）"
    assert top.coverage > 0


def test_search_changes_returns_nothing_for_a_noise_query(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_changes(tmp_path, service)

    assert service.changes_hits("合唱团的排练时间安排") == []


def test_search_changes_does_not_tokenize(tmp_path: Path) -> None:
    """**不依赖分词**：判据只看字符二元组，所以半句话也能命中。

    这条是闭包约束的落点（``jieba`` 不在发行包里）：旧检索那一整套（BM25、AST 切块、
    标题加权）是为几百 KB 的流水准备的，池子变小之后它是纯粹的复杂度。
    """
    service = _service(tmp_path)
    _seed_changes(tmp_path, service)

    hits = service.changes_hits("回答先给")  # 切得出词才怪，但字对重合够

    assert hits


def test_search_changes_ranks_the_more_recent_record_first_on_a_tie(
    tmp_path: Path,
) -> None:
    """同分时**近的排前面**："我刚改了什么"比"半年前改了什么"更常是问话的意图。"""
    service = _service(tmp_path)
    service.add("用户要求回答简短", SECTION_PREFERENCES)
    service.add("用户要求回答简短一些", SECTION_PREFERENCES)

    hits = service.changes_hits("用户要求回答简短")

    assert len(hits) >= 2
    assert hits[0].start_line > hits[-1].start_line


def test_forget_accepts_a_topic_and_refuses_to_guess(tmp_path: Path) -> None:
    """`forget` 的入参是**话题**：原样找不到时按唯一子串兜一次，对得上多条就列候选。"""
    service = _service(tmp_path)
    service.add("项目代号叫 kylab", SECTION_PROJECTS, group="内网部署")
    service.add("项目目标是 11 月交付", SECTION_PROJECTS, group="内网部署")

    ambiguous = service.forget("项目")

    assert ambiguous.action == "rejected"
    assert ambiguous.reason == "ambiguous"
    assert "kylab" in ambiguous.receipt and "11 月交付" in ambiguous.receipt

    unique = service.forget("代号叫 kylab")

    assert unique.action == "forgotten"
    assert unique.text == "项目代号叫 kylab"


def test_group_rename_rewrites_entries_and_logs_one_change(tmp_path: Path) -> None:
    """组改名 = 一次顶替（§3.1 第 2 条）：改的是分组名，正文不动，变更流只有一条。

    它不是"每条各顶替一次"——组标题本身就是一个可顶替的对象，所以两条同组条目
    只该留下**一条**旧名 → 新名的记录。
    """
    service = _service(tmp_path)
    service.add("项目目标是不出公网。", SECTION_PROJECTS, group="内网部署")
    service.add("项目已决定用 PostgreSQL。", SECTION_PROJECTS, group="内网部署")

    result = service.rename_group(SECTION_PROJECTS, "内网部署", "内网知识库")

    assert result.action == "replaced"
    assert [(entry.text, entry.group) for entry in service.read().entries] == [
        ("项目目标是不出公网。", "内网知识库"),
        ("项目已决定用 PostgreSQL。", "内网知识库"),
    ]
    replaced = [item for item in service.changes() if item.action == af.ACTION_REPLACED]
    assert len(replaced) == 1
    assert replaced[0].old == "内网部署"
    assert replaced[0].new == "内网知识库"


def test_group_rename_round_trips_through_restore(tmp_path: Path) -> None:
    """组改名也走「还原」（§6.2）：旧组名写回，新组名作为一次新的顶替进流。

    它钉的是 ``restore`` 的第三个分支：组改名记录的旧值/新值是**组名**而不是条目
    正文，按"档案里有没有这条条目"找不到它，得按 `group` 字段回溯。
    """
    service = _service(tmp_path)
    service.add("项目目标是不出公网。", SECTION_PROJECTS, group="内网部署")
    before = (tmp_path / PROFILE).read_bytes().decode("utf-8")

    service.rename_group(SECTION_PROJECTS, "内网部署", "内网知识库")
    restored = service.restore("内网部署")

    assert restored.action == "restored"
    assert (tmp_path / PROFILE).read_bytes().decode("utf-8") == before
