"""门禁脚本自己的用例：`scripts/check_layering.py` 的 U1（界面文案）与 U2 之外的判据抽查。

**为什么这一份必须有**：这条规则已经静默死过两次——
第一次是正则里的 `\\b` 被 heredoc 变成了退格符（脚本里记着这件事），
第二次是 P5 之后组件全成了 `.tsx`，而 `check_ui_copy` 只认 `.vue` / `.ts`
（规则因为"什么都没扫到"而永远绿，看输出和"代码干净"一模一样）。

所以这里钉的不是"某段代码有没有违规"，而是**规则本身会不会报**：
拿一份**故意违规**的 `.tsx` 进去，它必须报出来；干净的和注释里的不算。

（放在 `backend/tests/unit/` 是照 `test_deployment_manifests.py` 的先例：
仓库级脚本的 Python 用例也住在这里。）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

#: 本文件在 ``backend/tests/unit/`` 下，往上三层是仓库根。
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated_data_dir():  # type: ignore[no-untyped-def]
    """顶掉 conftest 里那个 autouse 的隔离夹具。

    它顺带要求一个 PostgreSQL 测试库（没有就整体 skip）；而这一份用例**不碰应用、
    不碰数据库**——只把临时文件喂给 `check_ui_copy` 看它报不报。让它们因为"没有测试库"
    而跳过，等于这道守卫在最需要它的场合（本机、没有 PG）静默不跑。
    """
    return None


def _load_checker() -> ModuleType:
    """按路径加载 `scripts/check_layering.py`（它不是包里的模块，import 不进来）。"""
    path = ROOT / "scripts" / "check_layering.py"
    spec = importlib.util.spec_from_file_location("check_layering_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    return _load_checker()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_tsx_is_actually_scanned(checker: ModuleType, tmp_path: Path) -> None:
    """`.tsx` 必须在扫描范围内——P5 之后组件全是 `.tsx`，漏了它这条规则就是死的。"""
    path = _write(tmp_path, "SomePage.tsx", '<PageShell description="这一页是什么">\n')
    violations = checker.check_ui_copy(path)
    assert violations, "起手就漏：.tsx 没被扫，U1 等于没在跑"
    assert violations[0].rule == "U1"


def test_description_on_page_shell_is_flagged(checker: ModuleType, tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Page.tsx",
        "<PageShell\n" '  title="概览"\n' '  description="这里是你的全部知识库"\n' ">\n",
    )
    violations = checker.check_ui_copy(path)
    assert [item.rule for item in violations] == ["U1"]


def test_jsx_classname_family_is_flagged(checker: ModuleType, tmp_path: Path) -> None:
    """React 写 `className=`，Vue 写 `class=`——两种都要认出来。"""
    path = _write(tmp_path, "Panel.tsx", '<p className="panel-desc muted">说明</p>\n')
    violations = checker.check_ui_copy(path)
    assert [item.rule for item in violations] == ["U1"]


def test_description_on_other_components_is_fine(checker: ModuleType, tmp_path: Path) -> None:
    """只盯 `PageShell` / `PageHeader`：别的组件上叫 description 的属性不算这条。"""
    path = _write(tmp_path, "Field.tsx", '<Input description="这一段是给读屏器的" />\n')
    assert checker.check_ui_copy(path) == []


def test_comments_are_not_flagged(checker: ModuleType, tmp_path: Path) -> None:
    """注释里提到这些词，多半正是在解释"为什么删掉它"——要留着，不算违规。"""
    path = _write(
        tmp_path,
        "Notes.tsx",
        "// 原来这里挂着 PageShell 的 description（「这一页是什么」），§12.168 删掉了\n"
        '// 也不要写成 className="page-desc"\n',
    )
    assert checker.check_ui_copy(path) == []


def test_clean_tsx_is_clean(checker: ModuleType, tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Clean.tsx",
        '<PageShell title="概览" tools={<Button>刷新</Button>}>\n  <List />\n</PageShell>\n',
    )
    assert checker.check_ui_copy(path) == []


@pytest.mark.parametrize(
    "name",
    ["Foo.test.tsx", "Foo.spec.tsx", "Foo.test.ts", "Foo.spec.ts", "test_x.py", "x_test.py"],
)
def test_t1_flags_test_files_inside_source_roots(
    checker: ModuleType, tmp_path: Path, name: str
) -> None:
    """T1：源码目录里出现测试文件要报——**`.tsx` 那也是测试文件**。

    这条正则原来只认 `.test.ts`（Vue 时代的写法），于是 `Foo.test.tsx` 放进
    `frontend/src/` 不会被拦——与 U1 那次后缀漏网是同一个病。
    """
    src = tmp_path / "frontend" / "src" / "features" / "chat"
    src.mkdir(parents=True)
    path = src / name
    path.write_text("export {}\n", encoding="utf-8")
    assert [item.rule for item in checker.check_test_placement(path, tmp_path)] == ["T1"]


def test_t1_ignores_tests_outside_source_roots(checker: ModuleType, tmp_path: Path) -> None:
    outside = tmp_path / "frontend" / "tests"
    outside.mkdir(parents=True)
    path = outside / "chat-ui.test.tsx"
    path.write_text("export {}\n", encoding="utf-8")
    assert checker.check_test_placement(path, tmp_path) == []


def test_s1_flags_a_real_ps1_without_bom(checker: ModuleType, tmp_path: Path) -> None:
    """S1 要能报：`scripts/` 下真的少了 BOM 的 .ps1（PowerShell 5.1 会当 GBK 读）。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "dev-backend.ps1").write_bytes("Write-Output '你好'\n".encode())

    assert [item.rule for item in checker.check_ps1_bom(tmp_path)] == ["S1"]


def test_s1_ignores_generated_ps1_shims(checker: ModuleType, tmp_path: Path) -> None:
    """`scripts/*/node_modules/.bin/*.ps1` 是 npm 生成的垫片：不入库，也不该报。

    起因：写盘层把 Node 脚本放在 `scripts/deck/`（`npm install` 会在那儿生成
    `.bin/image-size.ps1` 之类的垫片），S1 于是对**我们改不了的文件**报警——
    而这类误报会让真正的违规淹在噪音里（`scan_emoji.py` 的 `SKIP_DIRS` 是同一条处置）。
    """
    shim = tmp_path / "scripts" / "deck" / "node_modules" / ".bin"
    shim.mkdir(parents=True)
    (shim / "image-size.ps1").write_bytes(b"#!/usr/bin/env node\n")

    assert checker.check_ps1_bom(tmp_path) == []
