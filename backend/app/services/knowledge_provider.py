r"""**知识库提供者客户端**（M3 阶段 2）：本机服务层打 NAS 知识库的**唯一落点**。

## 它是哪三件套的客户端

提供者那一侧（NAS）的三件套是"握手 / 主能力 / 状态"（v0.3 §3.1）。握手的**端点在
``api/v1/provider.py``（阶段 1，服务器档）**；这一侧是**客户端**——本机档没有知识库
数据源，它的角色就是"调那台提供者"。方法的落点逐条对齐方案 §1.3：

| 能力 | 端点 | 这个方法 |
| --- | --- | --- |
| 连通性 + 能力集 + 库清单 | ``GET /provider/handshake`` | `status` |
| 检索（轮次中） | ``POST /search`` | `retrieve_sources` |
| 入库（上传） | ``POST /knowledge-bases/{kb_id}/documents`` | `submit` |
| 入库（进度） | ``GET /documents/{id}`` ＋ ``/timeline`` | `document_status` |
| 库清单 / 库详情 | ``GET /knowledge-bases[/{id}]`` | `knowledge_meta` |
| 库管理（建 / 改 / 删 / 目录 / 分享 / 切块） | 已有端点，**页面直连 NAS** | **本机侧不映射** |

表里那些名字都在 :class:`KnowledgeProviderClient` 上。第三行那个上传口带 ``start=true``
（NAS 自己入队，所以本机侧的 ``enqueue_ingest`` 是空操作）；第五行那个 reader 是
**给阶段 4 的 ``RemoteMetaStore`` 用的**。

## 两条口径（都是刻意分档的，别"顺手统一"）

1. **握手缓存**（方案 §3.2）：TTL **30s**、**进程内存不落库**（落库是 M4 的事）、
   探针超时 **3s**、**单次调用失败不改状态**（一次检索超时不该让导航消失，会抖）、
   **未知按缺席**（还没探过 = 不在）、**绝不回退进程内检索**（本机根本没有那些表，
   回退只会得到假结果）。`state != ready` 时前端每 30s 轮询一次、`ready` 时不轮询
   ——那是**前端**的节奏，这一侧只负责"缓存 30s + 被问到才探"。
2. **两种错误类型是有意的**（方案 §5.1）：
   - 入库那一面（:meth:`submit`）抛 ``KnowledgeBaseUnavailable`` —— 它的调用方是
     **HTTP 端点**（笔记「加入知识库」、产物「存进知识库」），既有映射把它折成
     **503 + 那句原因**（``core/exceptions.py``）；抛 ``RuntimeError`` 一族会变成
     500"内部错误"，把"这个部署现在没这个能力"藏起来（R10）；
   - 检索那一面（:meth:`retrieve_sources` / :meth:`document_status`）抛
     ``RemoteUnavailableError`` / ``RemoteRejectedError`` —— 调用方是**工具循环**，
     它按这两档分"连不上"与"被拒"（``tool_loop`` 的失败分档照旧）。

## 地址与凭据：一处权威 + 一处覆盖（方案 §4.2）

- 权威：壳的 ``config.json``（``server`` + ``api_key``）→ 边车 ``--server`` / ``--token``；
- 覆盖：运行期键 ``provider.knowledge.base_url``（设置页可改）→ 引导级 ``kb_url``
  （排障 / 多 NAS）→ 权威。解析是**纯函数** :func:`resolve_provider_target`：
  ``enabled`` 显式关 → 不配；``token`` 同理只有引导级两个来源，
  **不落库、不进日志**（R3）。
- **每次调用现取目标**：设置页改了地址，下一轮/下一次调用立刻生效，不用重启边车。

## 组合根怎么用它（阶段 3 已落地）

``core/services.py::build_services`` 在**本机档**建这一个客户端（服务器档是 ``None``：
知识库就是它自己），然后**一次换线**（方案 §5.1）：

| 接缝 | 拿到的东西 |
| --- | --- |
| ``ChatService(knowledge=…)`` | **本类本身**（检索那一半）|
| ``NotesService(ingest=…, documents=…)`` | :meth:`ingest_gateway` / :meth:`enqueue_gateway` |
| ``ArtifactService(ingest=…, documents=…)`` | 同上（**同一对对象**，不另开一条路）|
| ``Services.ingest`` / ``Services.documents`` | 同上（`ingest_file` 与文件入库那两个端点走它）|

那两个网关的窄视图就是下面两个 Protocol（:class:`IngestGateway` / :class:`EnqueueGateway`）
——``Services`` 上那两个槽位的注解说到底是它们，不是 ``IngestService`` / ``DocumentService``。
**没配/不可用时** ``submit`` 抛 ``KnowledgeBaseUnavailable``（既有的 503 映射）。

## 那条 reader（阶段 4 已接上）

``knowledge_meta()`` 就是 ``RemoteMetaStore`` 要的那个 ``KnowledgeMetaReader``：形状照
方案 §2.3 定死（``get_knowledge_base`` / ``list_knowledge_bases``，**回原始 dict** ——
到 ``KnowledgeBaseRecord`` 的映射在 storage 那一侧），失败一律折成
``KnowledgeBaseUnavailable``（``stores.meta`` 那一面只认这一族：未配 / 连不上 / 被拒 /
5xx 对调用方是同一个答案"现在取不到"）。**阶段 4 起它在组合根后挂进
``stores.meta.kb``**（``core/services.py``），不再是"只提供、不装配"。

## 检索与写为什么不进快照、快照住在哪一层（M4 阶段 2）

M4 给读路径的元数据（库列表 / 库详情）加了一份落本机 SQLite 的快照，**本模块的方法体
一行没改**：快照包在 :meth:`knowledge_meta` 返回的那个 reader **外面**
（``services/kb_cache.CachedKnowledgeMetaReader``）——命中零网络、未命中同步取一次、
远端失败回快照并记一条日志（决策 D-C）。插在这一层的理由还是那一条：``stores.meta``
与 ``services/`` 的调用点一个字不用改（M3 §2.4 的承诺）。

**检索（:meth:`retrieve_sources`）与写（:meth:`submit`）绝不进快照**，
:meth:`document_status` 那样的活数据同样不进——检索决定"这一轮依据了什么"（会进
``sources`` 与 ``[n]`` 引用编号），缓存它等于让回答依据过期资料（v0.3 §6.3 定案）。
判据**只有一份，在 ``services/kb_cache.py``**：``CACHEABLE_RESOURCES``（正向五个资源）、
``NEVER_CACHED``（负向逐条理由）、``PROVIDER_SURFACE``（**本类每个公开方法的分类表**）。
谁给这个类加了一个公开方法而没在那张表里表态，
``tests/unit/services/test_kb_cache.py`` 的分类守卫用例当场红——"顺手缓存一下"这条路
在那一步就撞墙。

**另外三个资源的取数走 :meth:`page_meta`**（M4 阶段 4）：文档列表 / 文档条目 / 库内目录
没有 reader 通路，而 ``/local/kb-cache/*`` 那族端点要的就是"发一次 GET、原样把 JSON
递回去"的那三下。它与 :meth:`knowledge_meta` 同一条钥匙、同一套错误口径，只是读的东西
不同——两个方法各管一个读面（方案 §2.3 的"reader 面"与"页面面"），别把它们合成一个。

## httpx 仍然不进导入闭包（R12）

``httpx`` 的取法复用 ``remote_clients._httpx()``（模块级 import 会把 click + pygments +
rich 拖进客户端的导入闭包，约 5.5 MB）。**用模块属性查而不是 from-import**：那个函数
本身是"用例换掉传输"的接缝（``monkeypatch.setattr(remote_clients, "_httpx", …)``），
from-import 会把名字绑死在导入那一刻，补丁就失效了。判据见 ``scripts/sidecar-closure.py``
（17 包不涨）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.services import remote_clients
from app.services.chat import SourceRef
from app.services.remote_clients import (
    DEFAULT_KB_TIMEOUT,
    RemoteClientError,
    RemoteKnowledgeClient,
    RemoteRejectedError,
    RemoteUnavailableError,
    _auth_headers,
)
from app.storage.base import KnowledgeBaseUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_UPLOAD_TIMEOUT",
    "HANDSHAKE_TIMEOUT_SECONDS",
    "HANDSHAKE_TTL_SECONDS",
    "PROTOCOL_VERSION",
    "SETTING_BASE_URL",
    "SETTING_ENABLED",
    "STATE_READY",
    "STATE_UNAVAILABLE",
    "STATE_UNCONFIGURED",
    "EnqueueGateway",
    "IngestGateway",
    "KnowledgeProviderClient",
    "ProviderStatus",
    "ProviderTarget",
    "resolve_provider_target",
]

PROTOCOL_VERSION = 1
"""本机**认识**的握手协议版本（与 ``api/v1/provider.py`` 的发布值同源，**只增**）。

客户端规则（方案 §1.2 裁量 3）：对方报的版本**大于本机所知即判不可用**，原因句子里
带版本号，**绝不硬试**——把新协议的语义错误当成网络故障是最难查的一类错。
"""

HANDSHAKE_TTL_SECONDS = 30.0
"""握手结论的缓存时长（进程内存，**不落库**——落库是 M4 的元数据缓存）。

30s 是方案 §3.2 定死的值：够短，断连能在半分钟内被发现；够长，切菜单不会每次等一次
NAS 往返（R1）。
"""

HANDSHAKE_TIMEOUT_SECONDS = 3.0
"""探针超时。比检索那 30s 短一个量级：握手回答的是"能不能用"，不是"这次查到什么"，
而它跑在请求线程里（方案 §3.2）。"""

DEFAULT_UPLOAD_TIMEOUT = 20.0
"""上传一份文件给多少秒：给够但别无限等——交付失败要**如实报**。

（M2 那份实现里它叫 ``sidecar.ARTIFACT_UPLOAD_TIMEOUT_SECONDS``；M3 阶段 2 随入库
一起收编到这里，名字跟着用途改：这一侧上传的不只是产物，还有笔记与本机文件。）
"""

STATE_UNCONFIGURED = "unconfigured"
STATE_UNAVAILABLE = "unavailable"
STATE_READY = "ready"
"""三态（方案 §3.1）。

``unconfigured``（没配地址 / 显式关闭）与 ``unavailable``（配了但连不上、凭据错、
版本不认识）是两句不同的话：前者的下一步是"去配一个"，后者是"去看看它怎么了"。
**没有第四态**：`unknown`（还没探过）由前端按"不在"渲染（§3.2），这一侧
``status()`` 一被问到就一定会给出一个真结论。"""

SETTING_ENABLED = "provider.knowledge.enabled"
SETTING_BASE_URL = "provider.knowledge.base_url"
"""两个运行期键（落本机库 ``app_settings``，方案 §4.1）。

**不放进** ``services/runtime_config.SETTING_GROUPS``：那份注册表同时是服务器档
``GET /settings`` 的渲染来源，把"知识库提供者"塞进去会让 NAS 网页端的设置页长出一条
对它毫无意义的配置（§4.1）。本机档由 ``/local/provider`` 自己读写（阶段 5）。
"""

DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
"""``provider.knowledge.enabled`` 的"关"档（认这几种写法，大小写不敏感）。

**默认（没有这个键时）是开**：地址在就接上去（与"没配地址"由地址那一条单独判）。
"""

UNCONFIGURED_REASON = (
    "这台机器还没接知识库提供者：壳里的 server 与 provider.knowledge.base_url 都是空的。"
    "在「知识库连接」里填一个地址（或设 KYLAB_KB_URL），"
    "或者先在界面里登录那台 NAS、把钥匙交给桌面壳。"
)
"""``unconfigured`` 的那句人话 + 下一步（方案 §3.1：两种"不在"各一句）。"""

DISABLED_REASON = (
    "知识库提供者被关掉了（provider.knowledge.enabled=0）。要接回来，在「知识库连接」里打开它。"
)
"""``unconfigured`` 的另一种成因，单独一句（"关掉"与"没填地址"是两件事）。"""


# ---------------------------------------------------------------- 地址与凭据解析


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    """提供者目标：**一个地址 + 一把钥匙**（解析的结果，不是配置本身）。

    ``configured=False`` 时 ``reason`` 非空——它是"为什么不配"的那句人话，
    由 :func:`resolve_provider_target` 给（两种成因各一句）。
    """

    base_url: str
    token: str
    configured: bool
    reason: str = ""

    @property
    def credential(self) -> str:
        """凭据状态（``configured`` / ``missing``）——**只看有没有，不回显**（方案 §3.1）。"""
        return "configured" if self.token else "missing"


def resolve_provider_target(
    settings: Settings, get_setting: Callable[[str], str]
) -> ProviderTarget:
    """地址与凭据的**唯一解析处**（纯函数：只读入参，不打网络、不读写库）。

    规则（方案 §4.1 那段伪码，逐条落在这里）：

    - ``provider.knowledge.enabled`` 显式关（``0`` / ``false`` / ``no`` / ``off``）→
      **不配**（连地址都不再解析：关掉就是关掉，不用去猜它本来会连哪儿）；
    - ``url``：运行期覆盖 → ``Settings.kb_url``（排障 / 多 NAS）→ ``Settings.server_url``
      （**壳里那台 NAS**，也是默认档）；
    - ``token``：``Settings.kb_token`` → ``Settings.token``（**只有引导级两个来源**，
      凭据不落库、不进日志）；
    - 一个地址都没有 → ``configured=False``。

    纯函数的意义：这些分支能被用例逐个钉住（含"设置里关了"与"地址从哪来"四条路），
    而不用先跑起一台 NAS。
    """
    if (get_setting(SETTING_ENABLED) or "").strip().lower() in DISABLED_VALUES:
        return ProviderTarget(
            "", (settings.kb_token or settings.token or "").strip(), False, DISABLED_REASON
        )
    base = (
        (get_setting(SETTING_BASE_URL) or "").strip()
        or (settings.kb_url or "").strip()
        or (settings.server_url or "").strip()
    )
    token = (settings.kb_token or "").strip() or (settings.token or "").strip()
    if not base:
        # 没地址 = 这一档没接提供者：如实回"不可用"，**绝不回退进程内检索**（§3.2）
        return ProviderTarget("", token, False, UNCONFIGURED_REASON)
    return ProviderTarget(base.rstrip("/"), token, True)


# ------------------------------------------------------------------ 状态（握手）


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """一次握手的结论（方案 §3.1 那个响应体的**本机侧形状**）。

    字段与 ``GET /local/provider`` 的响应一一对应（阶段 5 的 ``ProviderStatusOut``
    直接照 :meth:`to_payload` 回，不另建一份模型——两处各建一份迟早漂）。

    ``ready`` 时才有能力集 / 调用者 / 库清单那四段：不 ready 时它们不是"空"，
    而是**根本没有**（回了空对象，界面就得猜"是没探到还是真没有"）。
    """

    state: str
    reason: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    base_url: str = ""
    credential: str = "missing"
    protocol_version: int | None = None
    app_version: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    caller: dict[str, Any] = field(default_factory=dict)
    knowledge_bases: tuple[dict[str, Any], ...] = ()

    @property
    def available(self) -> bool:
        """``state == ready`` 的别名（前端的 ``available`` 那一位，方案 §3.1）。"""
        return self.state == STATE_READY

    def to_payload(self) -> dict[str, Any]:
        """→ 一个可以原样回给前端的 dict（时间转 ISO 串，**不含凭据**）。"""
        payload: dict[str, Any] = {
            "state": self.state,
            "available": self.available,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
            "base_url": self.base_url,
            "credential": self.credential,
        }
        if self.available:
            payload.update(
                {
                    "protocol_version": self.protocol_version,
                    "app_version": self.app_version,
                    "capabilities": self.capabilities,
                    "caller": self.caller,
                    "knowledge_bases": list(self.knowledge_bases),
                }
            )
        return payload


class KnowledgeProviderClient:
    """知识库提供者的客户端（本机档那一侧的**唯一**出口）。

    ``get_setting`` 是"读运行期设置"的入口（生产给 ``RuntimeConfigService.get``，
    即本机库 ``app_settings`` 那一份）；不给 = 这一档没有运行期设置（用例与排障）。
    ``settings`` 是引导级配置（壳 / 环境变量）；不给就用全局单例。
    ``transport`` 是给用例注入假传输用的（``httpx.MockTransport``，测试里绝不打真网络）。

    线程安全：``status()`` 会写缓存，最坏情况是两个线程同时探一次（都不改状态）。
    这一层刻意**不加锁**——握手是只读探测，多探一次的代价远小于一把锁（R1 要的是
    "别拖慢交互路径"，不是"绝不重复探测"）。
    """

    def __init__(
        self,
        *,
        get_setting: Callable[[str], str] | None = None,
        settings: Settings | None = None,
        transport: Any = None,
        ttl: float = HANDSHAKE_TTL_SECONDS,
        handshake_timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
        read_timeout: float = DEFAULT_KB_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._get_setting = get_setting or (lambda key: "")
        self._transport = transport
        self._ttl = ttl
        self._handshake_timeout = handshake_timeout
        self._upload_timeout = upload_timeout
        self._read_timeout = read_timeout
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._status: ProviderStatus | None = None
        self._checked: float = 0.0

    # -------------------------------------------------------------- 地址与状态

    def target(self) -> ProviderTarget:
        """这次调用该打哪儿、用哪把钥匙（**每次现取**：设置改了立刻生效）。"""
        return resolve_provider_target(self._settings, self._get_setting)

    def status(self, refresh: bool = False) -> ProviderStatus:
        """握手的结论（缓存 **30s**，进程内存；``refresh=True`` 强制重探）。

        什么时候探（方案 §3.2）：**不在启动时挡路** —— 首次被问到才探，之后 TTL 内
        直接回缓存（`ready` 时不轮询；`state != ready` 的每 30s 重探是**前端**的节奏）。

        结论的三档：``unconfigured`` / ``unavailable`` / ``ready``。**探针绝不改别人
        的状态**：一次检索超时不代表提供者没了（那会让导航抖），所以这条路上只有
        "握手本身"的结论算数（见模块头的两条口径）。
        """
        now = self._clock()
        if not refresh and self._status is not None and now - self._checked < self._ttl:
            return self._status
        status = self._probe()
        self._status = status
        self._checked = now
        return status

    # ------------------------------------------------------------------ 检索

    def retrieve_sources(
        self,
        *,
        query: str,
        kb_ids: list[str],
        top_k: int | None = None,
        candidate_k: int = 40,
        reader: object | None = None,
    ) -> list[SourceRef]:
        """检索出处（签名与 ``KnowledgeClient`` 协议**逐字一致**，
        ``services/knowledge_client.py:45-53``）——换实现时调用点一个字不用改。

        实现委托给 ``RemoteKnowledgeClient``（**检索那一件的唯一实现**，本类持有它；
        映射沿用 P2 那份：hits → 带编号的 ``SourceRef``）。失败**抛**而不是回空
        （"不可用"与"没命中"分得开），而且**不改握手状态**。

        ``reader`` 是进程内实现用来跨查询复用块缓存的；远端没有这个概念，保留参数只是
        为了签名逐字一致（``RemoteKnowledgeClient`` 的同名参数也是这个理由）。
        """
        target = self._target_or_raise("检索")
        return RemoteKnowledgeClient(
            target.base_url,
            token=target.token,
            timeout=self._read_timeout,
            transport=self._transport,
        ).retrieve_sources(
            query=query, kb_ids=kb_ids, top_k=top_k, candidate_k=candidate_k, reader=reader
        )

    # ------------------------------------------------------------------ 入库

    def submit(
        self,
        *,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        document_id: str | None = None,
        uploaded_by: str | None = None,
        folder_id: str | None = None,
    ) -> Any:
        """把一份字节送进知识库（``POST /knowledge-bases/{kb_id}/documents``，multipart）。

        签名**逐字对齐** ``IngestService.submit``（``services/ingest.py:170-179``，含
        ``mime_type`` / ``folder_id``）：笔记（``text/markdown``）与产物那两条路是照那个
        签名调的，少一个就是 ``TypeError``（M2 那份 ``sidecar._LocalIngest`` 的真形态）。

        与 ``IngestService`` 的差异逐条写清（都是"远端做不到"，不是"忘了"）：

        - ``document_id``：**NAS 不由我们指定新文档的 id**，所以它当 ``Idempotency-Key``
          头用（同一个键重试不产生第二份文档）——**回给你的 id 是 NAS 那一侧的**，
          与本机 ``create_document`` 那个"指定 id"不是同一件事；
        - ``uploaded_by`` 走 ``X-Kylab-Operator`` 头：归属标注，**不参与鉴权**（G6 同一口径）；
        - ``start=true``：让 NAS **自己入队**摄取（所以本机侧的 ``enqueue_ingest`` 是空操作，
          见 :meth:`enqueue_gateway`）；
        - ``folder_id`` 非空时随查询串带上（目录必须属于同一个库，那是 NAS 那边的校验）。

        返回 ``SimpleNamespace(document=…, is_duplicate=…)``：调用方读的三个字段
        （``outcome.document.id`` / ``.name`` / ``outcome.is_duplicate``）同形。

        失败**一律折成** ``KnowledgeBaseUnavailable``：这一面的调用方是 HTTP 端点，
        既有映射把它折成 503 + 那句原因（R10）——"未配"与"不可用"对用户是同一句
        "现在用不了知识库"，而两者的**原因**都原样留在句子里。
        """
        target = self._target_or_raise("入库", storage_face=True)
        headers: dict[str, str] = {}
        if uploaded_by:
            headers["X-Kylab-Operator"] = uploaded_by
        if document_id:
            headers["Idempotency-Key"] = document_id
        params: dict[str, str] = {"start": "true"}
        if folder_id:
            params["folder_id"] = folder_id
        try:
            response = self._send(
                "入库",
                target=target,
                method="POST",
                path=f"/knowledge-bases/{knowledge_base_id}/documents",
                timeout=self._upload_timeout,
                params=params,
                # ``mime_type`` 为 None 时**不写死一个值**：让 httpx 按文件名猜
                # （与浏览器上传同一形态，服务器读的是 ``file.content_type`` 那一栏）。
                # 写死 ``application/octet-stream`` 会把"我们知道它是什么"变成"不知道"，
                # 解析路由凭后缀 / MIME / 内容三样一起判，少一样就少一条线索。
                files={"file": (filename, content, mime_type)},
                headers=headers,
            )
            payload = self._read_json(response, what="入库", target=target)
        except RemoteClientError as exc:
            # 这一面（HTTP 端点）与 reader（`stores.meta`）共用同一句：对用户来说
            # "未配 / 连不上 / 被拒 / 5xx"都是"知识库现在用不了"，原因原样跟在括号里。
            raise _unavailable(exc) from exc
        document = payload.get("document") or {}
        new_id = str(document.get("id") or "")
        if not new_id:
            # 回包认得出来但少了 id：**如实报**，别造一个假的（调用方要拿它回填笔记/产物）
            raise KnowledgeBaseUnavailable(
                f"知识库提供者入库后没回文档 id（{target.base_url}）：{str(payload)[:200]}"
            )
        return SimpleNamespace(
            document=SimpleNamespace(id=new_id, name=str(document.get("name") or filename)),
            is_duplicate=bool(payload.get("is_duplicate")),
        )

    def ingest_gateway(self) -> _IngestGateway:
        """``IngestService.submit`` 那一面的**窄视图**（组合根把它放到 ``Services.ingest``）。

        为什么是一个视图而不是 ``self``：服务器档那个位置是整个 ``IngestService``
        （``replace`` / ``retry`` / 内部仓储都露着），而本机档这个位置只该有"提交一份
        字节"这一件事——多露一个方法就多一条"看起来能调、其实没人接"的路。
        """
        return _IngestGateway(self)

    def enqueue_gateway(self) -> _EnqueueGateway:
        """``documents.enqueue_ingest`` 那一面的窄视图：**空操作**（见那个类的说明）。"""
        return _EnqueueGateway()

    # ------------------------------------------------------- 进度 / 元数据（读）

    def document_status(self, document_id: str) -> dict[str, Any]:
        """入库进度：文档现状 + 阶段时间线（``GET /documents/{id}`` ＋ ``/timeline``）。

        返回 ``{"document": {...}, "timeline": {...}}``——**NAS 的两段原样**，不在这层
        重新建模：M3 只有设置面板与将来那个 ``get_document_status`` 工具会读它
        （方案 §5.2 明确本阶段不给本机 agent 摆那个工具，本机也不做轮询 UI）。

        失败走**工具面那一档**（``RemoteUnavailableError`` / ``RemoteRejectedError``）：
        与检索同源——调用方是循环/工具，不是 HTTP 端点。文档不存在（404）**照实抛出**，
        由调用方说"那篇文档不在这个提供者上了"（R5：不自动清理、不静默置空）。
        """
        target = self._target_or_raise("查文档进度")
        return {
            "document": self._get_json(target, f"/documents/{document_id}", what="查文档进度"),
            "timeline": self._get_json(
                target, f"/documents/{document_id}/timeline", what="查文档进度"
            ),
        }

    def knowledge_meta(self) -> _KnowledgeMetaReader:
        """给 ``RemoteMetaStore`` 用的 reader（**阶段 4 起由组合根后挂进 `stores.meta.kb`**）。

        reader 的形状照方案 §2.3 的 ``KnowledgeMetaReader`` 协议**逐字**（阶段 4 的
        ``RemoteMetaStore`` 直接拿它当参数，不需要在这里再定义一份 Protocol）：

        - ``get_knowledge_base(kb_id) -> dict | None``：``GET /knowledge-bases/{kb_id}``，
          **404 → None**（契约：``None`` = 没有这个库；**取不到**是另一个答案，见下）；
        - ``list_knowledge_bases() -> list[dict]``：``GET /knowledge-bases`` 的 ``items``。

        两个方法都回**原始 dict**：``KnowledgeBaseRecord`` 是 storage 那一侧的类型，
        映射只该发生在 ``RemoteMetaStore`` 里（storage 认得那个类型，services 这侧
        只把 NAS 的原样 JSON 递过去——多一层转手就多一处会漂的形状）。

        **取不到时一律抛 ``KnowledgeBaseUnavailable``**（未配 / 关掉 / 连不上 / 被拒 /
        5xx）：reader 这一面就是 ``stores.meta``，而那一面只认这一族错误——"现在取不到"
        对调用方是一个答案，原因留在句子里；分档（``RemoteUnavailableError`` 那两档）
        是工具循环那一面的事（见模块头"两种错误类型是有意的"）。把连不上伪装成回一个
        空清单或 ``None``，只会让调用方把"没连上"读成"库里没有"。
        """
        return _KnowledgeMetaReader(self)

    def page_meta(self) -> _PageMetaReader:
        """**页面面**那三个读取（M4 阶段 4）：文档列表 / 文档条目 / 库内目录。

        与 :meth:`knowledge_meta` 是方案 §2.3 分的那**两个读面**：那一个给
        ``RemoteMetaStore``（每轮每库读一次库详情），这一个给 ``/local/kb-cache/*``
        那族端点——快照的另外三个资源（``doc_list`` / ``document`` / ``folders``）
        没有 reader 通路，取数就得从这一层出（端点拿到的是"整取一次"的闭包）。

        三条读取**都是发一次 GET**（快照那一层要的就是这个形状）：整取 → 算哈希 →
        相同只推 ``checked_at``。回来的都是 NAS 的**原始 JSON**，不在这层重新建模。

        **筛选参数刻意不在签名里**（没有 ``q`` / ``stage`` / ``source_kind``）：
        快照只认规范视图（决策 D-D，机械形态是签名本身，见
        ``services/kb_cache.doc_list_scope_key``）——搜索结果是"这一问的答案"，
        过期即误导，它不该有副本。

        只读、不写、不碰进度：进度与时间线走 :meth:`document_status`（活数据，
        §1.2 明确不进快照）；库与目录的写操作页面直连 NAS（本机侧不映射，见模块头那张表）。
        """
        return _PageMetaReader(self)

    # ------------------------------------------------------------------ 内部

    def _target_or_raise(self, what: str, *, storage_face: bool = False) -> ProviderTarget:
        """拿目标；没配就按面分档抛（两种错误类型是有意的，见模块头）。"""
        target = self.target()
        if target.configured:
            return target
        reason = f"{what}不了：{target.reason}"
        if storage_face:
            raise KnowledgeBaseUnavailable(reason)
        raise RemoteUnavailableError(reason)

    def _probe(self) -> ProviderStatus:
        """真探一次（``status()`` 的缓存之外那一半）。

        **探针自己绝不抛**：它认不出的异常也算"不可用"（原因里带类型名，全文进日志）。
        理由很实在——这一条的调用方是工具表、状态页与（阶段 5 的）``/local/provider``，
        它们**没有一个**该因为一次探测崩掉；而"崩掉"的表现会是一条 500 或一份拿不到
        工具表的对话，比"这一档显示不可用"糟得多。
        """
        target = self.target()
        checked = self._now()
        if not target.configured:
            return ProviderStatus(
                state=STATE_UNCONFIGURED,
                reason=target.reason,
                checked_at=checked,
                base_url=target.base_url,
                credential=target.credential,
            )

        def unavailable(reason: str) -> ProviderStatus:
            return ProviderStatus(
                state=STATE_UNAVAILABLE,
                reason=reason,
                checked_at=checked,
                base_url=target.base_url,
                credential=target.credential,
            )

        try:
            payload = self._handshake(target)
        except RemoteClientError as exc:
            # 两档：被拒（凭据 / 地址）与不可用（连不上 / 5xx）的**句子由 _handshake 给**
            # （那里知道状态码），这里只补一句下一步。
            return unavailable(f"{exc}。下一步：核对「知识库连接」里的地址与凭据。")
        except Exception as exc:  # 探针绝不抛，理由见本方法说明
            logger.warning("握手探测没能跑完（当不可用处理）", exc_info=True)
            return unavailable(f"握手没跑成（{type(exc).__name__}）：{exc}")

        version = payload.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return unavailable(
                f"握手响应里没有协议版本（{target.base_url}）："
                "对面回的不是知识库提供者的握手体，地址可能指到了别的服务。"
            )
        if version > PROTOCOL_VERSION:
            # **绝不硬试**：新协议的语义可能完全不同，猜错了会被当成网络故障
            return unavailable(
                f"知识库提供者的协议版本是 {version}，本机只认识到 {PROTOCOL_VERSION}"
                "（M3 的值）——请升级桌面端 / 本机后端，不要用旧客户端去猜新协议。"
            )
        return ProviderStatus(
            state=STATE_READY,
            reason="",
            checked_at=checked,
            base_url=target.base_url,
            credential=target.credential,
            protocol_version=version,
            app_version=str(payload.get("app_version") or ""),
            capabilities=_as_dict(payload.get("capabilities")),
            caller=_as_dict(payload.get("caller")),
            knowledge_bases=tuple(
                item for item in payload.get("knowledge_bases") or [] if isinstance(item, dict)
            ),
        )

    def _handshake(self, target: ProviderTarget) -> dict[str, Any]:
        """这一次握手（状态码分档在这里，因为"凭据"与"地址"是两句话）。"""
        response = self._send(
            "握手",
            target=target,
            method="GET",
            path="/provider/handshake",
            timeout=self._handshake_timeout,
        )
        code = response.status_code
        if code in (401, 403):
            raise RemoteRejectedError(
                f"知识库提供者拒绝了这把凭据（HTTP {code}）：{_body(response)}"
            )
        if code == 404:
            raise RemoteRejectedError(
                f"这个地址上没有握手端点（HTTP 404）：{target.base_url}/provider/handshake"
            )
        payload = self._read_json(response, what="握手", target=target)
        return payload or {}

    def _send(
        self,
        what: str,
        *,
        target: ProviderTarget,
        method: str,
        path: str,
        timeout: float,
        params: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str | None]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发一次请求。**连不上 / 超时在这里就抛**——那是"不可用"，与状态码是两件事。

        ``httpx`` 的取法见模块头（复用 ``remote_clients._httpx()`` 那个接缝）。
        """
        httpx = remote_clients._httpx()
        try:
            with httpx.Client(
                base_url=target.base_url,
                timeout=timeout,
                transport=self._transport,
                headers=_auth_headers(target.token),
            ) as client:
                return client.request(method, path, params=params, files=files, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(
                f"{what}：连不上知识库提供者（{target.base_url}）：{type(exc).__name__}: {exc}"
            ) from exc

    def _get_json(self, target: ProviderTarget, path: str, *, what: str) -> dict[str, Any]:
        """GET 一段 JSON（用读超时）。失败按两档抛（4xx 被拒 / 5xx 不可用）。"""
        response = self._send(
            what, target=target, method="GET", path=path, timeout=self._read_timeout
        )
        return self._read_json(response, what=what, target=target)

    def _read_json(
        self,
        response: Any,
        *,
        what: str,
        target: ProviderTarget,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        """状态码 → 分档 → 解析 JSON。

        ``allow_missing=True``（只有"读一个库"那一条用）时 **404 → None**：
        ``None`` 是那个 reader 契约里"没有这个库"的答案，不是"出错了"。
        """
        code = response.status_code
        if code == 404 and allow_missing:
            return None
        if code >= 500:
            raise RemoteUnavailableError(
                f"{what}：知识库提供者出错了（HTTP {code}）：{_body(response)}"
            )
        if code >= 400:
            raise RemoteRejectedError(
                f"{what}：知识库提供者拒绝了请求（HTTP {code}）：{_body(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteUnavailableError(
                f"{what}：知识库提供者回的不是 JSON（HTTP {code}，{target.base_url}）"
            ) from exc
        return payload if isinstance(payload, dict) else {}


class IngestGateway(Protocol):
    """``IngestService.submit`` 那一面的**窄视图**（组合根把服务图里三个入库口都指向它）。

    为什么是一个 Protocol 而不是直接用 ``IngestService`` 当注解：本机档那个位置**只有**
    这一件事（提交一份字节），而服务器档那个位置是整个 ``IngestService``（``replace`` /
    ``retry`` / 内部仓储都露着）。``Services.ingest`` 的注解写成后者就是一句谎
    ——本机档的调用方按它去调 ``replace`` 会当场 ``AttributeError``，而那本该在
    **读代码**的时候就看得出来。

    :class:`app.services.ingest.IngestService` 也**结构上满足**它（签名逐字一致），
    所以服务器档那份不用改一个字就落在同一张网里。
    """

    def submit(
        self,
        *,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        document_id: str | None = None,
        uploaded_by: str | None = None,
        folder_id: str | None = None,
    ) -> Any: ...


class EnqueueGateway(Protocol):
    """``DocumentService.enqueue_ingest`` 那一面的**窄视图**。

    本机档是 :class:`_EnqueueGateway`（空操作：上传口带 ``start=true``，NAS 那边自己
    入队了）；服务器档是真 ``DocumentService``。
    """

    def enqueue_ingest(self, document_id: str) -> Any: ...


class _IngestGateway:
    """``IngestService.submit`` 的窄视图（组合根点 ``provider.ingest_gateway()`` 拿它）。"""

    def __init__(self, client: KnowledgeProviderClient) -> None:
        self._client = client

    def submit(
        self,
        *,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        document_id: str | None = None,
        uploaded_by: str | None = None,
        folder_id: str | None = None,
    ) -> Any:
        """与 ``IngestService.submit`` **同形**（说明见 ``KnowledgeProviderClient.submit``）。"""
        return self._client.submit(
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            content=content,
            mime_type=mime_type,
            document_id=document_id,
            uploaded_by=uploaded_by,
            folder_id=folder_id,
        )


class _EnqueueGateway:
    """``documents.enqueue_ingest`` 的**空操作**实现。

    为什么能空：服务器那个上传口带 ``start=true`` 时**自己已经入队**（``api/v1/documents.py``
    的 ``_do_upload`` 里那处 ``enqueue_ingest``）——本机再喊一次就是**重复入队**
    （把同一份文档的摄取任务重排一遍）。而本机档**没有队列**可管，连"假装排过一次"
    都不该做。

    （这段理由原来挂在 ``sidecar._UploadedDocuments`` 上，M3 阶段 2 随入库一起收编到
    这里——它说的是提供者客户端的性质，不是边车的性质。）
    """

    def enqueue_ingest(self, document_id: str) -> Any:
        """接受入参、**什么都不做**：队已经在那一边排上了（id 回 ``None`` 即无新任务）。"""
        return SimpleNamespace(id=None)


class _KnowledgeMetaReader:
    """``knowledge_meta()`` 返回的那个对象：**只声明方案 §2.3 要的两件事**。

    刻意不实现别的：这份 reader 是给 ``RemoteMetaStore`` 用的，多一个方法就多一处
    "storage 层以为自己能调、其实没人接"的地方（M4 的缓存层插在它和 HTTP 之间，
    也只需要这两个方法）。

    **失败的口径是"storage 那一面"**（见 :meth:`KnowledgeProviderClient.knowledge_meta`）：
    取不到一律 ``KnowledgeBaseUnavailable``，只有"远端说没有这个库"才是 ``None``。
    """

    def __init__(self, client: KnowledgeProviderClient) -> None:
        self._client = client

    def get_knowledge_base(self, kb_id: str) -> dict[str, Any] | None:
        """这个库的元数据；**404 → ``None``**（没有这个库），取不到则抛。

        ``_send`` / ``_read_json`` 那两档（连不上 / 5xx / 被拒）在**这一层**折成
        ``KnowledgeBaseUnavailable``——调用它的 ``RemoteMetaStore`` 在 storage 层，
        它认得的只有这一族错误（那句原因原样带过去）。
        """
        target = self._client._target_or_raise("读知识库元数据", storage_face=True)
        try:
            response = self._client._send(
                "读知识库元数据",
                target=target,
                method="GET",
                path=f"/knowledge-bases/{kb_id}",
                timeout=self._client._read_timeout,
            )
            return self._client._read_json(
                response, what="读知识库元数据", target=target, allow_missing=True
            )
        except RemoteClientError as exc:
            raise _unavailable(exc) from exc

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """这次调用看得见的全部库（受限 key 只看到范围内的：过滤在 NAS 那一侧）。"""
        target = self._client._target_or_raise("列知识库", storage_face=True)
        try:
            payload = self._client._get_json(target, "/knowledge-bases", what="列知识库")
        except RemoteClientError as exc:
            raise _unavailable(exc) from exc
        items = payload.get("items") or []
        return [item for item in items if isinstance(item, dict)]


class _PageMetaReader:
    """``page_meta()`` 返回的那个对象：**只声明页面面要的那三件事**（M4 阶段 4）。

    与 ``_KnowledgeMetaReader`` **同一套口径**（回 NAS 的原始 dict、``None`` = 远端明确说
    没有这个东西、取不到抛 ``KnowledgeBaseUnavailable``），只是读的东西不同：那三个
    没有 reader 通路的资源——文档列表 / 文档条目 / 库内目录。

    刻意不实现别的：快照面要什么、这一层就有什么。多一个方法就多一条"看起来能调、
    其实没人接"的路，而这一层唯一要保证的是"发一次 GET、原样把 JSON 递回去"。
    """

    def __init__(self, client: KnowledgeProviderClient) -> None:
        self._client = client

    def list_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        """这个库的文档列表（``GET /knowledge-bases/{kb_id}/documents``）。

        ``folder_id`` 与 ``root`` 互斥（照 NAS 那条端点）：两个都不给 = 整个库。
        分页照 NAS 的 ``limit`` / ``offset``；快照那一面的 ``page`` / ``size`` 是同一件事
        的另一种说法（``offset = (page - 1) * size``，换算在调用方，见
        ``services/kb_cache.doc_list_scope_key``）。
        """
        params = {"limit": str(limit), "offset": str(offset)}
        if folder_id:
            params["folder_id"] = folder_id
        elif root:
            params["root"] = "true"
        return self._read(f"/knowledge-bases/{kb_id}/documents", what="读文档列表", params=params)

    def document(self, document_id: str) -> dict[str, Any] | None:
        """一篇文档的条目信息（``GET /documents/{document_id}``）——详情页 / 抽屉的入口帧。

        **不是进度**：进度与时间线走 :meth:`KnowledgeProviderClient.document_status`
        （活数据，冻结的进度条是最糟的假象，§1.1）。
        """
        return self._read(f"/documents/{document_id}", what="读文档条目")

    def folders(self, kb_id: str) -> dict[str, Any] | None:
        """这个库的目录树（``GET /knowledge-bases/{kb_id}/folders``）。"""
        return self._read(f"/knowledge-bases/{kb_id}/folders", what="读库内目录")

    def _read(
        self, path: str, *, what: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """发一次 GET、回一份原始 JSON（这一层只做这一件事）。

        ``404 → None``（"没有这个东西"是答案，与"取不到"是两件事）；其余失败折成
        ``KnowledgeBaseUnavailable`` —— 与 ``_KnowledgeMetaReader`` 那两个方法逐字
        同一套处置（那两处照旧不动：M4 只加这一族，不重写已验收的那条线）。
        """
        client = self._client
        target = client._target_or_raise(what, storage_face=True)
        try:
            response = client._send(
                what,
                target=target,
                method="GET",
                path=path,
                timeout=client._read_timeout,
                params=params,
            )
            return client._read_json(response, what=what, target=target, allow_missing=True)
        except RemoteClientError as exc:
            raise _unavailable(exc) from exc


def _unavailable(exc: RemoteClientError) -> KnowledgeBaseUnavailable:
    """远端失败 → ``stores.meta`` / HTTP 端点那一面那句话（**唯一一处**，两处调用共用）。

    "未配 / 连不上 / 被拒 / 5xx"对用户是同一个答案："知识库现在用不了"——而原因
    （哪一档、HTTP 多少、服务端那句话）原样跟在括号里，排障要的信息一个字不少。
    """
    return KnowledgeBaseUnavailable(f"知识库提供者现在用不了（{exc}）")


def _as_dict(value: object) -> dict[str, Any]:
    """只手认 dict：对面加了字段/换了形状时保留原样，不在这里发明结构。"""
    return dict(value) if isinstance(value, dict) else {}


def _body(response: Any) -> str:
    """错误响应体的前 200 字（够看出服务端那句话，又不至于把日志灌满）。"""
    return str(getattr(response, "text", "") or "")[:200]
