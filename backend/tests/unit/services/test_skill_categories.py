"""技能分类：**覆盖 178 条、每条只归一类、生成物与判据一致**。

镜像同构：``app/services/skill_categories.py`` → 本文件。

这一层是纯函数 + 两份文件，不需要真库：喂进去的是"本机装了什么"（`installed.json`）
与各技能的 `SKILL.md` frontmatter。所以这里钉的是**归属判据本身**，
而"页面上按分类列出来"那件事由前端用例 + 真浏览器截图钉（见交付记录）。

`backend/data/` 是运行期数据、不入库，缺了这些用例会**跳过**而不是编一份假数据来测：
在别的机器上它们本来就没有意义（分类判据本身仍然可用，`--check` 也能跑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import skill_categories as sc

INSTALLED = sc.INSTALLED_PATH
requires_data = pytest.mark.skipif(
    not INSTALLED.exists(), reason="本机没装技能集（backend/data/ 是运行期数据，不入库）"
)


def _rows() -> list[dict[str, str]]:
    return sc._rows_from_disk()


@requires_data
def test_every_installed_skill_gets_exactly_one_declared_category() -> None:
    """不遗、不重、不越界：178 条每一条都在 `CATEGORIES` 里且只落一类。"""
    rows = _rows()
    assignments = sc.classify(rows)
    declared = {item.slug for item in sc.CATEGORIES}

    assert len(rows) == 178, "本机初始技能集应为 178 条"
    assert len(assignments) == len(rows), "一条技能只能有一个归属（slug 唯一）"
    assert set(assignments.values()) <= declared, "出现了没登记的类别"
    assert set(assignments) == {row["slug"] for row in rows}


@requires_data
def test_counts_add_up_to_the_total_and_every_category_has_a_label() -> None:
    """每类统计要加得回总数；每个类别都得有中文名（页面直接用）。"""
    assignments = sc.classify(_rows())
    counts = {item.slug: 0 for item in sc.CATEGORIES}
    for category in assignments.values():
        counts[category] += 1

    assert sum(counts.values()) == len(assignments) == 178
    for item in sc.CATEGORIES:
        assert item.label.strip(), f"{item.slug} 没有中文名"
    assert sc.CATEGORIES[-1].slug == sc.OTHER, "「其他」固定在最后一档"


@requires_data
def test_the_mapping_file_is_in_sync_with_the_rules() -> None:
    """生成物（页面读的那份 JSON）必须与判据一致——不一致说明有人只改了一边。

    改判据之后忘了 `--write` 就会在这里红：那是**故意的**，因为页面读的是 JSON，
    而"分类"的唯一真相是 `SIGNALS`。
    """
    payload = json.loads(sc.MAPPING_PATH.read_text(encoding="utf-8"))
    assignments = sc.all_assignments()

    assert payload["assignments"] == assignments
    assert payload["categories"] == {item.slug: item.label for item in sc.CATEGORIES}


def test_the_builtin_skills_are_pinned_by_name() -> None:
    """**产品自带**的几条按名字钉死（2026-09-29 用户裁定），别落进「其他」。

    它们不在 `installed.json` 里（那份记的是"装进来的"），但接口一样会列出来：
    `kylab-delegate` / `kylab-knowledge-base` / `kylab-memory` / `kylab-web` → 效率与自动化，
    `kylab-office-export` → 文档与办公。
    """
    assert sc.BUILTIN_SKILLS == {
        "kylab-delegate": "productivity",
        "kylab-knowledge-base": "productivity",
        "kylab-memory": "productivity",
        "kylab-web": "productivity",
        "kylab-office-export": "documents",
    }
    for slug, expected in sc.BUILTIN_SKILLS.items():
        assert sc.category_of(slug) == expected
        # 钉死归钉死，类别本身必须登记过（否则页面上会被当未知分类再兜一次）
        assert expected in {item.slug for item in sc.CATEGORIES}

    # 生成物里也要有它们（否则页面读 JSON 时仍然落「其他」）
    payload = json.loads(sc.MAPPING_PATH.read_text(encoding="utf-8"))
    for slug, expected in sc.BUILTIN_SKILLS.items():
        assert payload["assignments"][slug] == expected


@requires_data
def test_only_the_unclassifiable_one_is_left_in_other() -> None:
    """「其他」应当**很小**，而且那条拿不准的一直在（`xiaoyue-companion`）。

    **不再是"只剩它一条"**（2026-09-29 如实报）：库从初始技能集涨到上千条之后，
    信号表认不出的名字变多了（当前 16 条，见 `--check` 的清单）。这里钉的是
    **两条不脆的性质**：
    ① 「其他」占整体的比例很小（≤ 10%，当前 16/183 ≈ 8.7%）——它是兜底，不是主分类；
    ② `xiaoyue-companion`（虚拟伴侣：描述里没有任何领域信号）仍然在里面——
       写这一条是为了防止有人为了让数字好看，随便给它塞一个类别。
    """
    assignments = sc.all_assignments()
    others = sorted(slug for slug, category in assignments.items() if category == sc.OTHER)

    assert "xiaoyue-companion" in others
    too_many = f"「其他」太多了（{len(others)}）：{others}"
    assert len(others) <= max(5, len(assignments) // 10), too_many


def test_the_frontend_mirror_is_byte_identical() -> None:
    """前端那份是**镜像**：两份必须逐字节相同（页面读镜像，不赌 Vite 跨目录）。

    **哪份是源**：后端 `app/services/skill_categories.json`——它由
    `python -m app.services.skill_categories --write` 生成。前端那份只是副本：
    改了判据要 `--write`，再把文件复制到页面那个特性目录
    （`frontend/src/features/misc/capabilities/`）。这里就是那道闸——**不复制就红**。
    """
    mirror = (
        # parents: [0]=services [1]=unit [2]=tests [3]=backend [4]=仓库根
        Path(__file__).resolve().parents[4]
        / "frontend"
        / "src"
        / "features"
        / "misc"
        / "capabilities"
        / "skillCategories.json"
    )
    assert mirror.exists(), f"前端镜像不在：{mirror}"
    assert mirror.read_bytes() == sc.MAPPING_PATH.read_bytes(), (
        "两份分类映射不一致：改了判据就跑 --write，然后把 "
        "backend/app/services/skill_categories.json 复制成前端那一份"
    )


@requires_data
def test_unmistakable_skills_land_in_the_expected_category() -> None:
    """几条"名字就说明了一切"的锚点：规则被改坏时它们先红。

    **只用名字判**（`category_of(slug, slug, "")`）：这一节验的是**规则表**，
    不是库里那条技能的实时描述——导入器（2026-09-29 那批 12k 技能）会重写描述，
    拿它当断言对象等于让用例随数据漂（真发生过：`academic-poster` 的描述被改过之后
    这一条就红了，而规则一个字没动）。

    最后两条**本来就靠描述才认得出**（名字里没有"论文/学术"这类信号），
    所以给它们**测试自己持有的描述**：仍然是钉规则，仍然不看库里的那份。
    """
    anchors = {
        "pdf-pro": "documents",
        "image-to-editable-ppt": "slides",
        "knowledge-graph": "learning",
        "playwright-skill": "code",
        "code-review-skill": "code",
        "seo-geo-aeo": "marketing",
        "personal-finance-skill": "business",
        "tasknotes": "productivity",
    }
    for slug, expected in anchors.items():
        assert sc.category_of(slug, slug, "") == expected, f"{slug} 应当属于 {expected}"

    description_driven = {
        "academic-poster": ("学术会议海报：把论文做成一张 research poster", "research"),
        "sci-download": ("下载论文全文（paper / PDF / 文献）", "research"),
    }
    for slug, (description, expected) in description_driven.items():
        assert sc.category_of(slug, slug, description) == expected, f"{slug} 应当属于 {expected}"


def test_no_signal_means_other_and_the_rule_table_explains_itself() -> None:
    """一条信号都不命中 → 「其他」（拿不准的不硬塞）；信号表的权重只有 1/2/3 三档。"""
    assert sc.category_of("zzz-nothing-matches-this-xyz") == sc.OTHER
    assert sc.OTHER == "other"

    for category, patterns in sc.SIGNALS:
        assert category in {item.slug for item in sc.CATEGORIES}
        assert patterns, f"{category} 没有任何信号"
        for weight, pattern in patterns:
            assert weight in (1, 2, 3), f"{category} 的权重只许 1/2/3，见到 {weight}"
            assert pattern.strip()


def test_the_name_beats_the_description() -> None:
    """名字里的命中按 ×2 计：描述里顺带提到别的领域的词，抢不走名字说清了的那条。

    `book-to-skill` 的描述里满是 PDF/DOCX/代码字样，名字直接说了"把书变成技能"。
    """
    text = "converts books and documents (PDF, EPUB, DOCX)"
    assert sc.category_of("book-to-skill", "", text) == "learning"
    assert sc.category_of("lineage-skill", "", "audio video pdf slides notes") == "learning"
