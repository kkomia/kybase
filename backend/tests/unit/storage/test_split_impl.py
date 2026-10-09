"""分档路由与"不可用"那半的接口核对（不需要 PostgreSQL）。

镜像同构：``app/storage/split_impl/`` → ``tests/unit/storage/test_split_impl.py``。

三块内容（M2 实施方案 §2.1 / §6.4）：

1. **域完备性**：``LOCAL_METHODS`` 与 ``REMOTE_METHODS`` 恰好划开 ``MetaStore`` 的全部
   抽象方法、两者不相交，``RouterMetaStore`` 是**完整**的 ``MetaStore``
   （``__abstractmethods__`` 是空集——它是"真的实现了每个方法"的结果，不是赋出来的）；
2. **转发确实是转发**：每个方法转到归属那一半，参数原样、返回值原样；
3. **不可用要抛**：``Unavailable*`` 的**每个方法**都抛 ``KnowledgeBaseUnavailable``
   （逐个调用，128 个方法一个不漏），``search`` 那句写明"检索在 NAS 知识库"。

**M3 阶段 4 加了一块**（``RemoteMetaStore``，那份也在这里逐名核对）：

4. **KB 侧的真实现**：公开方法**恰好**覆盖 ``REMOTE_METHODS``（多出来的唯一一个只有
   装配口 ``bind_reader``）；``IMPLEMENTED``（两个真映射）之外**逐个**照旧抛那句原句；
   没 ``bind`` 就调那两个已实现的方法时抛的是**那句中文**，不是 ``AttributeError``。

外加一条**子进程**断言（§7 阶段 2 的完成判据）：``KYLAB_DEPLOYMENT=local`` 下跑一次
真 ``build_stores()``，``psycopg`` / ``boto3`` / ``duckdb`` 一个都不许进 ``sys.modules``。
为什么必须换进程：本进程里 ``tests/conftest.py`` 早就 import 了 psycopg，
在脏 ``sys.modules`` 上查"有没有 import"永远查不出东西。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.storage.base import FullTextStore, MetaStore, StorageError, TabularStore, VectorStore
from app.storage.split_impl import (
    IMPLEMENTED,
    KB_UNAVAILABLE_MESSAGE,
    LOCAL_METHODS,
    REMOTE_METHODS,
    SEARCH_UNAVAILABLE_MESSAGE,
    UNBOUND_MESSAGE,
    KnowledgeBaseUnavailable,
    RemoteMetaStore,
    RouterMetaStore,
    UnavailableFullTextStore,
    UnavailableMetaStore,
    UnavailableTabularStore,
    UnavailableVectorStore,
)
from app.storage.sqlite_impl import LOCAL_METHODS as SQLITE_LOCAL_METHODS

pytestmark = pytest.mark.local

#: backend/（本文件在 backend/tests/unit/storage/ 下，上溯三级）
BACKEND = Path(__file__).resolve().parents[3]

#: 服务器档才用得上的三份后端（约 108 MB，见 ``core/storage.py`` 那段"为什么惰性"）。
SERVER_BACKENDS = ("psycopg", "psycopg_pool", "boto3", "botocore", "duckdb")


class _Recorder:
    """一个"什么方法都有、只记下被调用"的假实现，用来验转发本身。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def method(*args: object, **kwargs: object) -> str:
            self.calls.append((name, args, kwargs))
            return f"{self.label}:{name}"

        return method


# ------------------------------------------------------------------ 域完备性


def test_the_two_halves_partition_the_whole_interface() -> None:
    """``LOCAL ∪ REMOTE`` 恰好是全部抽象方法，且两者不相交（§2.1 要求的那两条）。"""
    abstract = set(MetaStore.__abstractmethods__)
    both_halves = LOCAL_METHODS | REMOTE_METHODS

    assert both_halves == abstract
    assert not LOCAL_METHODS & REMOTE_METHODS
    assert len(LOCAL_METHODS) + len(REMOTE_METHODS) == len(abstract)


def test_local_methods_come_from_the_single_source_of_truth() -> None:
    """本机域的清单**只有一份**：split_impl 转出来的是 sqlite_impl 那一份。

    两份手写清单迟早会漂，而漂掉的那个方法只会在某条边角路径上以 ``AttributeError``
    出现（阶段 0+1 交接的偏离 4）。
    """
    assert LOCAL_METHODS is SQLITE_LOCAL_METHODS


def test_router_is_a_complete_meta_store() -> None:
    """``RouterMetaStore`` 没有剩下的抽象方法——这是"每个抽象方法都生成了转发器"的结果。"""
    assert issubclass(RouterMetaStore, MetaStore)
    assert RouterMetaStore.__abstractmethods__ == frozenset()


def test_unavailable_meta_store_covers_exactly_the_kb_half() -> None:
    """KB 侧实现**恰好**覆盖 ``REMOTE_METHODS``：一个不多、一个不少。

    与 ``SqliteMetaStore`` 恰好覆盖 ``LOCAL_METHODS`` 是同一条纪律的两半——多一个
    就是偷偷实现了另一域的活，少一个就是路由到那里时才炸。
    """
    public = {
        name
        for name, value in vars(UnavailableMetaStore).items()
        if not name.startswith("_") and callable(value)
    }
    assert public == set(REMOTE_METHODS)


@pytest.mark.parametrize(
    ("implementation", "interface"),
    [
        (UnavailableVectorStore, VectorStore),
        (UnavailableFullTextStore, FullTextStore),
        (UnavailableTabularStore, TabularStore),
    ],
)
def test_unavailable_stores_implement_their_interface(
    implementation: type, interface: type
) -> None:
    """三个"不可用"仓储是各自接口的**完整**实现（每个方法都在，只是都抛）。"""
    assert issubclass(implementation, interface)
    assert implementation.__abstractmethods__ == frozenset()


# --------------------------------------------- KB 侧的真实现（M3 阶段 4）


def test_remote_meta_store_covers_exactly_the_kb_half() -> None:
    """``RemoteMetaStore`` 的方法面**同样恰好**是 ``REMOTE_METHODS``（多出的只有装配口）。

    ``bind_reader`` 是装配期的东西（组合根后挂 reader，理由见 ``remote_meta`` 的模块头），
    不是 ``MetaStore`` 上的方法——所以判据写成"公开方法 == KB 域那一半 **加上它**"：
    除了它，一个都不许多（多一个就是想偷偷实现另一域的活）。

    与上一条不同的是取法：那两个真实现写在基类上（读代码看得到映射），其余在生成的
    那一层上，所以这里走 ``dir()`` 走完整个 MRO。
    """
    public = {
        name
        for name in dir(RemoteMetaStore)
        if not name.startswith("_") and callable(getattr(RemoteMetaStore, name))
    }
    assert public - {"bind_reader"} == set(REMOTE_METHODS)
    assert "bind_reader" in public, "装配口不见了：组合根就挂不上 reader 了"


def test_remote_meta_store_maps_exactly_the_two_methods_of_the_plan() -> None:
    """真映射的只有 §2.1 结论里那两个——``IMPLEMENTED`` 就是那句结论的机器可读形态。"""
    assert frozenset({"get_knowledge_base", "list_knowledge_bases"}) == IMPLEMENTED
    assert IMPLEMENTED <= REMOTE_METHODS


def test_remote_meta_store_still_raises_for_everything_but_the_two_mapped() -> None:
    """``IMPLEMENTED`` 之外**逐个**调用 → 抛 ``KnowledgeBaseUnavailable``，原句保留。

    "原句"就是 ``UnavailableMetaStore`` 那一句（两边用的是同一个生成器，不是抄的）：
    本机档这些方法的答案没变，变的是"能力在才在"——能映射的映射，映射不了的如实抛。
    """
    store = RemoteMetaStore()
    called = 0
    for name in REMOTE_METHODS - IMPLEMENTED:
        with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
            getattr(store, name)()
        assert str(excinfo.value) == KB_UNAVAILABLE_MESSAGE, name
        called += 1
    assert called == len(REMOTE_METHODS) - len(IMPLEMENTED)


def test_remote_meta_store_says_the_provider_is_not_wired_yet() -> None:
    """没 ``bind_reader`` 就调那两个已实现的方法：抛**那句中文**，不是 ``AttributeError``。

    "这台机器还没接上提供者"是**部署状态**（组合根那一行没跑到），该给用户一句人话 +
    既有的 503 映射，而不是一个"属性不存在"的内部错误。
    """
    store = RemoteMetaStore()

    with pytest.raises(KnowledgeBaseUnavailable) as get_error:
        store.get_knowledge_base("kb_a")
    assert str(get_error.value) == UNBOUND_MESSAGE
    with pytest.raises(KnowledgeBaseUnavailable) as list_error:
        store.list_knowledge_bases()
    assert str(list_error.value) == UNBOUND_MESSAGE

    assert "知识库提供者" in UNBOUND_MESSAGE
    assert "组合根" in UNBOUND_MESSAGE


def test_retrieval_stays_out_of_the_remote_meta_store() -> None:
    """方案 §2.2 的结论钉在这里：**检索不进 ``stores.meta``**（别改成走 NAS 的半吊子）。

    两条理由见 §2.2：① 要让 ``stores.meta.search`` 走 NAS，就得在本机把服务器那个
    "向量 + 全文 + 融合 + 重排"的服务搬过来套壳；② ``ChatService`` 早在更上一层的
    ``retrieve_sources`` 整段委托了。

    所以这里断言的是**这条路不存在**：``search`` 不在 KB 侧那两个集合里（于是
    ``RemoteMetaStore`` 上也没有它，路由表也不会往这儿发）。"本机档调用检索仍然是
    ``SEARCH_UNAVAILABLE_MESSAGE`` 那句"那一半在 ``tests/unit/core/test_storage.py``
    的本地档用例里，对着真装配出来的 bundle 断言。
    """
    assert "search" not in REMOTE_METHODS
    assert "search" not in IMPLEMENTED


# ------------------------------------------------------------------ 转发


def test_every_method_routes_to_exactly_one_half() -> None:
    """逐个方法核对归属：路由表与 ``LOCAL_METHODS`` 必须是同一个答案。"""
    local, kb = _Recorder("local"), _Recorder("kb")
    router = RouterMetaStore(local, kb)

    for name in MetaStore.__abstractmethods__:
        expected = local if name in LOCAL_METHODS else kb
        assert router.route_of(name) is expected, name


def test_calls_are_forwarded_verbatim() -> None:
    """转发**原样**：位置参数、关键字参数、返回值都不经手改。

    这也是"签名写成 ``*args, **kwargs``"的理由——``MetaStore`` 里大量方法是关键字
    专用的，逐个手抄签名抄不准（抄错的后果是运行期 ``TypeError``，只在某条路径上出现）。
    """
    local, kb = _Recorder("local"), _Recorder("kb")
    router = RouterMetaStore(local, kb)

    assert router.create_conversation("record", workspace_id="ws_1") == "local:create_conversation"
    assert local.calls == [("create_conversation", ("record",), {"workspace_id": "ws_1"})]

    assert router.create_document("record") == "kb:create_document"
    assert kb.calls == [("create_document", ("record",), {})]
    assert local.calls == [("create_conversation", ("record",), {"workspace_id": "ws_1"})]


# ------------------------------------------------------------------ 不可用要抛


def _unavailable_instances() -> list[object]:
    return [
        UnavailableMetaStore(),
        UnavailableVectorStore(),
        UnavailableFullTextStore(),
        UnavailableTabularStore(),
    ]


def test_every_unavailable_method_raises_instead_of_pretending() -> None:
    """**每个方法**都抛——不留任何"悄悄回空值"的口子（空结果会被读成"库里没有"）。

    逐个调用（不带参数即可：这些方法在检查参数之前就抛），一个方法都不放过。
    """
    called = 0
    for store in _unavailable_instances():
        for name, value in vars(type(store)).items():
            if name.startswith("_") or not callable(value):
                continue
            with pytest.raises(KnowledgeBaseUnavailable):
                getattr(store, name)()
            called += 1
    assert called == len(REMOTE_METHODS) + sum(
        len(interface.__abstractmethods__)
        for interface in (VectorStore, FullTextStore, TabularStore)
    )


def test_unavailable_methods_say_nas_and_where_to_look() -> None:
    """报错句子里要有"在 NAS 上""知识库""去哪儿看状态"这三件事（否则用户不知道该去哪儿）。

    M3 起第三条从"等 M3"换成"设置里的「知识库连接」"——提供者已经落地，
    再让人等下一个阶段就是把出路指错地方。
    """
    for store in _unavailable_instances():
        names = [name for name, value in vars(type(store)).items() if not name.startswith("_")]
        with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
            getattr(store, names[0])()
        message = str(excinfo.value)
        assert "NAS" in message, message
        assert "知识库连接" in message, message
        assert "知识库" in message, message


def test_search_says_retrieval_is_on_the_nas_knowledge_base() -> None:
    """``search`` 单独一句（§2.2）：**检索在 NAS 知识库**（本机走提供者那条链）。"""
    assert "检索在 NAS 知识库" in SEARCH_UNAVAILABLE_MESSAGE

    for store in (UnavailableVectorStore(), UnavailableFullTextStore()):
        with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
            store.search()
        assert str(excinfo.value) == SEARCH_UNAVAILABLE_MESSAGE


def test_the_unavailable_error_is_a_storage_error() -> None:
    """``StorageError`` 子类：services 不必 import 具体实现就能捕获（接口层的承诺）。"""
    error = KnowledgeBaseUnavailable()
    assert isinstance(error, StorageError)
    assert str(error) == KB_UNAVAILABLE_MESSAGE
    assert str(KnowledgeBaseUnavailable("自定义一句")) == "自定义一句"


# ------------------------------------------------------------------ 本地档不背服务器后端


def test_local_deployment_builds_every_field_without_the_server_backends(tmp_path: Path) -> None:
    """§7 阶段 2 完成判据：``KYLAB_DEPLOYMENT=local`` 下五个字段齐备，三份后端都没 import。

    跑在**干净解释器**里（本进程早就 import 过 psycopg，查不出东西），用**环境变量**钉档
    （与生产同一条路：``get_settings()`` + ``build_stores()``），并把 ``.env`` 会带来的
    ``KYLAB_DATABASE_URL`` 压成空串——不压的话本机档会当场拒绝（那正是它该做的事）。
    """
    script = textwrap.dedent(
        f"""
        import sys

        from app.core.storage import build_stores

        stores = build_stores()
        fields = ("meta", "vectors", "fulltext", "objects", "tabular")
        print("missing:", [name for name in fields if getattr(stores, name, None) is None])
        print("backends:", sorted(name for name in {SERVER_BACKENDS!r} if name in sys.modules))
        """
    )
    env = {
        **os.environ,
        "KYLAB_DEPLOYMENT": "local",
        "KYLAB_DATA_DIR": str(tmp_path / "data"),
        "KYLAB_DATABASE_URL": "",
        "PYTHONIOENCODING": "utf-8",
    }
    # 命令是"本仓库自己的解释器 + 一段写死在文件里的脚本"，不是外部输入
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "missing: []" in result.stdout, result.stdout
    assert "backends: []" in result.stdout, result.stdout
