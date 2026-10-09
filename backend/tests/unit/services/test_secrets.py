"""系统钥匙串：三份实现与命名规范（M5 阶段 6，方案 §4.1）。

镜像同构：``app/services/secrets.py`` → 本文件。

## 三条口径（方案 §4.1 那三条，逐条钉）

1. **读不到 = 没配**（不回退到别处读明文）：``get`` 回 ``None``；
2. **写不了 = 明确失败**：不可用时 ``set`` 抛 ``SecretStoreUnavailable``（端点 503），
   **绝不静默写回明文**；
3. **三份实现**：Windows（真机走一遍写-读-改-删，并在 ``cmdkey /list`` 里核对）、
   Null（CI 在 Linux 上跑的就是这一支）、InMemory（用例）。

## 真机那一条（``cmdkey``）

``cmdkey /list`` 是"这一条真的进了 Windows 凭据管理器"的**外部**证据：它读的是系统那份
数据库，而不是我们自己的读回路径——所以它能证明"写成功了"这件事不是我们自己安慰自己。
用例跑完把那条删掉（``cmdkey`` 里不该留下我们写的东西）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from app.core.exceptions import SecretStoreUnavailable, SecretTooLarge
from app.services.secrets import (
    MAX_BLOB_BYTES,
    TARGET_PREFIX,
    USER_NAME,
    InMemorySecretStore,
    NullSecretStore,
    SecretStore,
    WindowsCredentialStore,
    model_provider_target,
    nas_token_target,
    normalize_origin,
    platform_store,
    setting_target,
    target_name,
    use_keychain,
)

pytestmark = pytest.mark.local

WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(not WINDOWS, reason="Windows 凭据管理器只在 Windows 上")
requires_no_windows = pytest.mark.skipif(WINDOWS, reason="这一条说的是别的平台上没有凭据管理器")


@pytest.fixture
def probe_name() -> str:
    """一次性的探测名字（**每次都不一样**：用例之间不互相踩，也不踩真机上的旧数据）。

    用完由用例自己删；名字带 uuid 是为了"跑到一半被杀"也不会与下一次撞上。
    """
    return target_name("probe", f"pytest-{uuid.uuid4().hex[:8]}")


# ------------------------------------------------------------------ 命名规范


def test_the_three_targets_are_built_from_one_rule() -> None:
    """三处用途的名字都由 :func:`target_name` 拼出来（``kylab:<用途>:<对象 id>``）。

    这条判据的价值在"防手抄"：谁能给名字里加一处 ``f"kylab:..."``，谁就能在壳与后端
    之间造出两个名字——而那种分叉的表现是"壳写的钥匙后端读不到"（最难查的一类问题）。
    """
    assert TARGET_PREFIX == "kylab"
    assert USER_NAME == "kylab"
    assert model_provider_target("mp_abc123") == "kylab:model_provider:mp_abc123"
    assert setting_target("web.search_api_key") == "kylab:setting:web.search_api_key"
    assert nas_token_target("http://nas.test:8090") == "kylab:nas_token:http://nas.test:8090"


def test_a_name_must_have_both_halves() -> None:
    """两段都不能空：空段拼出来的名字会在钥匙串里与别人撞上（如实拒，不做清洗）。"""
    with pytest.raises(ValueError):
        target_name("", "mp_1")
    with pytest.raises(ValueError):
        target_name("model_provider", "  ")
    with pytest.raises(ValueError):
        target_name("n:a", "mp_1")


def test_an_origin_has_exactly_one_spelling() -> None:
    """同一个 NAS 的两种写法归一成**同一个名字**（否则改一处、另一处还是旧的）。"""
    assert normalize_origin("http://nas.test:8090/api/v1") == "http://nas.test:8090"
    assert normalize_origin("  HTTP://NAS.test:8090/  ") == "http://nas.test:8090"
    assert nas_token_target("http://nas.test:8090/api/v1") == nas_token_target(
        "http://nas.test:8090/"
    )
    # 没有 scheme 时不替用户补 http（补了就会与壳里填的那个串分叉）
    assert nas_token_target("nas.test:8090") == "kylab:nas_token:nas.test:8090"
    with pytest.raises(ValueError):
        nas_token_target("   ")


def test_a_port_survives_in_the_name() -> None:
    """``:`` 在最后一段里是合法的（NAS 地址本来就有端口）——名字只拼不拆。"""
    assert target_name("nas_token", "http://nas.test:8090") == (
        "kylab:nas_token:http://nas.test:8090"
    )


def test_use_keychain_needs_a_store_that_really_works() -> None:
    """判据只有一条：**有 store 且它真的可用**（服务器档 / Linux / CI 都回假）。"""
    assert use_keychain(None) is False
    assert use_keychain(NullSecretStore()) is False
    assert use_keychain(InMemorySecretStore(available=False)) is False
    assert use_keychain(InMemorySecretStore()) is True


def test_platform_store_matches_the_platform() -> None:
    """组合根与 CLI 都调它：Windows 给凭据管理器，别的平台给 Null（一份选择）。"""
    store = platform_store()
    if WINDOWS:
        assert isinstance(store, WindowsCredentialStore)
    else:
        assert isinstance(store, NullSecretStore)
    assert isinstance(store, SecretStore)


# ------------------------------------------------------------------ InMemory（用例用）


def test_in_memory_reads_back_what_it_wrote() -> None:
    store = InMemorySecretStore()

    assert store.available() is True
    assert store.get("kylab:setting:x") is None  # 读不到 = 没配

    store.set("kylab:setting:x", "s3cret")

    assert store.get("kylab:setting:x") == "s3cret"
    assert store.names() == ("kylab:setting:x",)


def test_in_memory_delete_is_idempotent() -> None:
    store = InMemorySecretStore()
    store.set("kylab:setting:x", "value")

    store.delete("kylab:setting:x")
    store.delete("kylab:setting:x")  # 本来就没有也算成功（迁移器会重跑）

    assert store.get("kylab:setting:x") is None


def test_an_empty_value_is_a_delete() -> None:
    """空值不是秘密：``set(name, "")`` 与 ``delete(name)`` 是同一件事（三个实现同一条）。"""
    store = InMemorySecretStore()
    store.set("kylab:setting:x", "value")

    store.set("kylab:setting:x", "")

    assert store.get("kylab:setting:x") is None


def test_in_memory_refuses_a_value_that_is_too_long() -> None:
    """超上限**如实拒、绝不截断**（截断会写进去半把钥匙，而用户以为存好了）。"""
    store = InMemorySecretStore()

    with pytest.raises(SecretTooLarge):
        store.set("kylab:setting:x", "k" * (MAX_BLOB_BYTES + 1))

    assert store.get("kylab:setting:x") is None


def test_an_unavailable_in_memory_store_behaves_like_null() -> None:
    store = InMemorySecretStore(available=False)

    assert store.get("kylab:setting:x") is None
    with pytest.raises(SecretStoreUnavailable):
        store.set("kylab:setting:x", "value")
    with pytest.raises(SecretStoreUnavailable):
        store.delete("kylab:setting:x")


# ------------------------------------------------------------------ Null（其余平台）


def test_null_reads_as_not_configured_and_refuses_to_write() -> None:
    """R4 的判据原话：Null 分支下 ``set`` 抛、``get`` 回 ``None``。"""
    store = NullSecretStore()

    assert store.available() is False
    assert store.get("kylab:setting:x") is None
    with pytest.raises(SecretStoreUnavailable) as raised:
        store.set("kylab:setting:x", "value")
    assert "钥匙串" in str(raised.value), "要说人话（端点把这句话原样给用户）"
    with pytest.raises(SecretStoreUnavailable):
        store.delete("kylab:setting:x")


# ------------------------------------------------------------------ Windows（真机）


@requires_no_windows
def test_the_windows_store_cannot_be_built_elsewhere() -> None:
    """别的平台上根本构造不出来（而不是构造出来一个写不进去的对象）。"""
    with pytest.raises(SecretStoreUnavailable):
        WindowsCredentialStore()


@requires_windows
def test_windows_round_trip_and_delete(probe_name: str) -> None:
    """真机走一遍：写 → 读 → 改 → 删 → 再读（每一步都对着系统那份数据库）。"""
    store = WindowsCredentialStore()
    assert store.available() is True, "这台机器上凭据管理器应当可用"

    try:
        assert store.get(probe_name) is None

        store.set(probe_name, "kylab_probe_值-with-utf8")
        assert store.get(probe_name) == "kylab_probe_值-with-utf8"

        store.set(probe_name, "second-value")
        assert store.get(probe_name) == "second-value", "同一个名字再写就是覆盖"
    finally:
        store.delete(probe_name)

    assert store.get(probe_name) is None
    store.delete(probe_name)  # 幂等


@requires_windows
def test_windows_accepts_exactly_the_documented_limit(probe_name: str) -> None:
    """2560 字节是**上限本身**（不是"大概能写"）：刚好写得进，多一字节拒。"""
    store = WindowsCredentialStore()
    try:
        store.set(probe_name, "a" * MAX_BLOB_BYTES)
        assert store.get(probe_name) == "a" * MAX_BLOB_BYTES

        with pytest.raises(SecretTooLarge):
            store.set(probe_name, "a" * (MAX_BLOB_BYTES + 1))
    finally:
        store.delete(probe_name)


@requires_windows
def test_windows_entry_is_visible_to_cmdkey(probe_name: str) -> None:
    """**外部证据**：``cmdkey /list`` 里看得到 ``kylab:…`` 那一行（阶段 6 的物证）。

    为什么值得一条用例：``get`` 是我们自己的读回路径，"写进系统凭据管理器"这件事
    它证明不了（一个只写进内存的假实现也能让 read-back 通过）。``cmdkey`` 读的是
    系统那份数据库，而用户在那个图形界面上看到的也是它。
    """
    if shutil.which("cmdkey") is None:  # pragma: no cover - 极老的 Windows
        pytest.skip("这台机器上没有 cmdkey")

    store = WindowsCredentialStore()
    name = probe_name
    try:
        store.set(name, "kylab_probe_cmdkey")

        listing = _cmdkey_list()
        assert f"target={name}" in listing, "凭据管理器里应当有它"
        assert "kylab" in listing.lower(), "那一条的 User 是 kylab"
    finally:
        store.delete(name)

    assert f"target={name}" not in _cmdkey_list(), "用例跑完不该留下我们写的东西"


def _cmdkey_list() -> str:
    """``cmdkey /list`` 的输出。

    两处细节：**从 Python 起进程**（Git Bash 会把 ``/list`` 当路径改写，手工跑要
    ``MSYS_NO_PATHCONV=1``）；按**系统 ANSI 代码页**解码（``mbcs``）——cmdkey 输出的
    标签是本地化的，按 UTF-8 解会变成乱码（我们只断言 ASCII 那几段，所以不影响判据，
    但乱码的输出没法贴进报告）。
    """
    binary = shutil.which("cmdkey")
    assert binary is not None  # 调用方已经跳过"没有 cmdkey"的情况
    done = subprocess.run(  # noqa: S603 - 固定命令与参数，没有外部输入
        [binary, "/list"], capture_output=True, check=False
    )
    return (done.stdout + done.stderr).decode("mbcs", errors="replace")
