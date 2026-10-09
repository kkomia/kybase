"""运行期配置：三层优先级、快照与掩码（`services/runtime_config.py`）。

交接文档把"配置层 721 行没有直接单测"列为**最大的质量缺口**——设置页改错一个字段
可能悄悄影响所有调用，而没有测试会红。本文件补上最要紧的几条：

1. 优先级 **数据库 > `.env` 引导 > 代码默认**（顺序错了会让"部署时预设"永远压住网页设置）；
2. 密钥**只回显掩码**，且**拒绝把掩码当新值回写**（那会把真密钥写成 `sk-xu…ten`）；
3. 快照的**模型身份只来自注册表**（v0.8 归属整理），未绑定即未配置。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.storage import build_stores, reset_stores
from app.services.credentials import MIGRATED_SETTING_KEYS
from app.services.model_registry import ModelRegistryService
from app.services.runtime_config import (
    DEFAULTS,
    KEYCHAIN_SETTING_KEYS,
    SECRET_KEYS,
    SETTING_GROUPS,
    RuntimeConfigService,
    mask_secret,
)
from app.services.secrets import InMemorySecretStore, setting_target
from app.storage.base import StoreBundle
from tests.conftest import bind_model


def test_defaults_apply_when_nothing_is_configured(runtime: RuntimeConfigService) -> None:
    assert runtime.get("embedding.batch_size") == DEFAULTS["embedding.batch_size"]
    assert runtime.get_int("chat.top_k") == 6
    assert runtime.get("不存在的键") == ""


def test_env_bootstrap_is_the_middle_layer(bundle) -> None:  # type: ignore[no-untyped-def]
    """`.env` 只是"部署时预设一次"，它高于代码默认、低于数据库。"""
    settings = Settings(embedding_batch_size=8, llm_temperature=0.9)  # type: ignore[call-arg]
    runtime = RuntimeConfigService(bundle, settings)

    assert runtime.get("embedding.batch_size") == "8"
    assert runtime.get("llm.temperature") == "0.9"


def test_database_beats_env_and_defaults(bundle) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(embedding_batch_size=8)  # type: ignore[call-arg]
    runtime = RuntimeConfigService(bundle, settings)

    runtime.set({"embedding.batch_size": "4"})

    assert runtime.get("embedding.batch_size") == "4"


def test_memory_keys_can_be_preset_from_env(bundle) -> None:  # type: ignore[no-untyped-def]
    """记忆的开关 / 落点也能从 ``.env`` 预设（v0.1.1）。

    为什么值得这两项有一条路：容器部署要在 compose 里一次写清"记忆落在哪、
    要不要开"，而不是让用户先去界面上找开关。而在这之前 ``_bootstrap_value``
    的映射表里没有它们——写进 .env 是**静默无效**的（这比没有更糟）。
    （第三项"服务地址"已随 ReMe 一起删，见 memory.py 的模块头。）
    """
    settings = Settings(  # type: ignore[call-arg]
        memory_enabled=True,
        memory_workspace="memo",
    )
    runtime = RuntimeConfigService(bundle, settings)

    assert runtime.get_bool("memory.enabled") is True
    assert runtime.get("memory.workspace") == "memo"


def test_memory_defaults_stay_when_the_env_says_nothing(bundle) -> None:  # type: ignore[no-untyped-def]
    """没给引导值时**沿用代码默认**：开关是**开**（v0.56 改，§7.3）。

    旧口径默认关，理由是"打开它会启动定期捕获"——一次捕获就是一次模型调用。
    档案制把"注入"与"捕获"拆成两个开关之后，这个理由失效了：注入本身
    **一次模型调用都不产生**（只是把档案拼进这一轮的上下文），所以"默认关"
    只等于"这个功能默认不存在"。而捕获那条路已经在链路上废掉（§4.1），
    默认值怎么设都不会让它开始花钱。
    """
    runtime = RuntimeConfigService(bundle, Settings())  # type: ignore[call-arg]

    assert runtime.get("memory.enabled") == DEFAULTS["memory.enabled"] == "true"
    assert runtime.get("memory.workspace") == DEFAULTS["memory.workspace"]
    # 人设清单只剩两份（§7.2）：档案不在里面——它走自己的开关
    assert runtime.get("memory.persona_files") == DEFAULTS["memory.persona_files"]
    assert runtime.get("memory.persona_files") == "SOUL.md,AGENTS.md"


def test_empty_value_falls_back_to_the_default(runtime: RuntimeConfigService) -> None:
    """写空 = "没设过"：**类型化读取器**（get_int/_as_float 等）据此回落默认值。

    `get()` 本身如实回显存进去的空串；兜默认是读取方的责任——这条区别值得钉住，
    因为它决定了"清空一个项"到底是恢复默认还是变成一个空值。
    """
    runtime.set({"chat.top_k": "20"})
    assert runtime.get_int("chat.top_k") == 20

    runtime.set({"chat.top_k": ""})

    assert runtime.get("chat.top_k") == ""
    assert runtime.get_int("chat.top_k") == int(DEFAULTS["chat.top_k"])


def test_masked_secret_is_never_written_back(runtime: RuntimeConfigService) -> None:
    """界面上显示的 `sk-xu…ten` 不是密钥——把它写回去会把真凭据毁掉。"""
    runtime.set({"mineru.token": "sk-real-value"})
    runtime.set({"mineru.token": mask_secret("sk-real-value")})

    assert runtime.get("mineru.token") == "sk-real-value"


def test_describe_masks_secrets_and_reports_configured(
    runtime: RuntimeConfigService,
) -> None:
    runtime.set({"mineru.token": "sk-real-value"})
    groups = {group["key"]: group for group in runtime.describe()["groups"]}
    fields = {field["key"]: field for field in groups["mineru"]["fields"]}

    token = fields["mineru.token"]
    assert token["configured"] is True
    assert "…" in token["value"]
    assert "sk-real-value" not in token["value"]
    # 非密钥原样回显
    assert fields["mineru.endpoint"]["value"] != ""


def test_mask_secret_hides_short_values_completely() -> None:
    assert mask_secret("") == ""
    assert mask_secret("short") == "•••••"
    assert mask_secret("sk-abcdefghij").startswith("sk-")
    assert "…" in mask_secret("sk-abcdefghij")


def test_every_secret_key_is_a_declared_setting() -> None:
    declared = {field["key"] for group in SETTING_GROUPS.values() for field in group["fields"]}
    assert declared >= SECRET_KEYS


# --------------------------------------------------------------------- 快照


def test_embedding_snapshot_is_unconfigured_without_a_binding(
    runtime: RuntimeConfigService,
) -> None:
    assert runtime.embedding().is_configured is False
    assert runtime.llm().is_configured is False


def test_embedding_snapshot_takes_identity_from_the_registry(
    runtime: RuntimeConfigService, bundle
) -> None:  # type: ignore[no-untyped-def]
    bind_model(
        ModelRegistryService(bundle),
        "embedding",
        model_id="bge-m3",
        capabilities=["embedding"],
        dim=1024,
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-embed",
    )
    runtime.set({"embedding.batch_size": "16"})

    snapshot = runtime.embedding()

    assert (snapshot.base_url, snapshot.api_key, snapshot.model_id) == (
        "https://api.siliconflow.cn/v1",
        "sk-embed",
        "bge-m3",
    )
    assert (snapshot.dim, snapshot.batch_size) == (1024, 16)
    assert snapshot.is_configured is True
    # 协议是行为参数：没设过就是默认（OpenAI 兼容），既有部署一位不变
    assert snapshot.protocol == "openai"


def test_embedding_protocol_comes_from_the_setting(
    runtime: RuntimeConfigService,
) -> None:
    """`embedding.protocol` 是设置页那一栏（也能由 .env 引导），默认 openai。"""
    runtime.set({"embedding.protocol": "wemm"})

    assert runtime.embedding().protocol == "wemm"

    # 不认识的值归一化成默认，而不是让整条向量通道不可用
    runtime.set({"embedding.protocol": "ollama"})

    assert runtime.embedding().protocol == "openai"


def test_media_capability_is_declared_per_registered_model(
    runtime: RuntimeConfigService, bundle
) -> None:  # type: ignore[no-untyped-def]
    """门控要问"**这台机器上有没有任何一处**能嵌媒体"。

    协议是按模型的：全局默认还是 openai，只要某一个库绑的模型声明了 wemm，
    媒体直通解析器就得挂上——否则那个库的图片 / 视频在解析阶段就被"暂不支持"挡掉了。
    """
    registry = ModelRegistryService(bundle)

    assert runtime.embedding_supports_media() is False  # 什么都没配

    # 一个 OpenAI 兼容的模型：与从前一样，没有媒体能力
    bind_model(registry, "embedding", model_id="bge-m3", capabilities=["embedding"], dim=1024)
    assert runtime.embedding_supports_media() is False

    # 另一个模型声明了 wemm（按模型设的那一栏）——门控必须开
    provider = registry.create_provider(
        kind="embedding", name="局域网 WeMM", base_url="http://192.168.31.18:8234"
    )
    registry.register_model(
        provider_id=provider.id,
        model_id="WeMM-Embedding-2B-Q4_K_M.gguf",
        dim=1024,
        capabilities=["embedding"],
        options={"protocol": "wemm"},
    )

    assert runtime.embedding_supports_media() is True


def test_global_wemm_protocol_opens_the_media_gate_without_any_model(
    runtime: RuntimeConfigService,
) -> None:
    """全局协议切成 wemm 时，连模型都不用登记就该开（另一条判据）。"""
    runtime.set({"embedding.protocol": "wemm"})

    assert runtime.embedding_supports_media() is True


def test_llm_snapshot_lets_model_options_override_sampling(
    runtime: RuntimeConfigService, bundle
) -> None:  # type: ignore[no-untyped-def]
    registry = ModelRegistryService(bundle)
    provider = registry.create_provider(
        kind="llm", name="推理家", base_url="https://reason.example.com/v1", api_key="sk-r"
    )
    model = registry.register_model(
        provider_id=provider.id,
        model_id="deepseek-reasoner",
        capabilities=["chat"],
        options={"temperature": 0.1, "enable_thinking": True},
    )
    registry.bind("chat", model.id)
    runtime.set({"llm.temperature": "0.7"})

    snapshot = runtime.llm()

    # 模型自带的采样参数优先于设置页（推理模型的最优区间不同）
    assert snapshot.temperature == 0.1
    assert snapshot.enable_thinking is True
    # 模型没指定长度上限时不发这个字段（设置页也管不着它了）
    assert snapshot.max_tokens is None
    assert snapshot.model_id == "deepseek-reasoner"


def test_thinking_defaults_to_on_with_medium_effort(runtime: RuntimeConfigService, bundle) -> None:  # type: ignore[no-untyped-def]
    """思考**默认开**：主流模型默认都思考，关掉是例外。强度默认中档。"""
    bind_model(ModelRegistryService(bundle), "chat", model_id="m-default", capabilities=["chat"])

    snapshot = runtime.llm()

    assert snapshot.enable_thinking is True
    assert snapshot.thinking_effort == "medium"


def test_model_options_override_thinking_effort_and_dialect(
    runtime: RuntimeConfigService, bundle
) -> None:  # type: ignore[no-untyped-def]
    registry = ModelRegistryService(bundle)
    provider = registry.create_provider(
        kind="llm", name="推理家", base_url="https://reason.example.com/v1", api_key="sk-r"
    )
    model = registry.register_model(
        provider_id=provider.id,
        model_id="m",
        capabilities=["chat"],
        options={"enable_thinking": False, "thinking_effort": "high", "thinking_dialect": "qwen"},
    )
    registry.bind("chat", model.id)

    snapshot = runtime.llm()

    assert snapshot.enable_thinking is False
    assert snapshot.thinking_effort == "high"
    assert snapshot.thinking_dialect == "qwen"


def test_describe_exposes_thinking_effort_as_a_select(runtime: RuntimeConfigService) -> None:
    """设置页靠这个结构渲染下拉；候选值必须由后端给出，前端不硬编码。"""
    groups = {group["key"]: group for group in runtime.describe()["groups"]}
    fields = {field["key"]: field for field in groups["llm"]["fields"]}

    assert fields["llm.enable_thinking"]["type"] == "bool"
    effort = fields["llm.thinking_effort"]
    assert effort["type"] == "select"
    assert [option["value"] for option in effort["options"]] == ["low", "medium", "high"]
    assert effort["value"] == "medium"


def test_describe_exposes_search_providers_as_a_select(runtime: RuntimeConfigService) -> None:
    """搜索服务商同样由后端给候选值（**从 `web.SEARCH_PROVIDERS` 派生**）。

    原先它是自由文本框、标签里写着「（tavily / bocha）」，等于让用户照着**手打**——
    打错一个字母就是一句"不认识的搜索供应商"。这里钉住三件事：
    是 `select`、候选值来自那张表、并且默认值确实落在候选里。
    """
    from app.services.web import SEARCH_PROVIDERS

    groups = {group["key"]: group for group in runtime.describe()["groups"]}
    fields = {field["key"]: field for field in groups["web"]["fields"]}

    provider = fields["web.search_provider"]
    assert provider["type"] == "select"
    assert [option["value"] for option in provider["options"]] == list(SEARCH_PROVIDERS)
    # 密钥仍是 secret：对外只有掩码与"是否已配置"
    assert fields["web.search_api_key"]["type"] == "secret"
    assert provider["value"] in SEARCH_PROVIDERS


def test_cloud_parser_snapshots_come_from_settings(runtime: RuntimeConfigService) -> None:
    runtime.set({"mineru.token": "m-token", "paddleocr.token": "p-token"})

    assert runtime.mineru().token == "m-token"
    assert runtime.paddleocr().token == "p-token"


def test_max_tokens_is_no_longer_a_setting(runtime: RuntimeConfigService) -> None:
    """回复长度上限**不再是设置项**：默认不传这个字段，交还给模型自己。

    它曾经默认 2048，实测会把回复预算掐死在思考阶段、正文一个字都出不来（且时好时坏）。
    抬到 16384 只是降低概率；正确做法是不替模型决定长度。想显式限制的走模型注册的
    ``options.max_tokens``（见 test_registry_bridge）。
    """
    assert "llm.max_tokens" not in DEFAULTS
    assert all(
        field["key"] != "llm.max_tokens"
        for group in SETTING_GROUPS.values()
        for field in group["fields"]
    )
    assert runtime.llm().max_tokens is None


# ------------------------------------------------------------------ 读取缓存


def _count_reads(bundle, monkeypatch):  # type: ignore[no-untyped-def]
    """把仓储的批量读取包一层计数：用来断言"第二次真的没查库"。"""
    calls: list[list[str]] = []
    original = bundle.meta.get_settings

    def spy(keys):  # type: ignore[no-untyped-def]
        calls.append(list(keys))
        return original(keys)

    monkeypatch.setattr(bundle.meta, "get_settings", spy)
    return calls


def test_repeated_reads_hit_the_cache(runtime, bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """同一个键连着读两次，只查一次库。

    一次读取的固定开销实测约 6ms（PG 自己只花 2ms，其余是连接池借还 + 往返），
    而它在每次请求的路径上——一轮对话要读十几次设置。
    """
    calls = _count_reads(bundle, monkeypatch)

    assert runtime.get_int("chat.top_k") == runtime.get_int("chat.top_k")
    assert len(calls) == 1


def test_a_key_that_is_not_in_the_database_is_remembered_too(
    runtime,
    bundle,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """ "库里没有这一项"也要记住。

    否则"没配过的键"每次都白查一遍——而设置页打开的 ``describe()`` 里大半都是这种键。
    """
    calls = _count_reads(bundle, monkeypatch)

    assert runtime.get("mineru.token") == ""
    assert runtime.get("mineru.token") == ""

    assert len(calls) == 1


def test_many_reads_share_one_query(runtime, bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``get_many`` 只发一条 SQL（§12.116 的口径），且第二次不再发。"""
    keys = ["chat.top_k", "chat.section_chars", "chat.material_chars"]
    calls = _count_reads(bundle, monkeypatch)

    runtime.get_many(keys)
    runtime.get_many(keys)

    assert len(calls) == 1
    assert sorted(calls[0]) == sorted(keys)


def test_set_clears_the_cache_immediately(runtime, bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """写完立刻读得到新值：``set`` 主动清缓存，"改完马上看"不走 TTL。"""
    calls = _count_reads(bundle, monkeypatch)

    runtime.get("chat.top_k")
    runtime.set({"chat.top_k": "9"})

    assert runtime.get_int("chat.top_k") == 9
    # 首次读一次 + set 清掉之后再读一次
    assert len(calls) == 2


def test_keys_written_by_other_services_are_never_cached(
    runtime,
    bundle,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """按实体生成的键（``document.<id>.*`` / ``trash.<id>.*``）**不进缓存**。

    它们的写入方（摄入、回收站）直接落库、不经过本服务的 ``set()``；
    缓存了就会在写入后读到旧值，而"偶尔读到旧值"这种 bug 极难复现。
    """
    calls = _count_reads(bundle, monkeypatch)
    key = "document.doc_1.original_path"

    runtime.get(key)
    runtime.get(key)

    assert len(calls) == 2


def test_entries_expire_after_the_ttl(runtime, bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """过了 TTL 要重新查库——这一层的作用是"同一轮里反复读"，不是"永不更新"。"""
    calls = _count_reads(bundle, monkeypatch)
    # 取负数而不是 0：0 会让"同一时刻的两次读"仍算命中，用例会偶然变红
    monkeypatch.setattr("app.services.runtime_config._CACHE_TTL_SECONDS", -1.0)

    runtime.get("chat.top_k")
    runtime.get("chat.top_k")

    assert len(calls) == 2


# ------------------------------------------------------------ 钥匙串收编（M5 阶段 6）
#
# 这一节的用例**自带本机库**（带 `local` marker）：文件里其他用例跑在 PG 上，而"钥匙串
# 是凭据的家"这件事只在本机档成立（R14：服务器档恒 NullSecretStore，它库里那份凭据不动）。
#
# 口径（方案 §4.2）：**收编过的那几个键**（`KEYCHAIN_SETTING_KEYS`）只问钥匙串——
# 读不到 = 没配，不回退去读库里那份明文；**只登记的那几个**（mineru / paddleocr token）
# 照旧走库与 `.env`（它们的家没有变）。前端契约（`describe()` 的掩码与 `configured`）
# 一个字都不改。


@pytest.fixture
def local_bundle(tmp_path: Path) -> Iterator[StoreBundle]:
    """本机档的真装配（SQLite 落在 tmp_path）。"""
    stores = build_stores(
        Settings(_env_file=None, deployment="local", data_dir=tmp_path / "data")  # type: ignore[call-arg]
    )
    yield stores
    reset_stores()


@pytest.fixture
def keychain() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def keyed_runtime(local_bundle: StoreBundle, keychain: InMemorySecretStore) -> RuntimeConfigService:
    """连着钥匙串的那一份（本机档 + 钥匙串可用 → 收编过的键改道）。"""
    return RuntimeConfigService(local_bundle, secrets=keychain)


@pytest.mark.local
def test_the_redirect_list_matches_the_migrator() -> None:
    """改道的那几个 == 迁移器真正会搬的那几个（两份清单必须一致）。

    它们分开写是因为**读者不同**：这一份说的是"读哪儿"，那一份说的是"搬什么"。
    但要是不一致，就会出现两种坏结果之一：读改了却没搬（用户配好的值当场变成"没配"），
    或者搬了却没改读（库里清了、读的还是库 → 永远读到空）。
    """
    assert KEYCHAIN_SETTING_KEYS <= SECRET_KEYS
    assert set(MIGRATED_SETTING_KEYS) & SECRET_KEYS == KEYCHAIN_SETTING_KEYS
    assert frozenset({"web.search_api_key"}) == KEYCHAIN_SETTING_KEYS


@pytest.mark.local
def test_a_collected_key_is_read_from_the_keychain(
    keyed_runtime: RuntimeConfigService,
    local_bundle: StoreBundle,
    keychain: InMemorySecretStore,
) -> None:
    """收编过的键**只问钥匙串**：库里那份明文在也不读（过渡态不是真相源）。"""
    local_bundle.meta.set_setting("web.search_api_key", "still-in-the-database")

    assert keyed_runtime.get("web.search_api_key") == "", "库里那份明文不该被读出来"

    keychain.set(setting_target("web.search_api_key"), "from-the-keychain")
    assert keyed_runtime.get("web.search_api_key") == "from-the-keychain"

    keychain.delete(setting_target("web.search_api_key"))
    assert keyed_runtime.get("web.search_api_key") == "", "读不到 = 没配"


@pytest.mark.local
def test_a_collected_key_is_read_from_the_keychain_in_bulk_too(
    keyed_runtime: RuntimeConfigService, keychain: InMemorySecretStore
) -> None:
    """``get_many`` 也要走同一条口径（快照那几个读都走它）。

    漏了它，那些读就会"读不到 → 悄悄回落到库里那份明文"——正是第一条口径要挡的事。
    """
    keychain.set(setting_target("web.search_api_key"), "bulk-key")

    values = keyed_runtime.get_many(["web.search_api_key", "chat.top_k"])

    assert values["web.search_api_key"] == "bulk-key"
    assert values["chat.top_k"] == DEFAULTS["chat.top_k"], "没改道的键照旧"


@pytest.mark.local
def test_writing_a_collected_key_lands_in_the_keychain_only(
    keyed_runtime: RuntimeConfigService,
    local_bundle: StoreBundle,
    keychain: InMemorySecretStore,
) -> None:
    """写入路径：**密钥落钥匙串、库里不落**；空值就是删掉那一条。"""
    local_bundle.meta.set_setting("web.search_api_key", "the-old-plaintext")

    keyed_runtime.set({"web.search_api_key": "the-new-key"})

    assert keychain.get(setting_target("web.search_api_key")) == "the-new-key"
    assert local_bundle.meta.get_setting("web.search_api_key") is None, (
        "库里那份旧明文顺手清掉（它已经是死数据，留着只会让「还有 N 处等着迁」永远不为 0）"
    )

    keyed_runtime.set({"web.search_api_key": ""})

    assert keychain.get(setting_target("web.search_api_key")) is None
    assert local_bundle.meta.get_setting("web.search_api_key") is None


@pytest.mark.local
def test_a_masked_value_is_never_written_into_the_keychain(
    keyed_runtime: RuntimeConfigService, keychain: InMemorySecretStore
) -> None:
    """掩码被当成新值回写 → **跳过**（老坑；现在它挡在写钥匙串之前）。"""
    keyed_runtime.set({"web.search_api_key": "the-real-key"})

    keyed_runtime.set({"web.search_api_key": mask_secret("the-real-key")})

    assert keychain.get(setting_target("web.search_api_key")) == "the-real-key"


@pytest.mark.local
def test_a_registered_only_key_still_reads_and_writes_the_database(
    keyed_runtime: RuntimeConfigService,
    local_bundle: StoreBundle,
    keychain: InMemorySecretStore,
) -> None:
    """**只登记、没收编**的那两个照旧走库（它们的家没有变）。

    把它们的读也改成"只看钥匙串"，用户原先配好的那份会当场变成"没配"（设置页显示未配置），
    而迁移器又不去搬它们——那个值等于被静默丢掉。
    """
    local_bundle.meta.set_setting("mineru.token", "db-mineru-token")

    assert keyed_runtime.get("mineru.token") == "db-mineru-token"

    keyed_runtime.set({"mineru.token": "db-mineru-token-2"})

    assert local_bundle.meta.get_setting("mineru.token") == "db-mineru-token-2"
    assert "kylab:setting:mineru.token" not in keychain.names(), "没收编的键不进钥匙串"


@pytest.mark.local
def test_describe_keeps_the_same_contract(
    keyed_runtime: RuntimeConfigService, keychain: InMemorySecretStore
) -> None:
    """**前端契约零改动**：`describe()` 回的仍是掩码 + ``configured``（值不出去）。"""
    keychain.set(setting_target("web.search_api_key"), "sk-abcdefghij")

    entry = next(
        field
        for group in keyed_runtime.describe()["groups"]
        for field in group["fields"]
        if field["key"] == "web.search_api_key"
    )

    assert entry["value"] == mask_secret("sk-abcdefghij")
    assert entry["configured"] is True
    assert "sk-abcdefghij" not in str(entry), "原值一个字节都不出去"


@pytest.mark.local
def test_without_a_keychain_everything_stays_as_before(
    local_bundle: StoreBundle,
) -> None:
    """不传钥匙串（服务器档 / 手工装配 / CLI）时**一条行为都不变**：库就是凭据的家。"""
    runtime = RuntimeConfigService(local_bundle)
    local_bundle.meta.set_setting("web.search_api_key", "db-plaintext")

    assert runtime.get("web.search_api_key") == "db-plaintext"

    runtime.set({"web.search_api_key": "db-plaintext-2"})

    assert local_bundle.meta.get_setting("web.search_api_key") == "db-plaintext-2"


@pytest.mark.local
def test_an_unavailable_store_keeps_the_database_as_the_home(
    local_bundle: StoreBundle,
) -> None:
    """钥匙串**不可用**（Linux 桌面 / CI / 容器）时照旧走库：那种档里库就是家。

    在这一档上"一律走钥匙串"会把用户已经配好的凭据读成"没配"（而写又写不进去），
    等于把一个可用的配置功能关掉。判据只有一处：``secrets.use_keychain``。
    """
    runtime = RuntimeConfigService(local_bundle, secrets=InMemorySecretStore(available=False))
    local_bundle.meta.set_setting("web.search_api_key", "db-plaintext")

    assert runtime.get("web.search_api_key") == "db-plaintext"
    runtime.set({"web.search_api_key": "db-plaintext-2"})
    assert local_bundle.meta.get_setting("web.search_api_key") == "db-plaintext-2"
