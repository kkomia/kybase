"""MCP 边界的失败契约：**领域失败一律以我们的中文原因 + ``isError`` 回去**（v0.65）。

镜像同构：``app/mcp_server/server.py`` 的 ``make_handler`` → 本文件。

**为什么单独一份**（``test_server.py`` 管装配与凭据链）：这里只盯一件事——
``core/exceptions.py`` 里那两个"领域异常"家族（``KylabError`` 与**刻意**独立成一支的
``EmbeddingError``）从工具层逃出来时，调用方看到的是什么。

**现场**（实测）：嵌入端点连不上时，``EmbeddingError`` 从 handler 的
``except KylabError`` 旁边逃出去，被 mcp SDK 压成一句没有细节的英文
``Error executing tool search``——项目精心设计的"领域失败以 ``isError: true`` +
中文原因回给调用方"这条契约被绕过，而且**被压掉的恰好是最该看见的那半句**
（哪个库的嵌入服务不可用）。下面第三条用例把那个"压"本身也钉住了。

**这一条必须是协议层的用例**（真 MCP 客户端 + 真 server）：直调 ``call_tool`` 或
直调 handler 都验不到"SDK 会不会把它压成那一句英文"——被压掉的正是这条缝里的东西。

**用替身而不是真仓储**：这条验的是边界契约，不是凭据链与存储链（那两条在
``test_server.py`` 里走真链）。所以这里把"工具层抛 ``EmbeddingError``"摆成唯一自变量，
于是它在缺 PG 的机器上也能跑（``local`` 那一档）。
"""

from __future__ import annotations

import json
from functools import cache
from types import SimpleNamespace

import pytest

from app.core.caller import Caller
from app.core.exceptions import EmbeddingError, ForbiddenError
from app.mcp_server.auth import MCP_KEY_ENV

#: 用例全程进程内替身，不碰仓储（见模块说明），所以按 ``local`` 那一档跑。
pytestmark = pytest.mark.local

#: 一句**中文**原因：它必须原样出现在客户端拿到的载荷里。
#: 这也正是真实故障里最该说出口的那半句（哪个库的嵌入服务不可用）。
EMBEDDING_REASON = (
    "WeMM 嵌入服务不可用（3 次尝试都失败，http://100.66.1.12:8234/v1/embeddings）："
    "ConnectTimeout: timed out"
)

#: handler 没认下那个异常时，客户端会看到的英文（第三条用例把这点钉住）。
SDK_UNEXPECTED_TOOL_ERROR = "Error executing tool search"


@cache
def _stub_services():  # type: ignore[no-untyped-def]
    """组合根的最小替身。

    ``build_server`` 只向它要"这一张脸"（中间件构造时收下它），而凭据判定被下面那个
    patch 绕开了——这条用例验的是**失败契约**，不是"凭据 → 主体"那条链。

    **必须是 ``cache`` 包着的**（不是裸函数）：``conftest`` 的收尾会调
    ``reset_services()``，而它内部是 ``get_services.cache_clear()``——这里替掉的正是
    那个名字，裸函数会在每个用例收尾时抛 ``AttributeError``。
    """
    return SimpleNamespace(
        api_keys=SimpleNamespace(visible_tool_names=lambda caller: None),
    )


@pytest.fixture
def as_boundary(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """把一个"总是管理员"的调用者装进中间件，并给出一条可用的凭据名。

    三处 patch 都只为绕开存储：``get_services``（组合根要连 PG）、``resolve_caller``
    （按 Key 查库）与 ``call_tool``（每条用例自己决定工具层抛什么）。
    **真正被测的那一段——handler 的 ``except`` 与 SDK 的返回值——一个字节都没被替换。**
    """
    import app.core.services as core_services
    import app.mcp_server.auth as auth
    import app.mcp_server.server as server_module

    monkeypatch.setattr(core_services, "get_services", _stub_services)
    monkeypatch.setattr(auth, "resolve_caller", lambda services, token: Caller(is_admin=True))
    monkeypatch.setenv(MCP_KEY_ENV, "边界用例的凭据")

    def install(error: Exception) -> None:
        def _raise(services, tool_name, payload, *, caller):  # type: ignore[no-untyped-def]
            raise error

        monkeypatch.setattr(server_module, "call_tool", _raise)

    return install


async def _call_search():  # type: ignore[no-untyped-def]
    """用**真的 MCP 客户端**连上真的 server 调一次 ``search``（in-memory 传输）。"""
    from mcp import Client

    from app.mcp_server.server import build_server

    async with Client(build_server()) as client:
        return await client.call_tool("search", {"query": "眼轴长度"})


async def test_an_embedding_failure_comes_back_as_is_error_with_the_chinese_reason(
    as_boundary,  # type: ignore[no-untyped-def]
) -> None:
    """``EmbeddingError`` 逃出工具层时，客户端拿到的是**中文原因 + isError**。

    这是那个实测 bug 的正面用例：从前客户端只收到 ``Error executing tool search``
    ——一句没有细节的英文，既说不出"哪个库"，也说不出"嵌入服务连不上"。
    """
    as_boundary(EmbeddingError(EMBEDDING_REASON))

    result = await _call_search()

    assert result.is_error is True, "领域失败不是成功（v0.62 的契约）"
    text = result.content[0].text
    assert "Error executing tool" not in text, "SDK 那句英文一个字母都不该出现"
    assert json.loads(text) == {"error": EMBEDDING_REASON}, '形状仍是 {"error": …}'


async def test_a_kylab_error_still_goes_through_the_same_path(
    as_boundary,  # type: ignore[no-untyped-def]
) -> None:
    """另一支（``KylabError``）**一个字都没变**：同一段 ``except`` 一并认下两者。

    这条是回归保护：加 ``EmbeddingError`` 时最容易动的是返回形状（比如把两支持成
    两个分支、顺手改了文本），而调用方的解析器认的就是它。
    """
    as_boundary(ForbiddenError("只读凭据不能删除文档"))

    result = await _call_search()

    assert result.is_error is True
    assert json.loads(result.content[0].text) == {"error": "只读凭据不能删除文档"}


async def test_an_unrecognised_failure_is_still_the_sdk_english_line(
    as_boundary,  # type: ignore[no-untyped-def]
) -> None:
    """**契约只覆盖那两个领域异常家族**，没被认领的异常照旧是 SDK 那句英文。

    这一条同时钉住三件事：

    - 那个 bug 的形状（``EmbeddingError`` 从前就是走到这里来的）：``isError: true``
      加一句没有细节的英文——调用方既不知道发生了什么，也不知道该找谁；
    - **别顺手扩成 ``except Exception``**：程序缺陷被伪装成"领域失败"之后，日志里那句
      带完整栈的 ERROR 也就没了（实测 SDK 会 ``logger.exception`` 之后才压成英文，
      见 ``mcp/server/mcpserver/server.py`` 的 "raised an unexpected exception"）；
    - ``is_error`` 这一位不变（两种路都是 ``true``）：**区别只在文本**——所以"看不到
      细节"这件事只能靠文本形状去发现，这正是不该把它压在日志里自己看的原因。

    那句英文是 **mcp 2.x 的原文**：SDK 换大版本时这里会先红，而那正是该重新核对
    这条边界契约的时候（见模块头的三个口子）。
    """
    as_boundary(RuntimeError("这是程序缺陷，不是领域失败"))

    result = await _call_search()

    assert result.is_error is True
    assert result.content[0].text == SDK_UNEXPECTED_TOOL_ERROR
