"""``secure_erase``：**同一次运行里**就把明文擦干净（M5 阶段 6 收尾）。

镜像同构：``app/storage/sqlite_impl/connection.py`` 的 ``Database.secure_erase``
→ 本文件（那一层是唯一能同时设 ``PRAGMA secure_delete`` 与跑检查点的地方）。

## 这一条判据为什么要单独一个文件

阶段 6 的迁移器清完库里的明文之后，**主库文件**是干净的，但 ``kylab.db-wal`` 里还留着
旧帧（实测 630 KB 的 ``-wal`` 里 2 次命中）——只有一次 ``wal_checkpoint(TRUNCATE)``
才会把那个文件截掉。当时 ``services/`` 够不到这一句（L2：不许 import ``sqlite3``），
所以判据只能退到"进程退出之后"（``Database.close()`` 里那次检查点）。这一轮把
`secure_delete` → `VACUUM` → 收 WAL 三件事收进一个方法，判据就回到**当次运行**：

- 造一份含哨兵串的本机库 → 跑擦除 → **同一个进程、不重启、不关库**，
  ``kylab.db`` / ``kylab.db-wal`` / ``kylab.db-shm`` **三个都 0 命中**；
- 配一条变异验证：**不跑擦除**时那些串查得到（否则"0 命中"可能只是查错了地方）；
- 再钉两条边界：``-wal`` 被截成 0 字节（不是"还在但内容没了"），以及"收不干净时如实抛"
  （有别的读者占着 → ``StorageError``，而不是假装干净）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.storage import build_stores, reset_stores
from app.storage.base import LocalEraser, StorageError, StoreBundle
from app.storage.sqlite_impl import connection as connection_module
from app.storage.sqlite_impl.connection import Database

pytestmark = pytest.mark.local

SEARCH_KEY = "web.search_api_key"
SEARCH_SENTINEL = "tavily_SENTINEL_erase_7c31"
MODEL_SENTINEL = "kylab_sk_SENTINEL_erase_2b95"
#: 库文件的三个伴生文件（判据是**三个都 0 命中**，不是"加起来 0"）。
DB_FILES = ("kylab.db", "kylab.db-wal", "kylab.db-shm")


@pytest.fixture(autouse=True)
def _clean_stores() -> Iterator[None]:
    reset_stores()
    yield
    reset_stores()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def stores(data_dir: Path) -> StoreBundle:
    return _local_stores(data_dir)


def _local_stores(data_dir: Path) -> StoreBundle:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=data_dir
    )
    return build_stores(settings)


def _hits(data_dir: Path, needle: str) -> dict[str, int]:
    """那三个文件里各命中几次（**按文件报**：失败时一眼看得出是哪一份没擦干净）。"""
    return {
        name: (data_dir / name).read_bytes().count(needle.encode("utf-8"))
        if (data_dir / name).exists()
        else 0
        for name in DB_FILES
    }


def _seed(stores: StoreBundle) -> None:
    """种两处明文：一个设置键（迁移时会删行）+ 一场供应商的凭据列（迁移时会清列）。"""
    stores.meta.set_setting(SEARCH_KEY, SEARCH_SENTINEL)
    from app.storage.base import ModelProviderRecord

    stores.meta.create_model_provider(
        ModelProviderRecord(id="prov_erase", kind="llm", name="擦除用例", api_key=MODEL_SENTINEL)
    )


def _clear_like_the_migrator(stores: StoreBundle) -> None:
    """迁移器清明文的那两下：设置那一行**删掉**、供应商那一列**清空**。"""
    stores.meta.delete_setting(SEARCH_KEY)
    record = stores.meta.get_model_provider("prov_erase")
    assert record is not None
    record.api_key = ""
    stores.meta.update_model_provider(record)


# ------------------------------------------------------------------ 最强的那条


def test_the_plaintext_is_gone_from_all_three_files_in_the_same_run(
    stores: StoreBundle, data_dir: Path
) -> None:
    """**同一次运行里**（不重启、不关库）三个文件全部 0 命中——这一轮的重点。

    变异验证在前两段：不擦的话两处明文都查得到（其中一份就在 ``-wal`` 里，那正是
    阶段 6 只能退到"进程退出之后"的原因）。
    """
    _seed(stores)

    before = _hits(data_dir, SEARCH_SENTINEL)
    assert before["kylab.db-wal"] > 0, "WAL 模式下明文的最近一份提交先落在 -wal 里（变异验证）"
    assert _hits(data_dir, MODEL_SENTINEL)["kylab.db-wal"] > 0

    _clear_like_the_migrator(stores)
    # 清完但**还没擦**：主库干净了，-wal 里那几帧还在（这一步是"为什么需要 secure_erase"）
    assert _hits(data_dir, SEARCH_SENTINEL)["kylab.db-wal"] > 0

    stores.eraser.secure_erase()

    after = _hits(data_dir, SEARCH_SENTINEL)
    assert after == dict.fromkeys(DB_FILES, 0), f"设置那把还在：{after}"
    assert _hits(data_dir, MODEL_SENTINEL) == dict.fromkeys(DB_FILES, 0), "供应商那把还在"


def test_the_wal_file_is_truncated_not_just_emptied_of_content(
    stores: StoreBundle, data_dir: Path
) -> None:
    """``-wal`` 是**被截成 0 字节**（不是"还在、只是内容看着没了"）。

    这条比"grep 0 命中"更硬：它能证明收 WAL 那一步真的跑成了 ``TRUNCATE``——
    ``PASSIVE`` / ``FULL`` 只回收可复用空间，文件长度不变，旧字节还躺在里面等着被捡。
    """
    _seed(stores)
    wal = data_dir / "kylab.db-wal"
    assert wal.stat().st_size > 0, "写完之后 WAL 里应当有东西"

    stores.eraser.secure_erase()

    assert wal.exists(), "截断（TRUNCATE）不是删除：还有连接开着的时候文件在、长度是 0"
    assert wal.stat().st_size == 0


def test_erasing_twice_is_fine(stores: StoreBundle, data_dir: Path) -> None:
    """幂等：擦第二次不报错，也不会把库弄坏（``VACUUM`` 与检查点都是可重跑的动作）。

    判据是"第二次擦完还是干净的"——迁移器整体也能重跑（它逐项幂等），所以这一步必须成立。
    """
    _seed(stores)
    _clear_like_the_migrator(stores)

    stores.eraser.secure_erase()
    stores.eraser.secure_erase()

    assert _hits(data_dir, SEARCH_SENTINEL) == dict.fromkeys(DB_FILES, 0)
    assert _hits(data_dir, MODEL_SENTINEL) == dict.fromkeys(DB_FILES, 0)


def test_the_eraser_keeps_the_database_usable(stores: StoreBundle) -> None:
    """擦完库还能照常用（重建库文件之后 schema / 表 / 读写都还在）。"""
    _seed(stores)

    stores.eraser.secure_erase()

    stores.meta.set_setting("llm.temperature", "0.7")
    assert stores.meta.get_setting("llm.temperature") == "0.7"
    assert stores.meta.get_model_provider("prov_erase") is not None


# ------------------------------------------------------------------ 装配与协议


def test_the_assembled_bundle_exposes_the_eraser(stores: StoreBundle) -> None:
    """组合根把 ``StoreBundle.eraser`` 指向那个共用的本机存储对象（本机档才有）。"""
    assert stores.eraser is not None
    assert isinstance(stores.eraser, LocalEraser)
    assert stores.eraser is stores.ledger is stores.snapshot, "六个字段是同一个实例"


# ------------------------------------------------------------------ 收不干净时如实抛


def test_a_busy_checkpoint_is_reported_instead_of_swallowed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """还有读者占着旧快照 → **如实抛** ``StorageError``（不假装干净）。

    那种情形下前两步（抹零 + 重建库文件）已经完成、**主库是干净的**，唯独 ``-wal``
    里那几帧收不掉——这句话必须让调用方看见（由它决定再跑一次还是接受残留）。

    现场造法：另开一条连接持一个**旧**读事务（读到某一帧为止），然后往库上写新的一帧
    ——检查点只能复制到那个读者的位置，于是 ``TRUNCATE`` 拿不到"整段都能收"这个前提。
    ``busy_timeout`` 与重试次数在这里都调成最小，避免为了造这个现场等十几秒。
    """
    monkeypatch.setattr(connection_module, "BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(connection_module, "_WAL_TRUNCATE_ATTEMPTS", 1)
    stores = _local_stores(data_dir)
    _seed(stores)
    _clear_like_the_migrator(stores)

    reader = Database(data_dir / "kylab.db", allow_multiple_instances=True)
    reader.open()
    conn = reader.connection()
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM conversations").fetchone()  # 读者的快照停在这里
        stores.meta.set_setting("llm.temperature", "0.9")  # 之后又写了一帧，读者看不到

        with pytest.raises(StorageError) as raised:
            stores.eraser.secure_erase()
    finally:
        conn.execute("ROLLBACK")
        reader.close()

    assert "-wal" in str(raised.value), "那句话要说清残留的是 WAL 里那几帧"
    # 放开读者之后（reader 已 close）再擦一次就干净了：这正是"可重跑"
    stores.eraser.secure_erase()
    assert _hits(data_dir, SEARCH_SENTINEL) == dict.fromkeys(DB_FILES, 0)
