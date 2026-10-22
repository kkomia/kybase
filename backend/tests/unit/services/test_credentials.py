"""``app/services/credentials.py`` 的单元测试。

镜像同构：``app/services/credentials.py`` → ``tests/unit/services/test_credentials.py``。

**v0.1.0 精简之后的形态**：这个服务在 NAS 部署里是空转的（没有系统钥匙串），
所以这里钉的是那两条**如实**的行为（不可用时 ``pending=0`` / ``migrate`` 抛），
以及在**有**钥匙串时那套逐项幂等的收编语义——库侧换成 PG 测试仓储，
原先那些"从 ``kylab.db`` 的字节里 grep 明文"的断言随旧版拆除。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import SecretStoreUnavailable
from app.services.credentials import MIGRATED_SETTING_KEYS, CredentialsService
from app.services.secrets import InMemorySecretStore, NullSecretStore, model_provider_target
from app.storage.base import StoreBundle

PROVIDER_KEY = "sk-provider-plaintext"


def _seed_provider(stores: StoreBundle, *, api_key: str = PROVIDER_KEY) -> str:
    """登记一个带明文 api_key 的供应商，返回它的 id。"""
    from app.services.model_registry import ModelRegistryService

    record = ModelRegistryService(stores).create_provider(
        kind="llm", name="测试供应商", base_url="https://api.example.com/v1", api_key=api_key
    )
    return record.id


def test_the_server_deployment_reports_no_pending_plaintext(bundle: StoreBundle) -> None:
    """没有系统钥匙串的那一档（NAS / 容器）：**如实报"没有等着迁的东西"**。

    "等着迁"这件事在那台机器上根本不存在——库就是凭据的家（R14 的边界）。
    """
    _seed_provider(bundle)

    status = CredentialsService(bundle, NullSecretStore()).status()

    assert status == {"store": "unavailable", "pending_migration": 0}


def test_migrating_without_a_keychain_refuses_the_whole_run(bundle: StoreBundle) -> None:
    """钥匙串整条不可用 → 抛（端点翻 503），**不是**回一份"逐项失败"的报告。

    一项都写不进去时，N 条一模一样的"写不进去"比一句"这台机器没有钥匙串"难懂得多；
    而"绝不退回库里写明文"这条纪律也靠它守住。
    """
    service = CredentialsService(bundle, NullSecretStore())

    with pytest.raises(SecretStoreUnavailable):
        service.migrate()


def test_migrating_moves_a_provider_key_into_the_keychain(bundle: StoreBundle) -> None:
    """有钥匙串时：写进钥匙串 + **清掉库里那一列**，行本身留着。"""
    provider_id = _seed_provider(bundle)
    keychain = InMemorySecretStore()

    report = CredentialsService(bundle, keychain).migrate()

    assert report.ok is True
    assert [item["item"] for item in report.migrated] == [f"model_provider:{provider_id}"]
    assert keychain.get(model_provider_target(provider_id)) == PROVIDER_KEY
    record = bundle.meta.get_model_provider(provider_id)
    assert record is not None and record.api_key == "", "库里那份明文必须清掉"
    assert report.pending_migration == 0


def test_a_second_run_is_a_no_op(bundle: StoreBundle) -> None:
    """逐项幂等：迁过之后库里已经空了 → 那一项**根本不出现**在待迁清单里。"""
    _seed_provider(bundle)
    service = CredentialsService(bundle, InMemorySecretStore())

    service.migrate()
    second = service.migrate()

    assert second.migrated == [] and second.failed == []
    assert service.pending() == 0


def test_the_keychain_wins_when_it_holds_a_different_value(bundle: StoreBundle) -> None:
    """钥匙串里那份**不一样**时：以钥匙串为准，库里那份旧的清掉，绝不覆盖新值。"""
    provider_id = _seed_provider(bundle)
    keychain = InMemorySecretStore()
    keychain.set(model_provider_target(provider_id), "sk-the-newer-one")

    report = CredentialsService(bundle, keychain).migrate()

    assert [item["item"] for item in report.skipped] == [f"model_provider:{provider_id}"]
    assert keychain.get(model_provider_target(provider_id)) == "sk-the-newer-one"
    record = bundle.meta.get_model_provider(provider_id)
    assert record is not None and record.api_key == ""


def test_only_the_provider_cell_is_still_collected(bundle: StoreBundle) -> None:
    """收编清单里只剩模型供应商标记那一处。

    原先还有 ``web.search_api_key``（联网搜索随 调用方拆除而消失）——
    这一条是那个边界变更的刹车：清单里再冒出键名时，它得是有意识加的。
    """
    assert MIGRATED_SETTING_KEYS == ()
