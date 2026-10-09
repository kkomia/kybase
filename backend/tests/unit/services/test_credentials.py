"""旧明文收编：迁移器（M5 阶段 6，方案 §4.2 / §4.3）。

镜像同构：``app/services/credentials.py`` → 本文件。

## 两条最强的判据（方案 §4.3-4）

1. **库文件原始字节里 grep 哨兵串 → 0 命中**（收编 + 清页之后）——**同一次运行里、
   不重启、不关库**，主库 / ``-wal`` / ``-shm`` 三个文件一起看（阶段 6 时这一条只能
   退到"进程退出之后"，因为那时够不到收 WAL 那一步；见 `LocalEraser.secure_erase`）；
2. **变异验证**：同一份现场**不跑迁移**时那些串查得到——否则第 1 条可能只是因为
   "查的地方本来就没有东西"（这条与阶段 2 那条擦洗判据同一手法）。

变异验证那一段仍然先"关库再读字节"：本机库是 WAL 模式，写进去的明文先落在 ``-wal``
里，关库那一次 ``wal_checkpoint(TRUNCATE)`` 之后它才落进主库文件——那样这份证据与"迁移
之后"的形态是同一种（都读主库 + 伴生文件），两段可比。判据问的东西始终是同一个：
**用户盘上那些字节能被谁捡到**。

## 现场长什么样

| 种下去的东西 | 收编之后 |
| --- | --- |
| ``app_settings['web.search_api_key']`` | 进钥匙串，**库里那一行删掉** |
| ``model_providers.api_key``（两家供应商） | 进钥匙串，**列清空**（行留着） |
| ``app_settings['mineru.token']``（只登记） | **原样留着**（它的家没变） |
| ``mcp_servers.env`` / ``headers``（只登记） | **原样留着** |
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import SecretStoreUnavailable
from app.core.storage import build_stores, reset_stores
from app.services import credentials as module
from app.services.credentials import MIGRATED_SETTING_KEYS, CredentialsService
from app.services.secrets import InMemorySecretStore, NullSecretStore, setting_target
from app.storage.base import MCPServerRecord, ModelProviderRecord, StoreBundle

pytestmark = pytest.mark.local

SEARCH_KEY = "web.search_api_key"
SEARCH_SENTINEL = "tavily_SENTINEL_phase6_9f2c"
MODEL_SENTINELS = {
    "prov_1": "kylab_sk_SENTINEL_prov1_4b81",
    "prov_2": "kylab_sk_SENTINEL_prov2_77cd",
}
REGISTERED_SENTINELS = {
    "mineru.token": "mineru_SENTINEL_phase6_7ae4",
    "paddleocr.token": "paddleocr_SENTINEL_phase6_51ba",
}
MCP_SENTINEL = "mcp_SENTINEL_phase6_b3d0"


# ------------------------------------------------------------------ 夹具


@pytest.fixture(autouse=True)
def _clean_stores() -> Iterator[None]:
    reset_stores()
    yield
    reset_stores()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _stores(data_dir: Path) -> StoreBundle:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=data_dir
    )
    return build_stores(settings)


def _seed(stores: StoreBundle) -> None:
    """把那五处（两处该迁、三处只登记）都种进本机库。"""
    stores.meta.set_setting(SEARCH_KEY, SEARCH_SENTINEL)
    for key, value in REGISTERED_SENTINELS.items():
        stores.meta.set_setting(key, value)
    for provider_id, sentinel in MODEL_SENTINELS.items():
        stores.meta.create_model_provider(
            ModelProviderRecord(
                id=provider_id, kind="llm", name=f"供应商 {provider_id}", api_key=sentinel
            )
        )
    stores.mcp_servers.create_mcp_server(
        MCPServerRecord(
            id="mcp_1",
            name="外部服务",
            transport="stdio",
            target="npx",
            env={"TOKEN": MCP_SENTINEL},
            headers={"Authorization": "Bearer x"},
        )
    )


def _close() -> None:
    """关库（``Database.close()`` 会做一次 ``wal_checkpoint(TRUNCATE)``）。

    于是主库文件里是"这件事的全部"：明文的提交不再只躺在 ``-wal`` 里。
    """
    reset_stores()


def _grep(data_dir: Path, needle: str) -> int:
    """整个数据目录里那份明文出现了几次（**扫字节**，不是查表——查表看不到空闲页）。"""
    hits = 0
    for path in sorted(data_dir.rglob("*")):
        if path.is_file():
            hits += path.read_bytes().count(needle.encode("utf-8"))
    return hits


def _per_file(data_dir: Path, needle: str) -> dict[str, int]:
    """库的那三个文件里各命中几次（**按文件报**：失败时一眼看得出是哪一份没擦干净）。"""
    return {
        name: (data_dir / name).read_bytes().count(needle.encode("utf-8"))
        if (data_dir / name).exists()
        else 0
        for name in ("kylab.db", "kylab.db-wal", "kylab.db-shm")
    }


def _all_plaintext(data_dir: Path) -> dict[str, int]:
    needles = {
        "search": SEARCH_SENTINEL,
        **{f"model:{key}": value for key, value in MODEL_SENTINELS.items()},
        **{f"registered:{key}": value for key, value in REGISTERED_SENTINELS.items()},
        "mcp": MCP_SENTINEL,
    }
    return {name: _grep(data_dir, value) for name, value in needles.items()}


# ------------------------------------------------------------------ ① 最强的那条判据


def test_migrating_moves_the_plaintext_out_of_the_database_bytes(data_dir: Path) -> None:
    """收编之后：**两处该迁的明文在数据目录里 0 命中**，三处"只登记"的原样留着。

    三段各自的角色：

    1. **变异验证**（收编前）：关门之后再 grep，两处明文都查得到——否则"0 命中"可能只是
       查错了地方（先关库是为了让那份证据落在主库文件里，WAL 只在进程活着时是必需的）；
    2. **收编**：`CredentialsService.migrate()` 跑一遍（它收尾会调 `secure_erase`）；
    3. **判据（这一轮升级的地方）**：**同一次运行里、不重启、不关库**，三个文件都 0 命中。
       阶段 6 时这里还得先 `reset_stores()`（关库）才干净——因为当时 `-wal` 里的旧帧
       要等 `Database.close()` 那次检查点才消失；现在三件事（抹零 / VACUUM / 收 WAL）
       在 `migrate()` 里一次做完。
    """
    stores = _stores(data_dir)
    _seed(stores)
    _close()  # 只为变异验证那一段：让明文落进主库文件再查

    before = _all_plaintext(data_dir)
    assert before["search"] > 0, "没跑迁移时这份明文查得到（变异验证）"
    assert before["model:prov_1"] > 0
    assert before["registered:mineru.token"] > 0

    stores = _stores(data_dir)
    keychain = InMemorySecretStore()
    report = CredentialsService(stores, keychain).migrate()
    # **不关库、不重启**：判据就在这一次运行里成立
    assert _per_file(data_dir, SEARCH_SENTINEL) == {
        "kylab.db": 0,
        "kylab.db-wal": 0,
        "kylab.db-shm": 0,
    }, "收编过的明文不该还能在盘上捡到（三个文件都要干净）"
    assert _per_file(data_dir, MODEL_SENTINELS["prov_1"]) == {
        "kylab.db": 0,
        "kylab.db-wal": 0,
        "kylab.db-shm": 0,
    }

    after = _all_plaintext(data_dir)
    assert after["search"] == 0
    assert after["model:prov_1"] == 0 and after["model:prov_2"] == 0
    assert after["registered:mineru.token"] > 0, "只登记的那两处**本来就该留着**"
    assert after["registered:paddleocr.token"] > 0
    assert after["mcp"] > 0, "MCP 那两列不收编（只登记）"
    assert report.pending_migration == 0
    assert [item["item"] for item in report.migrated] == [
        f"setting:{SEARCH_KEY}",
        "model_provider:prov_1",
        "model_provider:prov_2",
    ]


def test_the_report_carries_the_value_into_the_keychain(data_dir: Path) -> None:
    """搬进钥匙串的是**原来那个值**（不是掩码、不是空串），而且报告里的键就那五个。"""
    stores = _stores(data_dir)
    _seed(stores)
    keychain = InMemorySecretStore()

    report = CredentialsService(stores, keychain).migrate()

    assert keychain.get(setting_target(SEARCH_KEY)) == SEARCH_SENTINEL
    assert keychain.get("kylab:model_provider:prov_1") == MODEL_SENTINELS["prov_1"]
    assert set(report.as_dict()) == {
        "store",
        "migrated",
        "skipped",
        "failed",
        "pending_migration",
    }
    assert report.store == "available"
    assert report.ok is True
    assert all(item["reason"] == "" for item in report.migrated)


def test_the_database_cell_is_cleared_not_just_hidden(data_dir: Path) -> None:
    """库里那一处**真的没了**：设置那一行删掉、供应商那一列清空（行留着）。"""
    stores = _stores(data_dir)
    _seed(stores)

    CredentialsService(stores, InMemorySecretStore()).migrate()

    assert stores.meta.get_setting(SEARCH_KEY) is None, "方案 §4.2：迁移完成后删掉那一行"
    for provider_id in MODEL_SENTINELS:
        record = stores.meta.get_model_provider(provider_id)
        assert record is not None, "供应商那一行要留着（只有凭据搬了家）"
        assert record.api_key == "", "列清空（清列而不是删行）"
    assert stores.meta.get_setting("mineru.token") == REGISTERED_SENTINELS["mineru.token"]


# ------------------------------------------------------------------ ② 幂等与可重跑


def test_a_second_run_is_a_no_op(data_dir: Path) -> None:
    """逐项幂等：第二次跑**一条都不搬**，而且只剩"已经迁过"那一种跳过理由。"""
    stores = _stores(data_dir)
    _seed(stores)
    keychain = InMemorySecretStore()
    service = CredentialsService(stores, keychain)
    first = service.migrate()
    assert len(first.migrated) == 3

    second = service.migrate()

    assert second.migrated == [] and second.failed == []
    assert second.pending_migration == 0
    assert len(second.skipped) == 0, "库里已经空了 → 那一项根本不出现（不是「跳过」）"


def test_a_leftover_plaintext_whose_value_is_already_in_the_keychain_is_cleaned(
    data_dir: Path,
) -> None:
    """钥匙串里已经有同一个值（上一次迁过、只差清库）→ 跳过并**顺手清掉库里那份**。

    这一档是"迁了、但清页那一步之前被打断"的现场：库里那份是纯重复，留着只会让
    ``pending_migration`` 一直不为 0——用户点十次"迁"都清不掉它。
    """
    stores = _stores(data_dir)
    _seed(stores)
    keychain = InMemorySecretStore()
    keychain.set(setting_target(SEARCH_KEY), SEARCH_SENTINEL)
    keychain.set("kylab:model_provider:prov_1", MODEL_SENTINELS["prov_1"])
    keychain.set("kylab:model_provider:prov_2", MODEL_SENTINELS["prov_2"])

    report = CredentialsService(stores, keychain).migrate()

    assert report.migrated == []
    assert len(report.skipped) == 3
    assert all("已经" in item["reason"] for item in report.skipped)
    assert stores.meta.get_setting(SEARCH_KEY) is None
    # 跳过也算"这一轮干过活"：清页那一步照样跑（不然库里那份重复的永远清不掉）
    assert _per_file(data_dir, SEARCH_SENTINEL) == {
        "kylab.db": 0,
        "kylab.db-wal": 0,
        "kylab.db-shm": 0,
    }


def test_the_keychain_wins_when_it_holds_a_different_value(data_dir: Path) -> None:
    """钥匙串里那份**不一样** → 以钥匙串为准（用户改过之后，库里那份是旧的）。

    覆盖钥匙串是**明确不做**的：那会把用户刚填进去的新值抹掉，而库里那份旧值一点用都
    没有（它已经不在生效路径上了）。库里那份照样清掉。
    """
    stores = _stores(data_dir)
    _seed(stores)
    keychain = InMemorySecretStore()
    keychain.set(setting_target(SEARCH_KEY), "the-new-value")

    report = CredentialsService(stores, keychain).migrate()

    assert keychain.get(setting_target(SEARCH_KEY)) == "the-new-value"
    assert stores.meta.get_setting(SEARCH_KEY) is None
    assert any("以钥匙串为准" in item["reason"] for item in report.skipped)


# ------------------------------------------------------------------ ③ 失败那一档


class _BrokenStore(InMemorySecretStore):
    """写不进去的钥匙串（只在写这一个动作上坏掉，读还是好的）。"""

    def __init__(self, *, broken_name_part: str) -> None:
        super().__init__()
        self._broken = broken_name_part

    def set(self, name: str, value: str) -> None:
        if self._broken in name:
            raise SecretStoreUnavailable("这一条写不进去（用例构造的）")
        super().set(name, value)


def test_a_failed_item_keeps_its_plaintext_and_can_be_retried(data_dir: Path) -> None:
    """§4.3-5：写不进去的那一项**不清明文**，如实进 ``failed``，修好之后重跑就能迁走。"""
    stores = _stores(data_dir)
    _seed(stores)
    keychain = _BrokenStore(broken_name_part="model_provider")

    report = CredentialsService(stores, keychain).migrate()

    assert report.ok is False
    assert [item["item"] for item in report.failed] == [
        "model_provider:prov_1",
        "model_provider:prov_2",
    ]
    assert all("写不进去" in item["reason"] for item in report.failed)
    assert report.pending_migration == 2, "还剩两处（失败的那两处就是可重跑的量）"
    assert stores.meta.get_setting(SEARCH_KEY) is None, "能迁的照常迁（逐项、不是全有全无）"
    assert stores.meta.get_model_provider("prov_1").api_key == MODEL_SENTINELS["prov_1"]
    assert _grep(data_dir, MODEL_SENTINELS["prov_1"]) > 0, "明文原样留着"

    # 修好之后重跑：那两处这一次迁得走。**跳过列表是空的**——设置那一项在上一轮就迁完
    # 并清了库（库里没有明文 → 那一项根本不出现），所以这一轮没有"跳过"可报。
    fixed = InMemorySecretStore()
    second = CredentialsService(stores, fixed).migrate()

    assert second.ok is True and second.pending_migration == 0
    assert [item["item"] for item in second.migrated] == [
        "model_provider:prov_1",
        "model_provider:prov_2",
    ]
    assert second.skipped == [] and second.failed == []


def test_an_unavailable_store_refuses_the_whole_run(data_dir: Path) -> None:
    """钥匙串整条不可用 → **抛**（端点翻 503），而 ``status`` 如实回 unavailable + 0。

    为什么不是"逐项失败"：那种情况下"这次迁移的结果"就是"这台机器收不了"，
    列 N 条同样的原因只会把这件事说糊涂。
    """
    stores = _stores(data_dir)
    _seed(stores)
    service = CredentialsService(stores, NullSecretStore())

    assert service.status() == {"store": "unavailable", "pending_migration": 0}
    with pytest.raises(SecretStoreUnavailable) as raised:
        service.migrate()
    assert "钥匙串" in str(raised.value)

    assert stores.meta.get_setting(SEARCH_KEY) == SEARCH_SENTINEL, "一个字节都没动"
    assert len(MIGRATED_SETTING_KEYS) == 1, "收编清单只有那一个键（§4.2）"


# ------------------------------------------------------------------ ④ 壳侧那半（NAS 钥匙）


def test_the_nas_token_helpers_are_ready_for_the_shell(data_dir: Path) -> None:
    """``kylab:nas_token:<origin>`` 的读写：壳写、后端读（或反过来）走的是同一个名字。

    两处细节：同一个 NAS 的两种写法归一成同一个名字；没配时读回 ``None``
    （壳据此知道"要去登录一次"）。
    """
    stores = _stores(data_dir)
    service = CredentialsService(stores, InMemorySecretStore())

    assert service.nas_token("http://nas.test:8090") is None

    service.set_nas_token("http://nas.test:8090/api/v1", "kylab_sk_long_lived")
    assert service.nas_token("http://nas.test:8090/") == "kylab_sk_long_lived"

    service.delete_nas_token("HTTP://NAS.test:8090")
    assert service.nas_token("http://nas.test:8090") is None


# ------------------------------------------------------------------ ⑤ CLI


def _cli(monkeypatch: pytest.MonkeyPatch, stores: StoreBundle, keychain: object) -> None:
    """把 CLI 的装配换成"连着我们这一份库与钥匙串"（装配本身在下面那条用例里单独看）。"""
    service = CredentialsService(stores, keychain)  # type: ignore[arg-type]
    monkeypatch.setattr(module, "_build", lambda data_dir: service)


def test_the_cli_migrates_and_prints_the_report(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``python -m app.services.credentials migrate``：打报告、退出码 0。"""
    stores = _stores(data_dir)
    _seed(stores)
    _cli(monkeypatch, stores, InMemorySecretStore())

    code = module.main(["migrate", "--data-dir", str(data_dir)])

    out = capsys.readouterr().out
    assert code == 0
    assert f"setting:{SEARCH_KEY}" in out and "model_provider:prov_1" in out
    assert "迁了 3、跳过 0、失败 0；还剩 0 处明文" in out


def test_the_cli_says_why_it_cannot_migrate(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有钥匙串的机器上：``migrate`` 走 stderr + 退出码 1，``status`` 仍然 0（那是结论）。"""
    stores = _stores(data_dir)
    _seed(stores)
    _cli(monkeypatch, stores, NullSecretStore())

    status_code = module.main(["status", "--data-dir", str(data_dir)])
    status_out = capsys.readouterr()
    assert status_code == 0
    assert '"store": "unavailable"' in status_out.out
    assert "本机没有可用的系统钥匙串" in status_out.err

    code = module.main(["migrate", "--data-dir", str(data_dir)])
    err = capsys.readouterr().err
    assert code == 1
    assert "这台机器收不了" in err


def test_the_cli_fails_with_a_nonzero_code_when_an_item_fails(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """有项没迁成 → 退出码 1（脚本据此知道"还得再来一次"）。"""
    stores = _stores(data_dir)
    _seed(stores)
    _cli(monkeypatch, stores, _BrokenStore(broken_name_part="model_provider"))

    code = module.main(["migrate", "--data-dir", str(data_dir)])

    captured = capsys.readouterr()
    assert code == 1
    assert "没迁成：model_provider:prov_1" in captured.err
    assert "还剩 2 处明文" in captured.out


def test_the_cli_can_read_write_and_delete_the_nas_token(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``nas-token`` 那三个动作：**壳写、后端读**（或反过来）走的是同一个名字。

    跨侧对拍最省事的做法就是这一条：壳里登录一次，然后跑
    ``python -m app.services.credentials nas-token --origin http://nas:8090 --show``
    —— 打出来的串与壳手里那串一致就说明两侧的名字规则一致（不一致会读到"没配"）。
    默认只打掩码，``--show`` 才把值打到终端上（那是对拍专用的动作）。
    """
    stores = _stores(data_dir)
    _cli(monkeypatch, stores, InMemorySecretStore())

    def run(*extra: str) -> int:
        """跑一条 ``nas-token`` 子命令（省下把长参数表抄三遍）。"""
        return module.main(
            ["nas-token", "--data-dir", str(data_dir), "--origin", "http://nas:8090", *extra]
        )

    assert run() == 1
    assert "没配" in capsys.readouterr().out

    assert run("--set", "kylab_sk_长钥匙") == 0
    assert "kylab:nas_token:http://nas:8090" in capsys.readouterr().out

    run()
    assert capsys.readouterr().out.strip() == "kyl…长钥匙", "默认只打掩码"

    assert run("--show") == 0, "同一个 origin 的另一种写法（带 path）也叫得响"
    assert capsys.readouterr().out.strip() == "kylab_sk_长钥匙"

    run("--delete")
    assert run() == 1


def test_the_cli_nas_token_needs_an_origin(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stores = _stores(data_dir)
    _cli(monkeypatch, stores, InMemorySecretStore())

    code = module.main(["nas-token", "--data-dir", str(data_dir)])

    assert code == 1
    assert "--origin" in capsys.readouterr().err


def test_a_bundle_without_an_eraser_still_shrinks_the_pages(data_dir: Path) -> None:
    """没有擦除面时（``eraser`` 为 ``None``）退回 ``vacuum()``：不崩，但**只做得到一半**。

    这一档在真实部署里不存在（钥匙串不可用时 ``migrate`` 一开始就抛了），可接口上它是个
    合法状态——所以行为要写死并钉住：空闲页照样收（主库干净），但 **WAL 那几帧收不掉**
    （那正是 ``secure_erase`` 存在的理由）。它也是 `MetaStore.vacuum()` 至今仍被用到的
    两个调用点之一（另一个是管理员那个"存储维护 → 整理"）。
    """
    from dataclasses import replace

    stores = _stores(data_dir)
    _seed(stores)
    bare = replace(stores, eraser=None)

    report = CredentialsService(bare, InMemorySecretStore()).migrate()

    assert report.ok is True and report.pending_migration == 0
    assert _per_file(data_dir, SEARCH_SENTINEL)["kylab.db"] == 0, "至少主库是干净的"


def test_a_busy_wal_checkpoint_becomes_a_conflict_not_a_500(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收 WAL 撞上别的读者 → ``ConflictError``（409）——**迁移本身是成功的**。

    为什么值得一条用例：这一步的失败不是"没迁成"，而是"收尾没做完"。原样把
    ``StorageError`` 抛到端点上会变成 500「服务端出错了」——那句话把"重跑一次这一步
    就好"藏了起来（而它确实可重跑：抹零 / VACUUM / 收 WAL 三个动作都幂等）。

    现场造法同 ``test_secure_erase.py``：另开一条连接持一个**旧**读事务，之后往库上写
    一帧——检查点只能复制到那个读者的位置。``busy_timeout`` 与重试次数在这里调到最小
    （两处都在**建库之前**调，新连接才会用上这个口径），否则为了造现场要等十几秒。
    """
    from app.core.exceptions import ConflictError
    from app.storage.sqlite_impl import connection as connection_module
    from app.storage.sqlite_impl.connection import Database

    monkeypatch.setattr(connection_module, "BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(connection_module, "_WAL_TRUNCATE_ATTEMPTS", 1)
    stores = _stores(data_dir)
    _seed(stores)
    reader = Database(data_dir / "kylab.db", allow_multiple_instances=True)
    reader.open()
    conn = reader.connection()
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM conversations").fetchone()
        stores.meta.set_setting("llm.temperature", "0.9")

        with pytest.raises(ConflictError) as raised:
            CredentialsService(stores, InMemorySecretStore()).migrate()
    finally:
        conn.execute("ROLLBACK")
        reader.close()

    message = str(raised.value)
    assert "-wal" in message and "重跑一次" in message, "要说清哪一步没成、下一步做什么"
    assert stores.meta.get_setting(SEARCH_KEY) is None, "库里那份已经清了（迁移那几步是成功的）"
