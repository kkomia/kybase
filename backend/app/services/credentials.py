"""旧明文收编：把库里那几处凭据搬进系统钥匙串（M5 阶段 6，方案 §4.2 / §4.3）。

**v0.1.0 瘦身之后这个服务在 NAS 部署里是"空转"的**：系统钥匙串只存在于桌面机器上，
而这个进程只有服务器形态（`NullSecretStore`，R14）——所以 :meth:`status` 如实报
``store: unavailable``、``pending_migration: 0``，:meth:`migrate` 则抛
``SecretStoreUnavailable``（绝不退回库里写明文）。它跟着共享底座留下，
是因为钥匙串那一整套读写（含 :meth:`nas_token` 三个方法）本身没有业务语义。

## 搬哪几处、不搬哪几处（方案 §4.2 那张表）

**收编这一处**（读路径也跟着改道了，见 ``model_registry``）：

- ``model_providers.api_key`` → ``kylab:model_provider:<id>``
  （模型注册表在读；迁完**清列**，行本身留着）。

原先还有 ``app_settings['web.search_api_key']`` 那一处——联网搜索随 Agent 侧代码
拆除了，那个键不复存在（见 `runtime_config.KEYCHAIN_SETTING_KEYS`：现在是空集）。

**只登记、不迁**（这里一行代码都没有——留在库里是**有意的**，不是漏了）：
``mineru.token`` / ``paddleocr.token`` 与服务器引导级（PG 口令 / S3 密钥）都不进钥匙串，
它们的家就是库与 ``.env``。

这份清单写在这里，是为了让下一个人不必重新判一遍"哪些该收、哪些不该收"。

## 四条纪律

1. **逐项幂等**：钥匙串里已经有同一个值 → 跳过（顺手把库里那份重复的清掉）；
   **库里已经空了 → 那一项根本不出现**（"没什么可迁"不是"跳过了一项"）；
2. **先写钥匙串、后清库**：顺序反过来的话，一次崩在中间就永久丢了一把钥匙
   （而"库里还有明文"至少是可重跑的状态）；
3. **失败不清明文**（§4.3-5）：那一条进 ``failed``、库里那份原样留着，
   下次可以重跑——"不清就不算迁完"；
4. **清页**：迁移完在源库上收一次空闲页（见 :meth:`CredentialsService._shrink`）。

## 过渡态可观测（§4.2 末段）

**不静默迁移**：改用户的存储位置要有意识。所以有一个能读到的数
（:meth:`CredentialsService.status` → ``pending_migration``）：
迁移完成前"库里有值、钥匙串没有"是唯一允许的中间态，
而它就等于 ``pending_migration > 0``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import SecretStoreUnavailable
from app.services.secrets import (
    SecretStore,
    model_provider_target,
    nas_token_target,
    setting_target,
)
from app.storage.base import ModelProviderRecord, StoreBundle

__all__ = [
    "MIGRATED_SETTING_KEYS",
    "REGISTERED_ONLY_CELLS",
    "CredentialsService",
    "MigrationReport",
]

logger = logging.getLogger(__name__)

MIGRATED_SETTING_KEYS: tuple[str, ...] = ()
"""``app_settings`` 里**真的会被搬走**的那几个键（收编档）。

加一个新键之前先回答两个问题：**谁在读它**（本机档上真的有人读吗）、
它会不会超过钥匙串单条上限（2560 字节）。答不上来就进
:data:`REGISTERED_ONLY_CELLS`——"只登记"是一个正当结论，不是待办。
"""

REGISTERED_ONLY_CELLS: tuple[tuple[str, str], ...] = (
    ("app_settings", "mineru.token"),
    ("app_settings", "paddleocr.token"),
    ("mcp_servers", "env"),
    ("mcp_servers", "headers"),
)
"""**只登记、不迁**的那几处（方案 §4.2）与理由——这份清单是给读代码的人看的。

它同时是一份"我们想过这件事"的证据：将来谁要收编其中一处，先删掉这里的一行，
再去改 :data:`MIGRATED_SETTING_KEYS` / 加一条新的迁移动作。
"""

_ITEM_SETTING = "setting"
_ITEM_PROVIDER = "model_provider"


@dataclass(slots=True)
class MigrationReport:
    """迁移结果：**逐项**说清"迁了 / 跳过了（为什么）/ 失败了（为什么）"。

    ``store`` 是 ``available`` / ``unavailable``（与 :meth:`CredentialsService.status`
    同一个词表）；``pending_migration`` 是**跑完之后**还剩几处——成功是 0，
    有失败的就是失败的那几处（那个数就是"可重跑"的判据）。
    """

    store: str
    migrated: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    pending_migration: int = 0

    @property
    def ok(self) -> bool:
        """一处都没失败才算成功（**没有任何明文时也算**：本来就没得迁）。"""
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "migrated": self.migrated,
            "skipped": self.skipped,
            "failed": self.failed,
            "pending_migration": self.pending_migration,
        }


@dataclass(slots=True)
class _Item:
    """一条待迁的明文：名字（报告里那一项）+ 值 + 钥匙串里的名字 + **怎么清掉库里那份**。"""

    item: str
    value: str
    target: str
    clear: Callable[[], None]


class CredentialsService:
    """把本机库里那几处明文凭据收进系统钥匙串（方案 §4.2）。

    它是**显式动作**的服务：没有任何调用点会顺手调它（不静默迁移），
    三个入口（端点 / 面板 / CLI）都落在 :meth:`migrate` 上。
    """

    def __init__(self, stores: StoreBundle, secrets: SecretStore) -> None:
        self._stores = stores
        self._secrets = secrets

    # ------------------------------------------------------------------ 读

    def status(self) -> dict[str, Any]:
        """``GET /local/secrets`` 的那两个字段（**只报数与可用性，绝不回显秘密**）。

        ``pending_migration`` 在钥匙串不可用的档上恒为 0——那种机器上"等着迁"这件事
        根本不存在（库就是凭据的家，R14 的服务器档与 §4.5 的 CI 边界都是这一支）。
        """
        return {"store": self._store_word(), "pending_migration": self.pending()}

    def pending(self) -> int:
        """库里还有几处明文等着迁（**不可用时恒 0**，理由见 :meth:`status`）。"""
        if not self._secrets.available():
            return 0
        return len(self._items())

    # ------------------------------------------------------------------ 迁

    def migrate(self) -> MigrationReport:
        """把待迁的明文逐项搬进钥匙串，迁一项清一项；**返回报告**（不抛逐项失败）。

        钥匙串**整条不可用**时抛 :class:`SecretStoreUnavailable`（端点翻成 503）：
        那种情况下"逐项失败"没有意义——一项都写不进去，而报告里 N 条一模一样的
        "写不进去"比一句"这台机器没有钥匙串"难懂得多。
        """
        if not self._secrets.available():
            raise SecretStoreUnavailable(
                "这台机器没有可用的系统钥匙串（Windows 凭据管理器）："
                "凭据只能留在本机库里，做不到「收进钥匙串」这一步。"
                "（Linux 桌面 / 容器 / CI 都属于这一档，方案 §4.5 的边界）"
            )
        report = MigrationReport(store=self._store_word())
        for item in self._items():
            try:
                self._move(item, report)
            except Exception as exc:  # 写不进去 → **不清明文**，如实报，下次可重跑
                reason = str(exc) or exc.__class__.__name__
                report.failed.append({"item": item.item, "reason": reason})
                logger.warning("这一项没迁成（%s）：%s", item.item, reason)
        if report.migrated or report.skipped:
            self._shrink()
        report.pending_migration = self.pending()
        logger.info(
            "凭据收编：迁了 %d、跳过 %d、失败 %d（还剩 %d 处明文）",
            len(report.migrated),
            len(report.skipped),
            len(report.failed),
            report.pending_migration,
        )
        return report

    def _move(self, item: _Item, report: MigrationReport) -> None:
        """搬一条：**先写钥匙串、后清库**（顺序反了会在崩溃时永久丢钥匙）。"""
        existing = self._secrets.get(item.target)
        if existing == item.value:
            # 幂等：上一次已经迁过，只剩库里这份重复的
            item.clear()
            report.skipped.append(
                {"item": item.item, "reason": "钥匙串里已经是同一个值；库里那份重复的已清掉"}
            )
            return
        if existing is not None:
            # 钥匙串里有一份**不一样**的：用户在这之后又改过（新值只落钥匙串），
            # 所以以钥匙串为准，库里那份是旧的。**不覆盖钥匙串**（那会把用户的新值抹掉）。
            item.clear()
            report.skipped.append(
                {
                    "item": item.item,
                    "reason": "钥匙串里已有一份不同的值（以钥匙串为准）；库里那份旧的已清掉",
                }
            )
            return
        self._secrets.set(item.target, item.value)
        item.clear()
        report.migrated.append({"item": item.item, "reason": ""})

    def _shrink(self) -> None:
        """清页：把腾出来的那些页**真的**交还给系统（方案 §4.3-3）。

        清掉库里的行 / 列之后，腾出来的空闲空间还留在表里，所以迁移完在源库上收一次页
        （``MetaStore.vacuum``；PG 的 ``VACUUM`` 幂等，重跑无害）。

        **只剩这一步了**：原先还有一条 ``StoreBundle.eraser`` 的
        ``secure_delete`` → ``VACUUM`` → ``wal_checkpoint(TRUNCATE)`` 三连，
        那是给 SQLite 主库 + ``-wal`` 收尾用的（本机档与那个库随 v0.1.0 瘦身一起拆除）。
        PG 的预写日志在服务端，进程内没有"顺手把 WAL 截掉"这件事。
        """
        self._stores.meta.vacuum()

    # ------------------------------------------------------------------ 待迁清单

    def _items(self) -> list[_Item]:
        """库里还剩哪些明文（**只有非空的才算**：空串不是秘密）。

        "库里已空 → skip" 在报告里表现为**那一项根本不出现**：没有明文就没有待迁项，
        而报告是给"要不要再跑一次"用的判据——把一堆"没什么可迁"的行列进去，
        真正的失败会淹在里面。
        """
        found: list[_Item] = []
        for key in MIGRATED_SETTING_KEYS:
            value = self._stores.meta.get_setting(key)
            if not (value or "").strip():
                continue
            found.append(
                _Item(
                    item=f"{_ITEM_SETTING}:{key}",
                    value=value,
                    target=setting_target(key),
                    clear=_DeleteSetting(self._stores, key),
                )
            )
        for record in self._stores.meta.list_model_providers():
            value = (record.api_key or "").strip()
            if not value:
                continue
            found.append(
                _Item(
                    item=f"{_ITEM_PROVIDER}:{record.id}",
                    value=value,
                    target=model_provider_target(record.id),
                    clear=_ClearProviderKey(self._stores, record),
                )
            )
        return found

    def _store_word(self) -> str:
        return "available" if self._secrets.available() else "unavailable"

    # ------------------------------------------------------------------ NAS 钥匙（壳侧那半）

    def nas_token(self, origin: str) -> str | None:
        """读 NAS 长期钥匙（``kylab:nas_token:<origin>``）；``None`` = 没配。

        给**壳侧那半**与阶段 8 的对拍用（壳启动时自己也会读同一把），
        后端这条路本身不需要它——边车的 ``--token`` 仍然从引导级来（R3/R14：
        令牌不进 HTTP 请求体、不落库）。
        """
        return self._secrets.get(nas_token_target(origin))

    def set_nas_token(self, origin: str, token: str) -> None:
        """写 NAS 长期钥匙（壳侧首次运行把 ``config.json`` 里那份明文迁进来时用）。"""
        self._secrets.set(nas_token_target(origin), token)

    def delete_nas_token(self, origin: str) -> None:
        """删 NAS 长期钥匙（退出登录 / 换 NAS 时用）。"""
        self._secrets.delete(nas_token_target(origin))


class _DeleteSetting:
    """清掉库里那一行（``app_settings``：**删行**而不是写空——方案 §4.2 的原话）。"""

    __slots__ = ("_key", "_stores")

    def __init__(self, stores: StoreBundle, key: str) -> None:
        self._stores = stores
        self._key = key

    def __call__(self) -> None:
        self._stores.meta.delete_setting(self._key)


class _ClearProviderKey:
    """清掉 ``model_providers.api_key`` 那一列（**清列**，行本身要留着）。"""

    __slots__ = ("_record", "_stores")

    def __init__(self, stores: StoreBundle, record: ModelProviderRecord) -> None:
        self._stores = stores
        self._record = record

    def __call__(self) -> None:
        # 拿一条新的记录再清：`_items()` 到清这一下之间可能有别的写入
        # （迁移是秒级的显式动作，但"读旧值再整行写回"会顺手把别的字段带回旧版本）
        fresh = self._stores.meta.get_model_provider(self._record.id)
        if fresh is None:  # pragma: no cover - 迁移期间被删掉了
            return
        fresh.api_key = ""
        self._stores.meta.update_model_provider(fresh)
