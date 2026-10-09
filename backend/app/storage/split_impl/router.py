r"""按域分流的 ``MetaStore``（M2「会话落本机」实施方案 §2.1）。

## 为什么接缝落在 ``meta`` 上

``services/`` 里有 346 处 ``stores.meta.<方法>`` 走的是**宽接口**，而本机档只有一半的
数据在本机（会话、消息、事件、产物、笔记、设置、工作区、定时任务、MCP、模型注册、
用量都落 SQLite；知识库、文档、切块、向量、全文在 NAS 上）。要让那 346 处**零改动**地
分域落库，接缝就只能在这个对象本身：

    services/ ──▶ StoreBundle.meta ──▶ RouterMetaStore ─┬─▶ SqliteMetaStore（本机域）
                                                        └─▶ RemoteMetaStore（KB 域）

M3 阶段 4 已经把 KB 侧换成 ``RemoteMetaStore``（打 NAS 窄 API），而这一层与
``services/`` **一行都没改**——它只认"KB 侧那一个对象"这个位置，不认那个对象是谁
（本模块里的 ``UnavailableMetaStore`` 因此还在：它是"KB 域整个全抛"那一形态的参照物
与用例对象）。

## 方法名与签名**不手抄**

``MetaStore`` 有两百来个抽象方法，手抄必漏——而漏掉的那个只会表现为"某条边角路径上
``AttributeError``"。所以转发方法按 ``MetaStore.__abstractmethods__`` **机械生成**：
接口加了方法，这里在导入时就自动跟上。

**生成方法而不是改 ``__abstractmethods__``**：后者是 ABC 的实现细节，改它等于把接口检查
关掉（"``__abstractmethods__`` 是空集"应当是**真的实现了每个方法**的结果，不该是被赋出来的）。
生成的只是每个方法那一行转发，真身与路由表在 ``_Router`` 里，读那一个类就够。

**本机/KB 两半的清单只有一份**：``LOCAL_METHODS`` 的唯一落点是
``app/storage/sqlite_impl/``（阶段 0+1 交接的偏离 4），KB 域是它相对 ``MetaStore`` 的**补集**，
在本模块用一行集合差算出来。两份手写清单迟早会漂，而漂掉的那个方法没有任何征兆。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.storage.base import (
    KB_UNAVAILABLE_MESSAGE,
    FullTextStore,
    KnowledgeBaseUnavailable,
    MetaStore,
    TabularStore,
    VectorStore,
)
from app.storage.sqlite_impl import LOCAL_METHODS

__all__ = [
    "KB_UNAVAILABLE_MESSAGE",
    "LOCAL_METHODS",
    "REMOTE_METHODS",
    "SEARCH_UNAVAILABLE_MESSAGE",
    "KnowledgeBaseUnavailable",
    "RouterMetaStore",
    "UnavailableFullTextStore",
    "UnavailableMetaStore",
    "UnavailableTabularStore",
    "UnavailableVectorStore",
]

REMOTE_METHODS: frozenset[str] = frozenset(MetaStore.__abstractmethods__) - LOCAL_METHODS
"""KB 域方法集 = ``MetaStore`` 的抽象方法**减去** ``LOCAL_METHODS``（补集，不手抄）。

两半必须**恰好**划开全部方法且不相交：少了谁，"调用它会掉进 ``AttributeError``"；
重复了谁，"这个方法的归属有两个答案"。用例逐名核对这两条。
"""

SEARCH_UNAVAILABLE_MESSAGE = (
    "检索在 NAS 知识库：本机档没有向量与全文索引"
    "（本机走的是「知识库提供者」；连接状态在设置里的「知识库连接」）"
)
"""``search`` 单独一句：**检索在 NAS 知识库**（本机走提供者那条链，不在这里）。

与检索工具失败时的口径一致（见 ``services/remote_clients.py`` 的模块头）：
空结果会被读成"库里没有这条"，而真相是"这个部署没有知识库"——下一步动作完全不同。

**M3 起后半句改了**：原先写的是"…，M3 接知识库提供者"（那时的承诺）——
现在已经接上（`services/knowledge_provider.py`），所以这句说的是"**这个仓储**没有索引、
那条真链在哪"。它仍然要被逐字钉着（`tests/unit/core/test_storage.py:242`）：
改成"其实我这儿有索引"，本机档就会静默走进进程内检索——那是方案 §2.2 明确不许的事。
"""

# ``KnowledgeBaseUnavailable`` 与 ``KB_UNAVAILABLE_MESSAGE`` 住在 ``storage/base.py``
# （接口层），这里只是**再导出**（上面的 import + ``__all__``）：M3 起抛出它的不止本模块的
# ``Unavailable*``，还有 ``services/knowledge_provider.py``（提供者客户端，方案 §5.1）——
# 而 services 只允许 import 接口层。搬走那条实读记录见 ``base.py`` 里那个类的说明。


class _Router:
    """路由器的真身：两份实现句柄 + 一张路由表。

    动态生成出来的 ``RouterMetaStore`` 只比它多两百来个一行转发，所以读这一个类
    就知道分流是怎么回事。
    """

    #: 本机域实现（``SqliteMetaStore``）。
    local: Any
    #: KB 侧实现（M2 是 ``UnavailableMetaStore``，M3 阶段 4 起是 ``RemoteMetaStore``）。
    kb: Any

    def __init__(self, local: object, kb: object) -> None:
        self.local = local
        self.kb = kb
        # **路由表在构造时定下**：一个方法归谁只在装配时算一次，调用路径上只剩一次查表；
        # 它也把"两域不相交、合起来正好是全部方法"变成了可读的结构，而不是散在各处的判断。
        self._routes: dict[str, object] = {
            name: local if name in LOCAL_METHODS else kb for name in MetaStore.__abstractmethods__
        }

    def route_of(self, name: str) -> object:
        """``name`` 归哪一份实现（自检与排障用；正常调用不经过它）。"""
        return self._routes[name]


def _forwarder(name: str) -> Callable[..., Any]:
    """生成 ``name`` 的转发方法：按路由表把调用**原样**交给归属实现。

    签名写成 ``*args, **kwargs`` 是刻意的：``MetaStore`` 里大量方法是关键字专用
    （``list_messages`` / ``list_conversations`` 那一族），逐个手抄签名既抄不准
    （16 处形参与默认值对不上的那次教训见 ``tests/unit/storage/test_repositories.py``），
    也没有必要——转发不改参数、不改返回值、不改异常。
    """

    def method(self: _Router, *args: Any, **kwargs: Any) -> Any:
        return getattr(self._routes[name], name)(*args, **kwargs)

    method.__name__ = name
    method.__qualname__ = f"RouterMetaStore.{name}"
    method.__doc__ = f"转发给 ``{name}`` 的归属实现（本机域见 ``LOCAL_METHODS``）。"
    return method


def _build_router() -> type:
    """按 ``MetaStore`` 的抽象方法生成 ``RouterMetaStore``。"""
    namespace: dict[str, Any] = {name: _forwarder(name) for name in MetaStore.__abstractmethods__}
    namespace["__doc__"] = (
        "按域分流的元数据仓储：本机域转 ``local``，KB 域转 ``kb``。\n\n"
        "两百来个方法都是生成出来的转发器（名字取自 ``MetaStore`` 的抽象方法），"
        "所以「接口加了一个方法」会自动出现在这里，不需要有人记得来补。"
    )
    return type("RouterMetaStore", (_Router, MetaStore), namespace)


RouterMetaStore = _build_router()
"""``MetaStore`` 的分流实现（本机档 ``stores.meta`` 就是它）。"""


def _unavailable_method(name: str, *, owner: str) -> Callable[..., Any]:
    """生成 ``name`` 的"不可用"实现：**一调用就抛** ``KnowledgeBaseUnavailable``。"""

    message = SEARCH_UNAVAILABLE_MESSAGE if name == "search" else KB_UNAVAILABLE_MESSAGE

    def method(self: object, *args: Any, **kwargs: Any) -> Any:
        raise KnowledgeBaseUnavailable(message)

    method.__name__ = name
    method.__qualname__ = f"{owner}.{name}"
    method.__doc__ = (
        "本机档没有这个能力（知识库在 NAS 上）：调用即抛 ``KnowledgeBaseUnavailable``。"
    )
    return method


def _build_unavailable(
    owner: str, methods: Iterable[str], bases: tuple[type, ...], *, doc: str
) -> type:
    """生成一个"每个方法都抛"的实现；方法与签名同样从接口机械取。"""
    namespace: dict[str, Any] = {name: _unavailable_method(name, owner=owner) for name in methods}
    namespace["__doc__"] = doc
    return type(owner, bases, namespace)


def _unavailable_namespace_doc(what: str) -> str:
    return (
        f"本地档的{what}：本机没有它的数据源（知识库在 NAS 上），每个方法都抛"
        " ``KnowledgeBaseUnavailable``。\n\n"
        "**不是空实现**：静态空实现（返回空列表）与「抛」在协议层是两个完全不同的答案，"
        "而这个类要给的答案是后者。"
    )


UnavailableMetaStore = _build_unavailable(
    "UnavailableMetaStore",
    REMOTE_METHODS,
    (),
    doc=(
        "本机档的 KB 侧元数据实现：KB 域的方法**每个都抛** ``KnowledgeBaseUnavailable``。\n\n"
        "只覆盖 ``REMOTE_METHODS``（与 ``SqliteMetaStore`` 只覆盖 ``LOCAL_METHODS`` 是"
        "同一条纪律的两半——多一个就是偷偷实现了另一域的活）。M3 阶段 4 起装配点用的是"
        " ``remote_meta.RemoteMetaStore``（两个真映射 + 其余照旧抛，用的是本模块的生成器），"
        "它留下作「KB 域整个全抛」那一形态的参照物与用例对象——换的时候本机域那半一行没动。"
    ),
)

UnavailableVectorStore = _build_unavailable(
    "UnavailableVectorStore",
    VectorStore.__abstractmethods__,
    (VectorStore,),
    doc=_unavailable_namespace_doc("向量仓储（pgvector）"),
)

UnavailableFullTextStore = _build_unavailable(
    "UnavailableFullTextStore",
    FullTextStore.__abstractmethods__,
    (FullTextStore,),
    doc=_unavailable_namespace_doc("全文仓储（tsvector）"),
)

UnavailableTabularStore = _build_unavailable(
    "UnavailableTabularStore",
    TabularStore.__abstractmethods__,
    (TabularStore,),
    doc=(
        _unavailable_namespace_doc("表格副本（DuckDB）")
        + "\n\n**这个文件里没有 ``duckdb`` 的 import，一行都没有**：它不在客户端运行时里"
        "（约 35.6 MB），本机档连碰都不该碰它。"
    ),
)
