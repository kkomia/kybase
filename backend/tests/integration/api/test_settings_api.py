"""设置端点（PATCH /settings）的可写键契约。

**为什么值得单测**：可写键的白名单必须与"运行时真正会读的键"完全一致。
这里曾经硬编码过一份副本，v0.8 把模型身份收进注册表后副本没跟着删——
于是 `llm.api_key` / `embedding.base_url` 这类键仍被接受、回"已保存"，
而运行时根本不读，属于**静默无效**。现在白名单从 `SETTING_GROUPS` 派生。

## 为什么这一份现在打**本机档**（NAS 网页端退役，2026-10-05）

`/settings` 只在**本机档**存在（服务器档那张表里没有它）：运行期配置表
`app_settings` 在本机那份 SQLite 里（见 `api/v1/router.py` 的 `local_router` 那一段），
NAS 上的设置页随网页端一起退役。所以这一份用 `local_client`（不登录 ——
本机档不设门禁，调用主体由 `api/auth.py::current_caller` 短路成"本机主人"）。

**摘掉的两条**（都是"只对服务器档成立"的断言，本机档没有这个语义）：

- ``test_member_cannot_write_settings``：靠 `/users` + `/auth/login` 造成员、验成员写设置
  403。本机档**不挂账号体系**（`users` / `auth` 三张表都不在本机库里，见 router.py
  里"明确不挂"那一段），主体恒为"本机主人"，没有第二个身份可以扮演；而服务器档的
  `/settings` 已经退役，没有装机形态能承接这条断言。**设置端点的门槛本身没放松**：
  `api/v1/settings.py` 上的 `WriteDep` 一个字没改，只是它现在只在"没有第二个身份的
  那一个档"里被挂出来。
- ``test_settings_view_is_admin_only``：同上（`/settings` 的管理员门槛在本机档无从验证）。

纯代码的那一条（设置页字段与可写键同源）与档位无关，留在原处。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.local


def test_behavior_settings_can_be_written(local_client: TestClient) -> None:
    """行为参数（批大小、温度、提示词…）仍可写。"""
    response = local_client.patch(
        "/api/v1/settings",
        json={"values": [{"key": "embedding.batch_size", "value": "16"}]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 1, "rejected": []}


def test_model_identity_keys_are_rejected(local_client: TestClient) -> None:
    """模型身份键必须被**拒绝并回执**，而不是"保存成功"。

    这些键在 v0.8 之后由模型注册表承担；再接受就等于给一条写不动任何东西的假入口。
    返回 `rejected` 而不是静默忽略，是为了让拼错/过期键当场可见。
    """
    stale = [
        "llm.api_key",
        "llm.base_url",
        "llm.model_id",
        "embedding.api_key",
        "embedding.base_url",
        "embedding.model_id",
        "embedding.dim",
        "rerank.base_url",
    ]
    response = local_client.patch(
        "/api/v1/settings",
        json={"values": [{"key": key, "value": "x"} for key in stale]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == 0
    assert sorted(body["rejected"]) == sorted(stale)


def test_unknown_key_is_reported_not_silently_ignored(local_client: TestClient) -> None:
    response = local_client.patch(
        "/api/v1/settings", json={"values": [{"key": "nope.nope", "value": "1"}]}
    )

    assert response.json() == {"updated": 0, "rejected": ["nope.nope"]}


def test_settings_page_and_writable_keys_come_from_one_source() -> None:
    """设置页渲染的分组字段 = 可写键集合（两边同源，不可能漂）。"""
    from app.api.v1.settings import _KNOWN_KEYS
    from app.services.runtime_config import SETTING_GROUPS

    declared = {
        str(field["key"]) for group in SETTING_GROUPS.values() for field in group["fields"]
    }
    assert declared == _KNOWN_KEYS


def test_settings_reports_whether_embedding_is_configured(local_client: TestClient) -> None:
    """设置页要能区分"没配"与"配了"：前端据此决定建库入口能不能点。

    （**从 `test_rest_api.py` 搬来的**，判据一个字没改：它打的是 ``/settings``，
    而那一族随 NAS 网页端退役只在本机档了。）
    """
    body = local_client.get("/api/v1/settings").json()

    # 测试环境显式开着开发兜底，因此没绑定注册模型 → 未配置
    assert body["embedding_configured"] is False
    assert body["embedding_is_development"] is True
    # 模型身份不在设置页分组里了（v0.8）：那里只剩行为参数。
    # `embedding.protocol` 也是行为参数（"这个端点说哪套协议"），所以它在这一组里；
    # 地址 / 密钥 / 模型名 / 维度仍然只在注册表里，一个都不许漏到这一层。
    embedding_group = next(g for g in body["groups"] if g["key"] == "embedding")
    assert [f["key"] for f in embedding_group["fields"]] == [
        "embedding.batch_size",
        "embedding.protocol",
    ]
