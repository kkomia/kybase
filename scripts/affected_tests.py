"""按「这次改了什么」算出该跑哪些用例——把工程规范 §5.2.1 机械化。

**为什么需要它**：规范早就写明"改一处只跑受影响的那几个文件"，但受影响的范围
**没有人能可靠地手算**：

- 后端还能靠镜像同构（``app/services/memory.py`` → ``tests/unit/services/test_memory.py``），
  可"谁 import 了它"得靠 grep；改动落在被广泛引用的模块上时，手算必漏；
- 前端根本没有镜像关系（``ModelRegistryPanel.tsx`` 的断言躺在 ``misc-registry.test.tsx``
  里），规范自己记着 2026-09-27 漏过一次：只跑了 ``misc-settings``，而钉住那次改动的
  断言在 ``misc-registry`` 里，**直到收尾跑全量才红**。

手算既然会漏，就别手算。这个脚本把两件事一起做掉：

1. 建**反向依赖图**（谁 import 了谁），从改动文件出发取闭包——这正好补上规范里
   那句"外加直接依赖它的调用方"；
2. 把闭包映射到用例文件（后端：镜像同名 + import；前端：``@/...`` 说明符 + 组件名）。

**几道"宁滥勿缺"的保险**（漏测的代价远大于多跑几个文件）：

- 改动落在枢纽文件（``main.py`` / ``config.py`` / ``services.py`` / ``storage.py`` /
  ``conftest.py`` / ``pyproject.toml`` / ``vite.config.ts`` …）→ 直接判全量，
  它们的爆炸半径本来就是全部；
- 受影响的用例超过阈值（默认 60%）→ 也判全量，不为省几分钟去赌漏测；
- **判全量时自动并行**（pytest-xdist，装了才用）：这台 16 核机器上全量实测
  串行 749s → ``-n 8`` 166s（4.5x，全绿）。定向跑不并行——起 worker 比省的还贵；
- 前端相对导入与 ``vi.mock('@/…')`` 都算依赖；解析不出来的说明符按"可能影响"放行。

用法：

    python scripts/affected_tests.py                 # 只报告，不跑
    python scripts/affected_tests.py --run           # 真跑（后端 pytest + 前端 vitest）
    python scripts/affected_tests.py --base origin/main --run
    python scripts/affected_tests.py --all --run     # 强制全量
    python scripts/affected_tests.py --json          # 给别的脚本消费

包装脚本（会顺带把测试库连接串从 ``backend/.env`` 借过来）：
``scripts/test-changed.sh`` / ``scripts/test-changed.ps1``。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
PY = BACKEND / ".venv" / "Scripts" / "python.exe"
if not PY.is_file():
    PY = BACKEND / ".venv" / "bin" / "python"

#: git 的绝对路径：写 "git" 会拿到 PATH 上的任意一份（bandit 的 S607 问的就是这个）。
GIT = shutil.which("git") or "git"

# ---------------------------------------------------------------- 忽略与枢纽

#: 这些目录名出现在路径里就**不参与**影响面计算：缓存、依赖、生成物。
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".cache",
        ".venv",
        ".ruff_cache",
        ".pytest_cache",
        ".pnpm-store",
        ".shots",
        ".workflow",
        ".playwright-mcp",
        ".zcode",
        "__pycache__",
        "node_modules",
        "dist",
    }
)

#: 临时目录的命名族。它们本不该待在仓库里：这台机器上 ``tempfile.gettempdir()``
#: 会**回退到 cwd**（系统临时目录不可写），于是 ``gen_api_spec.py`` 的
#: ``mkdtemp(prefix="kylab-openapi-")`` 与 pytest 的 basetemp 都落在仓库根。
#: 不把它们算成"改动"，否则每次跑门禁都会把范围判成全量。
IGNORED_NAME_RE = re.compile(r"^(kylab-openapi-|pytest-of-|_iobench_|tmp[a-z0-9_]{8}$)")

#: 后端枢纽模块：改它们等于改全局，直接判全量（不靠闭包去推）。
BACKEND_HUBS = frozenset(
    {
        "app.main",
        "app.core.config",
        "app.core.services",
        "app.core.storage",
        "app.core.logging",
        "app.core.exceptions",
        "app.core.security",
    }
)

#: 前端枢纽文件（相对 ``frontend/``）：构建/测试基建，改它们全部用例都可能受影响。
FRONTEND_HUBS = frozenset(
    {
        "vite.config.ts",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "eslint.config.js",
        "tsconfig.json",
        "tsconfig.app.json",
        "tsconfig.node.json",
        "index.html",
        "tests/setup.ts",
    }
)

#: 反向依赖**走到这里就不再外扩**的模块（组合根/枢纽）。
#:
#: 后端的依赖最终都会汇到组合根，而"谁都要用组合根"：不设这道闸，完整闭包会把
#: 所有走 ``create_app`` 的用例全部算成受影响（实测改一个 ``app/services/memory.py``
#: 就命中 54/167 个用例文件，定向等于白做）。
#:
#: 停在这里**不丢保护**：这几个模块自己作为**改动方**出现时走的是 ``BACKEND_HUBS``
#: 那条"直接判全量"——比顺着它们往下推更严。
WALK_STOP = frozenset(BACKEND_HUBS | {"app.main"})

#: 反向依赖的外扩跳数。1 跳 = 只算直接 import 它的（规范的字面口径）；
#: 2 跳多覆盖一层"经中间模块转手的调用方"。再往上收益很低、误伤很大。
DEFAULT_HOPS = 2

#: 后端用例文件的通配；与工程规范 §5.1 的命名铁律一致。
BACKEND_TEST_GLOB = "test_*.py"
#: 前端用例文件（``vite.config.ts`` 的 test.include 口径）。
FRONTEND_TEST_GLOBS = ("*.test.ts", "*.test.tsx")


# ---------------------------------------------------------------- 小工具


def is_ignored(rel_posix: str) -> bool:
    """这个相对路径要不要排除在影响面之外（缓存/生成物/临时目录）。"""
    parts = rel_posix.split("/")
    if any(part in IGNORED_DIRS for part in parts):
        return True
    return any(IGNORED_NAME_RE.match(part) for part in parts)


def writable_temp() -> str:
    """挑一个**真的能写**的临时目录给子进程用。

    这台机器上 ``TEMP`` 指向一个不可写的位置，后果有两条，都很难归因：

    1. Python 的 ``tempfile.gettempdir()`` 静默回退到 **cwd**，于是 pytest 的
       ``tmp_path`` 与 ``mkdtemp(prefix="kylab-openapi-")`` 全落进仓库根
       （本仓库根上已经躺了 47 个 ``kylab-openapi-*`` 与两个 ``pytest-of-*``，
       都是这么来的，而 ``.gitignore`` 只挡住了后者）；
    2. vitest 建不了它的临时目录，直接 ``EPERM: mkdir`` —— 前端用例一个都跑不起来。

    两条都不该由"跑测试的人"去修。这里挑一个确定可写的位置兜住：候选顺序是
    环境变量给的 TEMP/TMP、仓库内的 ``.tmp/``（2026-09-29 起的统一落点，见
    ``.tmp/README.md``），最后退回 ``.cache/tmp``（``.cache/`` 也在 .gitignore 里，
    所以哪一级回退都不会脏工作区）。
    """
    candidates = [
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        str(REPO / ".tmp"),
        str(REPO / ".cache" / "tmp"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        probe = path / f".kylab_write_probe_{os.getpid()}"
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe.write_text("x", encoding="utf-8")
        except OSError:
            continue
        with contextlib.suppress(OSError):
            probe.unlink()
        return str(path)
    return str(REPO / ".cache" / "tmp")


def child_env() -> dict[str, str]:
    """子进程的环境：把临时目录钉到 ``writable_temp()``（三个变量一起给，见上）。"""
    tmp = writable_temp()
    return {**os.environ, "TEMP": tmp, "TMP": tmp, "TMPDIR": tmp}


def run_git(args: list[str]) -> list[str]:
    """跑一条 git 命令，返回非空行。git 不可用或失败时抛 ``RuntimeError``。"""
    # S603 是误报：参数由本文件的常量拼成，不经过 shell，没有注入面。
    proc = subprocess.run(  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def run_git_z(args: list[str]) -> list[str]:
    """跑 git 并**按 NUL 切**文件名。

    ``--name-only`` 默认会把非 ASCII 路径转义成 ``"docs/\\350\\260..."`` 那种八进制
    （``core.quotepath`` 默认开），带空格的路径还会被引号包起来——两者都会让路径匹配
    静默失配。``-z`` 是唯一不用猜编码的做法。
    """
    proc = subprocess.run(  # noqa: S603 - 同 run_git，参数是本文件拼的
        [GIT, *args, "-z"], cwd=REPO, capture_output=True, encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return [chunk.replace("\\", "/") for chunk in proc.stdout.split("\x00") if chunk.strip()]


def changed_files(base: str, *, include_untracked: bool = True) -> list[str]:
    """相对 ``base`` 发生改动的文件（含已暂存、未暂存，以及未跟踪的新文件）。"""
    names: set[str] = set()
    # --diff-filter=ACMRD：增/改/重命名/删除都留着。删除的文件没有内容可分析，
    # 但它可能正是某个用例断言的对象——映射时找不到文件会自然忽略。
    for name in run_git_z(["diff", "--name-only", "--diff-filter=ACMRD", base]):
        names.add(name)
    if include_untracked:
        for name in run_git_z(["ls-files", "--others", "--exclude-standard"]):
            names.add(name)
    return sorted(name for name in names if not is_ignored(name))


# ---------------------------------------------------------------- 后端依赖图


def _resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """把 ``from .x import y`` 还原成点分绝对模块名（与 check_layering 同一套算法）。"""
    package = module.rsplit(".", 1)[0] if "." in module else module
    parts = package.split(".")
    base_parts = parts[: len(parts) - node.level + 1]
    base = ".".join(base_parts) if base_parts else package
    return f"{base}.{node.module}" if node.module else base


def python_imports(path: Path, module: str, known: set[str]) -> set[str]:
    """一个 Python 文件 import 到的**已知**模块集合。

    ``from app.services import memory`` 这种写法要同时记下 ``app.services`` 与
    ``app.services.memory``：前者说明"依赖了这个包"，后者才是真正被引用的模块——
    只记前者会让闭包停在中途，从而漏掉调用方。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_relative(module, node) if node.level else (node.module or "")
            if target in known:
                found.add(target)
            for alias in node.names:
                deeper = f"{target}.{alias.name}"
                if deeper in known:
                    found.add(deeper)
    return found


def backend_module_of(path: Path) -> str:
    """``backend/app/services/memory.py`` → ``app.services.memory``（``__init__`` 折叠掉）。"""
    rel = path.relative_to(BACKEND).with_suffix("")
    parts = [part for part in rel.parts if part != "__init__"]
    return ".".join(parts)


@lru_cache(maxsize=1)
def build_backend_graph() -> tuple[
    dict[str, set[str]], dict[str, Path], dict[str, Path], dict[str, set[str]]
]:
    """建后端依赖图。

    返回 ``(反向依赖, app 模块→文件, 用例模块→文件, 用例模块→它 import 的模块)``。

    用例自己的 import 也要进图：改动 ``tests/conftest.py`` 这类共享夹具时，
    要能顺着"谁 import 了它"辐射到用例。
    """
    modules: dict[str, Path] = {}
    for path in sorted((BACKEND / "app").rglob("*.py")):
        modules[backend_module_of(path)] = path
    tests: dict[str, Path] = {}
    for path in sorted((BACKEND / "tests").rglob(BACKEND_TEST_GLOB)):
        tests[backend_module_of(path)] = path

    # `tests.conftest` 不在 `tests` 字典里（它不是 test_*.py），但用例普遍 import 它，
    # 漏掉它等于"改了共享夹具却算不出影响面"。
    known = set(modules) | set(tests) | {"tests.conftest"}

    imported_by: dict[str, set[str]] = {}
    for module, path in modules.items():
        for target in python_imports(path, module, known):
            imported_by.setdefault(target, set()).add(module)

    test_imports: dict[str, set[str]] = {}
    for module, path in tests.items():
        test_imports[module] = python_imports(path, module, known)
        for target in test_imports[module]:
            imported_by.setdefault(target, set()).add(module)
    return imported_by, modules, tests, test_imports


def closure(seeds: set[str], edges: dict[str, set[str]]) -> set[str]:
    """在 ``edges``（被依赖 → 依赖它的）上取可达闭包。"""
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        node = stack.pop()
        for parent in edges.get(node, ()):  # type: ignore[arg-type]
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def dependents(
    seeds: set[str],
    edges: dict[str, set[str]],
    *,
    hops: int,
    stop: frozenset[str],
) -> set[str]:
    """反向依赖图上的"谁依赖它"，**最多 ``hops`` 跳，且不在 ``stop`` 里继续外扩**。

    为什么不取完整闭包：后端的依赖最终都会汇到组合根（``app/core/services.py``、
    ``app/main.py``），而"谁都要用组合根"。完整闭包的结果是所有走 ``create_app`` 的
    用例全部命中——实测改一个 ``app/services/memory.py`` 就命中 54/167 个用例文件，
    定向等于白做。

    停在这些模块上不丢东西：它们**自己作为改动方**出现时走的是
    ``BACKEND_HUBS`` 那条"直接判全量"，比顺着它们往下推更严。
    """
    frontier = set(seeds)
    found: set[str] = set()
    for _ in range(hops):
        nxt: set[str] = set()
        for node in frontier:
            for parent in edges.get(node, ()):  # type: ignore[arg-type]
                if parent in seeds or parent in found or parent in stop:
                    continue
                found.add(parent)
                nxt.add(parent)
        frontier = nxt
        if not frontier:
            break
    return found


# ---------------------------------------------------------------- 前端依赖图

#: ``'@/x/y'`` 形态的说明符（import / vi.mock / require 都长这样）。
SPECIFIER_RE = re.compile(r"""['"](@/[A-Za-z0-9_./\-]+)['"]""")


def frontend_specifier(path: Path) -> str:
    """``frontend/src/features/x/Y.tsx`` → ``@/features/x/Y``。"""
    rel = path.relative_to(FRONTEND / "src").with_suffix("").as_posix()
    return f"@/{rel}"


def resolve_specifier(spec: str, known: dict[str, Path]) -> Path | None:
    """把 ``@/a/b`` 落到具体文件（依次试 ``.ts`` / ``.tsx`` / ``index.*``）。"""
    if not spec.startswith("@/"):
        return None
    stem = spec[2:]
    for candidate in (
        f"{stem}.ts",
        f"{stem}.tsx",
        f"{stem}/index.ts",
        f"{stem}/index.tsx",
    ):
        if candidate in known:
            return known[candidate]
    return None


def frontend_imports(path: Path, known: dict[str, Path]) -> set[str]:
    """一个前端文件依赖的源文件（``@/`` 别名 + 相对导入）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    found: set[str] = set()
    for spec in SPECIFIER_RE.findall(text):
        target = resolve_specifier(spec, known)
        if target is not None:
            found.add(target.relative_to(FRONTEND / "src").as_posix())
    # 相对导入：`./parts/Foo` / `../shared/x`
    for raw in re.findall(r"""['"](\.[^'"]+)['"]""", text):
        base = (path.parent / raw).resolve()
        for ext in (".ts", ".tsx"):
            candidate = base.with_suffix(ext)
            if candidate.is_file() and candidate.is_relative_to(FRONTEND / "src"):
                found.add(candidate.relative_to(FRONTEND / "src").as_posix())
                break
        else:
            for index in ("index.ts", "index.tsx"):
                candidate = base / index
                if candidate.is_file() and candidate.is_relative_to(FRONTEND / "src"):
                    found.add(candidate.relative_to(FRONTEND / "src").as_posix())
                    break
    return found


@lru_cache(maxsize=1)
def build_frontend_graph() -> tuple[dict[str, Path], dict[str, set[str]]]:
    """返回 ``(相对 src 的路径 → 文件, 反向依赖)``。

    ``lru_cache``：建图要把 180 多个源文件读一遍，而 ``decide()`` 在一次进程里
    可能被调用很多次（用例里就是这样）。CLI 只调一次，缓存不会掩盖文件改动。
    """
    known: dict[str, Path] = {}
    for path in sorted((FRONTEND / "src").rglob("*")):
        if path.is_file() and path.suffix in (".ts", ".tsx"):
            known[path.relative_to(FRONTEND / "src").as_posix()] = path

    imported_by: dict[str, set[str]] = {}
    for rel, path in known.items():
        for target in frontend_imports(path, known):
            imported_by.setdefault(target, set()).add(rel)
    return known, imported_by


# ---------------------------------------------------------------- 判定


def decide(
    changed: list[str],
    *,
    threshold: float,
    hops: int = DEFAULT_HOPS,
) -> dict:
    """算出这次改动会影响的后端/前端用例文件。返回一份可 JSON 化的结果。"""
    reason_backend: list[str] = []
    reason_frontend: list[str] = []

    backend_src = [name for name in changed if name.startswith("backend/app/")]
    backend_tests = [name for name in changed if name.startswith("backend/tests/")]
    frontend_src = [name for name in changed if name.startswith("frontend/src/")]
    frontend_tests = [name for name in changed if name.startswith("frontend/tests/")]
    others = [
        name
        for name in changed
        if name
        not in set(backend_src + backend_tests + frontend_src + frontend_tests)
    ]

    imported_by, _modules, tests, test_imports = build_backend_graph()
    _, fe_imported_by = build_frontend_graph()

    all_backend = {path.relative_to(BACKEND).as_posix() for path in tests.values()}
    all_frontend = {
        path.relative_to(FRONTEND).as_posix()
        for glob in FRONTEND_TEST_GLOBS
        for path in (FRONTEND / "tests").glob(glob)
    }

    # 改动清单里的是**仓库相对**的 POSIX 路径；凡是拿它拼文件系统路径，
    # 都必须从 REPO 起算（`BACKEND / "backend/app/x.py"` 会拼成 backend\backend\...，
    # 得到一个静默不存在的路径——这类拼错不会报错，只会让判定悄悄变成"没有影响"）。
    def as_repo_file(name: str) -> Path:
        return REPO / name

    # ---- 后端
    hit_backend: dict[str, str] = {}
    force_backend = False

    hub_hits = sorted(
        name for name in backend_src if backend_module_of(as_repo_file(name)) in BACKEND_HUBS
    )
    if hub_hits:
        force_backend = True
        reason_backend.append(f"枢纽模块（改动即全量）：{'、'.join(hub_hits)}")
    if "backend/tests/conftest.py" in changed:
        force_backend = True
        reason_backend.append("共享夹具 backend/tests/conftest.py（所有用例都依赖它）")
    if any(name in ("backend/pyproject.toml", "backend/uv.lock") for name in changed):
        force_backend = True
        reason_backend.append("后端依赖清单变了")

    if not force_backend:
        # 1) 镜像同构：app/services/memory.py → tests/**/test_memory.py
        for name in backend_src:
            stem = Path(name).stem
            for rel in sorted(all_backend):
                if Path(rel).name == f"test_{stem}.py":
                    hit_backend.setdefault(rel, f"镜像 {name}")
        # 2) 反向依赖："谁 import 了它"（对应规范里那句"外加直接依赖它的调用方"）。
        #    最多两跳、不在组合根继续外扩——理由见 dependents()。
        seeds = {backend_module_of(as_repo_file(name)) for name in backend_src}
        affected = seeds | dependents(seeds, imported_by, hops=hops, stop=WALK_STOP)
        for rel in sorted(all_backend):
            module = backend_module_of(BACKEND / rel)
            overlap = sorted(test_imports.get(module, set()) & affected)
            if overlap:
                hit_backend.setdefault(rel, f"依赖 {overlap[0]}（反向闭包）")
        # 3) 用例文件自己改了 → 跑它自己
        for name in backend_tests:
            rel = as_repo_file(name).relative_to(BACKEND).as_posix()
            if rel in all_backend:
                hit_backend.setdefault(rel, "用例自身有改动")

    # ---- 前端
    hit_frontend: dict[str, str] = {}
    force_frontend = False

    fe_hub_hits = sorted(
        name.removeprefix("frontend/") for name in changed
        if name.removeprefix("frontend/") in FRONTEND_HUBS
    )
    if fe_hub_hits:
        force_frontend = True
        reason_frontend.append(f"前端枢纽文件（改动即全量）：{'、'.join(fe_hub_hits)}")

    fe_test_specs: dict[str, set[str]] = {}
    fe_test_text: dict[str, str] = {}
    for rel in sorted(all_frontend):
        text = (FRONTEND / rel).read_text(encoding="utf-8", errors="replace")
        fe_test_text[rel] = text
        fe_test_specs[rel] = set(SPECIFIER_RE.findall(text))

    if not force_frontend:
        seeds = {
            as_repo_file(name).relative_to(FRONTEND / "src").as_posix()
            for name in frontend_src
            if as_repo_file(name).is_file()
        }
        affected = closure(seeds, fe_imported_by) | seeds
        wanted = {frontend_specifier(FRONTEND / "src" / rel) for rel in affected}
        for rel in sorted(all_frontend):
            overlap = sorted(fe_test_specs[rel] & wanted)
            if overlap:
                hit_frontend.setdefault(rel, f"用到 {overlap[0]}")
        # 兜底：用例可能在注释/字符串里点名组件（或经 barrel 转出，说明符对不上）
        for name in frontend_src:
            token = Path(name).stem
            for rel in sorted(all_frontend):
                if rel not in hit_frontend and token in fe_test_text[rel]:
                    hit_frontend.setdefault(rel, f"点名了 {token}")
        for name in frontend_tests:
            rel = name.removeprefix("frontend/")
            if rel in all_frontend:
                hit_frontend.setdefault(rel, "用例自身有改动")

    # 改的东西不在 app/src/tests 里（文档、脚本、部署清单……）：
    # 与测试有关的那一类走"点名"规则——例如 scripts/check_layering.py 由
    # tests/unit/test_check_layering.py 按路径加载，这条关系不体现在 import 图上。
    for name in others:
        if name.startswith("frontend/"):
            continue
        if name.startswith("scripts/"):
            # 两种写法都认：**仓库相对路径**（``scripts/deck/render.mjs``——用例按路径加载
            # 脚本时点名写的就是它）与"scripts/ + 文件名"（顶层脚本那种写法）。
            # 只认后者的后果是``scripts/`` 下**带子目录**的脚本静默失配：改了写盘脚本，
            # 钉住它的用例不跑，而"没跑"与"跑了且绿"看起来一模一样。
            tokens = (name, f"scripts/{Path(name).name}")
            for rel in sorted(all_backend):
                if rel in hit_backend:
                    continue
                text = (BACKEND / rel).read_text(encoding="utf-8", errors="replace")
                if any(token in text for token in tokens):
                    hit_backend.setdefault(rel, f"点名了 {name}")

    # ---- 阈值保险
    if not force_backend and all_backend:
        ratio = len(hit_backend) / len(all_backend)
        if ratio > threshold:
            force_backend = True
            reason_backend.append(
                f"受影响用例占比 {ratio:.0%} > 阈值 {threshold:.0%}，判全量更划算"
            )
    if not force_frontend and all_frontend:
        ratio = len(hit_frontend) / len(all_frontend)
        if ratio > threshold:
            force_frontend = True
            reason_frontend.append(
                f"受影响用例占比 {ratio:.0%} > 阈值 {threshold:.0%}，判全量更划算"
            )

    return {
        "base_changed": changed,
        "backend": {
            "force_all": force_backend,
            "reasons": reason_backend,
            "targets": sorted(hit_backend),
            "why": hit_backend,
            "total": len(all_backend),
        },
        "frontend": {
            "force_all": force_frontend,
            "reasons": reason_frontend,
            "targets": sorted(hit_frontend),
            "why": hit_frontend,
            "total": len(all_frontend),
        },
    }


# ---------------------------------------------------------------- 报告与执行


def report(decision: dict, *, base: str, show_all: bool) -> None:
    """把判定结果打成人能读的一段。"""
    print(f"影响面判定（基准 {base}）")
    print(f"  改动 {len(decision['base_changed'])} 个文件")
    for name in decision["base_changed"]:
        print(f"    {name}")
    print()

    for side, label in (("backend", "后端"), ("frontend", "前端")):
        info = decision[side]
        if info["force_all"]:
            print(f"{label}：**全量**（{info['total']} 个用例文件）")
        else:
            print(f"{label}：定向 {len(info['targets'])} / {info['total']} 个用例文件")
        for reason in info["reasons"]:
            print(f"    理由：{reason}")
        listed = info["targets"] if show_all else info["targets"][:8]
        for rel in listed:
            print(f"    {rel}        <- {info['why'][rel]}")
        if not show_all and len(info["targets"]) > 8:
            print(f"    …（还有 {len(info['targets']) - 8} 个，用 --list 看全）")
        if not info["targets"] and not info["force_all"]:
            print(f"    （没有受影响的{label}用例）")
        print()


def default_workers() -> int:
    """判全量时的并行度：**本机实测过的那一档**，而不是 xdist 的 ``auto``。

    这台 16 核机器上，全量 3220 条用例实测：

    ==========  =========  ========
    跑法         墙钟        倍率
    ==========  =========  ========
    串行         749s       —
    ``-n 8``    166s       4.5x（全绿，零失败）
    ==========  =========  ========

    瓶颈是**共用的 NAS PostgreSQL**（xdist 每个 worker 建一个自己的临时库，
    见 ``conftest.py`` 的会话级 ``pg_database``）：连接、清库、DDL 全压在同一台库上。
    所以并行度不是越高越好——一半的核在跑应用代码，另一半在等库往返。
    ``-n auto``（=16）会把 16 份连接与 16 个临时库压上去，收益未必更高、风险更大，
    所以默认取 ``max(2, cpu // 2)``，与本机实测的那一档一致；要调就用 ``--extra``。
    """
    return max(2, (os.cpu_count() or 4) // 2)


def pytest_argv(decision: dict, extra: list[str], *, parallel: bool) -> list[str]:
    """拼后端 pytest 命令行。"""
    info = decision["backend"]
    targets = (
        ["tests"]
        if info["force_all"]
        else [str(BACKEND / rel) for rel in info["targets"]]
    )
    argv = [str(PY), "-m", "pytest", *targets, "-m", "not bench and not cloud", "-q"]
    # 只在**判全量**时并行：定向跑的通常就几个文件，起 worker 的开销比省下的还多。
    # 另外 xdist 装没装要现问——没装时加上 -n 会让 pytest 直接报"不认识这个参数"。
    if parallel and info["force_all"] and xdist_available():
        argv += ["-n", str(default_workers())]
    return argv + extra


def xdist_available() -> bool:
    """venv 里装了 pytest-xdist 吗（装在 dev 组里，见 pyproject 的 dependency-groups）。"""
    if not PY.is_file():
        return False
    probe = subprocess.run(  # noqa: S603 - 参数是常量
        [str(PY), "-c", "import xdist"],
        capture_output=True,
        env=child_env(),
    )
    return probe.returncode == 0


def vitest_argv(decision: dict, extra: list[str]) -> list[str] | None:
    """拼前端 vitest 命令行；vitest 不可用返回 None。"""
    info = decision["frontend"]
    files = [] if info["force_all"] else [str(FRONTEND / rel) for rel in info["targets"]]
    # 优先直接调 node 跑 vitest：本机 pnpm 只是个会报 "pnpm not found" 的壳，
    # 而 node_modules 里的 vitest 与 `pnpm test` 是同一份，行为一致且不受壳影响。
    vitest = FRONTEND / "node_modules" / "vitest" / "vitest.mjs"
    node = shutil.which("node")
    if node and vitest.is_file():
        return [node, str(vitest), "run", *files, *extra]
    if shutil.which("pnpm"):
        return ["pnpm", "test", "--", *files, *extra]
    return None


def execute(decision: dict, *, parallel: bool, extra: list[str], dry_run: bool) -> int:
    """跑受影响的后端/前端用例，返回合并后的退出码。"""
    if dry_run:
        print("（--dry-run：只列命令，不执行）")
        print("  后端：", " ".join(pytest_argv(decision, extra, parallel=parallel)))
        argv = vitest_argv(decision, extra)
        print("  前端：", " ".join(argv) if argv else "（找不到 vitest）")
        return 0

    failed = 0

    backend_info = decision["backend"]
    if backend_info["force_all"] or backend_info["targets"]:
        if not os.environ.get("KYLAB_TEST_DATABASE_URL"):
            print("!! 未设置 KYLAB_TEST_DATABASE_URL：需要 PostgreSQL 的用例会整体跳过，")
            print("   门禁会变成'绿得没有意义'。用 scripts/test-changed.sh 跑（它会从 .env 借）。")
        argv = pytest_argv(decision, extra, parallel=parallel)
        print(f"==> {' '.join(argv[:6])} …")
        sys.stdout.flush()
        # 参数是本文件拼的；env 里钉住临时目录，见 writable_temp()
        code = subprocess.run(  # noqa: S603
            argv, cwd=BACKEND, env=child_env()
        ).returncode
        if code != 0:
            failed += 1
            print(f"!! 后端用例失败（exit {code}）")
    else:
        print("==> 后端：没有受影响的用例，跳过")

    frontend_info = decision["frontend"]
    if frontend_info["force_all"] or frontend_info["targets"]:
        argv = vitest_argv(decision, extra)
        if argv is None:
            print("!! 找不到 vitest（node_modules 没装？），跳过前端")
        else:
            print(f"==> {' '.join(argv[:6])} …")
            sys.stdout.flush()
            # 参数是本文件拼的；env 里钉住临时目录，见 writable_temp()
            code = subprocess.run(  # noqa: S603
                argv, cwd=FRONTEND, env=child_env()
            ).returncode
            if code != 0:
                failed += 1
                print(f"!! 前端用例失败（exit {code}）")
    else:
        print("==> 前端：没有受影响的用例，跳过")

    print()
    print("全绿" if failed == 0 else f"有 {failed} 项失败")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="算出（并可执行）本次改动影响到的用例",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="HEAD",
                        help="比较基准（默认 HEAD，即未提交的改动；也常用 origin/main、HEAD~3）")
    parser.add_argument("--all", action="store_true", help="强制全量")
    parser.add_argument("--run", action="store_true", help="真的跑（默认只报告）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--list", action="store_true", help="把命中的用例全部列出来")
    parser.add_argument("--threshold", type=float, default=0.6,
                        help="受影响用例占比超过它就直接判全量（默认 0.6）")
    parser.add_argument("--hops", type=int, default=DEFAULT_HOPS,
                        help=f"后端反向依赖外扩几跳（默认 {DEFAULT_HOPS}；1 = 只算直接调用方）")
    parser.add_argument("--no-parallel", action="store_true",
                        help="判全量时也不用 pytest-xdist（默认装了就用）")
    parser.add_argument("--extra", default="", help="透传给 pytest/vitest 的额外参数")
    args = parser.parse_args()

    try:
        changed = changed_files(args.base)
    except RuntimeError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2

    if not changed and not args.all:
        print(f"相对 {args.base} 没有改动（工作区是干净的）。")
        print("要按分支/更早的基准比较：--base origin/main、--base HEAD~3")
        return 0

    decision = decide(changed, threshold=args.threshold, hops=args.hops)
    if args.all:
        decision["backend"].update(force_all=True, reasons=["--all"], targets=[])
        decision["frontend"].update(force_all=True, reasons=["--all"], targets=[])

    if args.json:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        return 0

    report(decision, base=args.base, show_all=args.list)
    extra = args.extra.split() if args.extra else []
    if args.run or args.dry_run:
        return execute(decision, parallel=not args.no_parallel, extra=extra, dry_run=args.dry_run)

    print("（只报告，没跑。加 --run 执行；加 --dry-run 只看命令）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
