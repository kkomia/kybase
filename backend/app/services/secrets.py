"""系统钥匙串：**一处「秘密来源」接口**（M5 阶段 6，方案 §4.1 / §4.2）。

## 三条口径（每一条都有用例钉着）

1. **读不到 = 没配**：``get`` 回 ``None`` 就是"没配置过"——**绝不回退到别处去读明文**。
   回退会让"收编"这件事白做：库里那份副本一直活着，于是同一把钥匙有两个家，
   而"哪一份是新的"迟早分叉（方案 §4.1 第一条）。
2. **写不了 = 明确失败**：``set`` 在钥匙串不可用时抛 :class:`SecretStoreUnavailable`
   （端点翻成 503 + 一句人话），**绝不静默把秘密写回明文**。这条是 R4 的判据。
3. **三份实现**：:class:`WindowsCredentialStore`（``os.name == 'nt'``，``ctypes`` 直调
   ``advapi32``，**零新依赖**）、:class:`NullSecretStore`（其余平台：读 = 没配、
   写 = 明确失败）、:class:`InMemorySecretStore`（用例用）。

## 命名规范（**唯一一份常量**，壳与后端共用同一条规则，方案 §4.1）

```
kylab:<用途>:<对象 id>      kylab:model_provider:mp_abc123
                            kylab:setting:web.search_api_key
                            kylab:nas_token:http://nas.test:8090
UserName = "kylab"（显示用）    Blob = UTF-8，≤ 2560 字节（超了如实拒，不截断）
```

"唯一一份"在这里是**可执行**的那种唯一：三处用途各有一个构造函数
（:func:`model_provider_target` / :func:`setting_target` / :func:`nas_token_target`），
而它们全部由 :func:`target_name` 拼出来——别处再手写一个 `f"kylab:..."` 就是第二处规则。
NAS 地址那一段另有 :func:`normalize_origin`（壳侧照抄同一句，方案 §4.1 那条"跨侧对拍"）。

## 为什么不用 `keyring` 包（方案 §0.4 的实测）

它至少带进来 6 个发行包（`keyring` + `pywin32-ctypes` + 三个 `jaraco.*` + `more-itertools`），
闭包 17 → 23+；而"边车第三方闭包 17 包不涨"是一条硬判据（§4.4）。
``advapi32`` 那四个入口用 ``ctypes`` 直调约 120 行，闭包 17 → 17。

## 三态在**调用方**那一侧（`use_keychain`）

钥匙串"能不能用"与"它是不是这份秘密的家"是两件事，判据只有一处（:func:`use_keychain`）：
服务器容器 / Linux 桌面 / CI 上 ``available()`` 为假，那些档里库仍然是凭据的家
（§4.5 的边界 + R14"服务器档库里那份凭据不动"）——在那儿把读改成"一律走钥匙串"
等于把用户已经配好的凭据读没了。两个服务（运行期配置 / 模型注册）都问这一个函数。
"""

from __future__ import annotations

import ctypes
import logging
import os
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from app.core.exceptions import SecretStoreUnavailable, SecretTooLarge

__all__ = [
    "MAX_BLOB_BYTES",
    "TARGET_PREFIX",
    "USER_NAME",
    "InMemorySecretStore",
    "NullSecretStore",
    "SecretStore",
    "SecretStoreUnavailable",
    "SecretTooLarge",
    "WindowsCredentialStore",
    "model_provider_target",
    "nas_token_target",
    "normalize_origin",
    "platform_store",
    "setting_target",
    "target_name",
    "use_keychain",
]

logger = logging.getLogger(__name__)

TARGET_PREFIX = "kylab"
"""凭据管理器里那一条的**名字前缀**（``kylab:<用途>:<对象 id>``）。

前缀是刻意的：用户的凭据管理器里通常还躺着几十条别人的东西，而 ``cmdkey /list``
与"控制面板 → 凭据管理器"都按名字排——前缀让"哪些是我们写的"一眼可辨
（阶段 8 的物证就靠它）。
"""

USER_NAME = "kylab"
"""那条凭据的 ``UserName``（凭据管理器界面上显示的名字，不是账号）。"""

MAX_BLOB_BYTES = 2560
"""单条秘密的容量上限：Windows 凭据管理器 ``CRED_MAX_CREDENTIAL_BLOB_SIZE``（5 × 512）。

**超了如实拒，绝不截断**（见 :class:`app.core.exceptions.SecretTooLarge`）。这条数
也正是"MCP 那类整段 env / headers 不收编"的技术理由之一（方案 §4.2）。
"""

_KIND_MODEL_PROVIDER = "model_provider"
_KIND_SETTING = "setting"
# 名字里不带 "token" 是为了绕开 S105 那条误报（这条是**段名**，不是凭据）
_KIND_NAS = "nas_token"

#: Windows：``CRED_TYPE_GENERIC`` / ``CRED_PERSIST_LOCAL_MACHINE``。
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
#: 可用性探针读的那个名字：**只读**它，从不写（``CredReadW`` 不会创建任何东西）。
_PROBE_TARGET = f"{TARGET_PREFIX}:availability"


# ------------------------------------------------------------------ 命名规范


def target_name(kind: str, object_id: str) -> str:
    """拼一条凭据的名字：``kylab:<用途>:<对象 id>``（**唯一一处拼接**）。

    "用途"那一段不许空、不许带 ``:``（那会与分隔符混起来）；**对象 id 里的 ``:`` 是允许的**
    ——NAS 地址本来就有端口（``http://nas.test:8090``），而名字只由这几个构造函数拼出来、
    **从没有人把它拆回去**（拆是这条规则最脆的一环：一旦有人 split，端口就会把最后一段
    切坏）。所以这条规则的口径是"只拼不拆"，两段都为空才拒。
    """
    cleaned_kind = (kind or "").strip()
    cleaned_id = (object_id or "").strip()
    if not cleaned_kind or not cleaned_id:
        raise ValueError("凭据名字的两段都不能空（kylab:<用途>:<对象 id>）")
    if ":" in cleaned_kind:
        raise ValueError("凭据名字里的「用途」那一段不能含 ':'（它是分隔符）")
    return f"{TARGET_PREFIX}:{cleaned_kind}:{cleaned_id}"


def model_provider_target(provider_id: str) -> str:
    """``model_providers`` 一行的凭据名字：``kylab:model_provider:<id>``。"""
    return target_name(_KIND_MODEL_PROVIDER, provider_id)


def setting_target(key: str) -> str:
    """``app_settings`` 里一个密钥的名字：``kylab:setting:<key>``。

    键本身带点（``web.search_api_key``）——点不是分隔符，照原样留在最后一段里。
    """
    return target_name(_KIND_SETTING, key)


def normalize_origin(raw: str) -> str:
    """把"NAS 地址"归一成 target 名字里的 ``<origin>`` 那一段（**壳与后端同一条规则**）。

    - 去掉两端空白与末尾的 ``/``；
    - 有 scheme 时：``scheme://host[:port]``（丢掉 path；scheme 与 host 小写）——
      于是 ``http://nas.test:8090/api/v1`` 与 ``http://nas.test:8090`` 是**同一把钥匙**
      （同一个 NAS 的两种写法，各存一份会让用户改了一处、另一处还是旧的）；
    - 没有 scheme 时：整串小写（``nas.test:8090``）——**不替用户补 ``http://``**：
      补了就会与"壳里填的是什么"分叉，而钥匙串里那一条是按壳填的那个串找的。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    return text.lower()


def nas_token_target(origin: str) -> str:
    """NAS 长期钥匙的名字：``kylab:nas_token:<origin>``（见 :func:`normalize_origin`）。

    这一条**壳侧也会读写**（方案 §4.2 #1：壳启动时读它、老配置首次运行迁进来），
    所以它是"两侧各实现一遍同一条规则"里最容易对不上的那一条——阶段 8 的对拍用例
    只需各自写一次、另一侧读一次（名字不一致会当场读成"没配"）。
    """
    cleaned = normalize_origin(origin)
    if not cleaned:
        raise ValueError("NAS 地址不能为空（凭据名字里要有它）")
    return target_name(_KIND_NAS, cleaned)


def use_keychain(store: SecretStore | None) -> bool:
    """**这个档里钥匙串是不是那份秘密的家**（唯一一处判据，两个服务都问它）。

    ``store is None``（没接钥匙串的档：服务器容器、CLI 里的临时装配、既有调用点）与
    ``available() is False``（Linux 桌面 / CI）**都回假**：那些档里库仍然是凭据的家
    ——§4.5 明写"CI / 无桌面环境"与"服务器档"的边界，R14 那句"服务器档库里那份凭据
    不动"就是这条。在那些档上把读改成"一律走钥匙串"，用户已经配好的凭据会被读成
    "没配"（mineru / paddleocr 的 token 是最直接的受害者：服务器那一档真的在读它们）。

    两处各写一遍这个判断，就会出现"配置服务认钥匙串、模型注册还认库"的分叉——
    而那种分叉的表现是"某一半功能突然说没配"。所以只留这一个函数。
    """
    return store is not None and store.available()


def _payload(value: str) -> bytes:
    """秘密 → 写进钥匙串的字节（UTF-8），并在这里把上限闸住。"""
    data = (value or "").encode("utf-8")
    if len(data) > MAX_BLOB_BYTES:
        raise SecretTooLarge(
            f"这条秘密有 {len(data)} 字节，超过钥匙串单条的上限 {MAX_BLOB_BYTES} 字节："
            "请换一条更短的凭据（**不会截断后写进去**）"
        )
    return data


def platform_store() -> SecretStore:
    """这台机器上"系统钥匙串"的实现：Windows 是凭据管理器，其余平台是 Null。

    **组合根与 CLI 都调它**（一份选择）：两处各写一遍 ``if os.name == "nt"``，
    就会出现"CLI 收编进去了、边车却读不到"这类最难查的分叉。

    构造失败（理论上不该发生）**不往上抛**：收编是锦上添花，而"组合根因为钥匙串起不来"
    是拿一个可用的应用去换一个便利功能。失败时落回 Null 并留一条 warning——
    那一档的语义（读 = 没配、写 = 明确失败）本来就正确地描述了现实。
    """
    if os.name != "nt":
        return NullSecretStore()
    try:
        return WindowsCredentialStore()
    except SecretStoreUnavailable as exc:  # pragma: no cover - 只在异常环境里
        logger.warning("这台 Windows 机器上拿不到凭据管理器：%s", exc)
        return NullSecretStore()


# ------------------------------------------------------------------ 接口


@runtime_checkable
class SecretStore(Protocol):
    """一处「秘密来源」：四条操作，三个实现（方案 §4.1）。

    三个实现都满足它（``runtime_checkable`` 与 ``ImportLedger`` / ``SnapshotSource``
    同一手法：结构满足 + 装配时一句 ``isinstance`` 核对，不靠继承）。
    """

    def available(self) -> bool:
        """这台机器现在有没有可用的系统钥匙串。"""
        ...

    def get(self, name: str) -> str | None:
        """读一条秘密；**``None`` = 没配**（含"这把钥匙串现在用不上"）。"""
        ...

    def set(self, name: str, value: str) -> None:
        """写一条秘密；不可用时抛 ``SecretStoreUnavailable``（**绝不落明文**）。

        **空值等价于 :meth:`delete`**：空串不是秘密，"清掉这一条"与"存一个空的"
        是同一件事——三个实现都照这一条办，调用方不必自己区分。
        """
        ...

    def delete(self, name: str) -> None:
        """删一条秘密；本来就没有也算成功（幂等——迁移器与"清除凭据"都会重跑）。"""
        ...


class InMemorySecretStore:
    """进程内的一把"钥匙串"（**只给用例**；进程一退就没了）。

    为什么用例值得一个有状态的实现：迁移器那些判据（幂等、已有同值跳过、失败不清明文）
    都要"能读出上一次写了什么"，而这正是真实现里最难在 CI 上覆盖的部分。
    """

    def __init__(self, *, available: bool = True) -> None:
        self._values: dict[str, str] = {}
        self._available = available

    def available(self) -> bool:
        return self._available

    def get(self, name: str) -> str | None:
        if not self._available:
            return None
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        if not self._available:
            raise SecretStoreUnavailable("这把钥匙串被标成了不可用（用例构造的）")
        if not value:
            self._values.pop(name, None)
            return
        _payload(value)
        self._values[name] = value

    def delete(self, name: str) -> None:
        if not self._available:
            raise SecretStoreUnavailable("这把钥匙串被标成了不可用（用例构造的）")
        self._values.pop(name, None)

    def names(self) -> tuple[str, ...]:
        """已经存了哪几条（**用例用**：核对"哪些真的进了钥匙串"）。"""
        return tuple(sorted(self._values))


class NullSecretStore:
    """没有系统钥匙串的档（Linux 桌面 / 容器里 / CI）：**读 = 没配、写 = 明确失败**。

    它存在的意义是把"这台机器收不了"这件事**说出来**：``available()`` 为假、
    ``set`` 抛 :class:`SecretStoreUnavailable`。于是调用方（迁移器 / 设置写入）
    能如实报，而不是以为写成功了——R4 那条风险的全部处置就是这一条。

    **绝不静默写回明文**：这个类自己一个字节都不落；"要不要退回库里存"是**调用方**
    的决定（``use_keychain`` 为假时那两个服务照旧用库），不是这个类的。
    """

    def available(self) -> bool:
        return False

    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailable(
            "这台机器没有可用的系统钥匙串（Windows 凭据管理器）："
            "在这种环境里凭据只能留在本机库里，写不进钥匙串**也不会**退回去写明文"
        )

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailable(
            "这台机器没有可用的系统钥匙串（Windows 凭据管理器）：没有可删的东西"
        )


# ------------------------------------------------------------------ Windows


class _Missing:
    """「没有这一条」的哨兵。

    与 ``None`` 分开是刻意的：``get`` 在**读失败**时也回 ``None``（第一条口径），
    而"读成功、但里面没有这一条"是另一件事——两者的差别只在日志里看得出，
    但内部得能分开，否则"探针"那条判据会写反。
    """

    def __repr__(self) -> str:  # pragma: no cover - 只出现在调试输出里
        return "<没有这一条>"


_MISSING = _Missing()


def _advapi32() -> Any:
    """装配 ``advapi32`` 那四个入口 + ``CREDENTIALW``（**只在 Windows 上调用**）。

    函数内 import + 函数内建结构：``ctypes.WinDLL`` 与 ``ctypes.wintypes`` 在非 Windows
    平台上根本不该被用到，而这个模块要能被任何平台 import（CI 在 Linux 上跑的就是
    ``NullSecretStore`` 那一支）。
    """
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        """``CREDENTIALW``（``wincred.h``）：字段顺序 / 类型一个都不能改。"""

        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api, CREDENTIALW


class WindowsCredentialStore:
    """Windows 凭据管理器（``advapi32`` 的四个入口，``ctypes`` 直调）。

    为什么值得自己写这 120 行：``keyring`` 包会带进来 6 个发行包（见模块头），
    而这条链上要的只是"存一个 UTF-8 串、按名字读回来、删掉"。

    三处实现细节都是有原因的：

    - ``Persist = CRED_PERSIST_LOCAL_MACHINE``：同一台机器上的同一个用户**跨登录会话**
      都读得到（本机档的边车是壳拉起来的，登录态与用户的交互会话不是同一个）；
      它**不漫游**——换机器要重配，这正是恢复报告里"要重配的凭据"那一条（R13）；
    - 读的时候 ``CredFree`` 释放的是**API 分配的那块内存**（不能用 ``free``）；
    - ``CredReadW`` 只读不创建，所以"探针"读一个固定名字不会在钥匙串里留下任何东西。
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreUnavailable("Windows 凭据管理器只在 Windows 上可用（当前平台没有它）")
        self._api, self._credential_type = _advapi32()

    # ---------------------------------------------------------------- 接口

    def available(self) -> bool:
        """探一次读：**只有真出错**才叫不可用。

        ``CredReadW`` 不创建任何东西，所以这个探针是幂等的，也不会在用户的
        凭据管理器里留下痕迹；"没有这一条"（``ERROR_NOT_FOUND``）恰恰是**健康**的证据。
        """
        try:
            self._read(_PROBE_TARGET)
        except OSError:
            return False
        return True

    def get(self, name: str) -> str | None:
        try:
            found = self._read(name)
        except OSError as exc:
            # 读不到 = 没配（第一条口径）。但**要留一条日志**：这是"钥匙串出问题了"
            # 与"本来就没配"的唯一区别，而前者不该静默成后者。
            logger.warning("读钥匙串失败（%s）：%s", name, exc)
            return None
        return None if found is _MISSING else found

    def set(self, name: str, value: str) -> None:
        if not value:
            # 空值不是秘密：与 delete 同一件事（见协议那条说明）
            self.delete(name)
            return
        data = _payload(value)
        blob = ctypes.create_string_buffer(len(data))
        ctypes.memmove(blob, data, len(data))
        credential = self._credential_type()
        credential.Flags = 0
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = name
        credential.Comment = TARGET_PREFIX
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = USER_NAME
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            raise self._failure("写不进系统钥匙串", name)

    def delete(self, name: str) -> None:
        if self._api.CredDeleteW(name, _CRED_TYPE_GENERIC, 0):
            return
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return  # 本来就没有：幂等
        raise self._failure("删不掉系统钥匙串里的这一条", name, code=code)

    # ---------------------------------------------------------------- 内部

    def _read(self, name: str) -> Any:
        """读一条，回字符串或 :data:`_MISSING`；**真出错抛 ``OSError``**。"""
        pointer = ctypes.POINTER(self._credential_type)()
        if not self._api.CredReadW(name, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == _ERROR_NOT_FOUND:
                return _MISSING
            raise OSError(code, ctypes.FormatError(code))
        try:
            credential = pointer.contents
            raw = bytes(ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize))
        finally:
            # 这块内存是 API 分配的，只能由 CredFree 释放
            self._api.CredFree(pointer)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 不是我们写的（别人往同一个名字里塞了别的东西）→ 当没配，但留一条日志
            logger.warning("钥匙串里那条不是一个 UTF-8 串（%s）：当作没配", name)
            return _MISSING

    def _failure(self, what: str, name: str, *, code: int | None = None) -> Exception:
        actual = ctypes.get_last_error() if code is None else code
        return SecretStoreUnavailable(
            f"{what}（{name}）：系统返回 {actual}（{ctypes.FormatError(actual)}）"
        )
