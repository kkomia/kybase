"""插件包与本地市场（v0.43）。

镜像同构：``app/services/plugins.py`` → 本文件。

这一层的错法都偏安静，所以逐条钉住：

1. **状态与内容分离**：插件目录**只读**（启停写在 app_settings）；
   写进去的话，"用户自己的插件目录"就会被我们改脏，而这件事在界面上看不出来。
2. **校验失败的要带着原因出现**：缺 name、名字不合法、组件路径逃逸，
   都不加载——但**不能从列表里消失**（静默藏掉会让用户以为没装上）。
3. **first match wins 是用户优先**（与技能那一档正好相反）：同名时以数据目录
   那份为准，内置那份是兜底。
4. **做不到的要明说**：命令 / 钩子 / 工具本轮只列出，每一类的状态里都要有"未实现"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.plugins import (
    BLOCKED_PREFIX,
    ENABLED_PREFIX,
    PLUGIN_FILE,
    PluginManifest,
    PluginService,
)


class _FakeSettings:
    """只够 ``PluginService`` 用的键值仓（它就只碰 app_settings 这一个仓储）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_setting(self, key: str) -> str | None:
        return self.values.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete_setting(self, key: str) -> None:
        self.values.pop(key, None)


class _FakeStores:
    """刻意**不**构造真的 ``StoreBundle``：那要求把五个仓储都填上，
    而填假的那几个只会让"这个服务到底依赖什么"变得看不清（照 MCP 客户端那份用例）。"""

    def __init__(self) -> None:
        self.app_settings = _FakeSettings()


def _write_plugin(root: Path, directory_name: str, **manifest: object) -> Path:
    """在 ``root/directory_name/`` 放一个插件。

    写进 ``plugin.json`` 的名字默认就是目录名，要造"目录名与 manifest 不一致 / 名字不合法"
    那种包时用 ``declared=…`` 显式覆盖（名字是 manifest 的字段，不能当形参）。
    """
    directory = root / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"name": directory_name}
    payload.update(manifest)
    (directory / PLUGIN_FILE).write_text(json.dumps(payload), encoding="utf-8")
    return directory


def _write_skill(plugin: Path, name: str, *, description: str = "干某件事") -> None:
    directory = plugin / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n", encoding="utf-8"
    )


def _service(tmp_path: Path, *, builtin: Path | None = None) -> tuple[PluginService, _FakeStores]:
    stores = _FakeStores()
    service = PluginService(
        tmp_path / "data", stores, builtin_dir=builtin or (tmp_path / "builtin")
    )  # type: ignore[arg-type]
    return service, stores


# --------------------------------------------------------------------- 发现源


def test_lists_user_and_builtin_plugins(tmp_path: Path) -> None:
    """两个来源都扫；**用户装的排前面**（与发现顺序一致，界面上不用猜）。"""
    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "inner-pack", description="随代码发布")
    _write_plugin(tmp_path / "data" / "plugins", "my-pack", description="用户放的")

    items = _service(tmp_path, builtin=builtin)[0].list()

    assert [(item.name, item.source) for item in items] == [
        ("my-pack", "user"),
        ("inner-pack", "builtin"),
    ]


def test_user_wins_on_name_collision(tmp_path: Path) -> None:
    """同名时以**先出现的那条**为准：用户目录排在前面（ZCode 的发现顺序）。

    这条与技能那一档（内置优先）**正好相反**，是有意照用户指定的顺序来的。
    """
    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "same", description="自带的")
    _write_plugin(tmp_path / "data" / "plugins", "same", description="用户放的")

    items = _service(tmp_path, builtin=builtin)[0].list()

    assert len(items) == 1
    assert items[0].source == "user"
    assert items[0].description == "用户放的"


def test_missing_builtin_dir_is_skipped(tmp_path: Path) -> None:
    """仓库没带 ``plugins/`` 目录时**跳过而不是报错**（用户指定：没有就跳过）。"""
    _write_plugin(tmp_path / "data" / "plugins", "my-pack")

    items = _service(tmp_path, builtin=tmp_path / "nowhere")[0].list()

    assert [item.name for item in items] == ["my-pack"]


def test_hidden_and_underscore_dirs_are_not_plugins(tmp_path: Path) -> None:
    """``.git`` / ``_draft`` 是用户自己的杂物，不该被算成"加载失败"。"""
    root = tmp_path / "data" / "plugins"
    _write_plugin(root, ".hidden")
    _write_plugin(root, "_draft")
    _write_plugin(root, "real")

    assert [item.name for item in _service(tmp_path)[0].list()] == ["real"]


def test_version_defaults_to_zero(tmp_path: Path) -> None:
    """没写 ``version`` 就是 ``0.0.0``（照 ZCode）。"""
    _write_plugin(tmp_path / "data" / "plugins", "my-pack")

    items = _service(tmp_path)[0].list()

    assert items[0].version == "0.0.0"


# --------------------------------------------------------------------- 校验失败


def test_missing_manifest_is_a_failed_record(tmp_path: Path) -> None:
    """**没有 plugin.json 的目录也要出现**：它是"我放进去了它却不出现"的答案。"""
    directory = tmp_path / "data" / "plugins" / "pack"
    directory.mkdir(parents=True)

    items = _service(tmp_path)[0].list()

    assert len(items) == 1
    assert items[0].loaded is False
    assert PLUGIN_FILE in items[0].error


def test_missing_name_is_a_failed_record(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "plugins" / "pack"
    directory.mkdir(parents=True)
    (directory / PLUGIN_FILE).write_text(json.dumps({"description": "没名字"}), encoding="utf-8")

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "缺 name" in items[0].error


def test_invalid_name_is_a_failed_record(tmp_path: Path) -> None:
    """名字同时是设置键的一部分，所以大写与空格都不收——**当场报出来**。"""
    _write_plugin(tmp_path / "data" / "plugins", "pack", **{"name": "Not Allowed"})

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "name 不合法" in items[0].error


def test_broken_json_is_a_failed_record(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "plugins" / "pack"
    directory.mkdir(parents=True)
    (directory / PLUGIN_FILE).write_text("{ 不是 JSON", encoding="utf-8")

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "不是合法 JSON" in items[0].error


def test_failed_plugin_keeps_its_state(tmp_path: Path) -> None:
    """加载失败与启停是两件事：改坏一个插件不该把它"已停用"这件事也弄丢。"""
    root = tmp_path / "data" / "plugins"
    _write_plugin(root, "pack")
    service, _stores = _service(tmp_path)
    service.disable("pack")
    (root / "pack" / PLUGIN_FILE).write_text("{}", encoding="utf-8")

    record = service.get("pack")

    assert record.loaded is False
    assert record.enabled is False


def test_tool_entry_escaping_the_plugin_root_is_a_failed_record(tmp_path: Path) -> None:
    """``entry`` 是别人写的包里唯一的路径声明，跑出插件根一律拒载。"""
    _write_plugin(
        tmp_path / "data" / "plugins",
        "pack",
        tools=[{"name": "读密钥", "entry": "../../../.ssh/id_rsa"}],
    )

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "逃逸插件根" in items[0].error


def test_absolute_entry_outside_the_plugin_root_is_a_failed_record(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "run.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("print(1)", encoding="utf-8")
    _write_plugin(
        tmp_path / "data" / "plugins", "pack", tools=[{"name": "x", "entry": str(outside)}]
    )

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "逃逸插件根" in items[0].error


def test_entry_inside_the_plugin_root_is_fine(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path / "data" / "plugins", "pack", tools=[{"name": "x", "entry": "src/index.js"}]
    )

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is True
    assert items[0].components[0].path == "src/index.js"


def test_symlinked_component_leaving_the_root_is_a_failed_record(tmp_path: Path) -> None:
    """符号链接指向仓库外：**按约定找组件**也要过这一道闸。"""
    outside = tmp_path / "outside-skill"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
    plugin = _write_plugin(tmp_path / "data" / "plugins", "pack")
    (plugin / "skills").mkdir()
    try:
        os.symlink(outside, plugin / "skills" / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器不允许建符号链接（Windows 需要开发者模式或管理员）")

    items = _service(tmp_path)[0].list()

    assert items[0].loaded is False
    assert "逃逸插件根" in items[0].error


# --------------------------------------------------------------------- 组件


def test_components_come_from_directory_conventions(tmp_path: Path) -> None:
    """四类能力面按目录约定发现，manifest 里**不写** skill / command / hook 的路径。"""
    plugin = _write_plugin(
        tmp_path / "data" / "plugins",
        "pack",
        tools=[{"name": "导出", "description": "导出文档", "entry": "src/export.py"}],
    )
    _write_skill(plugin, "查库", description="查知识库")
    (plugin / "commands").mkdir()
    (plugin / "commands" / "compact.md").write_text(
        "---\ndescription: 压缩上下文\n---\n\n正文\n", encoding="utf-8"
    )
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{}, {}]}}), encoding="utf-8"
    )

    record = _service(tmp_path)[0].list()[0]

    assert record.loaded is True
    assert list(record.kinds) == ["skill", "command", "hook", "tool"]
    by_kind = {item.kind: item for item in record.components}
    assert by_kind["skill"].name == "查库"
    assert by_kind["skill"].description == "查知识库"
    assert by_kind["command"].name == "compact"
    assert by_kind["command"].description == "压缩上下文"
    assert by_kind["hook"].name == "PostToolUse"
    assert by_kind["hook"].description == "2 条"
    assert by_kind["tool"].name == "导出"


def test_components_say_out_loud_what_is_not_implemented(tmp_path: Path) -> None:
    """四类**都还没接执行**——每一类的状态里都要有这一句（不假装支持）。"""
    plugin = _write_plugin(
        tmp_path / "data" / "plugins", "pack", tools=[{"name": "x", "entry": "a.py"}]
    )
    _write_skill(plugin, "s")
    (plugin / "commands").mkdir()
    (plugin / "commands" / "c.md").write_text("# 命令\n", encoding="utf-8")
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"Stop": [{}]}), encoding="utf-8")

    record = _service(tmp_path)[0].list()[0]

    statuses = {item.kind: item.status for item in record.components}
    assert "未实现" in statuses["command"]
    assert "未实现" in statuses["hook"]
    assert "未实现" in statuses["tool"]
    assert "没并进去" in statuses["skill"]


def test_broken_hooks_does_not_fail_the_whole_plugin(tmp_path: Path) -> None:
    """坏的 hooks.json 是**组件坏**，不是插件坏：manifest 是好的就照样加载。"""
    plugin = _write_plugin(tmp_path / "data" / "plugins", "pack")
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text("{ 不是 JSON", encoding="utf-8")

    record = _service(tmp_path)[0].list()[0]

    assert record.loaded is True
    assert record.components[0].kind == "hook"
    assert "不是合法 JSON" in record.components[0].description


def test_user_config_names_are_registered(tmp_path: Path) -> None:
    """``userConfig`` 两种形状都认；本轮**只登记名字**（录入还没做）。"""
    _write_plugin(
        tmp_path / "data" / "plugins", "pack", userConfig=[{"name": "token"}, {"name": "endpoint"}]
    )
    _write_plugin(tmp_path / "data" / "plugins", "pack2", userConfig={"token": {"sensitive": True}})

    items = {item.name: item for item in _service(tmp_path)[0].list()}

    assert items["pack"].user_config == ("token", "endpoint")
    assert items["pack2"].user_config == ("token",)


def test_manifest_rejects_malformed_declarations() -> None:
    """形状不对要报出来，不能静默当没有——写它的人以为声明生效了。"""
    with pytest.raises(InvalidRequestError):
        PluginManifest.parse({"name": "pack", "userConfig": "token"})
    with pytest.raises(InvalidRequestError):
        PluginManifest.parse({"name": "pack", "tools": {"name": "x"}})
    with pytest.raises(InvalidRequestError):
        PluginManifest.parse({"name": "pack", "tools": [{"description": "没名字"}]})


# --------------------------------------------------------------------- 启停


def test_disable_and_enable_write_settings_not_the_plugin_dir(tmp_path: Path) -> None:
    """**状态与内容分离**：插件目录一个字节都不改（照 ZCode）。"""
    root = tmp_path / "data" / "plugins"
    plugin = _write_plugin(root, "pack")
    before = sorted(item.name for item in plugin.rglob("*"))
    service, stores = _service(tmp_path)

    assert service.disable("pack").enabled is False
    assert service.enable("pack").enabled is True

    assert stores.app_settings.get_setting(f"{ENABLED_PREFIX}pack") == "true"
    assert sorted(item.name for item in plugin.rglob("*")) == before
    assert list(root.iterdir()) == [plugin]


def test_disabling_a_builtin_records_a_block_marker(tmp_path: Path) -> None:
    """内置插件停用 = 记一条**屏蔽**（照 ZCode 的"伪装卸载"），不是删掉它。

    它**仍然在列表里**（标着 blocked），因为"为什么这个内置的没生效"
    必须能在界面上看见；解除屏蔽要等用户再点启用。
    """
    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "inner-pack", version="1.2.0")
    service, stores = _service(tmp_path, builtin=builtin)

    record = service.disable("inner-pack")

    assert record.blocked is True
    assert record.enabled is False
    assert stores.app_settings.get_setting(f"{BLOCKED_PREFIX}inner-pack") == "1.2.0"
    assert (builtin / "inner-pack" / PLUGIN_FILE).is_file()  # 文件还在
    assert [item.name for item in service.list()] == ["inner-pack"]  # 也没从列表里消失


def test_disabling_a_user_plugin_has_no_block_marker(tmp_path: Path) -> None:
    """屏蔽标记只对内置的说得通：用户自己放的插件，停用就是停用。"""
    _write_plugin(tmp_path / "data" / "plugins", "my-pack")
    service, stores = _service(tmp_path)

    service.disable("my-pack")

    assert stores.app_settings.get_setting(f"{BLOCKED_PREFIX}my-pack") is None


def test_enabling_a_builtin_clears_the_block_marker(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "inner-pack")
    service, stores = _service(tmp_path, builtin=builtin)
    service.disable("inner-pack")

    record = service.enable("inner-pack")

    assert record.blocked is False
    assert record.enabled is True
    assert stores.app_settings.get_setting(f"{BLOCKED_PREFIX}inner-pack") is None


def test_default_state_is_enabled(tmp_path: Path) -> None:
    """没写过状态键 = 启用：把它放进插件目录这件事本身就是"我要用它"。"""
    _write_plugin(tmp_path / "data" / "plugins", "pack")

    record = _service(tmp_path)[0].list()[0]

    assert record.enabled is True
    assert record.blocked is False


def test_unknown_name_is_not_found(tmp_path: Path) -> None:
    service, _stores = _service(tmp_path)

    with pytest.raises(NotFoundError):
        service.get("nope")
    with pytest.raises(NotFoundError):
        service.disable("nope")


def test_listing_is_repeatable(tmp_path: Path) -> None:
    """扫描是幂等的（照 ZCode 的"幂等物化"能观察到的那一面）：跑两遍结果一样。"""
    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "inner-pack")
    _write_plugin(tmp_path / "data" / "plugins", "my-pack")
    service, _stores = _service(tmp_path, builtin=builtin)

    first = service.list()
    second = service.list()

    assert [(item.name, item.source, item.path) for item in first] == [
        (item.name, item.source, item.path) for item in second
    ]
