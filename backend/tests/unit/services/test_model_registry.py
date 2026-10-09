"""模型注册器（G1）。

镜像同构：``app/services/model_registry.py`` → 本文件。

**最要紧的一条性质**：注册器是**叠加层**——绑定了就以它为准，没绑定就回退到
``.env`` / 设置页那套。升级不能打断已有部署，这条必须有测试钉住。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UpstreamError,
)
from app.core.storage import build_stores, reset_stores
from app.services.model_registry import SLOTS, ModelRegistryService
from app.services.runtime_config import mask_secret
from app.services.secrets import InMemorySecretStore, model_provider_target
from app.storage.base import StoreBundle


@pytest.fixture
def registry(bundle) -> ModelRegistryService:  # type: ignore[no-untyped-def]
    return ModelRegistryService(bundle)


def _provider(registry: ModelRegistryService, **kwargs):  # type: ignore[no-untyped-def]
    base = {"kind": "llm", "name": "某供应商", "base_url": "https://api.example.com"}
    base.update(kwargs)
    return registry.create_provider(**base)


def _model(registry: ModelRegistryService, provider_id: str, **kwargs):  # type: ignore[no-untyped-def]
    base = {"provider_id": provider_id, "model_id": "some-model", "capabilities": ["chat"]}
    base.update(kwargs)
    return registry.register_model(**base)


# --------------------------------------------------------------------- 供应商


def test_create_and_get_provider(registry: ModelRegistryService) -> None:
    created = _provider(registry, name="深度求索", api_key="sk-abc")

    fetched = registry.get_provider(created.id)
    assert fetched.name == "深度求索"
    assert fetched.api_key == "sk-abc"
    assert fetched.enabled is True


def test_provider_kind_is_validated(registry: ModelRegistryService) -> None:
    """类别写错要当场拒绝：它决定设置页怎么分组，写个错值界面会无从渲染。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        _provider(registry, kind="随便什么")
    assert "未知的供应商类别" in str(excinfo.value)


def test_provider_name_cannot_be_blank(registry: ModelRegistryService) -> None:
    with pytest.raises(InvalidRequestError):
        _provider(registry, name="   ")


def test_get_missing_provider_raises(registry: ModelRegistryService) -> None:
    with pytest.raises(NotFoundError):
        registry.get_provider("prov_不存在")


def test_update_provider_keeps_the_key_when_not_supplied(
    registry: ModelRegistryService,
) -> None:
    """**``None`` 表示"没改"，空串表示"清空"**——这个区分是必要的。

    设置页把密钥掩码成占位符，用户不动它时前端回传的是掩码。
    若把"没传"当成"清空"，用户改个名字就会把密钥弄丢。
    """
    provider = _provider(registry, api_key="sk-original")

    registry.update_provider(provider.id, name="改了个名字")

    assert registry.get_provider(provider.id).api_key == "sk-original"


def test_update_provider_can_clear_the_key(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-original")

    registry.update_provider(provider.id, api_key="")

    assert registry.get_provider(provider.id).api_key == ""


def test_update_provider_can_replace_the_key(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-old")

    registry.update_provider(provider.id, api_key="sk-new")

    assert registry.get_provider(provider.id).api_key == "sk-new"


# --------------------------------------------------------------------- 模型


def test_register_model(registry: ModelRegistryService) -> None:
    provider = _provider(registry)
    created = _model(
        registry,
        provider.id,
        model_id="deepseek-chat",
        label="对话主力",
        capabilities=["chat"],
    )

    assert created.provider_id == provider.id
    assert list(created.capabilities) == ["chat"]
    assert registry.get_model(created.id).model_id == "deepseek-chat"


def test_duplicate_model_in_same_provider_is_rejected(
    registry: ModelRegistryService,
) -> None:
    """同一供应商下重复登记同一个 model_id 是纯粹的重复劳动，当场拒绝。"""
    provider = _provider(registry)
    _model(registry, provider.id, model_id="dup-model")

    with pytest.raises(ConflictError):
        _model(registry, provider.id, model_id="dup-model")


def test_same_model_id_under_different_providers_is_allowed(
    registry: ModelRegistryService,
) -> None:
    """两家供应商都叫 `gpt-4o-mini` 是常态（自建网关 + 官方），不该拦。"""
    first = _provider(registry, name="甲家")
    second = _provider(registry, name="乙家")

    _model(registry, first.id, model_id="gpt-4o-mini")
    _model(registry, second.id, model_id="gpt-4o-mini")  # 不该抛


def test_unknown_capability_is_rejected(registry: ModelRegistryService) -> None:
    provider = _provider(registry)
    with pytest.raises(InvalidRequestError) as excinfo:
        _model(registry, provider.id, capabilities=["chat", "fly"])
    assert "未知的能力标记" in str(excinfo.value)


def test_register_model_needs_an_existing_provider(registry: ModelRegistryService) -> None:
    with pytest.raises(NotFoundError):
        registry.register_model(provider_id="prov_不存在", model_id="m")


def test_dim_is_stored_for_embedding_models(registry: ModelRegistryService) -> None:
    """dim 是模型属性，登记一次就不该再让用户在两处各填一遍。"""
    provider = _provider(registry, kind="embedding")
    model = _model(registry, provider.id, model_id="bge-m3", dim=1024, capabilities=["embedding"])
    assert model.dim == 1024


# --------------------------------------------------------------------- 绑定


def test_bind_and_resolve(registry: ModelRegistryService) -> None:
    provider = _provider(registry)
    model = _model(registry, provider.id, model_id="deepseek-chat", capabilities=["chat"])

    registry.bind("chat", model.id)

    resolved = registry.resolve("chat")
    assert resolved is not None
    assert resolved[0].id == provider.id
    assert resolved[1].id == model.id


def test_unbound_slot_resolves_to_none(registry: ModelRegistryService) -> None:
    """**未绑定不是错误**，返回 None 让调用方回退到设置页那套。

    这正是"叠加层"的落点：升级不会让已有部署失效。
    """
    assert registry.resolve("chat") is None
    assert registry.bindings() == {}


def test_unbind_clears_the_binding(registry: ModelRegistryService) -> None:
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=["chat"])
    registry.bind("chat", model.id)

    registry.bind("chat", None)

    assert registry.resolve("chat") is None


def test_binding_requires_the_declared_capability(registry: ModelRegistryService) -> None:
    """把只声明了 embedding 的模型绑到「对话生成」上要当场拒绝。

    否则用户会得到一条"绑定成功"却在提问时报错的路径——排查成本很高。
    """
    provider = _provider(registry, kind="embedding")
    model = _model(registry, provider.id, capabilities=["embedding"])

    with pytest.raises(InvalidRequestError) as excinfo:
        registry.bind("chat", model.id)
    assert "未声明" in str(excinfo.value)


def test_model_without_capabilities_can_be_bound(registry: ModelRegistryService) -> None:
    """没声明能力时不拦：旧数据或手工登记可能留空，拦了反而没法用。"""
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=[])

    registry.bind("chat", model.id)

    assert registry.resolve("chat") is not None


def test_unknown_slot_is_rejected(registry: ModelRegistryService) -> None:
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=["chat"])
    with pytest.raises(InvalidRequestError) as excinfo:
        registry.bind("画图", model.id)
    assert "未知的用途" in str(excinfo.value)


def test_one_model_can_serve_multiple_slots(registry: ModelRegistryService) -> None:
    """同一家的同一个网关常常既做对话又做向量化（OpenAI 兼容服务）。"""
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=["chat", "embedding"])

    registry.bind("chat", model.id)
    registry.bind("embedding", model.id)

    assert registry.bindings() == {"chat": model.id, "embedding": model.id}


# --------------------------------------------------------------------- 级联


def test_delete_model_unbinds_it_from_slots(registry: ModelRegistryService) -> None:
    """**删模型必须解绑**：否则之后每次检索都报"绑定的模型不存在"，
    而用户在设置页只看到一个灰色下拉，很难联想到是自己删了模型。
    """
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=["chat"])
    registry.bind("chat", model.id)

    registry.delete_model(model.id)

    assert registry.resolve("chat") is None
    assert registry.bindings() == {}


def test_delete_provider_removes_its_models_and_unbinds(
    registry: ModelRegistryService, bundle
) -> None:  # type: ignore[no-untyped-def]
    """删供应商要连模型一起删——本项目连接没开 PRAGMA foreign_keys，级联不生效。"""
    provider = _provider(registry)
    model = _model(registry, provider.id, capabilities=["chat"])
    registry.bind("chat", model.id)

    registry.delete_provider(provider.id)

    assert registry.list_models() == []
    assert registry.resolve("chat") is None
    with pytest.raises(NotFoundError):
        registry.get_provider(provider.id)


def test_delete_provider_only_removes_its_own_models(registry: ModelRegistryService) -> None:
    first = _provider(registry, name="甲家")
    second = _provider(registry, name="乙家")
    _model(registry, first.id, model_id="a-model")
    kept = _model(registry, second.id, model_id="b-model")

    registry.delete_provider(first.id)

    assert [item.id for item in registry.list_models()] == [kept.id]


def test_dangling_binding_falls_back_instead_of_raising(
    registry: ModelRegistryService, bundle
) -> None:  # type: ignore[no-untyped-def]
    """绑定指向已不存在的模型时**回退而不是抛错**。

    这条路径正常操作走不到（删除时会解绑），但数据库被手工改过、
    或未来某条迁移出问题时会出现——那时让用户还能用，比整站报错好。
    """
    bundle.meta.set_setting("registry.slot.chat", "mdl_不存在")

    assert registry.resolve("chat") is None


# --------------------------------------------------------------------- 槽位定义


def test_slots_cover_the_three_model_uses() -> None:
    assert set(SLOTS) == {"chat", "embedding", "rerank"}
    for slot, spec in SLOTS.items():
        assert spec["label"], slot
        assert spec["capability"], slot


# --------------------------------------------------------------------- 供应商探活（评审批注 4）


@respx.mock
def test_probe_provider_reports_discovered_models(registry: ModelRegistryService) -> None:
    """注册时验活走 ``GET {base_url}/models``：不计费，同时验地址与凭据。"""
    provider = _provider(registry, api_key="sk-abc", base_url="https://api.example.com/v1")
    route = respx.get("https://api.example.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
    )

    detail = registry.probe_provider(provider.id)

    assert route.called
    assert "2 个模型" in detail
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-abc"


@respx.mock
def test_probe_provider_treats_401_as_bad_key(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-bad")
    respx.get("https://api.example.com/models").mock(return_value=httpx.Response(401))

    with pytest.raises(InvalidRequestError, match="API Key"):
        registry.probe_provider(provider.id)


@respx.mock
def test_probe_provider_does_not_fail_when_body_is_not_json(
    registry: ModelRegistryService,
) -> None:
    """有的端点不返回模型列表——那不算失败，只要鉴权过了就算可用。"""
    provider = _provider(registry, api_key="sk-abc")
    respx.get("https://api.example.com/models").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )

    assert "可用" in registry.probe_provider(provider.id)


def test_probe_provider_requires_base_url_and_key(registry: ModelRegistryService) -> None:
    no_url = _provider(registry, api_key="sk-abc", base_url="")
    with pytest.raises(InvalidRequestError, match="接口地址"):
        registry.probe_provider(no_url.id)

    no_key = _provider(registry, api_key="", base_url="https://api.example.com")
    with pytest.raises(InvalidRequestError, match="API Key"):
        registry.probe_provider(no_key.id)


# --------------------------------------------------------------------- 可用模型列表（下拉数据源）


@respx.mock
def test_list_available_models_cleans_and_dedupes(registry: ModelRegistryService) -> None:
    """上游列表里混着重复、空 id 与非字典条目——清理掉，别让下拉框出现重复项。"""
    provider = _provider(registry, api_key="sk-abc", base_url="https://api.example.com/v1")
    respx.get("https://api.example.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "BAAI/bge-m3", "owned_by": "siliconflow"},
                    {"id": "BAAI/bge-m3"},
                    {"id": "   "},
                    "not-a-dict",
                    {"id": "Qwen/Qwen2.5"},
                ]
            },
        )
    )

    models = registry.list_available_models(provider.id)

    assert models == [
        {"model_id": "BAAI/bge-m3", "owned_by": "siliconflow"},
        {"model_id": "Qwen/Qwen2.5", "owned_by": ""},
    ]


@respx.mock
def test_list_available_models_empty_when_upstream_has_no_list(
    registry: ModelRegistryService,
) -> None:
    """有的端点不返回模型列表：返回空列表而**不报错**，界面仍可手写模型 ID。"""
    provider = _provider(registry, api_key="sk-abc")
    respx.get("https://api.example.com/models").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )

    assert registry.list_available_models(provider.id) == []


@respx.mock
def test_list_available_models_maps_bad_key(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-bad")
    respx.get("https://api.example.com/models").mock(return_value=httpx.Response(401))

    with pytest.raises(InvalidRequestError, match="API Key"):
        registry.list_available_models(provider.id)


@respx.mock
def test_probe_provider_maps_transport_error_to_upstream(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-abc")
    respx.get("https://api.example.com/models").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(UpstreamError, match="无法连接"):
        registry.probe_provider(provider.id)


@respx.mock
def test_probe_provider_maps_server_error_to_upstream(registry: ModelRegistryService) -> None:
    provider = _provider(registry, api_key="sk-abc")
    respx.get("https://api.example.com/models").mock(return_value=httpx.Response(503))

    with pytest.raises(UpstreamError, match="503"):
        registry.probe_provider(provider.id)


# ------------------------------------------- 嵌入模型作为知识库属性（v11 设计调整）


def _embedding_model(registry: ModelRegistryService, **overrides):  # type: ignore[no-untyped-def]
    provider = _provider(registry, kind="embedding", api_key="sk-abc")
    payload = {"model_id": "bge-m3", "capabilities": ["embedding"], "dim": 1024}
    payload.update(overrides)
    return _model(registry, provider.id, **payload)


def test_embedding_target_returns_provider_and_model(registry: ModelRegistryService) -> None:
    model = _embedding_model(registry)

    provider, resolved = registry.embedding_target(model.id)

    assert resolved.model_id == "bge-m3"
    assert resolved.dim == 1024
    assert provider.api_key == "sk-abc"


def test_embedding_target_rejects_a_non_embedding_model(
    registry: ModelRegistryService,
) -> None:
    model = _model(registry, _provider(registry).id, capabilities=["chat"], dim=None)

    with pytest.raises(InvalidRequestError, match="embedding"):
        registry.embedding_target(model.id)


def test_embedding_target_requires_a_dimension(registry: ModelRegistryService) -> None:
    """维度没登记就无法建向量表——必须在这里拦住，而不是摄入到一半才炸。"""
    model = _embedding_model(registry, dim=None)

    with pytest.raises(InvalidRequestError, match="维度"):
        registry.embedding_target(model.id)


def test_embedding_target_rejects_a_disabled_provider(
    registry: ModelRegistryService,
) -> None:
    model = _embedding_model(registry)
    registry.update_provider(model.provider_id, enabled=False)

    with pytest.raises(InvalidRequestError, match="停用"):
        registry.embedding_target(model.id)


def test_embedding_target_rejects_a_missing_key(registry: ModelRegistryService) -> None:
    model = _embedding_model(registry)
    # 传空串 = 清空密钥（传 None 是"保持原值"，那是另一条语义）
    registry.update_provider(model.provider_id, api_key="")

    with pytest.raises(InvalidRequestError, match="API Key"):
        registry.embedding_target(model.id)


# ------------------------------------------------------------ 凭据收进钥匙串（M5 阶段 6）
#
# 这一组**自带本机库**（带 `local` marker）：文件里其他用例跑在 PG 上，而"凭据的家是
# 系统钥匙串"这件事只在本机档成立（R14：服务器档恒 NullSecretStore，它库里那份凭据不动）。
#
# 判据一句话：**API 形状零改动**（`ModelProviderRecord.api_key` 读得出真正的钥匙、
# `/model-registry` 那两个字段照旧），变的只是"它从哪儿来"——DB 那一列在钥匙串档上恒空。


@pytest.fixture
def local_bundle(tmp_path: Path) -> Iterator[StoreBundle]:
    stores = build_stores(
        Settings(_env_file=None, deployment="local", data_dir=tmp_path / "data")  # type: ignore[call-arg]
    )
    yield stores
    reset_stores()


@pytest.fixture
def keychain() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def keyed_registry(local_bundle: StoreBundle, keychain: InMemorySecretStore):
    return ModelRegistryService(local_bundle, secrets=keychain)


@pytest.mark.local
def test_a_new_provider_keeps_its_key_in_the_keychain_only(
    keyed_registry: ModelRegistryService,
    local_bundle: StoreBundle,
    keychain: InMemorySecretStore,
) -> None:
    """建档那次就把钥匙写进钥匙串；**DB 列是空的**，而返回的记录读得出真正的钥匙。"""
    created = keyed_registry.create_provider(
        kind="llm", name="供应商甲", base_url="https://api.example.com", api_key="sk-real-key"
    )

    assert keychain.get(model_provider_target(created.id)) == "sk-real-key"
    assert local_bundle.meta.get_model_provider(created.id).api_key == "", "明文不进库"
    assert created.api_key == "sk-real-key", "API 形状不变：调用方照旧读得到它"


@pytest.mark.local
def test_reads_fill_the_key_from_the_keychain(
    keyed_registry: ModelRegistryService, keychain: InMemorySecretStore
) -> None:
    """三个读面都要填：``get_provider`` / ``list_providers`` / ``resolve``。

    `resolve` 是最容易漏的那个（一条 JOIN 出来的记录），而它正好在
    "每建一次 LLM 客户端"的热路径上——漏了它的表现是"设置页显示已配置，
    真跑起来报没有 API Key"。
    """
    created = keyed_registry.create_provider(kind="llm", name="甲", api_key="sk-real-key")

    assert keyed_registry.get_provider(created.id).api_key == "sk-real-key"
    assert [item.api_key for item in keyed_registry.list_providers()] == ["sk-real-key"]

    model = keyed_registry.register_model(
        provider_id=created.id, model_id="m-1", capabilities=["chat"]
    )
    keyed_registry.bind("chat", model.id)
    resolved = keyed_registry.resolve("chat")
    assert resolved is not None
    assert resolved[0].api_key == "sk-real-key"

    keychain.delete(model_provider_target(created.id))
    assert keyed_registry.get_provider(created.id).api_key == ""
    with pytest.raises(InvalidRequestError, match="API Key"):
        keyed_registry.chat_target(model.id)


@pytest.mark.local
def test_updating_the_key_lands_in_the_keychain_and_empty_clears_it(
    keyed_registry: ModelRegistryService,
    local_bundle: StoreBundle,
    keychain: InMemorySecretStore,
) -> None:
    created = keyed_registry.create_provider(kind="llm", name="甲", api_key="sk-one")

    keyed_registry.update_provider(created.id, api_key="sk-two")
    assert keychain.get(model_provider_target(created.id)) == "sk-two"
    assert local_bundle.meta.get_model_provider(created.id).api_key == ""

    # None = 保持原值（前端不动那一栏时传的就是它）
    kept = keyed_registry.update_provider(created.id, name="改了名字")
    assert kept.api_key == "sk-two" and kept.name == "改了名字"

    # 空串 = 清空（明确动作）
    cleared = keyed_registry.update_provider(created.id, api_key="")
    assert cleared.api_key == "" and keychain.get(model_provider_target(created.id)) is None


@pytest.mark.local
def test_a_masked_key_is_treated_as_unchanged(
    keyed_registry: ModelRegistryService, keychain: InMemorySecretStore
) -> None:
    """界面上显示的掩码被回传时**不能当成新密钥**（那样密钥就被毁了）。"""
    created = keyed_registry.create_provider(kind="llm", name="甲", api_key="sk-abcdefghij")

    record = keyed_registry.update_provider(created.id, api_key=mask_secret("sk-abcdefghij"))

    assert record.api_key == "sk-abcdefghij"
    assert keychain.get(model_provider_target(created.id)) == "sk-abcdefghij"


@pytest.mark.local
def test_deleting_a_provider_takes_its_key_out_of_the_keychain(
    keyed_registry: ModelRegistryService, keychain: InMemorySecretStore
) -> None:
    """删供应商**先删钥匙串那一条**：不留一条谁也不认领的秘密。"""
    created = keyed_registry.create_provider(kind="llm", name="甲", api_key="sk-real-key")

    keyed_registry.delete_provider(created.id)

    assert keychain.get(model_provider_target(created.id)) is None
    assert keychain.names() == ()


@pytest.mark.local
def test_without_a_keychain_the_column_stays_the_home(local_bundle: StoreBundle) -> None:
    """不传钥匙串（服务器档 / 手工装配）时**一条行为都不变**：库那一列就是凭据的家。"""
    registry = ModelRegistryService(local_bundle)

    created = registry.create_provider(kind="llm", name="甲", api_key="sk-in-db")

    assert local_bundle.meta.get_model_provider(created.id).api_key == "sk-in-db"
    assert registry.get_provider(created.id).api_key == "sk-in-db"
