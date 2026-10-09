"""应用生命周期与启动装配（集成）。

守的是"服务起来的时候存储是否真的准备好了"——这一条不测，M1 的装配就只是纸面功夫。

v0.12 起存储是 PostgreSQL，所以"就绪"看的是 **schema 版本**（SQLite 时代看的是
``kylab.db`` 文件在不在）。守的东西没变：启动即就绪。

v0.1.1 起还守着"记忆/人设文件在启动时就铺好"（§12.224 第 12 条）：容器里的验收
是"新实例起来后「记忆」页就可写"，而那时还没有任何对话。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.storage import STORAGE_SUBDIRS, reset_stores
from app.services.memory import AGENTS_FILE, PROFILE_FILE, SOUL_FILE


def _assert_storage_ready(pg_database) -> None:
    from app.storage.postgres_impl.schema import SCHEMA_VERSION, current_version

    assert current_version(pg_database) == SCHEMA_VERSION, "启动后 schema 应已就位"


@pytest.fixture
def wired_app(monkeypatch, tmp_path):
    """把数据目录指到临时目录，并清掉配置与装配的缓存，避免串到别的测试。"""
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    reset_stores()
    try:
        from app.main import create_app

        yield create_app(), tmp_path / "data"
    finally:
        reset_stores()
        get_settings.cache_clear()


def test_lifespan_initializes_storage_on_startup(wired_app, pg_database) -> None:
    app, data_dir = wired_app

    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200

    _assert_storage_ready(pg_database)
    for subdir in STORAGE_SUBDIRS:
        # 测试里对象存储走本地实现（S3 环境变量被 conftest 清掉），目录应已建好
        assert (data_dir / subdir).is_dir()


def test_startup_is_repeatable(wired_app, pg_database) -> None:
    """重启（再来一次 lifespan）不应因迁移重复执行而失败。"""
    app, _ = wired_app

    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/api/v1/health").status_code == 200

    _assert_storage_ready(pg_database)


def test_docs_and_openapi_are_versioned(wired_app) -> None:
    app, _ = wired_app
    with TestClient(app) as client:
        assert client.get("/api/v1/docs").status_code == 200
        paths = client.get("/api/v1/openapi.json").json()["paths"]
    assert all(path.startswith("/api/v1") for path in paths)


def test_lifespan_lays_down_the_memory_templates(wired_app, pg_database) -> None:
    """启动时把记忆/人设文件**幂等**铺好（v0.1.1，§12.224 第 12 条）。

    容器里的验收是"新实例起来后「记忆」页就可写"——而新实例还没有任何对话，
    所以这件事只能发生在启动时（原先要等第一次对话或第一次 ``remember``，
    在那之前页面上什么都没有）。落点在数据目录下：容器里就是挂载卷里的
    ``/data/memory``，与其余数据一起持久化。

    第二次启动**不许覆盖用户改过的内容**——那份文件可能已经是他写了几天的东西。

    **v0.56 起只铺三份**（`SOUL.md` / `PROFILE.md` / `AGENTS.md`）：`MEMORY.md`
    随"记忆档案"退场（档案制 §7.2）——不再被播种、也不再被注入或写入
    （见 `services/memory.py::seed_persona`）。旧盘上已有的那份仍然在、仍然读得到，
    但那是"旧记忆（只读）"，不是"新装好的桌面第一次打开有什么"。
    **这一条更新于 2026-10-05**：它原来断言四份（含 ``MEMORY.md``），
    而代码在 v0.56 就只铺三份了——那是一次**漏改的期望**，与退役那一轮无关，
    顺手在这里对齐（`test_local_backend.py` 里早就写着"v0.56 起只种三份"）。
    """
    app, data_dir = wired_app
    workspace = data_dir / "memory"
    expected = {SOUL_FILE, PROFILE_FILE, AGENTS_FILE}

    with TestClient(app):
        assert {item.name for item in workspace.iterdir()} == expected
        # 这条测试里 memory.enabled 是**默认的关**：文件那一半不看那道闸
        # （见 services/memory.py），因此上面的断言同时守住了这一点

    (workspace / SOUL_FILE).write_bytes("我自己写的人格".encode())

    with TestClient(app):
        assert (workspace / SOUL_FILE).read_text(encoding="utf-8") == "我自己写的人格"
        # 第二次启动不该多铺也不该少铺（`AGENTS.md` 是 v0.56 补进来的那一份）
        assert {item.name for item in workspace.iterdir()} == expected


def test_data_directory_is_respected(monkeypatch, tmp_path, pg_database) -> None:
    """``KYLAB_DATA_DIR`` 必须真的生效（否则会把数据写到仓库里）。"""
    target = tmp_path / "custom-data"
    monkeypatch.setenv("KYLAB_DATA_DIR", str(target))
    get_settings.cache_clear()
    reset_stores()
    try:
        from app.main import create_app

        with TestClient(create_app()) as client:
            assert client.get("/api/v1/health").status_code == 200
        # 对象存储目录建在指定数据目录下（测试里走本地实现）
        assert Path(target, STORAGE_SUBDIRS[0]).is_dir()
    finally:
        reset_stores()
        get_settings.cache_clear()
