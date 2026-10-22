"""``scripts/affected_tests.py`` 自己的用例：钉住"改了 X 必须跑到 Y"这张表。

**为什么这一份必须有**：那个脚本存在的唯一理由是"别让人手算影响面"——而它一旦算错，
错的形状恰恰是最坏的一种：**静默漏跑**。门禁全绿、CI 也绿，问题留到收尾跑全量才红
（规范 5.2.1 记着 2026-09-27 就是这么漏的：只跑了 `misc-settings`，
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
    """镜像同构：``app/services/summary.py`` → ``tests/unit/services/test_summary.py``。

    （原先这条用的是 ``memory.py`` 那一对，它随 调用方拆除——换一对仍然存在的。）
    """
    info = _backend(affected, ["backend/app/services/summary.py"])

    assert "tests/unit/services/test_summary.py" in info["targets"]
    assert not info["force_all"], "单个服务模块的改动不该判全量"


def test_mirrored_integration_test_is_always_in_scope(affected: ModuleType) -> None:
    """集成侧镜像：``storage/postgres_impl/meta_store.py`` 找到集成那份镜像。"""
    info = _backend(affected, ["backend/app/storage/postgres_impl/meta_store.py"])

    assert "tests/integration/storage/test_meta_store.py" in info["targets"]


def test_changing_a_test_file_runs_that_file(affected: ModuleType) -> None:
    """用例自己改了 → 跑它自己（这条最容易漏，改断言时最需要它）。"""
    info = _backend(affected, ["backend/tests/unit/services/test_summary.py"])

    assert info["targets"] == ["tests/unit/services/test_summary.py"]


def test_repo_level_script_is_found_by_name(affected: ModuleType) -> None:
    """``scripts/`` 下的脚本不在 import 图上，靠"用例点名了它"兜住。

    ``tests/unit/test_check_layering.py`` 是按**路径**加载那个脚本的，
    改脚本不跑这条用例，等于门禁脚本坏了也没人知道。
    """
    info = _backend(affected, ["scripts/check_layering.py"])

    assert "tests/unit/test_check_layering.py" in info["targets"]


# **那条"子目录脚本按路径找到用例"的守卫已删**（v0.1.0 精简）：它举例的那一对
# 是 `scripts/deck/render.mjs` ↔ `tests/unit/services/deck/test_render.py`，
# 而 deck（出片）整条链路随 调用方拆除了——脚本里那条按路径匹配的机制还在，
# 但这个仓里已经没有"子目录脚本 + 钉住它的用例"这一对了，留着断言只会指向虚处。


# ------------------------------------------------------------------ 前端

def test_frontend_panel_maps_to_the_file_that_actually_pins_it(affected: ModuleType) -> None:
    """2026-09-27 那次漏网的复现（**换成这个仓里真实存在的一对**）。

    那次漏网的形状是："改了 A 面板，钉住它的断言其实在 B 用例里"。前端**没有**镜像关系，
    所以只能靠依赖图 + 说明符匹配——这里用知识库设置面板那一对钉住它：
    改 ``KnowledgeBaseSettings.tsx``，``tests/knowledge-settings.test.tsx`` 必须在跑。
    （原先用的是 kylab 前端的 ``ModelRegistryPanel`` ↔ ``misc-registry.test.tsx``，
    那份界面与那份用例都不在这个仓里。）
    """
    info = _frontend(
        affected, ["frontend/src/features/knowledge/KnowledgeBaseSettings.tsx"]
    )

    assert "tests/knowledge-settings.test.tsx" in info["targets"]


def test_frontend_component_pulls_in_its_page_and_shell_tests(affected: ModuleType) -> None:
    """改一个知识库设置面板，用到它的页面/外壳用例要跟着跑。

    （原先是聊天输入组件那一对：``Composer.tsx`` ↔ ``chat-paste-upload`` /
    ``chat-ui``——那份界面与那两条用例都不在这个仓里。）
    """
    info = _frontend(
        affected, ["frontend/src/features/knowledge/KnowledgeBaseSettings.tsx"]
    )

    assert "tests/knowledge-settings.test.tsx" in info["targets"], "直接钉住这个面板的那条"
    assert "tests/layout-shell.test.tsx" in info["targets"], "经应用壳转手的那条"


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
    backend = _backend(affected, ["backend/app/services/summary.py"])
    frontend = _frontend(
        affected, ["frontend/src/features/knowledge/KnowledgeBaseSettings.tsx"]
    )

    assert 0 < len(backend["targets"]) < backend["total"] / 2
    assert 0 < len(frontend["targets"]) < frontend["total"] / 2
