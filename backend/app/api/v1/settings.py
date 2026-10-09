"""设置端点（凭据与模型）：读写运行期配置 + 连接测试。

设计口径见《界面信息架构草案》§3：

- **读取**只给掩码与"是否已配置"，永不回显明文；
- **写入**留空表示不改动密钥，显式清空才删除；
- **测试连接**是"编辑动作的一部分"，所以结果就返回给弹窗，不另开一层页面。

测试连接刻意做得很轻：只验证"凭据能不能用、模型在不在、维度对不对"，
不做一次真实解析或索引——否则用户点一下"测试"就消耗掉一次配额。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_admin
from app.api.v1.schemas import (
    SettingsPatchIn,
    SettingsPatchOut,
    SettingsViewOut,
    TestConnectionOut,
)
from app.core.caller import Caller
from app.core.lazy_httpx import httpx
from app.core.services import Services, get_services
from app.services.runtime_config import SECRET_KEYS, SETTING_GROUPS

router = APIRouter(tags=["settings"])

_PROBE_TEXT = "kylab 连接测试"
_TEST_TIMEOUT_SECONDS = 30.0

#: 「还没配嵌入模型」时给的下一步动作。**双份并存期**：权威在 KB 侧的
#: `services/embedding/__init__.py::NOT_CONFIGURED_HINT`（那里还用它抛
#: `EmbeddingNotConfiguredError`），这里是**复制**而不是上移——这句话讲的是向量化配置，
#: 而向量化随 KB 走；剥离阶段 0 的刀 3 只是让本模块不再 import KB 域模块。
#: KB 面收口（设置页那段向量化连接测试随 KB 走去）时，这份拷贝与这段注释一起删。
_NOT_CONFIGURED_HINT = (
    "未配置嵌入模型：请在「设置 → 模型注册」添加供应商并登记向量化模型，"
    "再到「设置 → 向量化」把它选为默认嵌入模型"
)


def _known_setting_keys() -> frozenset[str]:
    """可写设置项的**唯一来源**：设置页渲染用的 ``SETTING_GROUPS``。

    此前这里另抄了一份硬编码清单。v0.8 把模型身份从设置页收进注册表之后，
    抄的那份没跟着删——于是 `llm.api_key` / `embedding.base_url` 这类键仍被接受、
    回"已保存"，而运行时根本不读它们（**静默无效，最难查的一类问题**）。
    从 ``SETTING_GROUPS`` 派生，两边就不可能再分叉。
    """
    return frozenset(
        str(field["key"]) for group in SETTING_GROUPS.values() for field in group["fields"]
    )


@router.get("/settings", response_model=SettingsViewOut, summary="运行期配置（密钥打码）")
def read_settings(
    services: Services = Depends(get_services),
    _: Caller = Depends(require_admin),
) -> SettingsViewOut:
    view = services.runtime.describe()
    return SettingsViewOut.model_validate(
        {
            "groups": view["groups"],
            "embedding_model_id": services.kb.embedder.model_id,
            "embedding_dim": services.kb.embedder.dim,
            "embedding_configured": services.runtime.embedding().is_configured,
            "embedding_is_development": services.kb.embedder.is_development,
            "rerank_enabled": services.kb.reranker.enabled,
        }
    )


@router.patch("/settings", response_model=SettingsPatchOut, summary="更新运行期配置")
def update_settings(
    payload: SettingsPatchIn,
    services: Services = Depends(get_services),
    _: Caller = Depends(require_admin),
) -> SettingsPatchOut:
    values = {item.key: item.value for item in payload.values}
    unknown = [key for key in values if key not in _KNOWN_KEYS]
    if unknown:
        # 拒绝未知键而不是静默忽略：拼错一个键却"保存成功"是最难查的一类问题
        return SettingsPatchOut(updated=0, rejected=unknown)

    services.runtime.set(values)
    return SettingsPatchOut(updated=len(values), rejected=[])


@router.post(
    "/settings/test/{target}",
    response_model=TestConnectionOut,
    summary="连通性测试（embedding / mineru / paddleocr）",
)
def test_connection(
    target: str,
    services: Services = Depends(get_services),
    _: Caller = Depends(require_admin),
) -> TestConnectionOut:
    if target == "embedding":
        return _test_embedding(services)
    if target == "mineru":
        return _test_mineru(services)
    if target == "paddleocr":
        return _test_paddleocr(services)
    if target == "llm":
        return _test_llm(services)
    return TestConnectionOut(ok=False, detail=f"不支持的目标：{target}")


# --------------------------------------------------------------------- 各目标


def _test_embedding(services: Services) -> TestConnectionOut:
    """真发一次最小请求：维度声明错了必须在这里暴露，而不是等摄入时炸。"""
    config = services.runtime.embedding()
    if not config.is_configured:
        return TestConnectionOut(ok=False, detail=_NOT_CONFIGURED_HINT)

    try:
        vectors = services.kb.embedder.embed([_PROBE_TEXT])
    except Exception as exc:
        # 第三方错误文案要原样给用户看：401、额度不足、维度不符的处理方式完全不同
        return TestConnectionOut(ok=False, detail=str(exc))

    if not vectors:
        return TestConnectionOut(ok=False, detail="端点返回了空结果")
    actual = len(vectors[0])
    if actual != config.dim:
        return TestConnectionOut(
            ok=False,
            detail=(
                f"模型实际输出 {actual} 维，设置里写的是 {config.dim} 维；维度不符会污染向量空间"
            ),
        )
    return TestConnectionOut(
        ok=True,
        detail=f"{config.model_id} 可用，维度 {actual}",
    )


def _test_mineru(services: Services) -> TestConnectionOut:
    """只验鉴权：故意发一个不存在的 batch_id，401 说明 token 有问题，其他都算通过。"""
    config = services.runtime.mineru()
    if not config.is_configured:
        return TestConnectionOut(ok=False, detail="尚未配置 API Token")

    url = f"{config.endpoint}/extract-results/batch/kylab-connection-test"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=_TEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return TestConnectionOut(ok=False, detail=f"无法连接 MinerU：{exc}")

    if response.status_code == 401:
        return TestConnectionOut(ok=False, detail="Token 无效或已过期（90 天不可续期）")
    return TestConnectionOut(ok=True, detail="Token 可用（未消耗解析额度）")


def _test_paddleocr(services: Services) -> TestConnectionOut:
    """同样只验鉴权：查询一个不存在的 job，401/403 才算失败。"""
    config = services.runtime.paddleocr()
    if not config.is_configured:
        return TestConnectionOut(ok=False, detail="尚未配置 API Token")

    try:
        response = httpx.get(
            f"{config.endpoint}/kylab-connection-test",
            headers={"Authorization": f"bearer {config.token}"},
            timeout=_TEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return TestConnectionOut(ok=False, detail=f"无法连接 PaddleOCR：{exc}")

    if response.status_code in (401, 403):
        return TestConnectionOut(ok=False, detail="Token 无效或已过期")
    return TestConnectionOut(ok=True, detail="Token 可用（未消耗解析额度）")


def _test_llm(services: Services) -> TestConnectionOut:
    """真发一次最小对话：这是唯一能验证"模型会不会说话"的办法。

    只看鉴权是不够的——推理模型在 max_tokens 不够时 `content` 会是空的，
    那正是最需要被测出来的坑（实测过）。
    """
    config = services.runtime.llm()
    if not config.is_configured:
        return TestConnectionOut(
            ok=False,
            detail=(
                "未选定对话模型：请在「设置 → 模型注册」登记对话模型，"
                "再到「设置 → 对话模型」把它选为默认"
            ),
        )

    try:
        answer = services.chat.probe()
    except Exception as exc:  # 第三方错误文案要原样给用户看
        return TestConnectionOut(ok=False, detail=str(exc))

    if not answer.strip():
        return TestConnectionOut(
            ok=False,
            detail="模型返回了空内容：若是推理模型，请关闭「深度思考」或换一个非推理模型",
        )
    return TestConnectionOut(ok=True, detail=f"{config.model_id} 可用（回复：{answer[:20]}）")


_KNOWN_KEYS = _known_setting_keys()

# 密钥键必须都在已知键里，否则设置页"保存成功"但值写不进去
_MISSING_SECRET_KEYS = SECRET_KEYS - _KNOWN_KEYS
if _MISSING_SECRET_KEYS:  # pragma: no cover - 配置写错时立刻炸，而不是线上才发现
    raise RuntimeError(f"以下密钥键未登记：{sorted(_MISSING_SECRET_KEYS)}")
