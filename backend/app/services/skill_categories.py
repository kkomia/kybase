"""**初始技能集**（178 条）的分类方案：一套加权信号 + 一份可评审的映射。

## 这套分类是干什么的

库里那 178 条技能是随本机一起装进来的**初始技能集**（`backend/data/installed.json`
+ `backend/data/skills/<slug>/SKILL.md`）。它们没有分类字段，页面上只能平铺一长列，
所以这里给出**一套判据**把它分成 12 类（+「其他」），页面按它分组展示。

## 分类依据（两条，按优先级）

1. **`SKILL.md` 的 `description` 里的明确信号**（"slides / pptx / 论文 / 财报 / SEO"…）——
   描述写的正是"这个技能解决什么问题"，所以是主依据；
2. **技能名（slug）**：描述含糊时名字往往还是清楚的（`pdf-pro`、`image-to-editable-ppt`）。

`backend/data/installed.json` 里**没有**来源仓库字段（只有 `origin: upload:<slug>.zip`），
所以"按来源仓库的节来分"这条依据在本机数据上**不成立**，只能从描述与名字判。

## 判据怎么写：加权信号，不是"先匹配先赢"

`SIGNALS` 是 `{类别: ((权重, 正则), …)}`，一条技能对每一类**累加**命中的权重，
**分最高的那一类胜出**；平分时按 `CATEGORIES` 的顺序取靠前的；**全零 → 「其他」**。

第一版写的是"有序正则、先匹配先赢"，实测偏得很厉害：一长串正则里**任何一个泛词**
命中就把整条抢走——`required` 里的 `ui`、`regression testing` 里的 `regression`、
`emotional` 里的 `motion` 都真真切切抢过（那几类一度吃掉 30+ 条）。打分以后
"唯一信号"永远压得住"泛词"：`document-illustrator` 因为命中 `document-illustrator`(3)
落进「文档」，而不是被 `image`(2) 拉去「图像」。

权重只有三档，含义固定：

- **3**：这个词几乎只在那一类里出现（`pptx` / `comfyui` / `playwright` / `dcf` / `seo` /
  `zotero`）；
- **2**：常见但仍有指向性（`excel` / `meeting` / `figma` / `email`）；
- **1**：很泛的词（`design` / `image` / `api` / `experiment`），只用来**打破平局**。

## 怎么改

- **改一条归属**：调这一类或那一类的正则/权重即可，**别**直接改 `skill_categories.json`
  （它是生成物）；
- **加一类**：`CATEGORIES` 里加一行（slug 英文、label 中文），`SIGNALS` 里加一组信号；
  `CATEGORIES` 的顺序 = 页面上的分组顺序，也是平局时的先后；
- 改完跑 `python -m app.services.skill_categories --check` 看分布与「其他」，
  `--list` 看每条的得分依据，`--write` 重新生成 `skill_categories.json`（页面读那一份）。

## 与页面的关系

页面（`frontend/src/features/capabilities/**`）**不调后端新接口**（技能接口那条链路由
另一条 lane 在修，本模块刻意不碰 `services/skills.py` / `api/v1/skills.py`）：它读的是
一份 **JSON 映射**（slug → 类别）。两份文件的一致性由用例钉住
（`backend/tests/unit/services/test_skill_categories.py`），不会悄悄漂。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CATEGORIES",
    "SIGNALS",
    "category_of",
    "classify",
    "load_installed_slugs",
    "main",
    "scores_for",
]


@dataclass(frozen=True, slots=True)
class SkillCategory:
    """一类技能：`slug` 是稳定标识（写进映射、给前端当 key），`label` 是给人看的名字。"""

    slug: str
    label: str
    note: str = ""


#: 页面上的分组顺序**就是这一份的顺序**（"其他"固定最后，也是平局时的先后）。
CATEGORIES: tuple[SkillCategory, ...] = (
    SkillCategory("slides", "演示与幻灯片", "做 PPT / 幻灯片，或把别的东西转成可编辑幻灯片"),
    SkillCategory("documents", "文档与办公", "Word / PDF / 文档摘要与翻译、办公自动化"),
    SkillCategory("data", "表格与数据", "Excel/CSV 表格、数据分析、图表与看板、统计与建模库"),
    SkillCategory("design", "设计与视觉", "UI/UX、Figma、图标、动效、配色、可访问性、原型"),
    SkillCategory("media", "图像与音视频", "生成或处理图片、视频、音频、语音（含转录）"),
    SkillCategory("research", "科研与论文", "论文检索与写作、投稿与评审、实验设计、会议海报"),
    SkillCategory("code", "代码与开发", "写代码、评审、测试自动化、仓库与发布、平台排障"),
    SkillCategory("writing", "写作与文案", "改文风、去 AI 味、邮件与内部沟通、小说与虚构"),
    SkillCategory("marketing", "营销与增长", "SEO/AEO、电商与广告、邮件营销、CRM 与线索"),
    SkillCategory("business", "商业与金融", "财务与投资分析、产品与项目、咨询与战略、客户服务"),
    SkillCategory("productivity", "效率与自动化", "任务与会议、目标契约、工作流编排、技能管理"),
    SkillCategory("learning", "教学与学习", "把书/课/长材料变成可学的技能，知识图谱"),
    SkillCategory("other", "其他", "上面都不合适，或描述太含糊——交付里逐条列出为什么"),
)

OTHER = "other"

#: **产品自带**的技能：它们**不在** `backend/data/installed.json` 里——那份记的是
#: "装进来的"，而这几条随产品一起发布，`GET /api/v1/skills` 一样会把它们列出来。
#: 所以这里给一张**按名字钉死**的表（2026-09-29 用户裁定：它们是产品自带的能力，
#: 排在最前还是落「其他」对用户差别很大）。
#:
#: 为什么不给它们写正则：打分那套的坑就是"泛词抢技能"，而这几条的名字里
#: （`kylab-web` / `kylab-memory`…）本来就没有足够独特的词——钉名字既明确又不会误伤。
#: 将来产品又添自带技能时，**在这里加一行**（并跑 `--write`）。
BUILTIN_SKILLS: dict[str, str] = {
    "kylab-delegate": "productivity",
    "kylab-knowledge-base": "productivity",
    "kylab-memory": "productivity",
    "kylab-web": "productivity",
    "kylab-office-export": "documents",
}

#: 每类的**加权信号**（权重含义见模块头注）。顺序在打分里不重要，只影响打平时的先后。
SIGNALS: tuple[tuple[str, tuple[tuple[int, str], ...]], ...] = (
    (
        "slides",
        (
            (
                3,
                r"pptx?|powerpoint|keynote|\bslides?\b|\bdeck\b|html2pptx|poster\b|幻灯片|演示文稿",
            ),
            (2, r"presentation|decks?|汇报"),
        ),
    ),
    (
        "documents",
        (
            (
                3,
                r"\bpdf-pro\b|professional pdf|pdf forms|pdf redaction|office-automation|"
                r"document-illustrator|word 和 excel|word and excel",
            ),
            (
                2,
                r"docx\b|\bpdf\b|summariz\w*\s+(an?\s+)?(document|article|transcript|doc)|"
                r"translate\s+(books?|documents?)",
            ),
        ),
    ),
    (
        "data",
        (
            (
                3,
                r"xlsx|xlsm|\bcsv\b|\btsv\b|spreadsheet|tableau|statsmodels|scikit-learn|"
                r"\bpymc\b|openchart|d3\.js|sankey|tilemap",
            ),
            (
                2,
                r"\bexcel\b|dashboard|\bkpi\b|data quality|data pipeline|bayesian|clustering|"
                r"data visuali[sz]ation",
            ),
            (1, r"analytics|metrics?\b|charts?\b|visuali[sz]|数据可视化|看板|数据质量"),
        ),
    ),
    (
        "design",
        (
            (
                3,
                r"figma|wireframe|mockup|\bwcag\b|excalidraw|human interface|design system|"
                r"color (theory|naming|spaces?)",
            ),
            (
                2,
                r"\bui\b|\bux\b|prototype|responsive|\bcss\b|frontend|accessib|brand\b|"
                r"\bicons?\b|\bmotion\b|animation|palette|视觉|配色|图标|无障碍|线框",
            ),
            (1, r"design|\bdiagram\b"),
        ),
    ),
    (
        "media",
        (
            (
                3,
                r"comfyui|sogni|rawugc|cyberbara|morpheus|deepfake|resemble|"
                r"text-to-(image|video|speech)|\blora\b|transcri|subtitle|\basr\b",
            ),
            (
                2,
                r"\bimage\b|\bvideo\b|\baudio\b|\bmusic\b|\bspeech\b|\bvoice\b|图像|视频|音频|语音|配图",
            ),
        ),
    ),
    (
        "research",
        (
            (
                3,
                r"\bpaper\b|thesis|dissertation|manuscript|literature review|peer.review|"
                r"zotero|arxiv|pubmed|semantic scholar|\bdoi\b|\bcnki\b|论文|文献|投稿|审稿",
            ),
            (
                2,
                r"academic|scholarly|scientific|citation|bibliograph|\bjournal\b|"
                r"research (paper|writing|project|review)",
            ),
            (1, r"research|学术|实验"),
        ),
    ),
    (
        "code",
        (
            (
                3,
                r"playwright|cypress|\be2e\b|code review|refactor|\bgit\b|github|\bjava\b|"
                r"\brepo\b|repository|\bsql\b|codebase|代码",
            ),
            (
                2,
                r"\bapi\b|\bmcp\b|\bios\b|\bswift\b|android|python|rust|\bqa\b|developer|"
                r"software engineer|architecture|\bbug\b|debug|security review|开发",
            ),
            (1, r"\bcode\b"),
        ),
    ),
    (
        "writing",
        (
            (3, r"humaniz|copywrit|\bprose\b|plain language|novel|fiction|文案|润色|小说"),
            (
                2,
                r"\bwrit|rewrit|\bemail\b|\bblog\b|translat|\btone\b|status update|"
                r"internal comm|邮件|写作|翻译",
            ),
            (1, r"\bdraft\b|\bstory\b"),
        ),
    ),
    (
        "marketing",
        (
            (
                3,
                r"\bseo\b|\baeo\b|\bgeo\b|\bcrm\b|\bamz\b|etsy|amazon|ppc|\bads?\b|"
                r"e-?commerce|schema\.org|marketing|营销|电商",
            ),
            (2, r"growth|\bsales\b|\bleads?\b|funnel|conversion|广告|转化|线索"),
        ),
    ),
    (
        "business",
        (
            (3, r"\bdcf\b|\blbo\b|valuation|earnings|bookkeep|receipt|financ|财务|金融|投资"),
            (
                2,
                r"\bprd\b|roadmap|sprint|backlog|jira|ticket|warehouse|inventor|logistics|"
                r"\brisk\b|customer service|customer messages|de-escalation|angry|consult|"
                r"strateg|proposal|tender|\bbid\b|product manager|product management|"
                r"商业|咨询|战略",
            ),
            (1, r"business|pricing|budget|accounting|market siz"),
        ),
    ),
    (
        "productivity",
        (
            (3, r"workflow-orchestration|skill-creator|skill-forge|\bgoal\b|catalog|tasknotes"),
            (
                2,
                r"meeting|triage|inbox|overnight|schedul|\brecap\b|agenda|delegat|效率|自动化|会议",
            ),
        ),
    ),
    (
        "learning",
        (
            (3, r"book-to-skill|lineage|knowledge graph|knowledge-graph|curricul|教材"),
            (2, r"course|teach|tutor|学习|知识图谱"),
        ),
    ),
)

#: 生成物：slug → 类别（页面读它）。`--write` 重新生成。
MAPPING_PATH = Path(__file__).with_name("skill_categories.json")
#: 本机装了什么技能（`backend/data/` 是运行期数据、不入库，缺了就退化成"只校验规则"）。
INSTALLED_PATH = Path(__file__).resolve().parents[2] / "data" / "installed.json"


def _haystack(slug: str, name: str, description: str) -> str:
    """喂给信号表的文本：slug + 名字 + 描述拼起来（描述含糊的靠名字救回来）。"""
    return " ".join((slug, name, description)).casefold()


def scores_for(slug: str, name: str = "", description: str = "") -> dict[str, int]:
    """这一条在各类上的得分（**同一类里每条信号只算一次**，避免同一个词刷分）。

    **名字里的命中按 ×2 计**：名字是作者给这条技能起的"它是什么"，
    比描述里顺带提到的词更硬——`book-to-skill` 的描述里满是 PDF/DOCX/代码字样，
    名字却直接说了它是"把书变成技能"。不这么加权，它会被那些顺带的词抢走。
    名字同时按**原样与连字符换空格**两种形态喂进去（`book-to-skill` 与 `book to skill`
    都要能命中，信号表里两种写法都有）。
    """
    text = _haystack(slug, name, description)
    slug_text = f"{slug} {slug.replace('-', ' ')}".casefold()
    scores: dict[str, int] = {}
    for category, patterns in SIGNALS:
        total = 0
        for weight, pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                total += weight
            if re.search(pattern, slug_text, re.IGNORECASE):
                total += weight * 2
        if total:
            scores[category] = total
    return scores


def category_of(slug: str, name: str = "", description: str = "") -> str:
    """这一条归哪一类：**分最高的胜出**；平分按 `CATEGORIES` 顺序；全零 → 「其他」。

    **产品自带的那几条按名字钉死**（见 `BUILTIN_SKILLS`），不进打分：
    它们就那么几条，钉住比"再写一条正则"稳——泛词抢技能是这套打法唯一的坑
    （`required` 里的 `ui` 那类），自带技能不值得再冒一次这个险。
    """
    if slug in BUILTIN_SKILLS:
        return BUILTIN_SKILLS[slug]
    scores = scores_for(slug, name, description)
    if not scores:
        return OTHER
    best = max(scores.values())
    for item in CATEGORIES:  # CATEGORIES 的顺序就是平局时的先后
        if scores.get(item.slug) == best:
            return item.slug
    return OTHER  # pragma: no cover - max() 保证上面一定会 return


def classify(rows: list[dict[str, str]]) -> dict[str, str]:
    """一批技能 → `{slug: 类别}`（保持输入顺序，方便对照打印）。"""
    return {
        row["slug"]: category_of(row["slug"], row.get("name", ""), row.get("description", ""))
        for row in rows
    }


def load_installed_slugs(path: Path | None = None) -> list[str]:
    """本机装了哪些技能（就一个 slug 清单）。文件不在时返回空表。"""
    target = path or INSTALLED_PATH
    if not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    return list(data)


def _frontmatter_description(skill_dir: Path) -> str:
    """从 `SKILL.md` 的 frontmatter 里抠 `description`（只为生成映射时用）。"""
    candidates = sorted(skill_dir.glob("*.md"))
    if not candidates:
        return ""
    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        return ""
    fields: dict[str, str] = {}
    key: str | None = None
    for line in match.group(1).splitlines():
        if re.match(r"^[A-Za-z_-]+:", line):
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip().strip("'\"")
        elif key and line.startswith((" ", "\t")):
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    return fields.get("description", "")


def _rows_from_disk() -> list[dict[str, str]]:
    """本机那 178 条：slug + 名字 + 描述（描述从各技能的 `SKILL.md` 读）。"""
    skills_dir = INSTALLED_PATH.parent / "skills"
    rows: list[dict[str, str]] = []
    for slug in load_installed_slugs():
        description = _frontmatter_description(skills_dir / slug) if skills_dir.is_dir() else ""
        rows.append({"slug": slug, "name": slug, "description": description})
    return rows


def all_assignments() -> dict[str, str]:
    """**页面会看到的那一批**：本机装进来的 178 条 + 产品自带的 5 条。

    分开写是因为数据来源不同（前者读 `installed.json`，后者是随产品发布的常量），
    而页面/生成物要的是**合成之后**的那一份——少写一半就会出现"自带技能落其他"。
    """
    merged = classify(_rows_from_disk())
    merged.update(BUILTIN_SKILLS)
    return merged


def build_mapping() -> dict[str, object]:
    """生成物内容：类别顺序 + `{slug: 中文名}` + `{技能: 类别}`。

    `order` 是**展示顺序**：JSON 落盘时键名排过序（`sort_keys=True`，为的是 diff 稳定），
    光看 `categories` 那个对象读不出"先给用户看哪一类"——所以顺序单独给一份列表。
    """
    return {
        "order": [item.slug for item in CATEGORIES],
        "categories": {item.slug: item.label for item in CATEGORIES},
        "assignments": all_assignments(),
    }


def main(argv: list[str] | None = None) -> int:
    """`--check` 打分布、`--list` 打每条的判据、`--write` 重新生成 JSON。"""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    rows = _rows_from_disk()
    if not rows:
        print("没读到 installed.json（本机数据不在）——分类判据本身仍然可用")
        return 1
    for slug in BUILTIN_SKILLS:
        rows.append({"slug": slug, "name": slug, "description": "(产品自带，按名字钉死)"})
    assignments = classify(rows)
    counts: dict[str, int] = {item.slug: 0 for item in CATEGORIES}
    for category in assignments.values():
        counts[category] = counts.get(category, 0) + 1

    if "--list" in args:
        for row in rows:
            slug = row["slug"]
            scores = scores_for(slug, row["name"], row["description"])
            why = "、".join(f"{key}={value}" for key, value in sorted(scores.items()))
            print(f"{assignments[slug]:12s} {slug:44s} {why or '(无信号)'}")
        return 0

    print(f"合计 {len(assignments)} 条")
    for item in CATEGORIES:
        print(f"  {item.slug:12s} {item.label:10s} {counts.get(item.slug, 0):3d}")
    unknown = sorted(set(assignments.values()) - {item.slug for item in CATEGORIES})
    if unknown:
        print("信号表里有未登记的类别：", unknown)
        return 2
    others = sorted(slug for slug, category in assignments.items() if category == OTHER)
    print(f"其他（{len(others)}）：", "、".join(others))
    if sum(counts.values()) != len(assignments):
        print("计数与总数对不上（有类别没登记）")
        return 3

    if "--write" in args:
        payload = build_mapping()
        MAPPING_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {MAPPING_PATH}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工跑的入口
    raise SystemExit(main())
