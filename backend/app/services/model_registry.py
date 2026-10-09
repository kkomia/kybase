"""模型注册器（调研报告 G1）。

**为什么做这件事**：原先的模型配置是"全局各一套凭据"——设置页里
`embedding.base_url`、`llm.api_key` 各一份。够用，但有三处会立刻卡住实际使用：

1. **同一个供应商下要用多个模型**：便宜的做向量化、贵的做对话，
   而 `llm.model_id` 只能填一个；
2. **换模型要覆盖旧凭据**：想临时切到另一家对比效果，回来时得重新填一遍 key；
3. **看不出"这条模型是干什么用的"**：`dim`、能力（对话/向量/重排）散落在
   几组互不相干的输入框里，没有任何地方能一眼看全。

成熟产品（Dify / RAGFlow / FastGPT / MaxKB / Anything-LLM / Open WebUI，**6/6**）
都把这件事做成独立子系统：**供应商 → 模型目录 → 按用途选定**。本模块就是这三层。

**与既有配置的关系（关键设计）**：注册器是**叠加**层，不是替换。
- 某个用途**没有绑定**到注册表里的模型时，一切照旧走 `.env` / 设置页那套；
- 绑定了就以注册表为准。
这样升级不会打断已有部署，用户也能一个一个用途慢慢迁过来。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from app.core.exceptions import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UpstreamError,
)
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.services.secrets import SecretStore, model_provider_target, use_keychain
from app.storage.base import ModelProviderRecord, RegisteredModelRecord, StoreBundle

__all__ = ["SLOTS", "ModelRegistryService"]

logger = logging.getLogger(__name__)

#: 探活超时。设置页里点一下"测试"，用户盯着按钮等——不能让它挂在那里。
_PROBE_TIMEOUT_SECONDS = 10.0

#: 用途（任务槽位）。**这是这套体系的核心概念**：
#: 用户想的不是"我要配 embedding.base_url"，而是"向量化用哪个模型"。
#: 键同时也是 ``app_settings`` 里的槽位绑定键（见 ``BINDING_PREFIX``）。
SLOTS: dict[str, dict[str, str]] = {
    "chat": {"label": "对话生成", "capability": "chat"},
    "embedding": {"label": "向量化", "capability": "embedding"},
    "rerank": {"label": "重排（可选）", "capability": "rerank"},
}

BINDING_PREFIX = "registry.slot."

#: 供应商类别。与槽位不是一对一：一家供应商可以同时提供对话与向量化
#: （例如自建的 OpenAI 兼容网关），所以这里只描述"它是什么类型的服务"。
PROVIDER_KINDS: dict[str, str] = {
    "llm": "对话模型服务",
    "embedding": "向量化服务",
    "rerank": "重排服务",
    "parser": "文档解析服务",
}

CAPABILITIES: dict[str, str] = {
    "chat": "对话",
    "embedding": "向量化",
    "rerank": "重排",
}


class ModelRegistryService:
    """供应商与模型目录，以及"哪个用途用哪个模型"的绑定。

    **凭据的家**（M5 阶段 6，方案 §4.2）：供应商那把 API Key 搬进了系统钥匙串
    （``kylab:model_provider:<id>``），所以这一层要在**读出记录之后把它填回**、
    **写入时只落钥匙串**。对调用方完全透明：``ModelProviderRecord.api_key`` 这个字段
    与 `/model-registry` 那两个响应字段（``api_key_configured`` / ``api_key_hint``）
    **一个字节都没变**——变的只是"那个值从哪儿来"。

    ``secrets`` 不传 / 钥匙串不可用（服务器容器 / Linux 桌面 / CI）时一切照旧：
    库那一列就是凭据的家（§4.5 的边界 + R14"服务器档库里那份凭据不动"）。
    """

    def __init__(self, stores: StoreBundle, *, secrets: SecretStore | None = None) -> None:
        self._stores = stores
        self._secrets = secrets
        #: 凭据是不是改走钥匙串（判据只有一处：``secrets.use_keychain``）。
        #: 建对象时定一次——组合根建的是进程级单例，而每次读记录都去探一遍钥匙串
        #: 只会给热路径（``resolve`` 在每建一次 LLM 客户端的路上）平白加一次系统调用。
        self._keychain = use_keychain(secrets)

    # ------------------------------------------------------------------ 凭据（钥匙串那一侧）

    def _with_key(self, record: ModelProviderRecord) -> ModelProviderRecord:
        """把记录里的 ``api_key`` 补成**真正生效的那一把**（钥匙串档上问钥匙串）。

        读出来的地方全都过这一层：`get_provider` / `list_providers` / `resolve` /
        两个写方法的返回值。少过一处，那一处的调用方就会看到"没配"——而它明明配着。
        """
        if not self._keychain or self._secrets is None:
            return record
        return replace(record, api_key=self._secrets.get(model_provider_target(record.id)) or "")

    def _for_db(self, record: ModelProviderRecord) -> ModelProviderRecord:
        """落库前的那一份：钥匙串档上**列里永远是空**（明文一个字节都不进库）。"""
        if not self._keychain:
            return record
        return replace(record, api_key="")

    def _store_key(self, provider_id: str, value: str) -> None:
        """把一把钥匙写进钥匙串（钥匙串档上的唯一写入路径）。

        写不进去时钥匙串自己抛 ``SecretStoreUnavailable``（端点 503）——**退回库里写明文
        是不允许的**（那正是要收编掉的东西）。这时调用它的那个写方法会整条失败，
        用户看到一句"钥匙串不可用"而不是"保存成功"。
        """
        if not self._keychain or self._secrets is None:
            return
        self._secrets.set(model_provider_target(provider_id), value)

    def _clear_key(self, provider_id: str) -> None:
        """删掉钥匙串里那一条（删供应商 / 清空凭据时；本来就没有也算成功）。"""
        if not self._keychain or self._secrets is None:
            return
        self._secrets.delete(model_provider_target(provider_id))

    # ------------------------------------------------------------------ 供应商

    def create_provider(
        self,
        *,
        kind: str,
        name: str,
        base_url: str = "",
        api_key: str = "",
        enabled: bool = True,
    ) -> ModelProviderRecord:
        if kind not in PROVIDER_KINDS:
            raise InvalidRequestError(
                f"未知的供应商类别：{kind}（可选：{'、'.join(PROVIDER_KINDS)}）"
            )
        cleaned = name.strip()
        if not cleaned:
            raise InvalidRequestError("供应商名称不能为空")
        provider_id = f"prov_{uuid.uuid4().hex[:12]}"
        stored = self._stores.meta.create_model_provider(
            ModelProviderRecord(
                id=provider_id,
                kind=kind,
                name=cleaned,
                base_url=base_url.strip(),
                # 钥匙串档上列里是空：明文只落钥匙串（先建档、再存钥匙——见下面那行）
                api_key="" if self._keychain else api_key.strip(),
                enabled=enabled,
            )
        )
        # 顺序是"先建档、再存钥匙"：钥匙串写失败时 provider 已经存在（UI 上看到"没配"），
        # 用户重填一次就走 PATCH 那条路——比"钥匙存进去了但供应商没建成"好收拾
        # （后者会在钥匙串里留一条谁也不认领的秘密）。
        if cleaned_key := api_key.strip():
            self._store_key(provider_id, cleaned_key)
        return self._with_key(stored)

    def get_provider(self, provider_id: str) -> ModelProviderRecord:
        record = self._stores.meta.get_model_provider(provider_id)
        if record is None:
            raise NotFoundError(f"供应商不存在：{provider_id}")
        return self._with_key(record)

    def embedding_target(self, model_pk: str) -> tuple[ModelProviderRecord, RegisteredModelRecord]:
        """取一个可直接用于嵌入的（供应商, 模型）。

        **校验放在这一处**：建库时选模型、运行时按 pk 解析，两处都要"能用"这个判断，
        分散写会漂。空 ``capabilities`` 视为"没声明"，与绑定用途时的宽容口径一致
        （旧数据不拦）；声明了就必须包含 ``embedding``。
        """
        model = self.get_model(model_pk)
        if model.capabilities and "embedding" not in model.capabilities:
            raise InvalidRequestError(
                f"模型「{model.label or model.model_id}」没有声明 embedding 能力，不能作为嵌入模型"
            )
        if not model.dim:
            raise InvalidRequestError(
                f"模型「{model.label or model.model_id}」没有登记向量维度（dim），无法用于嵌入"
            )
        provider = self.get_provider(model.provider_id)
        if not provider.enabled:
            raise InvalidRequestError(f"供应商「{provider.name}」已停用")
        if not provider.api_key.strip():
            raise InvalidRequestError(f"供应商「{provider.name}」还没有填写 API Key")
        return provider, model

    def chat_target(self, model_pk: str) -> tuple[ModelProviderRecord, RegisteredModelRecord]:
        """取一个可直接用于对话的（供应商, 模型）。

        与 ``embedding_target`` 同一理由，校验集中在一处：会话级选模型（v12）与运行时
        按 pk 解析都要"这个模型能不能真用来对话"这个判断，分散写迟早会漂。
        空 ``capabilities`` 视为"没声明"（旧数据不拦），声明了就必须包含 ``chat``。
        """
        model = self.get_model(model_pk)
        if model.capabilities and "chat" not in model.capabilities:
            raise InvalidRequestError(
                f"模型「{model.label or model.model_id}」没有声明对话能力，不能用于对话"
            )
        provider = self.get_provider(model.provider_id)
        if not provider.enabled:
            raise InvalidRequestError(f"供应商「{provider.name}」已停用")
        if not provider.api_key.strip():
            raise InvalidRequestError(f"供应商「{provider.name}」还没有填写 API Key")
        return provider, model

    def probe_provider(self, provider_id: str) -> str:
        """探活一家供应商：**用它的地址与凭据请求 ``GET {base_url}/models``**。

        为什么选这一步（第二轮评审批注 4 把"测试"从用途行移到供应商注册环节）：
        - ``/models`` 是 OpenAI 兼容端点的标准能力，**不消耗 token、不计费**，
          却同时验了"地址对不对""凭据有没有效"这两件注册时最会填错的事；
        - 不需要模型 ID：供应商刚注册时模型还没登记，"先登记一个模型才能测"
          会把测试推后到用户已经填错很久之后；
        - 真要验"这个模型会不会说话"仍然应该按用途测（``/slots/{slot}/test``）——
          那是另一件事，两者不互相替代。
        """
        provider = self.get_provider(provider_id)
        response = self._request_models(provider)
        entries = self._model_entries(response)
        if entries is None:
            return f"「{provider.name}」地址与凭据可用"
        return f"「{provider.name}」可用（发现 {len(entries)} 个模型）"

    def list_available_models(self, provider_id: str) -> list[dict[str, str]]:
        """上游 ``GET {base_url}/models`` 列出的模型，供"添加模型"时挑选。

        与 ``probe_provider`` 共用同一条请求路径：探活回答"通不通"，
        这里回答"有哪些"。**不落库**——上游动辄几十上百条，全登记只是噪声，
        "选哪一个"才是用户的决定。

        上游不给列表（有的端点只回 HTML、或格式不同）时返回空列表而**不报错**：
        界面照常允许手写模型 ID，这条路不能因为探测不了就被堵死。
        """
        provider = self.get_provider(provider_id)
        response = self._request_models(provider)
        entries = self._model_entries(response) or []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            out.append({"model_id": model_id, "owned_by": str(item.get("owned_by") or "")})
        return out

    def _request_models(self, provider: ModelProviderRecord) -> httpx.Response:
        """校验地址与凭据，请求 ``GET {base_url}/models``，并把状态码翻成我们的错误。

        探活与"拉列表"共用这一步：两处各写一份的话，鉴权失败的处理迟早会漂
        （一处说"API Key 无效"、另一处说"HTTP 401"），而用户看到的是同一件事。
        """
        if not provider.base_url.strip():
            raise InvalidRequestError(f"「{provider.name}」还没有填写接口地址")
        if not provider.api_key.strip():
            raise InvalidRequestError(f"「{provider.name}」还没有填写 API Key")

        url = provider.base_url.rstrip("/") + "/models"
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"无法连接「{provider.name}」：{exc}") from exc

        if response.status_code in (401, 403):
            raise InvalidRequestError("API Key 无效，或没有访问权限")
        if response.status_code >= 400:
            raise UpstreamError(f"「{provider.name}」返回 HTTP {response.status_code}")
        return response

    @staticmethod
    def _model_entries(response: httpx.Response) -> list[object] | None:
        """尽力解析 ``{"data": [...]}``；解析不出来返回 ``None``（**不算失败**）。"""
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return None

    def list_providers(self) -> list[ModelProviderRecord]:
        return [self._with_key(record) for record in self._stores.meta.list_model_providers()]

    def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
    ) -> ModelProviderRecord:
        """更新供应商。

        ``api_key`` 传 ``None`` 表示**保持原值**，传空串表示**清空**。
        这个区分是必要的：设置页把密钥掩码显示成 ``••••``，用户不动它时
        前端回传的是掩码而不是真实密钥——若把掩码当新值写进去，密钥就被毁了。
        所以"没改"与"清空"必须是两个不同的输入。

        钥匙串档上那把钥匙落在钥匙串（``kylab:model_provider:<id>``），DB 列**永远为空**：
        - 非 ``None``、非空 → 写钥匙串；
        - 空串 → 删掉钥匙串里那一条（"清空凭据"这个明确动作）；
        - 含 ``…`` 的值当"没改"跳过（那看着就是界面回传的掩码——与
          ``RuntimeConfigService.set`` 同一条防漂）。
        """
        record = self.get_provider(provider_id)
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise InvalidRequestError("供应商名称不能为空")
            record.name = cleaned
        if base_url is not None:
            record.base_url = base_url.strip()
        if api_key is not None:
            cleaned_key = api_key.strip()
            if "…" in cleaned_key:
                pass  # 掩码：当"没改"（防漂，理由见 docstring）
            elif cleaned_key:
                self._store_key(provider_id, cleaned_key)
                record.api_key = cleaned_key
            else:
                self._clear_key(provider_id)
                record.api_key = ""
        if enabled is not None:
            record.enabled = enabled
        self._stores.meta.update_model_provider(self._for_db(record))
        return self._with_key(record)

    def delete_provider(self, provider_id: str) -> None:
        """删供应商**连同它下面的模型**，并**解绑引用它们的槽位**。

        解绑这一步容易漏：模型没了但槽位还指着它，之后每次检索都报
        "绑定的模型不存在"——而用户在设置页看到的是一个灰色下拉，很难联想到
        是自己删了供应商造成的。

        钥匙串里那把钥匙**先删**（删供应商这条路上最该确定的事就是"凭据不再留着"；
        反过来先删行的话，一次失败会在钥匙串里留一条谁也认不领的秘密）。
        """
        self.get_provider(provider_id)
        self._clear_key(provider_id)
        for model in self._stores.meta.list_registered_models(provider_id):
            self._unbind_model(model.id)
        self._stores.meta.delete_model_provider(provider_id)

    # ------------------------------------------------------------------ 模型

    def register_model(
        self,
        *,
        provider_id: str,
        model_id: str,
        label: str = "",
        dim: int | None = None,
        capabilities: list[str] | None = None,
        options: dict[str, object] | None = None,
    ) -> RegisteredModelRecord:
        self.get_provider(provider_id)  # 供应商不存在就别登记
        cleaned = model_id.strip()
        if not cleaned:
            raise InvalidRequestError("模型 ID 不能为空")

        caps = tuple(capabilities or ())
        unknown = [item for item in caps if item not in CAPABILITIES]
        if unknown:
            raise InvalidRequestError(
                f"未知的能力标记：{'、'.join(unknown)}（可选：{'、'.join(CAPABILITIES)}）"
            )

        return self._stores.meta.create_registered_model(
            RegisteredModelRecord(
                id=f"mdl_{uuid.uuid4().hex[:12]}",
                provider_id=provider_id,
                model_id=cleaned,
                label=label.strip(),
                dim=dim,
                capabilities=caps,
                options=dict(options or {}),
            )
        )

    def get_model(self, model_pk: str) -> RegisteredModelRecord:
        record = self._stores.meta.get_registered_model(model_pk)
        if record is None:
            raise NotFoundError(f"模型不存在：{model_pk}")
        return record

    def list_models(self, provider_id: str | None = None) -> list[RegisteredModelRecord]:
        return self._stores.meta.list_registered_models(provider_id)

    def update_model(
        self,
        model_pk: str,
        *,
        model_id: str | None = None,
        label: str | None = None,
        dim: int | None = None,
        capabilities: list[str] | None = None,
        options: dict[str, object] | None = None,
    ) -> RegisteredModelRecord:
        record = self.get_model(model_pk)
        if model_id is not None:
            cleaned = model_id.strip()
            if not cleaned:
                raise InvalidRequestError("模型 ID 不能为空")
            record.model_id = cleaned
        if label is not None:
            record.label = label.strip()
        if dim is not None:
            record.dim = dim
        if capabilities is not None:
            unknown = [item for item in capabilities if item not in CAPABILITIES]
            if unknown:
                raise InvalidRequestError(f"未知的能力标记：{'、'.join(unknown)}")
            record.capabilities = tuple(capabilities)
        if options is not None:
            record.options = dict(options)
        try:
            self._stores.meta.update_registered_model(record)
        except ConflictError:
            raise
        return record

    def delete_model(self, model_pk: str) -> None:
        """删模型并**解绑引用了它的槽位**（理由同删供应商）。"""
        self.get_model(model_pk)
        self._unbind_model(model_pk)
        self._stores.meta.delete_registered_model(model_pk)

    # ------------------------------------------------------------------ 槽位绑定

    def bindings(self) -> dict[str, str]:
        """全部槽位绑定：``{槽位: 模型主键}``。未绑定的槽位不出现。"""
        out: dict[str, str] = {}
        for slot in SLOTS:
            value = self._stores.meta.get_setting(f"{BINDING_PREFIX}{slot}")
            if value:
                out[slot] = value
        return out

    def bind(self, slot: str, model_pk: str | None) -> None:
        """把某个用途绑定到某个模型；``None`` 表示解绑（回到走设置页那套）。"""
        if slot not in SLOTS:
            raise InvalidRequestError(f"未知的用途：{slot}（可选：{'、'.join(SLOTS)}）")
        if model_pk is None:
            self._stores.meta.delete_setting(f"{BINDING_PREFIX}{slot}")
            logger.info("用途 %s 已解绑，回退到设置页的配置", slot)
            return

        model = self.get_model(model_pk)
        capability = SLOTS[slot]["capability"]
        if model.capabilities and capability not in model.capabilities:
            # 只在该模型**声明了**能力时才校验：旧数据或手工登记可能留空
            raise InvalidRequestError(
                f"模型 {model.model_id} 未声明「{CAPABILITIES[capability]}」能力，"
                f"不能用于{SLOTS[slot]['label']}"
            )
        provider = self.get_provider(model.provider_id)
        if not provider.enabled:
            # 绑一个禁用的供应商不是错误，但必须说清楚——否则用户会以为
            # "绑定成功了却还是报未配置"
            logger.warning("模型 %s 的供应商 %s 处于禁用状态", model.model_id, provider.name)
        self._stores.meta.set_setting(f"{BINDING_PREFIX}{slot}", model_pk)
        logger.info("用途 %s 已绑定到 %s", slot, model.model_id)

    # ------------------------------------------------------------------ 解析

    def resolve(self, slot: str) -> tuple[ModelProviderRecord, RegisteredModelRecord] | None:
        """取某个用途实际生效的（供应商, 模型）。未绑定返回 ``None``。

        **未绑定不是错误**：调用方据此回退到 ``.env`` / 设置页那套配置。
        这正是"叠加层"的落点——升级不会让已有部署失效。

        实现上是**一条 JOIN**（``resolve_model_binding``）而不是三次查询：这个方法在
        每建一次 LLM 客户端的路径上，而一轮对话里每个工具步都要建一次——实测
        20ms（三条查询）→ 7ms（一条）。也没在这里加缓存：注册表改动是低频动作，
        而"改完读到旧值"这类窗口不值得为几毫秒去换。
        """
        if slot not in SLOTS:
            raise InvalidRequestError(f"未知的用途：{slot}")
        found = self._stores.meta.resolve_model_binding(f"{BINDING_PREFIX}{slot}")
        if found is not None:
            provider, model = found
            # 凭据也要补上：这条路在"每建一次 LLM 客户端"的热路径上，
            # 而 runtime_config 那两份快照（embedding / llm）就是从这里拿 api_key 的
            return self._with_key(provider), model
        # 解不出来：要么本来就没绑，要么绑的模型/供应商已经不在了。
        # 后者要留一条线索（前面那两种"不该发生，因为删的时候会解绑"的情形）。
        bound = self._stores.meta.get_setting(f"{BINDING_PREFIX}{slot}")
        if bound:
            logger.warning(
                "用途 %s 绑定的模型 %s 已不存在（或它的供应商已不存在），回退到设置页配置",
                slot,
                bound,
            )
        return None

    # ------------------------------------------------------------------ 内部

    def _unbind_model(self, model_pk: str) -> None:
        for slot, bound in self.bindings().items():
            if bound == model_pk:
                self._stores.meta.delete_setting(f"{BINDING_PREFIX}{slot}")
                logger.info("用途 %s 因模型被删而解绑", slot)
