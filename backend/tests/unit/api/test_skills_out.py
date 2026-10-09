"""``api/v1/skills.py`` 的 ``_out``：中文简介从哪来、两边都有时听谁的（v0.53）。

要钉的是一条**跨两处的一致性**：能力页（``_out``）与命令菜单
（``core/services.py`` 的 ``_skill_summaries``）读的是同一份数据、必须同一套优先序。
两边反了不会报错，只会让同一个技能在两个界面上显示两句不同的说明——
那种错没人会发现，除非有人正好两处都看。

优先序：**安装清单那份 > 技能 frontmatter 自带那份**
（市场那份是为中文界面存的、更短更贴；frontmatter 那份常是英文），
只有前者没有时才退回后者（仓库自带的 5 个走这条）。

v0.61 起再加一层：**列表端点还回分类与每类精选**（`category` / `featured` /
`categories`）。这一层要钉的是三件事——分类跟着记录走、精选是**全库口径**
（翻页不改变谁被标成精选）、以及它是**只增不改**的（老客户端不读这几个字段照旧跑）。
"""

from __future__ import annotations

from app.api.v1.skills import _out, list_skills
from app.services.skill_categories import CATEGORIES
from app.services.skills import SkillRecord


def _record(summary: str) -> SkillRecord:
    return SkillRecord(
        name="weekly-report",
        description="Weekly report",
        path="skills/weekly-report/SKILL.md",
        source="builtin",
        directory="weekly-report",
        summary=summary,
    )


def test_the_market_blurb_wins_when_both_sides_have_one() -> None:
    """两边都有：用清单那份（与 ``_skill_summaries`` 的覆盖顺序一致）。"""
    item = _out(_record("技能自己写的简介"), "市场装的时候存的中文简介")
    assert item.summary == "市场装的时候存的中文简介"


def test_it_falls_back_to_the_skills_own_summary() -> None:
    """清单里没有（仓库自带的技能没有安装那一步）：用 frontmatter 里那份。"""
    item = _out(_record("把结果整理成一份周报"), "")
    assert item.summary == "把结果整理成一份周报"


def test_no_summary_anywhere_is_an_empty_string() -> None:
    """两处都没有就是空串——界面按"没有中文简介"处理，不编一句出来。"""
    assert _out(_record("")).summary == ""


# ------------------------------------------------ 分类与精选（v0.61）


def _skill(
    name: str,
    *,
    category: str,
    source: str = "user",
    used_by_prompt: bool = True,
) -> SkillRecord:
    return SkillRecord(
        name=name,
        description=f"{name} 的说明",
        path=f"skills/{name}/SKILL.md",
        source=source,
        directory=name,
        category=category,
        used_by_prompt=used_by_prompt,
    )


class _StubSkills:
    def __init__(self, records: list[SkillRecord]) -> None:
        self._records = records

    def list(self) -> list[SkillRecord]:
        return list(self._records)


class _StubMarket:
    def __init__(self, installed: list[str], summaries: dict[str, str] | None = None) -> None:
        self._installed = installed
        self._summaries = summaries or {}

    def installed(self) -> dict[str, str]:
        return dict.fromkeys(self._installed, "upload:x.zip")

    def installed_records(self) -> dict[str, dict[str, str]]:
        return {
            name: {"origin": "upload:x.zip", "summary": summary}
            for name, summary in self._summaries.items()
        }


class _StubServices:
    """够列表端点用的最小壳（真 `Services` 要数据库，而这里验的是响应形状）。"""

    def __init__(
        self,
        records: list[SkillRecord],
        *,
        installed: list[str] | None = None,
        summaries: dict[str, str] | None = None,
    ) -> None:
        self.skills = _StubSkills(records)
        self.skill_market = _StubMarket(installed or [], summaries)


def test_the_list_marks_the_featured_two_and_carries_their_categories() -> None:
    """每类精选 2 条标在**技能自己身上**，分类清单给出"这一类叫什么、有几条"。

    精选只从**能用**的里挑：第二条被拦下的那种情况要能从标记上看出来。
    """
    services = _StubServices(
        [
            _skill("slide-a", category="slides"),
            _skill("slide-b", category="slides"),
            _skill("slide-c", category="slides"),
            _skill("code-a", category="code", used_by_prompt=False),
        ],
        installed=["slide-c"],
    )

    out = list_skills(services, None)  # type: ignore[arg-type]

    marks = {item.name: item.featured for item in out.items}
    assert marks == {"slide-a": True, "slide-b": False, "slide-c": True, "code-a": False}
    assert [item.name for item in out.items if item.featured] == ["slide-a", "slide-c"]
    slides = next(item for item in out.categories if item.slug == "slides")
    assert slides.label and slides.total == 3
    # 装过的那条排前面（"装进来的"比"随库躺着的"更能说明有人要它）
    assert slides.featured == ["slide-c", "slide-a"]
    # 全库只有一条能用的那一类：精选就给一条（不凑数、不报错）
    code = next(item for item in out.categories if item.slug == "code")
    assert code.featured == []


def test_the_category_list_keeps_the_page_order_and_the_library_wide_counts() -> None:
    """`categories` 的顺序 = `CATEGORIES`（页面分组顺序），条数是**全库**的。

    分页是"这一页给几条"，不是"这一类有几条"——后者随翻页变会把界面上的数字
    变成一团乱麻（与 `usable` 同一条口径）。
    """
    services = _StubServices(
        [_skill("slide-a", category="slides"), _skill("doc-a", category="documents")]
    )

    out = list_skills(services, None, limit=1)  # type: ignore[arg-type]

    assert [item.name for item in out.items] == ["slide-a"]  # 这一页只有一条
    assert [item.slug for item in out.categories] == [item.slug for item in CATEGORIES]
    counts = {item.slug: item.total for item in out.categories}
    assert counts["slides"] == 1 and counts["documents"] == 1
    assert out.usable == 2


def test_a_record_without_a_category_is_still_served() -> None:
    """没经过分类的记录（空串）照旧能列出来，只是不参选精选——**只增不改**。

    老客户端的形状（`items` + `usable`）一位都不能变：新字段全是默认值。
    """
    services = _StubServices([_skill("weekly-report", category="")])

    out = list_skills(services, None)  # type: ignore[arg-type]
    dumped = out.model_dump()

    assert dumped["items"] == [
        {
            "name": "weekly-report",
            "description": "weekly-report 的说明",
            "summary": "",
            "source": "user",
            "path": "skills/weekly-report/SKILL.md",
            "directory": "weekly-report",
            "used_by_prompt": True,
            "enabled": True,
            "flagged": [],
            "category": "",
            "featured": False,
            "discarded": False,
        }
    ]
    assert dumped["usable"] == 1
    assert [item.slug for item in out.categories] == [item.slug for item in CATEGORIES]
    assert all(item.featured == [] for item in out.categories)
