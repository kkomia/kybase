"""折叠迁移（期一，判据见 ``docs/设计/记忆档案-设计-v0.1.md`` §8、§9.2 第 10/11 条）。

镜像同构：``app/services/archive_migration.py`` → 本文件。

三条最容易做错的：重跑又折了一遍（水位失效）、动了旧文件（"只读旧文件"失守）、
把敏感形状或 ``daily/`` 现场折进档案。每一条都有用例钉着。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from app.services import archive_files as af
from app.services import archive_migration as mig

PROFILE = af.ARCHIVE_FILENAME

#: ``PROFILE.md`` **不在"旧文件"清单里**：按 §7.2 与 §10 第 2 条的建议口径，它改造成档案，
#: 正文从散文变成四区条目——它就是目的地。其余旧文件一个字节都不许变。
_OLD_FILES = (
    "MEMORY.md",
    "digest/personal/风格.md",
    "digest/procedure/发布.md",
    "digest/wiki/概念.md",
    "daily/2026-10-01.md",
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _workspace(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "PROFILE.md",
        """---
summary: 旧的身份文件
---

## 身份

- **名字：** 小又
- 用户是中文用户，称呼「小又」。

## 用户资料

- 用户要求回答先给结论、再列依据。

### 背景

*（他在意什么？）*
""",
    )
    _write(
        tmp_path,
        "MEMORY.md",
        """---
summary: 旧的核心记忆
---

## 核心长期记忆

- 用户的机器是 Windows Server 2019，配一张 L20。
- 项目「内网知识库」的目标是全部不出公网。
- 用户的数据库密码是 hunter2。
- """
        + "超长条目" * 40
        + "\n",
    )
    _write(tmp_path, "digest/personal/风格.md", "---\n---\n\n- 用户不看客套话。\n")
    _write(tmp_path, "digest/procedure/发布.md", "---\n---\n\n- 发布前先跑门禁。\n")
    _write(tmp_path, "digest/wiki/概念.md", "---\n---\n\n- 向量空间这个概念不该折叠。\n")
    _write(tmp_path, "daily/2026-10-01.md", "---\n---\n\n- 今天的现场，属于流水。\n")
    return tmp_path


def _fingerprints(root: Path) -> dict[str, str]:
    """旧文件的字节指纹（sha256；这里要的是"一个字节都没动"，不是内容哈希的强度）。"""
    return {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in _OLD_FILES}


def _run(root: Path, minute: int = 0):
    return mig.run_migration(root, now=lambda: datetime(2026, 10, 3, 15, minute))


def test_migration_folds_sources_into_sections(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    report = _run(root)

    archive = af.read_archive(root)
    by_text = {entry.text: entry for entry in archive.entries}
    assert by_text["用户是中文用户，称呼「小又」。"].section == af.SECTION_IDENTITY
    assert by_text["用户要求回答先给结论、再列依据。"].section == af.SECTION_PREFERENCES
    assert by_text["用户不看客套话。"].section == af.SECTION_PREFERENCES
    assert by_text["用户的机器是 Windows Server 2019，配一张 L20。"].section == af.SECTION_TOOLS
    assert by_text["项目「内网知识库」的目标是全部不出公网。"].section == af.SECTION_PROJECTS
    # ``digest/wiki`` 与 ``daily`` 不折叠
    rendered = (root / PROFILE).read_text(encoding="utf-8")
    assert "向量空间这个概念不该折叠。" not in rendered
    assert "今天的现场，属于流水。" not in rendered
    # 敏感形状只报条数、不进档案
    assert report.dropped_sensitive == 1
    assert "hunter2" not in rendered
    # 超长条目降级进草稿，不丢
    assert report.downgraded == 1
    draft = (root / mig.DRAFT_FILENAME).read_text(encoding="utf-8")
    assert "超长条目" in draft


#: 判据 §9.2 第 10 条（第二半）
def test_migration_keeps_source_files_byte_identical(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = _fingerprints(root)

    _run(root)

    assert _fingerprints(root) == before


#: 判据 §9.2 第 10 条（第一半）
def test_migration_is_idempotent(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _run(root, minute=0)
    first = (root / PROFILE).read_bytes()

    second = _run(root, minute=5)

    assert second.skipped
    assert (root / PROFILE).read_bytes() == first
    # 水位记下了源文件指纹（重跑的依据）
    watermark = mig.read_watermark(root)
    assert watermark.get("version") == 1
    expected = {"MEMORY.md", "digest/personal/风格.md", "digest/procedure/发布.md"}
    assert set(watermark.get("sources", {})) == expected


def test_migration_does_not_overwrite_user_entries(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _run(root)
    entries = af.read_archive(root).entries
    # 用户手工加一条，与旧条目数字相同、字面高度相似（机械判据会想顶替它）
    user_entry = af.ArchiveEntry(
        text="用户的机器是 Windows Server 2022，配一张 L40。", section=af.SECTION_TOOLS
    )
    af.write_archive(
        root,
        af.Archive(entries=(*entries, user_entry), updated="2026-10-04", unknown_sections=()),
    )
    # 源文件变一下（换成同一台机器、多两个字），逼迁移重跑
    _write(
        root,
        "MEMORY.md",
        """---
summary: 旧的核心记忆
---

## 核心长期记忆

- 用户的机器是 Windows Server 2022，配一张 L40 显卡。
""",
    )

    _run(root)

    texts = [entry.text for entry in af.read_archive(root).entries]
    # 用户那条**不被顶替**：迁移只顶替它自己搬进去的（§8.4）
    assert user_entry.text in texts
    assert "用户的机器是 Windows Server 2022，配一张 L40 显卡。" not in texts
