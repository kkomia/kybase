"""前端显示文案排查（v26，React 版）：清单 + 分类 + "AI 味"可疑项。

用法：python scripts/audit-copy.py
退出码：0 = 没有可疑项；1 = 有可疑项（**不接入 CI**，它是审阅工具不是门禁——
文案是否合适需要人判断，机械规则只负责把人该看的地方指出来）。

只认**会显示给用户**的文字：
- JSX 文本节点（`>新建会话<` 这种**单行**片段）；
- 属性/字符串字面量里的单行中文（placeholder / title / aria-label / toast 文案 …）。

排除：所有注释、多行代码块、含箭头/比较运算符的代码片段。

**v25 → v26（2026-09-23）**：P5 把前端从 Vue 换成 React 之后，这份脚本原来只认
`<template>` 文本节点、板块表也全是 `ChatView` 这类 Vue 文件名——跑起来只剩下 `.ts`
里的字符串，静默退化成"大部分文案看不到"（实测：104 条 / 18 个文件，板块只剩两个）。
现在改成认 JSX 文本节点，板块表按 React 的域重写。判据（AI_SMELL 与长度）一个字没改。
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent / "frontend" / "src"
CJK = re.compile(r"[\u4e00-\u9fff]")

#: 板块归类：按文件路径把文案归到用户能认出来的界面上（React 的域与文件名，见
#: 《React 迁移计划》§3 的所有权表）。
SURFACES = [
    ("登录/首次设置", ("misc/auth/", "LoginPage", "lib/session")),
    ("侧栏/框架", ("layout/", "AppShell", "SideNav", "AccountMenu")),
    ("概览（驾驶舱）", ("misc/dashboard/", "api/stats")),
    ("知识库列表", ("KnowledgeBasesView", "knowledge/store", "api/knowledgeBases")),
    (
        "知识库详情/文档列表",
        ("KnowledgeBaseView", "KnowledgeBaseSettings", "UploadDialog", "ShareDialog"),
    ),
    ("文档抽屉", ("DocumentDrawer", "preview/", "SourcePanel", "ProcessingTimeline")),
    ("对话", ("chat/", "api/chat")),
    ("笔记", ("notes/", "api/notes")),
    ("任务中心", ("misc/tasks/", "tasks/", "api/tasks", "status.ts")),
    ("Wiki", ("WikiView", "api/wiki")),
    (
        "设置",
        (
            "misc/settings/",
            "SettingsModal",
            "ModelRegistryPanel",
            "providerPresets",
            "api/settings",
            "api/modelRegistry",
        ),
    ),
    ("能力（技能/插件/MCP）", ("capabilities/", "api/capabilities")),
    ("记忆", ("misc/memory/", "api/memory")),
    ("工作区", ("misc/workspaces/", "layout/workspaces", "api/workspaces")),
    ("通用/其它", ()),
]

#: "AI 味"：营销腔、机械排比、论文腔连接词、教程腔、空泛副词、解释癖。
AI_SMELL = [
    (r"不仅.*(还|而且|更)", "排比腔「不仅…还…」"),
    (r"旨在|致力于|赋能|助力|打造|一站式|无缝|极致的?体验|强劲", "营销/公关用语"),
    (r"值得(注意|一提)的?是|总而言之|综上|换言之|换句话说|本质上|从某种意义上", "论文腔连接词"),
    (r"让(我们|你) |请(注意|记住)|可以看到", "教程腔"),
    (r"非常|十分|极其|极大地|显著地|完美地|轻松|强大", "空泛副词"),
    (r"^该|^此(功能|操作|项)|该功能|此功能|该操作|此操作", "说明书腔「该/此…」"),
    (r"—{2}|——", "破折号插入语"),
    (r"[（(][^）)]{20,}[）)]", "括号里塞一整句解释"),
    (r"我们(曾经|已经|当时|实测|发现)", "工程回忆录（第一人称叙述）"),
    (r"^[^。！？]{0,10}[:：].{30,}$", "「标题：长篇解释」式说明"),
]

#: 长度警戒：UI 里一句话超过这个字数，通常是在写文章而不是在指示。
LONG_COPY = 60


def strip_comments(source: str) -> str:
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    lines = []
    for line in source.splitlines():
        idx = line.find("//")
        if idx > 0 and not line[:idx].rstrip().endswith(":"):
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


def looks_like_code(text: str) -> bool:
    """把明显的代码片段排掉（多行、箭头、比较、属性访问链）。"""
    if "\n" in text:
        return True
    if any(token in text for token in ("=>", "===", "!==", "&&", "||", "?.", "?.")):
        return True
    # 模板字符串里带复杂表达式的（`.map(` / `[step.status]` 之类）
    if re.search(r"\$\{[^}]*[\[\]().]", text):
        return True
    return False


def collect(path: pathlib.Path) -> list[str]:
    raw = strip_comments(path.read_text(encoding="utf-8"))
    found: list[str] = []
    if path.suffix == ".tsx":
        # JSX 文本节点：`>新建会话<` 这种。两个细节——
        # 1. **逐行拆开**：跨行的标签块里夹着代码，整段看会被 looks_like_code 当"多行代码"滤掉，
        #    而真正的文案都是单行的（与旧 Vue 版处理 `<template>` 的办法一致）；
        # 2. `[^<>{}]+` 顺带把 `{表达式}` 挡在外面——那里面的中文是字符串字面量，
        #    下面那一遍字面量扫描会收，不会漏。
        for match in re.finditer(r">([^<>{}]+)<", raw):
            found += [piece.strip() for piece in match.group(1).splitlines() if CJK.search(piece)]
    for match in re.finditer(r"'([^'\\\n]*)'|\"([^\"\\\n]*)\"|`([^`\n]*)`", raw):
        value = match.group(1) or match.group(2) or match.group(3) or ""
        found.append(value)
    return [text.strip() for text in found if CJK.search(text) and not looks_like_code(text)]


def surface_of(rel: str) -> str:
    for name, needles in SURFACES:
        if any(needle in rel for needle in needles):
            return name
    return "通用/其它"


def main() -> None:
    if not ROOT.exists():
        print(f"找不到前端源码目录：{ROOT}", file=sys.stderr)
        raise SystemExit(2)
    by_surface: dict[str, set[str]] = defaultdict(set)
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in sorted(ROOT.rglob("*")):
        if path.suffix not in (".tsx", ".ts"):
            continue
        rel = str(path.relative_to(ROOT))
        for text in collect(path):
            by_surface[surface_of(rel)].add(text)
            if text not in seen:
                seen.add(text)
                items.append((rel, text))

    total = len(seen)
    print(f"共 {total} 条不同的中文显示文案，分布在 {len({rel for rel, _ in items})} 个文件\n")
    print("按板块：")
    for name, _ in SURFACES:
        texts = by_surface.get(name, set())
        if texts:
            print(f"  {len(texts):4d}  {name}")
    print()

    flagged: list[tuple[str, str, list[str]]] = []
    for rel, text in items:
        reasons = [why for pattern, why in AI_SMELL if re.search(pattern, text)]
        if len(text) > LONG_COPY:
            reasons.append(f"过长（{len(text)} 字）")
        if reasons:
            flagged.append((rel, text, reasons))

    print(f"可疑（AI 味或过长）：{len(flagged)} 条\n")
    for rel, text, reasons in sorted(flagged, key=lambda row: -len(row[1])):
        print(f"[{rel}] {'/'.join(reasons)}")
        print(f"    {text}\n")

    print("按模式统计：")
    counter = Counter(reason for _, _, reasons in flagged for reason in reasons)
    for reason, count in counter.most_common():
        print(f"  {count:3d}  {reason}")


    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
