r"""知识库元数据快照：SWR 的核心（M4 阶段 2，方案 §4）。

## 缓存在哪一层（§2.3，这一条决定其余一切）

**写者只有本机后端一个**，页面是纯读者。快照落在本机 SQLite（``StoreBundle.kb_cache``，
接口见 ``app.storage.base.KbMetaCache``），同时服务两个读面：

- **reader 面**（``stores.meta.kb`` → ``ChatService._kb_prompt``，每轮每库读一次）：
  :class:`CachedKnowledgeMetaReader` 包在 M3 的 ``knowledge_meta()`` **外面**，
  命中零网络——这是交互路径上最实的收益（v0.3 §6.1-2「交互路径零网络」）；
- **页面面**（阶段 4 的 ``/local/kb-cache/*``）：:meth:`KbMetaCacheService.snapshot` 把快照
  直接给出去，页面拿它「先画一帧」。**页面自己那条实时读一字不改**（仍是会话身份直连 NAS）。

**页面不许回写快照**：两个身份写同一行，「这份内容是谁看到的」就没有答案了（§2.3）。

## 什么进缓存、什么绝不进（§1，清单只有这一份）

正向 = ``CACHEABLE_RESOURCES`` 那五个读路径元数据资源；负向 = ``NEVER_CACHED``，
逐条带理由。:class:`KbMetaCacheService` 的每个入口都过 :meth:`KbMetaCacheService._require`，
非法资源名当场 ``ValueError``（「顺手缓存一下检索结果」在那一步就撞墙）。

四道机械防线（§1.3）：① 上面两份清单（唯一一份）；② ``PROVIDER_SURFACE`` ——
``KnowledgeProviderClient`` 的**每个公开方法**都必须在那张表里被分类（用例逐名核对，
出现新方法而未分类即红）；③ 行为用例（``retrieve_sources`` / ``submit`` 前后
``kb_meta_cache`` 行数不变）；④ 端点面只有 GET / POST revalidate / DELETE（阶段 4）。

## SWR（§4.2 的流程图逐行落在这里）

读（:meth:`KbMetaCacheService.snapshot`）：

- 命中且 15 秒内确认过（``REVALIDATE_AFTER_SECONDS``）→ 立即回，**零网络**；
- 命中但过了 15 秒 → 立即回 + 后台**恰好一次**再验证；
- 未命中 → 给了 ``fetch``（reader 面）就同步取一次（必须现在给答案），
  没给（页面面）就 ``available=False``（页面照旧骨架屏）；
- 超龄（30 天没确认，存储层判 + 顺手删行）/ payload 坏了（形状不对，这一层判）→ 当没有。

写（:meth:`KbMetaCacheService.refresh`）：整取 → ``sha256(canonical_json)`` → 与旧版比：

- 哈希相同 ⇒ **只推 ``checked_at``**（不重写 payload、不动 ``fetched_at``：§4.1 ——
  「没变」就是「如实更新」里的零变化，重写一遍只会让界面把「什么都没更新」说成
  「刚更新过」）；
- 哈希不同 ⇒ 整行换新（payload + version + fetched_at 一起换）；
- 失败 ⇒ ``stale=1`` + ``last_error``，**快照照旧可读**（「恰好没有这个库」仍是另一件事：
  远端 404 ⇒ 删行）。

``kb_list`` 那一次顺带把每库的 ``kb_detail`` 行按**同一份 payload 拆开写**（§3.1）：
一次「列库」的确认因此同时确认了每个库，reader 面每轮每库那一次读才真能零网络。

**NAS 读端点没有 ETag / Last-Modified**（§4.1 实测），所以「变没变」的判据只能是内容哈希；
``etag`` / ``last_modified`` 两列照建好、今天恒 ``NULL``（将来 NAS 侧加上条件 GET 时，
:data:`KbMetaFetcher` 要扩成能带回响应头的形状——今天不先造那个类型）。

## 防再验证风暴（§4.4）

同一 ``(provider, resource, scope)`` **单飞**（排上了就算在飞，不再排）+ 每键最短间隔
``REVALIDATE_AFTER_SECONDS`` + 失败退避 ``RETRY_BACKOFF_SECONDS`` + 后台**一个**守护线程
（先例见 ``api/v1/local.py`` 的旧会话导入；``sqlite_impl/connection.py`` 每线程一条连接，
后台线程自带一条，**无需加锁**）。这一层的内存状态**重启就忘**，最坏只是多探一次——
它不是事实源，事实源是那张表与 NAS。

## 地址隔离（§5）

键空间是 ``(provider, resource, scope_key)`` 三列，``provider`` 由 :data:`KbMetaProviderKey`
现取（设置页改了地址 ⇒ 立刻换一片键空间）。**按地址隔离、不清旧行**：家里的 NAS 与单位的
NAS 各留一份，切回去还能秒开。排完队之后地址变了的那一次再验证**跳过**——那一份内容
属于哪个地址说不清，宁可下次重新排。

## 严格可弃（v0.3 §5.3）

删了只丢速度，不丢数据：真话永远在 NAS 上。所以这里的每个判据都朝「宁可说没有，
也不要说错」，而且**没有任何调用方可以拿它做判断**——它只负责「先画一帧」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.storage.base import KbMetaCache, KbMetaCacheRecord, KbMetaCacheStats

logger = logging.getLogger(__name__)

__all__ = [
    "CACHEABLE_RESOURCES",
    "DOCUMENT",
    "DOC_LIST",
    "DOC_LIST_SCOPE_SEPARATOR",
    "FOLDERS",
    "KB_DETAIL",
    "KB_LIST",
    "NEVER_CACHED",
    "PROVIDER_SURFACE",
    "RETRY_BACKOFF_SECONDS",
    "REVALIDATE_AFTER_SECONDS",
    "SOURCE_READER",
    "SOURCE_REVALIDATE",
    "STRIPPED_FIELDS",
    "CachedKnowledgeMetaReader",
    "KbMetaCacheService",
    "KbMetaSnapshot",
    "KnowledgeMetaSource",
    "cache_version",
    "canonical_json",
    "doc_list_scope_key",
]


# ------------------------------------------------------------------ 资源清单（唯一一份）

KB_LIST = "kb_list"
KB_DETAIL = "kb_detail"
DOC_LIST = "doc_list"
DOCUMENT = "document"
FOLDERS = "folders"

CACHEABLE_RESOURCES: dict[str, str] = {
    KB_LIST: "库列表（GET /knowledge-bases）：列表页与卡片/行两态的骨架",
    KB_DETAIL: "库详情（GET /knowledge-bases/{id}）：reader 面每轮每库读的就是它",
    DOC_LIST: (
        "文档列表（GET /knowledge-bases/{id}/documents）：**只认规范视图**"
        "（无 q/stage/source_kind）"
    ),
    DOCUMENT: "文档条目（GET /documents/{id}）：文档详情页 / 抽屉的入口帧",
    FOLDERS: "库内目录（GET /knowledge-bases/{id}/folders）：与文档列表同屏的目录树",
}
"""**进缓存的五个资源**（§1.1）。键就是 :meth:`KbMetaCacheService._require` 认的全部取值，
值是一句为什么进——两份信息放一处，免得后来者往别处再抄一份清单。

``loadCounts`` 那次 ``listDocuments(root:true, limit:1)`` 也落在这里（``doc_list`` 的一个
``size:1`` 的视图），不必单开资源。"""

NEVER_CACHED: dict[str, str] = {
    "POST /search（检索）": (
        "v0.3 §6.3 定案：轮次中的检索调用不缓存（正确性优先）。它决定「这一轮依据了什么」，"
        "会进 sources 快照与 [n] 引用编号——缓存它等于让回答依据过期资料"
    ),
    "写类动作（入库 / 建库 / 改名 / 删库 / 目录 / 切块 / 分享 / Wiki 写）": (
        "§6.3 定案「写一律不进」；写成功后由页面那条链如实更新，并就地失效对应缓存键（§3.4-1）"
    ),
    "GET /documents/{id}/timeline、progress、租约 / 停滞": (
        "活数据；「看起来在动其实没动」是最坏的假象"
    ),
    "GET /documents/{id}/parts | /chunks | /preview | /download-url | /content": (
        "v0.3 §4：本地只留**元数据**；正文不是元数据，体积也不可控（§5.3 快照严格可弃）"
    ),
    "GET /trash | /api-keys | /users | /stats/* | /tasks | /maintenance/*": (
        "账号 / 后台 / 聚合，不属于「知识库读路径的元数据」"
    ),
    "GET /provider/handshake": (
        "它是**状态**不是数据，已有自己的 30s 进程内缓存（``services/knowledge_provider.py``）；"
        "M4 不落库"
    ),
}
"""**绝不进快照的那些东西**（§1.2），逐条带理由。

它与 :data:`CACHEABLE_RESOURCES` 一起构成 §1.3 的第一道防线：正向清单管「能不能写进来」，
这一份管「为什么不写」——将来有人问「检索为什么不缓存」，答案在这里，不在某次会议记录里。"""

PROVIDER_SURFACE: dict[str, frozenset[str]] = {
    # 地址与凭据的解析结果（配置本身，不是 NAS 的数据）。
    "target": frozenset(),
    # 握手结论：**状态**，自己已有 30s 进程内缓存（``HANDSHAKE_TTL_SECONDS``）。
    "status": frozenset(),
    # 检索：决定这一轮依据了什么（进 sources 与 [n] 编号）——缓存它等于让回答依据过期资料。
    "retrieve_sources": frozenset(),
    # 写：入库。写完页面那条链自己知道，这里留副本只会留下一份会说谎的旧清单。
    "submit": frozenset(),
    # 写口的窄视图（与 ``submit`` 同一件事）。
    "ingest_gateway": frozenset(),
    # 空操作（NAS 自己入队），没有任何数据可缓存。
    "enqueue_gateway": frozenset(),
    # 进度与时间线：活数据，冻结的进度条是最糟的假象（§1.1）。
    "document_status": frozenset(),
    # **唯一进快照的那一条**：reader 面的两个方法就是这两个资源（§2.3）。
    "knowledge_meta": frozenset({KB_LIST, KB_DETAIL}),
    # 页面面（``/local/kb-cache/*``）的三个读取：文档列表 / 文档条目 / 库内目录。
    # 它自己也只是个视图，但**它的三个方法就是那三个资源**（M4 阶段 4 加的，
    # 与 `knowledge_meta` 同一条口径：视图按"它的方法能喂哪个资源"分类）。
    "page_meta": frozenset({DOC_LIST, DOCUMENT, FOLDERS}),
}
"""``KnowledgeProviderClient`` 的公开方法逐名分类（§1.3 的第二道防线）。

值 = **这个方法的结果可以进哪个资源的快照**（空集 = 一律不进）。用例枚举那个类的公开
方法，与这张表的键**逐名相等**：谁加了一个公开方法却没在这一行表态，用例当场红——
「顺手缓存一下」这条路必须先过这一关。"""

STRIPPED_FIELDS: dict[str, tuple[str, ...]] = {
    KB_LIST: ("can_write", "can_manage"),
    KB_DETAIL: ("can_write", "can_manage"),
    DOC_LIST: ("progress",),
    DOCUMENT: ("progress",),
    FOLDERS: (),
}
"""进快照前**一律剥掉**的字段（§1.1 那张表的最后一列；键与 :data:`CACHEABLE_RESOURCES` 对齐）。

- ``can_write`` / ``can_manage`` 是**按调用者身份算的**（``api/v1/knowledge_bases.py`` 的
  ``kb_access_flags``）：落进快照就等于「把某一刻某个身份看到的东西当成了这个库的属性」，
  而快照是给**另一个身份**（页面）看的（决策点 D-B / R5）；
- ``progress`` 是活数据：**冻结的进度条是最糟的假象**（决策点 D-D / R1）。§1.1 那张表只在
  ``doc_list`` 那一格点名了它，但这条纪律是全局的——``document`` 那一行同样剥掉，
  「进度不来自快照」不因资源不同而松口。

``folders`` 刻意是空元组而不是「没有这一项」：**每个资源都要在这一行表态**，
漏掉的那个会静默地什么都不剥。"""

LIST_RESOURCES: frozenset[str] = frozenset({KB_LIST, DOC_LIST, FOLDERS})
"""载荷形状是 ``{"items": [...]}`` 的那三个资源（剥字段要**逐项**剥，不是剥外壳）。"""

DOC_LIST_SCOPE_SEPARATOR = "|"
"""文档列表视图指纹的分隔符（§1.1 的 ``kb_id|folder:…|page:…|size:…``）。

它还被 :meth:`KbMetaCacheService.invalidate` 用来拼「这一库的全部视图」那个前缀——
存储层不认识这个格式（``purge_kb_meta_cache`` 的 ``scope_prefix`` 说明），
所以拼前缀这件事只在这里发生。"""

REVALIDATE_AFTER_SECONDS = 15.0
"""**读触发再验证**的最短间隔（§4.4）：``checked_at`` 过了这么久，下一次读才再排一次。

两个作用：它是「新鲜」的判据（15 秒内命中直接回，连排都不排），也是每个键的排程下限
（多窗口 focus、页面一次挂 3 个视图，都落成一次再验证）。"""

RETRY_BACKOFF_SECONDS = 60.0
"""再验证**失败后**的退避（§4.4）：NAS 断着的时候不许每读一次就打一次。

与上面那条分开：失败**不推进** ``checked_at``（一次失败不是一次确认），所以「过期」这个
判据会一直为真——挡住风暴的只能是这一条内存退避（重启就忘，见模块头）。"""

SOURCE_READER = "reader"
"""这行是**读的时候同步取回来的**（未命中 → 现在给答案，``inner`` 那条路）。"""

SOURCE_REVALIDATE = "revalidate"
"""这行是**再验证换来的**（后台那一次或显式 ``POST /revalidate``）。

``source`` 的第三个取值 ``handshake`` 由 §3.1 留给「握手那份清单直接落一行」的将来
（今天不写：握手是状态，§1.2 明确不落库）。"""

NO_STORE_REASON = "这台机器没有留副本的地方（快照只在本机后端留）"
NO_SNAPSHOT_REASON = "本机还没有看到过这份内容"
BROKEN_SNAPSHOT_REASON = "本机留的那一份读不出来了，已经丢掉"
REMOTE_MISSING_REASON = "远端说没有这个东西"
"""``available=False`` 时那几句人话（阶段 4 原样回给前端，界面文案见阶段 5）。

刻意**不出现「缓存」二字**：这是要递到界面上去的句子（§4.5 / R10 / ``check_layering.py``
的 U2 是硬门禁）。真正给用户看的那两句（「上次更新于 X」/「现在连不上，这是上次看到的
内容（X）」）在 ``features/knowledge/snapshot.ts``，那是阶段 5 的活。"""

CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "api_key_hash",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "key_hash",
        "passwd",
        "password",
        "password_hash",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
"""**键名本身就是凭据**的那几种（R5 的兜底扫描，小写、连字符归一成下划线后比对）。"""

CREDENTIAL_KEY_SUFFIXES: tuple[str, ...] = ("_token", "_secret", "_password", "_credential")
"""名字以这些结尾的也算（``provider_secret`` / ``kb_token`` 这种拼法）。

**刻意不做 ``*_key`` 的泛匹配**：``object_key`` / ``avatar_key`` 是存储键，不是凭据——
把存储键当凭据剥掉，坏的是内容而不是安全。"""

CREDENTIAL_PATH_REPORT_LIMIT = 5
"""日志里最多报几条可疑路径（够定位，又不至于把一行日志灌满）。"""


# --------------------------------------------------------------------------- 类型

KbMetaFetcher = Callable[[], Any]
"""**取一次远端**（整取）：返回 NAS 的原样 JSON。

三条口径（§4.2）：

- 返回 ``None`` = 远端**明确说**「没有这个东西」（404）⇒ 删掉那一行，答案是「没有」；
- 抛异常 = 取不到（连不上 / 被拒 / 5xx / 别的一律）⇒ 有快照就回快照 + 记 ``stale``，
  没有快照就把那句原因原样抛给调用方（必须现在给答案，而答案是「取不到」）；
- **由调用方给**（reader 面给 ``inner`` 的一次调用，页面面给那一条 HTTP 读）：
  这一层因此不认识 HTTP，也不认识 ``knowledge_provider``（不 import 以免环）。"""

KbMetaProviderKey = Callable[[], str]
"""取当前的提供者地址（键空间的第一列）。每次现取——设置页改了地址立刻生效（§5）。"""


class KnowledgeMetaSource(Protocol):
    """reader 面的 ``inner``（**鸭子类型**：M3 的 ``knowledge_meta()`` 结构上满足它）。

    与 ``storage/split_impl/remote_meta.py::KnowledgeMetaReader`` **同形**：那两个方法就是
    方案 §2.3 的形状（回 NAS 的原始 dict；``None`` = 没有这个库；取不到抛
    ``KnowledgeBaseUnavailable``）。这里再声明一份而不是 import 那个：``services/``
    只许见 ``app.storage.base``（工程规范 §3.3 的 L2），而那个协议住在 ``split_impl`` 里。
    """

    def get_knowledge_base(self, kb_id: str) -> dict[str, Any] | None: ...

    def list_knowledge_bases(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class KbMetaSnapshot:
    """一次读快照的答案（``snapshot()`` / ``refresh()`` 共用这一个形状）。

    ``available=False`` 时 ``payload`` 是 ``None``、``reason`` 是那句人话（页面照旧骨架屏，
    阶段 4 原样回给前端）。``stale=True`` 表示**上一轮再验证失败了**：内容照旧可用，
    但要如实标出来（界面上那句「现在连不上，这是上次看到的内容（X）」）。
    """

    available: bool
    resource: str
    scope_key: str
    payload: Any = None
    version: str = ""
    source: str = ""
    fetched_at: datetime | None = None
    """这份**内容**是什么时候看到的（界面那句「上次更新于 X」读它）。"""
    checked_at: datetime | None = None
    """最近一次**确认**（含「确认过没变」）。**两个时间戳不许混**（§3.1 / §5）。"""
    stale: bool = False
    last_error: str = ""
    reason: str = ""
    revalidating: bool = False
    """这次读有没有顺带排一次后台再验证（页面据此知道「马上会更新的」）。"""


# --------------------------------------------------------------------- 规范化与版本戳


def canonical_json(payload: Any) -> str:
    """**唯一的一份**规范化序列化（§4.1）：``sort_keys=True, separators=(",", ":"),
    ensure_ascii=False``。

    哈希与落库**共用这一串文本**：两处各序列化一遍（比如哈希用排序、落库用原序）会让
    「内容没变」与「文本没变」变成两件事，而「变没变」整个判据就压在这上面。
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_version(payload: Any) -> str:
    """版本戳 = ``sha256:`` + ``sha256(canonical_json)``（§4.1）。

    NAS 的 KB 读端点没有 ETag / Last-Modified（实测，§4.1），所以「变没变」只能按内容算：
    再验证是「整取 + 比哈希」，哈希相同就是「确认过，还是那份」。
    """
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def doc_list_scope_key(
    kb_id: str,
    *,
    folder: str | None = None,
    root: bool = False,
    page: int = 1,
    size: int = 50,
) -> str:
    """文档列表**规范视图**的键：``kb_id|folder:<id|root|all>|page:<n>|size:<n>``（§1.1）。

    **筛选项在这个签名里没有位置**，这就是决策点 D-D 的机械形态：搜索（``q``）、阶段
    （``stage``）、来源（``source_kind``）**换不出键**——「这一问的答案」过期即误导，
    它不该有副本。阶段 4 的端点遇到带这些参数的请求直接 ``available:false`` + 原因；
    阶段 5 的 ``docListViewKey()``（前端）与这一处必须给出同一个串。

    ``page`` / ``size`` 与实时读那一条的 ``offset`` / ``limit`` 是同一件事的两种说法
    （``offset = (page - 1) * size``）：快照这一面按页说，实时那一面按偏移说（那边的
    分页参数就长这样）。默认值取后端文档列表的默认（``page=1`` / ``size=50``）。
    """
    if root and folder:
        raise ValueError("folder 与 root 互斥（一个说「这个目录里的」，一个说「未归档的」）")
    if page < 1:
        raise ValueError(f"page 从 1 起：{page!r}")
    if size < 1:
        raise ValueError(f"size 至少 1：{size!r}")
    where = f"folder:{folder}" if folder else ("folder:root" if root else "folder:all")
    return DOC_LIST_SCOPE_SEPARATOR.join([kb_id, where, f"page:{page}", f"size:{size}"])


# ------------------------------------------------------------------- 进快照前的收口


def _for_cache(resource: str, payload: Any) -> Any:
    """进快照前的形状收口（§1.1 的最后一列 + R5 的兜底）：剥完再回。

    两件事，顺序固定：

    1. **按资源剥字段**（:data:`STRIPPED_FIELDS`）：权限位与 ``progress`` 一律不落；
    2. **凭据兜底**：递归扫一遍，名字像凭据的键（:data:`CREDENTIAL_KEYS` /
       :data:`CREDENTIAL_KEY_SUFFIXES`）剥掉并把路径记进日志。

    **回给调用方的那一份就是这一份**（与落库的逐字相同）：两个形状会让「页面看到的」
    与「本机留着的」慢慢漂开，而它们本该是同一件事。

    这一层刻意**不抛**：快照严格可弃，出现一个不该有的键不该让整条读路径失败
    （与「payload 太大就不留副本」同一套处置）；但也**绝不**让它进表。发现即记一条日志
    ——它是「NAS 那边多了点什么」的信号，而不是静默的日常。
    """
    stripped = _strip_fields(resource, payload)
    found: list[str] = []
    scrubbed = _scrub_credentials(stripped, path="", found=found)
    if found:
        logger.warning(
            "知识库元数据里有疑似凭据的键，剥掉不留副本（%s）：%s",
            resource,
            "、".join(found[:CREDENTIAL_PATH_REPORT_LIMIT]),
        )
    return scrubbed


def _strip_fields(resource: str, payload: Any) -> Any:
    """剥掉这个资源声明过的那些字段（列表型资源逐项剥，见 :data:`LIST_RESOURCES`）。"""
    fields = STRIPPED_FIELDS[resource]
    if not fields:
        return payload
    if resource not in LIST_RESOURCES:
        return _drop_keys(payload, fields)
    if not isinstance(payload, dict):
        return payload
    items = payload.get("items")
    if not isinstance(items, list):
        return payload
    return {**payload, "items": [_drop_keys(item, fields) for item in items]}


def _drop_keys(node: Any, fields: tuple[str, ...]) -> Any:
    """从 dict 上删掉这几列（不是 dict 就原样回——形状的事由 ``_well_formed`` 判）。"""
    if not isinstance(node, dict):
        return node
    return {key: value for key, value in node.items() if key not in fields}


def _scrub_credentials(node: Any, *, path: str, found: list[str]) -> Any:
    """递归剥掉名字像凭据的键，顺手记下它们的路径（只给人看，不外传）。"""
    if isinstance(node, dict):
        kept: dict[str, Any] = {}
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if _looks_like_credential(str(key)):
                found.append(child)
                continue
            kept[key] = _scrub_credentials(value, path=child, found=found)
        return kept
    if isinstance(node, list):
        return [
            _scrub_credentials(item, path=f"{path}[{index}]", found=found)
            for index, item in enumerate(node)
        ]
    return node


def _looks_like_credential(key: str) -> bool:
    """键名像凭据吗（**只按名字判**：值长什么样不该由这一层猜）。"""
    name = key.strip().lower().replace("-", "_")
    if name in CREDENTIAL_KEYS:
        return True
    return any(name.endswith(suffix) for suffix in CREDENTIAL_KEY_SUFFIXES)


# ------------------------------------------------------------------- 从库里读出来的形状


def _decode(resource: str, payload: str) -> Any | None:
    """payload 文本 → 对象；**形状不对就是坏 payload**（回 ``None``，调用方删行）。

    存储层兜「是不是合法 JSON」（列上的 ``CHECK (json_valid(payload))``），形状是这一层的
    事：一张表可能跨版本留着老行、也可能被人手改过，而「读出来一半」比「当没有」糟得多
    （页面会把残缺的一份当真话画出来）。
    """
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value if _well_formed(resource, value) else None


def _well_formed(resource: str, payload: Any) -> bool:
    """这个资源要的形状：``{"items": [dict…]}`` 或一个 dict（§1.1 那五个资源只有这两种）。"""
    if not isinstance(payload, dict):
        return False
    if resource not in LIST_RESOURCES:
        return True
    items = payload.get("items")
    return isinstance(items, list) and all(isinstance(item, dict) for item in items)


# ---------------------------------------------------------------------------- 服务


@dataclass(slots=True)
class _KeyState:
    """一个键的**在内存里**的排程状态（不落库：重启就忘，最坏只是多探一次）。"""

    last_attempt: float = 0.0
    """上次**发起**再验证的时刻（单调秒）。0 = 从来没排过。"""
    backoff_until: float = 0.0
    """失败退避到什么时候（单调秒）。"""
    in_flight: bool = False
    """在飞**或已排队**（排上的那一刻就置位——单飞判据就是它）。"""


@dataclass(frozen=True, slots=True)
class _Job:
    """一次排队中的再验证。``key`` 里带上排程时的地址：地址改了就跳过这一份（§5）。"""

    key: tuple[str, str, str]
    fetch: KbMetaFetcher


class KbMetaCacheService:
    """知识库元数据快照服务（M4 阶段 2）：两个读面共用的那一份能力。

    ``cache`` 是 :class:`~app.storage.base.KbMetaCache`（本机档的 ``StoreBundle.kb_cache``）；
    服务器档它是 ``None`` —— 那一档没有「抄一份 NAS 快照」这条动作，``cache=None`` 时这个
    服务退化成「每次都取、什么都不留」（如实，不装成有缓存）。用例也借它省掉库。

    ``provider_key`` 取当前的提供者地址（键空间第一列，:data:`KbMetaProviderKey`）；
    ``clock`` 是单调钟（排程间隔与退避），``now`` 是墙上钟（快照时间戳）——**两把钟都要**，
    因为「15 秒内确认过」问的是墙上时间，而「15 秒内不再排」问的是「离上次发起多久」。
    三个都可以注入（用例靠它把时间拨到任意一刻，不用真等）。
    """

    def __init__(
        self,
        cache: KbMetaCache | None,
        *,
        provider_key: KbMetaProviderKey | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._cache = cache
        self._provider_key = provider_key or (lambda: "")
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._states: dict[tuple[str, str, str], _KeyState] = {}
        self._jobs: queue.Queue[_Job] | None = None
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 读（SWR 那一面）

    def snapshot(
        self, resource: str, scope_key: str = "", *, fetch: KbMetaFetcher | None = None
    ) -> KbMetaSnapshot:
        """读一份快照（§4.2 流程图左边那一列）。

        - **给了 ``fetch``**（reader 面）：命中就回；未命中**同步取一次**（必须现在给答案），
          取不到就抛（调用方那一面认的就是「取不到」）；过期时顺带排一次后台再验证。
        - **没给 ``fetch``**（页面面）：只回本机已有的那一份，未命中 ``available=False``
          ——页面照旧骨架屏，它的实时读自己会赢回来（「先画」不该自己变成一次 NAS 往返）。

        坏 payload 与超龄都按「没有」处置；超龄那一半由存储层判并在读的时候顺手删行。
        """
        self._require(resource)
        provider = self.provider()
        if self._cache is None:
            if fetch is None:
                return _unavailable(resource, scope_key, NO_STORE_REASON)
            return self._fetch_now(provider, resource, scope_key, fetch)
        row = self._cache.get_kb_meta_cache(provider, resource, scope_key)
        if row is not None:
            payload = _decode(resource, row.payload)
            if payload is not None:
                revalidating = (
                    fetch is not None
                    and self._needs_revalidate(row.checked_at)
                    and self._schedule(provider, resource, scope_key, fetch)
                )
                # 读的时候再过一次 `_for_cache`（幂等）：表里那份理应是剥好的，但**读的人
                # 不该依赖写的人**——将来谁改了写路径，「权限位不进快照」这条不该跟着塌。
                return _from_row(
                    row, payload=_for_cache(resource, payload), revalidating=revalidating
                )
            logger.warning(
                "知识库元数据快照的形状不对，丢掉这一行：%s/%s/%s", provider, resource, scope_key
            )
            self._cache.drop_kb_meta_cache(provider, resource, scope_key)
            if fetch is None:
                return _unavailable(resource, scope_key, BROKEN_SNAPSHOT_REASON)
            return self._fetch_now(provider, resource, scope_key, fetch)
        if fetch is None:
            return _unavailable(resource, scope_key, NO_SNAPSHOT_REASON)
        return self._fetch_now(provider, resource, scope_key, fetch)

    # ------------------------------------------------------------------ 写（再验证）

    def refresh(
        self,
        resource: str,
        scope_key: str = "",
        *,
        fetch: KbMetaFetcher,
        source: str = SOURCE_REVALIDATE,
    ) -> KbMetaSnapshot:
        """再验证一次：整取 → 算哈希 → 相同只推 ``checked_at`` / 不同整行换新（§4.1）。

        **失败不抛**（除了 ``_require`` 那一档）：远端连不上时回的是「那份快照还在，
        但它现在没被确认」（``stale=True`` + ``last_error``）——这正是 D-C 要的语义，
        也是页面顶上那句话的数据来源。没有快照可回时就 ``available=False`` + 那句原因。

        ``source`` 写进这行（``reader`` / ``revalidate``），**只作排障**：
        快照的可信度不取决于它是哪条路来的。
        """
        self._require(resource)
        provider = self.provider()
        key = (provider, resource, scope_key)
        self._begin(key)
        try:
            try:
                payload = fetch()
            except Exception as exc:  # 远端失败一律折成「这份没被确认」
                return self._record_failure(provider, resource, scope_key, exc)
            if payload is None:
                # 远端说没有这个东西（404）：那一行不作数了（「恰好没有这个库」是答案）
                self._drop(provider, resource, scope_key)
                return _unavailable(resource, scope_key, REMOTE_MISSING_REASON)
            return self._store(provider, resource, scope_key, payload, source=source)
        finally:
            self._end(key)

    # ------------------------------------------------------------------ 失效与清理

    def invalidate(self, resource: str, scope_key: str = "", *, views: bool = False) -> int:
        """**就地失效**（§3.4-1）：删掉这一键，返回删掉的条数。

        两个调用场景都是「这条内容不作数了」：写类动作成功之后（页面那条链自己知道写了
        什么），以及远端 404（那一支由 :meth:`refresh` 内部走 ``_drop``）。

        ``views=True`` 时 ``scope_key`` 视作**库 id**，删这个库的**全部文档列表视图**
        （前缀 ``<kb_id>|``）：一个库的视图有几十个（每页每目录一个），逐个删不是调用方
        该做的事；带分隔符的前缀由这里拼（存储层不认识视图指纹的格式）。
        """
        self._require(resource)
        if views and resource != DOC_LIST:
            raise ValueError(f"views=True 只对 {DOC_LIST} 有意义（别的资源一个键就是它自己）")
        if self._cache is None:
            return 0
        provider = self.provider()
        if views:
            return self._cache.purge_kb_meta_cache(
                provider=provider,
                resource=resource,
                scope_prefix=f"{scope_key}{DOC_LIST_SCOPE_SEPARATOR}",
            )
        return self._cache.drop_kb_meta_cache(provider, resource, scope_key)

    def purge(
        self,
        *,
        provider: str | None = None,
        resource: str | None = None,
        scope_key: str | None = None,
        scope_prefix: str | None = None,
    ) -> int:
        """按档清空（§3.4-4）：全清 / 按地址 / 按库，返回删掉的条数。

        四个条件给了就 AND；都不给就是**全清**（设置面板那颗「清除」）。
        与 :meth:`invalidate` 的分工：那个是「这一条不作数了」（一个键），
        这个是「把留着的清掉」（额度管理 / 手动清理）。
        """
        if resource is not None:
            self._require(resource)
        if self._cache is None:
            return 0
        return self._cache.purge_kb_meta_cache(
            provider=provider, resource=resource, scope_key=scope_key, scope_prefix=scope_prefix
        )

    def stats(self) -> KbMetaCacheStats:
        """**当前地址**的用量读数（设置面板那句「本机留了一份 / 最近更新」）。

        按当前地址收窄是刻意的：这个服务管的就是这一片键空间——换过地址之后旧地址的行
        还在库里（§5：按地址隔离、不清旧行），但它们不是「这台机器现在连的那台 NAS」。
        """
        if self._cache is None:
            return KbMetaCacheStats()
        return self._cache.kb_meta_cache_stats(provider=self.provider())

    # ------------------------------------------------------------------ 内部：取与写

    def provider(self) -> str:
        """当前的提供者地址（归一化：去尾斜杠）。**键空间靠它隔离，所以只在这里归一化。**"""
        return self._provider_key().rstrip("/")

    def _fetch_now(
        self, provider: str, resource: str, scope_key: str, fetch: KbMetaFetcher
    ) -> KbMetaSnapshot:
        """未命中时的**同步取一次**（reader 面：必须现在给答案）。

        失败**就地抛**（没有快照可回，「取不到」就是答案：M3 的 reader 契约要求
        ``KnowledgeBaseUnavailable`` 原样传到 ``stores.meta``）；成功就落一行
        （``source=reader``），回给调用方的与落库的是同一份（``_for_cache`` 之后的那份）。
        """
        payload = fetch()
        if payload is None:
            self._drop(provider, resource, scope_key)
            return _unavailable(resource, scope_key, REMOTE_MISSING_REASON)
        return self._store(provider, resource, scope_key, payload, source=SOURCE_READER)

    def _store(
        self, provider: str, resource: str, scope_key: str, payload: Any, *, source: str
    ) -> KbMetaSnapshot:
        """一份刚取到的内容 → 落库（或只推确认）+ 回一个「刚看到的」快照。

        哈希与上一次相同 ⇒ **只推 ``checked_at``**（§4.1）：payload / version / fetched_at
        都原封不动。KB_LIST 那一次之后顺带派生每库的 ``kb_detail`` 行。
        """
        cached = _for_cache(resource, payload)
        version = cache_version(cached)
        moment = _to_millis(self._now())
        previous = self._read(provider, resource, scope_key)
        if previous is not None and previous.version == version:
            if self._cache is not None:
                self._cache.touch_kb_meta_cache(
                    provider, resource, scope_key, checked_at=moment, stale=False, last_error=""
                )
            fetched_at = previous.fetched_at
        else:
            self._put(provider, resource, scope_key, cached, version, moment=moment, source=source)
            fetched_at = moment
        if resource == KB_LIST:
            self._derive_details(provider, cached, source=source, moment=moment)
        return KbMetaSnapshot(
            available=True,
            resource=resource,
            scope_key=scope_key,
            payload=cached,
            version=version,
            source=source,
            fetched_at=fetched_at,
            checked_at=moment,
        )

    def _put(
        self,
        provider: str,
        resource: str,
        scope_key: str,
        payload: Any,
        version: str,
        *,
        moment: datetime,
        source: str,
    ) -> None:
        """整行写进去（``put`` 自己在超限时拒收并记日志：那是它的判据，这里不重复判）。"""
        if self._cache is None:
            return
        self._cache.put_kb_meta_cache(
            KbMetaCacheRecord(
                provider=provider,
                resource=resource,
                scope_key=scope_key,
                payload=canonical_json(payload),
                version=version,
                source=source,
                fetched_at=moment,
                checked_at=moment,
            )
        )

    def _derive_details(
        self, provider: str, payload: Any, *, source: str, moment: datetime
    ) -> None:
        """``kb_list`` 的那一份 payload **拆开写**成每库一行 ``kb_detail``（§3.1）。

        为什么：避免两处内容各自过期。一次「列库」的确认（哈希没变也是确认）应当同时把
        每个库那一行推上去——reader 面「每轮每库读一次」因此不必各自再打一次 NAS。

        逐项比哈希：内容真的变了才整行换、``fetched_at`` 才前进；没变就只推 ``checked_at``。
        **剥字段走 ``KB_DETAIL`` 那一行**（列表项与详情是同一个 ``KnowledgeBaseOut``，
        权限位同样不许落）。
        """
        if self._cache is None or not isinstance(payload, dict):
            return
        items = payload.get("items")
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            kb_id = item.get("id")
            if not isinstance(kb_id, str) or not kb_id:
                # 没有 id 就派不出键（NAS 的列表项必然有 id）；跳过，不编一个出来
                continue
            detail = _for_cache(KB_DETAIL, item)
            version = cache_version(detail)
            previous = self._cache.get_kb_meta_cache(provider, KB_DETAIL, kb_id)
            if previous is not None and previous.version == version:
                self._cache.touch_kb_meta_cache(provider, KB_DETAIL, kb_id, checked_at=moment)
                continue
            self._put(provider, KB_DETAIL, kb_id, detail, version, moment=moment, source=source)

    def _record_failure(
        self, provider: str, resource: str, scope_key: str, exc: Exception
    ) -> KbMetaSnapshot:
        """再验证失败：记退避 + 把那行标成「上次没确认成」，**快照照旧可读**（§4.5）。

        ``checked_at`` **不推进**——一次失败不是一次确认（``SNAPSHOT_MAX_AGE_SECONDS`` 判的
        就是「最近一次确认」）；所以这份快照会一直「过期」，挡住反复重试的是上面那条内存
        退避，而不是时间戳。
        """
        reason = str(exc) or type(exc).__name__
        with self._lock:
            self._state((provider, resource, scope_key)).backoff_until = (
                self._clock() + RETRY_BACKOFF_SECONDS
            )
        logger.warning(
            "知识库元数据再验证失败（%s/%s/%s，%.0f 秒内不再排）：%s",
            provider,
            resource,
            scope_key,
            RETRY_BACKOFF_SECONDS,
            reason,
        )
        row = self._read(provider, resource, scope_key)
        payload = None if row is None else _decode(resource, row.payload)
        if row is None or payload is None:
            if row is not None:
                self._drop(provider, resource, scope_key)
            return _unavailable(resource, scope_key, reason)
        self._cache.touch_kb_meta_cache(  # type: ignore[union-attr]  # row 非 None ⇒ 有库
            provider,
            resource,
            scope_key,
            checked_at=row.checked_at,
            stale=True,
            last_error=reason,
        )
        return _from_row(row, payload=_for_cache(resource, payload), stale=True, last_error=reason)

    def _drop(self, provider: str, resource: str, scope_key: str) -> int:
        """删一行（远端说没有这个东西）。没有存储时是 0，不是错误。"""
        if self._cache is None:
            return 0
        return self._cache.drop_kb_meta_cache(provider, resource, scope_key)

    def _read(self, provider: str, resource: str, scope_key: str) -> KbMetaCacheRecord | None:
        """读一行（没有存储时是 ``None``：那一档什么都留不下，也就不可能有行）。"""
        if self._cache is None:
            return None
        return self._cache.get_kb_meta_cache(provider, resource, scope_key)

    # ------------------------------------------------------------------ 内部：排程与线程

    def _needs_revalidate(self, checked_at: datetime) -> bool:
        """这份快照**该再确认一次**了吗（§4.4：距上次确认超过 ``REVALIDATE_AFTER_SECONDS``）。"""
        return (self._now() - checked_at).total_seconds() >= REVALIDATE_AFTER_SECONDS

    def _schedule(self, provider: str, resource: str, scope_key: str, fetch: KbMetaFetcher) -> bool:
        """排一次后台再验证；**排上了回 True**（也就是说这一读触发了刷新）。

        三道门（§4.4 的「防再验证风暴」）：在飞（含已排队）不再排 / 每键最短间隔
        ``REVALIDATE_AFTER_SECONDS`` / 失败退避 ``RETRY_BACKOFF_SECONDS``。
        ``in_flight`` 在**排上的那一刻**置位——它是「单飞」的全部实现，也是防队列堆积的
        那道闸。
        """
        key = (provider, resource, scope_key)
        with self._lock:
            state = self._state(key)
            moment = self._clock()
            if state.in_flight:
                return False
            if moment - state.last_attempt < REVALIDATE_AFTER_SECONDS:
                return False
            if moment < state.backoff_until:
                return False
            state.in_flight = True
            state.last_attempt = moment
            self._queue().put(_Job(key=key, fetch=fetch))
            return True

    def _queue(self) -> queue.Queue[_Job]:
        """懒建队列与**那一个**守护线程（第一次有人要再验证时才起）。

        「一个」是 §4.4 写死的：多几个线程只会让 NAS 收到并发风暴，而这一层没有任何需要
        并发的地方（一次再验证就是一次 GET）。线程是 daemon：边车退出不该被一次再验证
        吊住（照 ``api/v1/local.py`` 那条导入线程的先例）。
        """
        jobs = self._jobs
        if jobs is None:
            jobs = queue.Queue()
            self._jobs = jobs
            worker = threading.Thread(
                target=self._work, args=(jobs,), name="kb-meta-revalidate", daemon=True
            )
            self._worker = worker
            worker.start()
        return jobs

    def _work(self, jobs: queue.Queue[_Job]) -> None:
        """后台线程的循环：取一件、做一件。**绝不让异常终结这个线程**。

        异常只留在那一轮里（``refresh`` 已经把远端失败折成 ``stale``，这里兜的是我们自己
        的 bug）：线程死了的话，下一次「读触发再验证」就永远不会发生，而现象是
        「快照悄悄不再更新」——那比一次失败难查得多。
        """
        while True:
            job = jobs.get()
            try:
                if job.key[0] != self.provider():
                    # 排上队之后地址变了：这一份内容属于哪个地址说不清，不写（§5）
                    logger.debug("后台再验证跳过：地址在排队期间改了（%s）", job.key[0])
                    continue
                self.refresh(job.key[1], job.key[2], fetch=job.fetch)
            except Exception:  # 线程绝不因一次任务而退出
                logger.exception("后台再验证异常退出：%s", job.key)
            finally:
                self._end(job.key)

    def _state(self, key: tuple[str, str, str]) -> _KeyState:
        """取（必要时建）一个键的状态。**调用方必须已经拿着 ``self._lock``**。"""
        state = self._states.get(key)
        if state is None:
            state = _KeyState()
            self._states[key] = state
        return state

    def _begin(self, key: tuple[str, str, str]) -> None:
        """一次再验证开始（同步的与后台的都走这里）：置在飞、记下发起时刻。"""
        with self._lock:
            state = self._state(key)
            state.in_flight = True
            state.last_attempt = self._clock()

    def _end(self, key: tuple[str, str, str]) -> None:
        """一次再验证结束（幂等：后台那份在外层再清一次，地址改了时两边清的是不同的键）。"""
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                state.in_flight = False

    def _require(self, resource: str) -> None:
        """只接受正向清单里的资源（§1.3 的第一道防线：非法直接 ``ValueError``）。"""
        if resource not in CACHEABLE_RESOURCES:
            raise ValueError(
                f"这个资源不进知识库元数据快照：{resource!r}；"
                f"能进的是 {sorted(CACHEABLE_RESOURCES)}（理由见 NEVER_CACHED）"
            )


class CachedKnowledgeMetaReader:
    """reader 面的 SWR 包装（§2.3 / §4.2）：``stores.meta.kb`` 与远端之间的那一层。

    ``inner`` 是**鸭子类型**（:class:`KnowledgeMetaSource`）：M3 的 ``knowledge_meta()``
    返回的那个对象结构上满足它。这里刻意不 import ``knowledge_provider``——反过来是本模块
    的判据被那边引用（那一侧的模块头写着「判据在 ``services/kb_cache.py``」），真 import
    就成环。

    ``stores.meta`` 与 ``services/`` 的调用点**一行不改**（M3 §2.4 的承诺）：这一层只是把
    「每次一次 NAS 往返」变成「命中零网络」，语义逐字保留——

    - 命中 → 立即回（**零网络**）；过期 → 回快照 + 后台再验证；
    - 未命中 → 同步取一次（必须现在给答案）；
    - 远端 404 → 删行、回 ``None``（「没有这个库」是答案）；
    - 远端失败 → 没有快照就抛 ``KnowledgeBaseUnavailable``（``inner`` 的原话），
      有快照就回快照 + **记一条日志**（D-C：读库元数据失败，用的是 X 分钟前的快照）。

    **组合根怎么接**（阶段 3 的那一处换线）：

        service = KbMetaCacheService(
            bundle.kb_cache, provider_key=lambda: provider.target().base_url
        )
        stores.meta.kb.bind_reader(CachedKnowledgeMetaReader(
            inner=provider.knowledge_meta(), cache=service))

    ``provider_key`` 归**服务**持有（键空间是它的事：读、写、清、报数五处都要它），
    reader 只管"这一次读的是谁"。装配期各一次，``stores.meta`` 与 ``services/`` 的调用点
    一个字不用改。
    """

    def __init__(self, *, inner: KnowledgeMetaSource, cache: KbMetaCacheService) -> None:
        self._inner = inner
        self._cache = cache

    def get_knowledge_base(self, kb_id: str) -> dict[str, Any] | None:
        """这个库的元数据；``None`` = 没有这个库（远端 404），取不到则抛。"""
        snapshot = self._cache.snapshot(
            KB_DETAIL, kb_id, fetch=lambda: self._inner.get_knowledge_base(kb_id)
        )
        if not snapshot.available:
            # 带 fetch 的那条路上，「不可用」只有一种成因：远端明确说没有这个库（404）。
            # 其余失败在取的那一刻就抛出去了（`_fetch_now`），与 M3 的 reader 契约逐字一致。
            return None
        _log_stale_snapshot(KB_DETAIL, kb_id, snapshot)
        return snapshot.payload

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """这次调用看得见的全部库（受限凭据的过滤在 NAS 那一侧做）。"""
        snapshot = self._cache.snapshot(
            KB_LIST, fetch=lambda: {"items": self._inner.list_knowledge_bases()}
        )
        if not snapshot.available:
            # 到不了这一支：列表端点要么回一份 items、要么按「取不到」抛（上面的 fetch 从不回 None）
            return []
        _log_stale_snapshot(KB_LIST, "", snapshot)
        payload = snapshot.payload
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]


# --------------------------------------------------------------------- 小工具（文件末尾）


def _unavailable(resource: str, scope_key: str, reason: str) -> KbMetaSnapshot:
    """``available=False`` 的那个形状（一句人话在 ``reason`` 里，页面照旧骨架屏）。"""
    return KbMetaSnapshot(available=False, resource=resource, scope_key=scope_key, reason=reason)


def _from_row(
    row: KbMetaCacheRecord,
    *,
    payload: Any,
    revalidating: bool = False,
    stale: bool | None = None,
    last_error: str | None = None,
) -> KbMetaSnapshot:
    """库里那一行 → 快照（``stale`` / ``last_error`` 不给就照行上的值）。"""
    return KbMetaSnapshot(
        available=True,
        resource=row.resource,
        scope_key=row.scope_key,
        payload=payload,
        version=row.version,
        source=row.source,
        fetched_at=row.fetched_at,
        checked_at=row.checked_at,
        stale=row.stale if stale is None else stale,
        last_error=row.last_error if last_error is None else last_error,
        revalidating=revalidating,
    )


def _to_millis(moment: datetime) -> datetime:
    """把时间戳对齐到**库里存的精度**（毫秒）。

    本机库的时间列只到毫秒（列名 ``_ms``，``_dump`` 截断）。回给调用方的时间戳若带着
    微秒，就会与「再读一次库里那个值」相差几百微秒——而「两个时间戳不许混」（§3.1 / §5）
    要求它们是**同一个值**，不是「差不多」。
    """
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


def _log_stale_snapshot(resource: str, scope_key: str, snapshot: KbMetaSnapshot) -> None:
    """D-C：拿一份「上次看到的」当答案时记一条日志（现象要说清楚，别等人去猜）。"""
    if not snapshot.stale:
        return
    logger.warning(
        "读知识库元数据失败，用的是%s的快照（%s/%s）：%s",
        _age_text(snapshot.fetched_at),
        resource,
        scope_key,
        snapshot.last_error,
    )


def _age_text(moment: datetime | None) -> str:
    """那句「X 分钟前」（日志用；界面上那句在阶段 5 的 ``snapshot.ts``）。"""
    if moment is None:  # pragma: no cover - 每个可用快照都有 fetched_at
        return "一份没记时间戳"
    seconds = max(0.0, (datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    return f"{seconds / 60:.0f} 分钟前"
