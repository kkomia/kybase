"""模型注册器端点（调研报告 G1）。

三层：**供应商 → 模型目录 → 按用途绑定**。成熟产品（6/6）都把这件事做成
独立子系统，而不是一组散落的环境变量。

**为什么要有 `/registry` 总览端点**：设置页一打开就需要"供应商 + 模型 + 用途状态"
三份数据。分三个请求既慢又会在慢网络下出现"供应商已经画出来了、模型还在转圈"
的半成品界面。一次给全，前端一次渲染。

**密钥纪律**：与设置页完全一致——只回"配没配"和一个掩码尾巴，
**绝不回密钥本身**。所以这里的响应可以安全地出现在日志、截图、前端缓存里。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from starlette.concurrency import run_in_threadpool

from app.api.auth import require_admin, require_read
from app.api.v1.schemas import (
    AvailableModelOut,
    AvailableModelsOut,
    ModelListOut,
    ModelOut,
    ModelRegisterIn,
    ModelUpdateIn,
    PresetModelOut,
    ProviderCreateIn,
    ProviderListOut,
    ProviderOut,
    ProviderPresetOut,
    ProviderUpdateIn,
    RegistryOut,
    SlotBindIn,
    SlotOut,
)
from app.core.caller import Caller
from app.core.exceptions import InvalidRequestError, UpstreamError
from app.core.services import Services, get_services
from app.services.llm import ChatMessage
from app.services.model_registry import CAPABILITIES, PROVIDER_KINDS, SLOTS
from app.services.provider_presets import PROVIDER_PRESETS
from app.services.runtime_config import mask_secret

router = APIRouter(prefix="/model-registry", tags=["model-registry"])


# --------------------------------------------------------------------- 组装


def _provider_out(services: Services, record) -> ProviderOut:  # type: ignore[no-untyped-def]
    return ProviderOut(
        id=record.id,
        kind=record.kind,
        name=record.name,
        base_url=record.base_url,
        enabled=record.enabled,
        created_at=record.created_at,
        updated_at=record.updated_at,
        api_key_configured=bool(record.api_key),
        api_key_hint=mask_secret(record.api_key) if record.api_key else "",
        model_count=len(services.models.list_models(record.id)),
    )


def _model_out(services: Services, record, bindings: dict[str, str]) -> ModelOut:
    provider = services.models.list_providers()
    owner = next((item for item in provider if item.id == record.provider_id), None)
    return ModelOut(
        id=record.id,
        provider_id=record.provider_id,
        provider_name=owner.name if owner else "",
        provider_kind=owner.kind if owner else "",
        model_id=record.model_id,
        label=record.label,
        dim=record.dim,
        capabilities=list(record.capabilities),
        options=dict(record.options),
        created_at=record.created_at,
        updated_at=record.updated_at,
        bound_slots=[slot for slot, pk in bindings.items() if pk == record.id],
    )


def _slot_out(services: Services, slot: str, bindings: dict[str, str]) -> SlotOut:
    spec = SLOTS[slot]
    bound_pk = bindings.get(slot)
    bound_label = ""
    provider_name = ""
    if bound_pk:
        try:
            model = services.models.get_model(bound_pk)
            owner = services.models.get_provider(model.provider_id)
            bound_label = model.label or model.model_id
            provider_name = owner.name
        except Exception:
            # 绑定指向已不存在的记录（不该发生，删除时会解绑）。
            # 这里不抛：总览接口整页失败，比少显示一个名字糟得多
            bound_label = "（记录已丢失）"

    # 最终是否可用：只看注册表里有没有绑定（v0.8 起设置页不再提供模型身份）
    registry_ready = bool(bound_pk)
    return SlotOut(
        slot=slot,
        label=spec["label"],
        capability=spec["capability"],
        bound_model_pk=bound_pk,
        bound_model_label=bound_label,
        provider_name=provider_name,
        configured=registry_ready,
        source="registry" if registry_ready else "none",
    )


# --------------------------------------------------------------------- 总览


@router.get("", response_model=RegistryOut, summary="注册器总览（供应商 + 模型 + 用途）")
def get_registry(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_read)],
) -> RegistryOut:
    bindings = services.models.bindings()
    providers = services.models.list_providers()
    models = services.models.list_models()
    return RegistryOut(
        providers=[_provider_out(services, item) for item in providers],
        models=[_model_out(services, item, bindings) for item in models],
        slots=[_slot_out(services, slot, bindings) for slot in SLOTS],
        provider_kinds=dict(PROVIDER_KINDS),
        capabilities=dict(CAPABILITIES),
        provider_presets=[
            ProviderPresetOut(
                id=preset.id,
                label=preset.label,
                kind=preset.kind,
                base_url=preset.base_url,
                hint=preset.hint,
                models=[
                    PresetModelOut(
                        model_id=item.model_id,
                        label=item.label,
                        capabilities=list(item.capabilities),
                        dim=item.dim,
                    )
                    for item in preset.models
                ],
            )
            for preset in PROVIDER_PRESETS
        ],
    )


# --------------------------------------------------------------------- 供应商


@router.get("/providers", response_model=ProviderListOut, summary="供应商列表")
def list_providers(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_read)],
) -> ProviderListOut:
    return ProviderListOut(
        items=[_provider_out(services, item) for item in services.models.list_providers()]
    )


@router.post(
    "/providers",
    response_model=ProviderOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建供应商",
)
def create_provider(
    payload: ProviderCreateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ProviderOut:
    """**只认管理员**：这里要写入明文密钥，属于凭据管理。

    与 `/settings`、`/api-keys` 同一档——普通 API Key 不该能读写凭据，
    否则一把泄露的读写密钥就能把所有人的模型指向别处。
    """
    record = services.models.create_provider(
        kind=payload.kind,
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    return _provider_out(services, record)


@router.patch("/providers/{provider_id}", response_model=ProviderOut, summary="修改供应商")
def update_provider(
    provider_id: str,
    payload: ProviderUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ProviderOut:
    record = services.models.update_provider(
        provider_id,
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    return _provider_out(services, record)


@router.delete(
    "/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除供应商（连同其模型）",
)
def delete_provider(
    provider_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    services.models.delete_provider(provider_id)


# --------------------------------------------------------------------- 模型


@router.get("/models", response_model=ModelListOut, summary="模型列表")
def list_models(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_read)],
) -> ModelListOut:
    bindings = services.models.bindings()
    return ModelListOut(
        items=[_model_out(services, item, bindings) for item in services.models.list_models()]
    )


@router.post(
    "/models",
    response_model=ModelOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记模型",
)
def register_model(
    payload: ModelRegisterIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ModelOut:
    record = services.models.register_model(
        provider_id=payload.provider_id,
        model_id=payload.model_id,
        label=payload.label,
        dim=payload.dim,
        capabilities=payload.capabilities,
        options=payload.options,
    )
    return _model_out(services, record, services.models.bindings())


@router.patch("/models/{model_pk}", response_model=ModelOut, summary="修改模型")
def update_model(
    model_pk: str,
    payload: ModelUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ModelOut:
    record = services.models.update_model(
        model_pk,
        model_id=payload.model_id,
        label=payload.label,
        dim=payload.dim,
        capabilities=payload.capabilities,
        options=payload.options,
    )
    return _model_out(services, record, services.models.bindings())


@router.delete(
    "/models/{model_pk}", status_code=status.HTTP_204_NO_CONTENT, summary="删除模型"
)
def delete_model(
    model_pk: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    services.models.delete_model(model_pk)


# --------------------------------------------------------------------- 用途绑定


@router.get("/slots", response_model=list[SlotOut], summary="用途（任务槽位）状态")
def list_slots(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_read)],
) -> list[SlotOut]:
    bindings = services.models.bindings()
    return [_slot_out(services, slot, bindings) for slot in SLOTS]


@router.put("/slots/{slot}", response_model=SlotOut, summary="绑定 / 解绑用途")
def bind_slot(
    slot: str,
    payload: SlotBindIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> SlotOut:
    """把某个用途绑定到某个模型（``model_pk`` 传 null 表示解绑）。

    用 ``PUT``：这是幂等的状态设置，重复绑定同一个模型结果一样。
    """
    services.models.bind(slot, payload.model_pk)
    return _slot_out(services, slot, services.models.bindings())


# --------------------------------------------------------------------- 验活


@router.post("/providers/{provider_id}/test", summary="测试供应商的地址与凭据是否可用")
async def test_provider(
    provider_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> dict[str, object]:
    """注册环节的验活：请求一次 ``GET {base_url}/models``，**不计费**。

    与 ``/slots/{slot}/test`` 的分工：这里验"地址与凭据对不对"（注册时最会填错的两件事），
    那里验"这个用途的模型会不会真的答话"（可能产生费用）。两者不互相替代。

    在**线程池**里跑：``httpx`` 是同步调用，直接放事件循环里会卡住整个服务。
    """
    detail = await run_in_threadpool(services.models.probe_provider, provider_id)
    return {"ok": True, "detail": detail}


@router.get(
    "/providers/{provider_id}/available-models",
    response_model=AvailableModelsOut,
    summary="拉取供应商可用的模型列表（探测，不落库）",
)
async def list_available_models(
    provider_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> AvailableModelsOut:
    """上游 ``GET {base_url}/models`` 的结果，喂给「添加模型」时的下拉框。

    **不落库**：上游列出的模型往往几十上百个，全登记进来只是噪声；
    用户"选哪一个"才是决定。手写输入仍然保留——有的端点不给列表，
    或者想要的模型不在列表里。

    只认管理员：它会用凭据请求上游（凭据探测面），与 ``/test`` 同一档。
    在**线程池**里跑，理由同 ``/test``（同步 httpx 会卡住事件循环）。
    """
    models = await run_in_threadpool(services.models.list_available_models, provider_id)
    return AvailableModelsOut(
        models=[AvailableModelOut(**item) for item in models],
        count=len(models),
    )


@router.post("/slots/{slot}/test", summary="测试该用途的模型是否可用")
async def test_slot(
    slot: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> dict[str, object]:
    """真调一次模型，回答"这条路通不通"。

    **只认管理员**：它会把凭据用于一次真实请求（可能产生费用），
    也等于让调用方间接验证"这把 key 有效"——那是凭据探测面。

    在**线程池**里跑：客户端是同步的，直接在事件循环里调会把整个服务卡住
    （对话接口就是这么被拖慢过）。
    """
    if slot not in SLOTS:
        raise InvalidRequestError(f"未知的用途：{slot}")
    if not services.models.bindings().get(slot):
        raise InvalidRequestError(
            f"{SLOTS[slot]['label']}尚未选定模型：请先在「模型注册」里登记，再选定为默认"
        )

    try:
        if slot == "chat":
            detail = await run_in_threadpool(_test_chat, services)
        elif slot == "embedding":
            detail = await run_in_threadpool(_test_embedding, services)
        else:
            detail = await run_in_threadpool(_test_rerank, services)
    except InvalidRequestError:
        raise
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(f"{SLOTS[slot]['label']}不可用：{exc}") from exc

    return {"ok": True, "detail": detail}


def _test_chat(services: Services) -> str:
    # 必须传 ChatMessage 而不是 dict：``complete`` 的契约是 ``Sequence[ChatMessage]``，
    # 给它 dict 会在 provider 内部炸成 "'dict' object has no attribute 'role'"
    # ——一个与"密钥对不对"毫不相干的报错，会让人以为模型配错了（踩过）
    snapshot = services.runtime.llm()
    client = services.chat._chat_factory(snapshot)
    reply = client.complete([ChatMessage(role="user", content="只回复两个字：可用")])
    return f"{snapshot.model_id} 可用（回复：{reply.strip()[:20]}）"


def _test_embedding(services: Services) -> str:
    snapshot = services.runtime.embedding()
    vectors = services.kb.embedder.embed(["连通性测试"])
    return f"{snapshot.model_id} 可用（返回 {len(vectors[0])} 维向量）"


def _test_rerank(services: Services) -> str:
    snapshot = services.runtime.rerank()
    if not snapshot.is_configured:
        raise InvalidRequestError("重排尚未配置")
    # 用两个词条真调一次：重排的失败常常是"接口能连但请求体格式不被接受"，
    # 只探连通性会漏掉这一类
    services.kb.reranker.rerank(query="眼轴长度", documents=["眼轴测量", "近视防控"], top_n=1)
    return f"{snapshot.model_id} 可用"
