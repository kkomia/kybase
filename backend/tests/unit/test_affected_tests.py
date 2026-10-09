"""``scripts/affected_tests.py`` 自己的用例：钉住"改了 X 必须跑到 Y"这张表。

**为什么这一份必须有**：那个脚本存在的唯一理由是"别让人手算影响面"——而它一旦算错，
错的形状恰恰是最坏的一种：**静默漏跑**。门禁全绿、CI 也绿，问题留到收尾跑全量才红
（规范 §5.2.1 记着 2026-09-27 就是这么漏的：只跑了 `misc-settings`，
钉住那次改动的断言其实在 `misc-registry` 里）。

所以这里钉的不是"脚本能跑"，而是几个**已知答案**：

1. 镜像同构（后端的第一判据）；
2. 前端**没有**镜像关系——`ModelRegistryPanel.tsx` 的断言必须能找到
   `misc-registry.test.tsx`（就是上面那次漏网，钉住它等于钉住这个脚本的立项理由）；
3. 枢纽与共享夹具必须判全量（宁滥勿缺的那道保险）；
4. 定向的结果必须**真的比全量小**——否则"定向"只是个说法。

（放在 `backend/tests/unit/` 是照 `test_check_layering.py` 与
`test_deployment_manifests.py` 的先例：仓库级脚本的 Python 用例也住在这里。）
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

    它顺带要求一个 PostgreSQL 测试库（没有就整体 skip），而这一份用例只读文件、
    不碰应用、不碰数据库。让它因为"没有测试库"而跳过，等于这道守卫在最需要它的
    场合（本机、没连 PG）静默不跑——与 `test_check_layering.py` 同一条理由。
    """
    return None


@pytest.fixture(scope="module")
def affected() -> ModuleType:
    """按路径加载 ``scripts/affected_tests.py``（它不是包里的模块，import 不进来）。"""
    path = ROOT / "scripts" / "affected_tests.py"
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backend(module: ModuleType, changed: list[str]) -> dict:
    return module.decide(changed, threshold=0.6)["backend"]


def _frontend(module: ModuleType, changed: list[str]) -> dict:
    return module.decide(changed, threshold=0.6)["frontend"]


# ------------------------------------------------------------------ 后端

def test_mirrored_unit_test_is_always_in_scope(affected: ModuleType) -> None:
    """镜像同构：``app/services/memory.py`` → ``tests/unit/services/test_memory.py``。"""
    info = _backend(affected, ["backend/app/services/memory.py"])

    assert "tests/unit/services/test_memory.py" in info["targets"]
    assert not info["force_all"], "单个服务模块的改动不该判全量"


def test_mirrored_integration_test_is_always_in_scope(affected: ModuleType) -> None:
    """集成侧镜像：``storage/postgres_impl/meta_store.py`` 找到集成那份镜像。"""
    info = _backend(affected, ["backend/app/storage/postgres_impl/meta_store.py"])

    assert "tests/integration/storage/test_meta_store.py" in info["targets"]


def test_changing_a_test_file_runs_that_file(affected: ModuleType) -> None:
    """用例自己改了 → 跑它自己（这条最容易漏，改断言时最需要它）。"""
    info = _backend(affected, ["backend/tests/unit/services/test_memory.py"])

    assert info["targets"] == ["tests/unit/services/test_memory.py"]


def test_repo_level_script_is_found_by_name(affected: ModuleType) -> None:
    """``scripts/`` 下的脚本不在 import 图上，靠"用例点名了它"兜住。

    ``tests/unit/test_check_layering.py`` 是按**路径**加载那个脚本的，
    改脚本不跑这条用例，等于门禁脚本坏了也没人知道。
    """
    info = _backend(affected, ["scripts/check_layering.py"])

    assert "tests/unit/test_check_layering.py" in info["targets"]


def test_script_in_a_subdirectory_of_scripts_is_found_by_its_path(
    affected: ModuleType,
) -> None:
    """``scripts/`` 下**带子目录**的脚本要按仓库相对路径找到它的用例（2026-10-05 补）。

    这是一个真实缺口的钉子：原先只拿文件名拼 ``scripts/render.mjs``，
    于是 ``scripts/deck/render.mjs`` 这条路**永远匹配不上**——改了写盘脚本，
    钉住它的 ``tests/unit/services/deck/test_render.py`` 不跑，而"没跑"与
    "跑了且绿"看起来完全一样。
    """
    info = _backend(affected, ["scripts/deck/render.mjs"])

    assert "tests/unit/services/deck/test_render.py" in info["targets"]


# ------------------------------------------------------------------ 前端

def test_frontend_panel_maps_to_the_file_that_actually_pins_it(affected: ModuleType) -> None:
    """2026-09-27 那次漏网的复现：断言在 ``misc-registry`` 里，不在 ``misc-settings`` 里。

    前端**没有**镜像关系，所以这条只能靠依赖图 + 说明符匹配。它红了就说明
    "定向"重新变回了猜。
    """
    info = _frontend(
        affected, ["frontend/src/features/misc/settings/ModelRegistryPanel.tsx"]
    )

    assert "tests/misc-registry.test.tsx" in info["targets"]


def test_frontend_component_pulls_in_its_page_and_shell_tests(affected: ModuleType) -> None:
    """改一个聊天输入组件，用到它的页面/外壳用例要跟着跑。"""
    info = _frontend(
        affected,
        [
            "frontend/src/features/chat/ui/Composer.tsx",
            "frontend/src/features/chat/ui/ComposerControls.tsx",
        ],
    )

    assert "tests/chat-paste-upload.test.tsx" in info["targets"], "直接 import Composer 的那条"
    assert "tests/chat-ui.test.tsx" in info["targets"], "经 ChatPage 转手的那条"


# ------------------------------------------------------------------ 保险

@pytest.mark.parametrize(
    "changed",
    [
        ["backend/app/core/config.py"],
        ["backend/app/core/services.py"],
        ["backend/app/core/storage.py"],
        ["backend/tests/conftest.py"],
    ],
)
def test_hubs_and_shared_fixtures_force_the_full_suite(
    affected: ModuleType, changed: list[str]
) -> None:
    """枢纽模块与共享夹具：改动即全量（不靠闭包去推，推不准）。"""
    info = _backend(affected, changed)

    assert info["force_all"], f"{changed} 应当判全量"


def test_targeted_scope_is_actually_smaller_than_the_full_suite(affected: ModuleType) -> None:
    """定向必须真的更小：一个模块的改动不该拖着整份套件跑。

    没有这一条，"定向"可以在某次改动后悄悄退化成全量而没人发觉——
    表现只是"又变慢了"，而慢是最不容易被当成 bug 的那类问题。
    """
    backend = _backend(affected, ["backend/app/services/memory.py"])
    frontend = _frontend(
        affected, ["frontend/src/features/misc/settings/ModelRegistryPanel.tsx"]
    )

    assert 0 < len(backend["targets"]) < backend["total"] / 2
    assert 0 < len(frontend["targets"]) < frontend["total"] / 2
