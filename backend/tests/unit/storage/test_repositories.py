"""按域切开的仓储窄协议（``app/storage/repositories.py``）。

这一步是拆 `MetaStore` 的**前置**，所以守卫的重点不是"能跑"，而是
**"这个接口说的和实现做的是不是同一件事"**：

1. 协议里的每个方法在 `MetaStore` 里真实存在，且**签名逐字一致**
   （手抄过一版，16 处形参与默认值对不上——这条用例就是那次教训的产物）；
2. **183 个方法恰好被域覆盖一次**：域是手工划的，"漏一个方法"不会报错，
   只会表现为"某个方法永远只能走 meta"，没有人会注意到；
3. 实现确实满足协议（结构化类型：不继承也必须满足，否则窄接口只是文档）；
4. `StoreBundle` 的窄视图与协议**一一对应**，且是**同一个实例**。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.storage import repositories
from app.storage.base import MetaStore, StoreBundle
from app.storage.postgres_impl.meta_store import PostgresMetaStore

#: backend/（本文件在 backend/tests/unit/storage/ 下，上溯三级）
ROOT = pathlib.Path(__file__).resolve().parents[3]

#: 协议 → `StoreBundle` 上的视图名。加了协议忘了视图，或者视图改名，这里会红。
VIEW_NAMES = {
    "KnowledgeBaseRepo": "knowledge_bases",
    "DocumentRepo": "documents",
    "FolderRepo": "folders",
    "NoteRepo": "notes",
    "ChunkRepo": "chunks",
    "ImageRepo": "images",
    "ParseResultRepo": "parse_results",
    "TaskQueueRepo": "tasks",
    "DataSourceRepo": "data_sources",
    "ApiKeyRepo": "api_keys",
    "WebhookRepo": "webhooks",
    "IdempotencyRepo": "idempotency",
    "ConversationRepo": "conversations",
    "WorkspaceRepo": "workspaces",
    "IdentityRepo": "identity",
    "ScheduleRepo": "schedules",
    "ShareRepo": "shares",
    "UsageRepo": "usage",
    "ModelRegistryRepo": "models",
    "MCPServerRepo": "mcp_servers",
    "TrashRepo": "trash",
    "SettingsRepo": "app_settings",
    "WikiRepo": "wiki",
    "MaintenanceRepo": "maintenance",
}

PROTOCOLS = tuple(getattr(repositories, name) for name in repositories.__all__)


def _class_source(cls: type) -> ast.ClassDef:
    module = ROOT / (cls.__module__.replace(".", "/") + ".py")
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls.__name__
    )


def _method_names(cls: type) -> list[str]:
    return [node.name for node in _class_source(cls).body if isinstance(node, ast.FunctionDef)]


def _signatures(cls: type) -> dict[str, str]:
    """方法名 → `(参数表|返回注解)` 的规范化文本（用 AST，不看格式）。"""
    found: dict[str, str] = {}
    for node in _class_source(cls).body:
        if isinstance(node, ast.FunctionDef):
            returns = ast.unparse(node.returns) if node.returns else ""
            found[node.name] = f"{ast.unparse(node.args)}|{returns}"
    return found


def test_every_protocol_method_exists_with_the_same_signature() -> None:
    """协议与 ABC 的**签名逐字一致**：名字对了但形参错了，调用方照样会在运行期炸。"""
    real = _signatures(MetaStore)
    mismatched: list[str] = []
    for protocol in PROTOCOLS:
        for name, signature in _signatures(protocol).items():
            if name not in real:
                mismatched.append(f"{protocol.__name__}.{name}：MetaStore 里没有这个方法")
            elif real[name] != signature:
                mismatched.append(
                    f"{protocol.__name__}.{name}：\n  协议 {signature}\n  实现 {real[name]}"
                )

    assert not mismatched, "\n".join(mismatched)


def test_the_domains_cover_every_method_exactly_once() -> None:
    """**一个方法恰好属于一个协议，且 183 个一个不漏。**

    域是手工划的，而"漏掉一个方法"不会让任何东西报错——它只会让那个方法永远只能走
    `meta`，而没人会注意到。反过来，一个方法出现在两个协议里，则会让"该注入哪个"
    变成看运气。两种情况都由 `scripts/_gen_repos.py` 在生成时核对过一次，
    这条用例保证**之后**的改动也逃不掉（改了归属、删了方法、加了方法）。
    """
    owner: dict[str, str] = {}
    clashes: list[str] = []
    for protocol in PROTOCOLS:
        for name in _method_names(protocol):
            if name in owner:
                clashes.append(f"{name}：{owner[name]} 与 {protocol.__name__}")
            owner[name] = protocol.__name__

    real = set(_method_names(MetaStore))
    unassigned = sorted(real - set(owner))
    unknown = sorted(set(owner) - real)

    assert not clashes, f"同一个方法被两个协议认领：{clashes}"
    assert not unassigned, f"这些方法还不属于任何域：{unassigned}"
    assert not unknown, f"这些方法在 MetaStore 里不存在：{unknown}"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_implementation_satisfies_the_protocol(bundle, protocol) -> None:  # type: ignore[no-untyped-def]
    """结构化类型：`PostgresMetaStore` 不继承协议，但必须满足它。

    `isinstance` 对 `runtime_checkable` 的协议只查"方法在不在"——够用了：
    签名一致性由上一条覆盖，这里守的是"实现别漏掉某个方法"。
    """
    assert isinstance(bundle.meta, protocol)


def test_every_protocol_has_a_bundle_view() -> None:
    """协议与窄视图一一对应：加了协议忘了加视图（或视图改名）会在这里红。"""
    assert set(repositories.__all__) == set(VIEW_NAMES), "协议清单与 VIEW_NAMES 对不上"
    for view in VIEW_NAMES.values():
        assert isinstance(getattr(StoreBundle, view, None), property), f"{view} 不是属性"


def test_narrow_views_are_the_same_instance(bundle) -> None:  # type: ignore[no-untyped-def]
    """窄视图只是**类型收窄**，不是换实现——否则"老路径与新路径"会读到两份状态。"""
    assert isinstance(bundle.meta, PostgresMetaStore)
    for view in VIEW_NAMES.values():
        assert getattr(bundle, view) is bundle.meta, f"{view} 不是同一个实例"


def test_the_bundle_exposes_exactly_the_declared_views() -> None:
    """视图清单不许悄悄多出来：多一个属性就意味着**多一个消费入口**，
    而 REVIEW 过的清单（VIEW_NAMES）才是"我们知道自己暴露了什么"的依据。"""
    declared = {
        name for name, value in inspect.getmembers(StoreBundle) if isinstance(value, property)
    }
    assert declared == set(VIEW_NAMES.values())
