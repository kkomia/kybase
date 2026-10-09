r"""本机档 KB 侧元数据的**真实现**：两个真映射，其余照旧抛（M3「知识库提供者」阶段 4）。

M2 的 ``UnavailableMetaStore`` 把 KB 域**每一个**方法都抛掉（"这个部署没有知识库"）。
M3 起本机不再"没有知识库"——知识库在 NAS 上，本机是它的**客户端**。所以 KB 侧换成这个
类：能打通的**如实打通**，打不通的**照旧如实抛**（v0.3 §3.1「"无"是一个合法实现」落到
方法级）。它只覆盖 ``REMOTE_METHODS``，按域分流由 ``RouterMetaStore`` 负责：

    services/ ──▶ StoreBundle.meta ──▶ RouterMetaStore ─┬─▶ SqliteMetaStore（本机域）
                                                        └─▶ RemoteMetaStore（KB 域）

## 只真映射两个方法（方案 §2.1 逐族处置表的结论）

``IMPLEMENTED`` 里那两件是**本机代码真的会读**的 KB 元数据——它们有本机调用方，
其余没有：

- ``ChatService._kb_prompt`` 逐库读 ``get_knowledge_base``（取库级提示词，``chat.py``），
  今天被 ``except`` 吞成一句 warning；M3 阶段 4 起它真的能把 NAS 上那句提示词取回来；
- ``list_knowledge_bases`` 与它同源（都是 ``GET /knowledge-bases[/{id}]``）。

**其余逐族的理由**（照施工单 §2.1 的表落在这里，免得后来者以为是漏了）：

- ``DocumentRepo`` / ``FolderRepo`` / ``ChunkRepo`` / ``ImageRepo`` / ``ParseResultRepo``：
  本机不读文档树；"入库进度"是提供者客户端的能力（``document_status``），不进 ``stores.meta``；
- ``TaskQueueRepo``：本机档**不启动消费者**，没有队列可管；
- ``DataSourceRepo`` / ``WebhookRepo`` / ``IdempotencyRepo``：服务器家当；
- ``ApiKeyRepo``：**尤其不许映射**——本机档的 ``Caller`` 是短路的本机主人，
  把 NAS 的钥匙表映射进来等于给本机装第二套鉴权；
- ``IdentityRepo``：本机不设门禁（v0.3 §8-1）；
- ``ShareRepo`` / ``TrashRepo`` / ``WikiRepo`` / 流水线维护：属 NAS 的账号体系与后台，
  页面直连；
- 写类（建库 / 改名 / 改切分 / 改提示词 / 删库）：本机档**没有调用方**，
  知识库管理是页面直连 NAS 的事。

## 检索**不进**这里（方案 §2.2，结论已定，不留给后来者再判）

① 要让 ``stores.meta.search`` 走 NAS，就得在本机把服务器那个"向量 + 全文 + 融合 + 重排"
的服务搬过来套壳，还得让 ``stores.vectors`` / ``stores.fulltext`` 装作存在；
② ``ChatService`` 早在**更上一层的接缝**（``retrieve_sources``）整段委托了，M3 只是把
那个委托对象换成提供者客户端。所以本机档的检索照旧是"这个部署没有索引"那一句
（``SEARCH_UNAVAILABLE_MESSAGE``），有用例守着——**别让它长成半吊子的远端检索**。

## 生成式：方法名与签名不手抄

两百来个方法手抄必漏，而漏掉的那个只会在某条边角路径上表现成 ``AttributeError``。
所以：

- 那两个真实现**写在 ``_RemoteMetaStore`` 的类体里**（读代码就看得到映射本身）；
- 其余的名字从 ``REMOTE_METHODS - IMPLEMENTED`` **机械生成**，而且生成器就是
  ``RouterMetaStore`` 用的那一个 ``_unavailable_method``——"原句保留"因此不是抄来的，
  是同一份代码（两句文案只有一个落点）。

## ``bind_reader``：为什么是后挂，不是构造参数

- **层序**：``core/storage.py`` 先于 ``core/services.py`` 跑（``bundle = stores or
  build_stores(resolved)``）——装配存储时 reader **还没出生**；
- **运行期配置**：reader 由 ``KnowledgeProviderClient`` 提供，而它要的是"能随设置改地址"
  的能力（每次调用现取目标）；那份能力**只有服务层有**（``runtime.get`` 那一份运行期
  设置）。把目标塞进构造参数，等于把"设置页改完即刻生效"钉死在启动那一刻。

所以 ``RemoteMetaStore()`` 无参构造，reader 由组合根**后挂一次**（``core/services.py``，
只在 ``deployment == "local"`` 那一支）。这一处注入只在**装配期**发生，也不动 ``router.py``
"路由表构造时定下"那条纪律——注入的是 KB 侧那个对象**内部**的引用，路由一个字没变。

**没 bind 就调用**：抛 ``KnowledgeBaseUnavailable(UNBOUND_MESSAGE)``，**不是
``AttributeError``**。"这台机器还没接上提供者"是**部署状态**，用户看到的该是一句人话
（而且既有 503 映射认得它），不是一个"属性不存在"的内部错误。

## M4（只写一句，本阶段不做）

读路径的元数据缓存（etag + SWR）就长在这个 reader 上：缓存层插在 ``KnowledgeMetaReader``
与 HTTP 之间，``stores.meta`` 与 ``services/`` 一行不改。M3 只把 ``IMPLEMENTED`` 定为这
两个方法，M4 往这个集合里加（有那条集合用例守着）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from app.storage.base import KnowledgeBaseRecord, KnowledgeBaseUnavailable
from app.storage.split_impl.router import (
    REMOTE_METHODS,
    # 两句文案与 ``UnavailableMetaStore`` **同源**（同一个生成器）：这是"原句保留"的
    # 实现方式——不是把句子抄过来，而是走同一份代码。私有的只是名字，不是边界。
    _unavailable_method,
)

__all__ = [
    "IMPLEMENTED",
    "UNBOUND_MESSAGE",
    "KnowledgeMetaReader",
    "RemoteMetaStore",
]

IMPLEMENTED: frozenset[str] = frozenset({"get_knowledge_base", "list_knowledge_bases"})
"""真映射的那两个方法（方案 §2.1 那张表的结论，也是"能力在，才在"的机器可读形态）。

**这一个集合就是 KB 侧的全部能力面**：用例逐名核对"公开方法 == ``REMOTE_METHODS``"，
所以往这里加一行 = 同时必须给出真实现（M4 扩展时加行即可，不会悄悄多出一个没实现的
名字，也不会悄悄少一个该实现的名字）。
"""

UNBOUND_MESSAGE = "知识库提供者还没接上（组合根未装配）"
"""未 ``bind_reader`` 就调那两个方法时的那句话（说的是**部署状态**，不是内部错误）。"""


class KnowledgeMetaReader(Protocol):
    """``RemoteMetaStore`` 要的那两件事——**storage 层不认识 HTTP，也不认识 services**。

    形状照方案 §2.3：回 NAS 的**原始 dict**（到 ``KnowledgeBaseRecord`` 的映射只发生在
    本模块里，多一层转手就多一处会漂的形状），并且：

    - ``None`` = **没有这个库**（远端 404）。它与"取不到"是两件事：前者是答案，
      后者是错误——把后者伪装成前者，调用方就会把"连不上"读成"库里没有"；
    - 取不到时抛 ``KnowledgeBaseUnavailable``：``stores.meta`` 那一面只认这一族错误
      （"未配 / 连不上 / 被拒 / 5xx"对调用方是同一个答案："现在取不到"）。分档留在
      客户端内部——工具循环那一面才需要 ``RemoteUnavailableError`` 那种细分。
    """

    def get_knowledge_base(self, kb_id: str) -> dict[str, object] | None:
        """这个库的元数据；``None`` = 没有这个库。"""
        ...

    def list_knowledge_bases(self) -> list[dict[str, object]]:
        """这次调用看得见的全部库（受限凭据的过滤在远端那一侧做）。"""
        ...


class _RemoteMetaStore:
    """``RemoteMetaStore`` 的真身：两件真实现 + 一个装配口（其余方法在子类上生成）。

    这一层就是"KB 侧实现"在读代码时的全部形状——两百来个生成出来的方法只比它多一行
    转发（而且全都抛），所以读这一个类就够了。
    """

    def __init__(self) -> None:
        self._reader: KnowledgeMetaReader | None = None

    def bind_reader(self, reader: KnowledgeMetaReader) -> None:
        """注入 reader（组合根**装配期一次**，见模块头"为什么是后挂，不是构造参数"）。

        刻意**不设"只能绑一次"的守卫**：真正的重复装配（两个 ServiceBuild 挂在同一个
        bundle 上）不是这一层拦得住的，而守卫会让"配置热更时换一个 reader"多一条无用分支。
        """
        self._reader = reader

    def _reader_or_raise(self) -> KnowledgeMetaReader:
        """拿 reader；没绑就抛**那句中文**（不是 ``AttributeError``，见模块头）。"""
        reader = self._reader
        if reader is None:
            raise KnowledgeBaseUnavailable(UNBOUND_MESSAGE)
        return reader

    def get_knowledge_base(self, kb_id: str) -> KnowledgeBaseRecord | None:
        """库详情：``GET /knowledge-bases/{kb_id}`` → 一条记录。

        ``None`` 照原样传出去（契约：**没有这个库**，不是出错）。远端传了残缺体（少了
        id）时用**问的那个 id**兜底：调用方问的就是它，凭空回一个空 id 只会让上游多一条
        看不懂的记录。
        """
        payload = self._reader_or_raise().get_knowledge_base(kb_id)
        if payload is None:
            return None
        return _to_record(payload, fallback_id=kb_id)

    def list_knowledge_bases(self) -> list[KnowledgeBaseRecord]:
        """库清单：``GET /knowledge-bases`` 的每一项一条记录（**顺序照远端给的顺序**）。"""
        return [_to_record(item) for item in self._reader_or_raise().list_knowledge_bases()]


def _build_remote() -> type:
    """生成 ``RemoteMetaStore`` 的「其余都抛」那半（两个真实现留在 ``_RemoteMetaStore`` 上）。"""
    namespace: dict[str, Any] = {
        name: _unavailable_method(name, owner="RemoteMetaStore")
        for name in REMOTE_METHODS - IMPLEMENTED
    }
    namespace["__doc__"] = (
        "本机档 KB 侧的真实现（M3 阶段 4）：``IMPLEMENTED`` 里那两件转 reader 打 NAS，"
        "其余每一个方法都抛 ``KnowledgeBaseUnavailable``（原句见 ``router.py``）。\n\n"
        "生成出来的只有「其余都抛」那半——名字取自 ``REMOTE_METHODS``，所以「接口加了一个 "
        "KB 方法」会自动出现在这里；那两个真实现写在 ``_RemoteMetaStore`` 上，"
        "装配口 ``bind_reader`` 也在那里。"
    )
    return type("RemoteMetaStore", (_RemoteMetaStore,), namespace)


RemoteMetaStore = _build_remote()
"""KB 侧的元数据实现（本机档 ``stores.meta.kb`` 就是它，见 ``core/storage.py``）。"""


# ---------------------------------------------------------------- 远端 JSON → 记录类型
#
# 两边逐栏对过（NAS 的 ``KnowledgeBaseOut`` ↔ 这里的 ``KnowledgeBaseRecord``）：
# 下面这几组是**同名同义**的那几栏；远端没有的那几栏（``embedding_base_url`` /
# ``owner_id`` / ``embedding_model_pk`` / ``updated_at``）留在记录类型自己的默认值上
# ——**不在这里编一个值出来**（"看起来有、其实是猜的"比"没有"糟得多）。
#
# 给默认值的那几栏**只在远端真给了值时**才覆盖：在 ``or`` 里再写一遍默认值
# （``"fixed"`` / 512 / 3 …）就是把记录类型的默认值抄成第二份，而两份迟早会漂。

_OPTIONAL_TEXT = ("description", "chunk_strategy", "suggested_prompt", "system_prompt")
_OPTIONAL_INT = ("chunk_size", "chunk_overlap", "suggested_count")
_OPTIONAL_FLAG = ("suggested_enabled", "wiki_enabled")


def _to_record(payload: Mapping[str, object], *, fallback_id: str = "") -> KnowledgeBaseRecord:
    """远端那一个库的 JSON → ``KnowledgeBaseRecord``。

    取值一律**宽容**（认不出的回空 / 默认，绝不抛）：对面换了一版、某一栏变形了的时候，
    表现该是"这一栏没有"，而不是让一次读库级提示词把整轮问答炸掉（``ChatService``
    那条路上它只是增强，不是依赖）。四栏必给的（id / name / embedding_model_id /
    embedding_dim）也宽容——远端那份契约里它们必在，但"没给"的形态不该把异常抛到
    调用方脸上。
    """
    fields: dict[str, Any] = {
        "id": _text(payload.get("id")) or fallback_id,
        "name": _text(payload.get("name")),
        "embedding_model_id": _text(payload.get("embedding_model_id")),
        "embedding_dim": _number(payload.get("embedding_dim"), default=0),
    }
    for key in _OPTIONAL_TEXT:
        value = _text(payload.get(key))
        if value:
            fields[key] = value
    for key in _OPTIONAL_INT:
        count = _number(payload.get(key), default=None)
        if count is not None:
            fields[key] = count
    for key in _OPTIONAL_FLAG:
        flag = payload.get(key)
        if flag is not None:
            fields[key] = _flag(flag)
    model_pk = _text(payload.get("suggested_model_pk"))
    if model_pk:
        fields["suggested_model_pk"] = model_pk
    created = _when(payload.get("created_at"))
    if created is not None:
        fields["created_at"] = created
    return KnowledgeBaseRecord(**fields)


def _text(value: object) -> str:
    """一栏文本（``None`` → 空串；别的按 ``str`` 取，不在这里发明格式）。"""
    return "" if value is None else str(value)


def _number(value: object, *, default: int | None = 0) -> int | None:
    """一栏整数（认不出就 ``default``——不猜）。

    ``bool`` 单独挡掉：它是 ``int`` 的子类，而"有没有"与"几条"是两件事。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _flag(value: object) -> bool:
    """一栏开关：布尔与 ``0/1`` / ``true/false/yes/no/on/off`` 那几种写法都认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _when(value: object) -> datetime | None:
    """一栏时间戳：ISO 8601 字符串 → ``datetime``；认不出回 ``None``（不猜时区、不抛）。

    NAS 那份是 pydantic 序列化出来的 ISO 串（含 ``Z`` 后缀），``datetime.fromisoformat``
    从 3.11 起认得它——不必自己写解析，也不必为此抬一个日期库依赖。
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
