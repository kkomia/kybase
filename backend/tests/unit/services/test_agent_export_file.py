"""运行代码 → 交付那份文件（v0.56，见《开发计划》§12.338 完善方案 2）。

**为什么单独一个文件**：这一组盯的是一条**跨了三层**的链路，而它此前是断的——
沙箱里跑出来的文件（`run_command` 写下的 png）**没有任何一条路能变成交付物**：
交付口只有 `export_document` / `export_table` / `export_deck`（把内容"描述"出来），
而"用代码做出来的东西描述不出来"。实测 K-03（"运行代码并把图给我"）就是卡在这里：
它在沙箱里真的写出了图，60 步 / 361 秒之后**交付物是 0**。

三层的分工与各自的判据：

| 层 | 它回答 | 这一组怎么验 |
| --- | --- | --- |
| 沙箱执行 | 命令真跑起来了吗、文件落盘了吗 | `run_isolated`（direct 档）跑真代码 + `stat` |
| 交付登记 | 那份文件变成产物了吗 | `call_tool("export_file", …)` → 产物记录 + 字节数 |
| 取回 | 对方点得到吗 | `services.artifacts.read_file` 按产物 id 读回同一批字节 |

**为什么沙箱那一步显式用 `.venv\\Scripts\\python.exe`**：K-03 的根因不是"没装库"，
而是**沙箱里裸 `python` 解析到的是 uv 托管的那个空解释器**（`site-packages` 里
一个包都没有），而 `uv run` 并不把 `.venv\\Scripts` 放进 PATH。
所以预装（`pyproject.toml` 的 `office` extra）要真正生效，还得让 direct 档的 `python`
落到 `.venv` —— 那一步在另一条 lane（`isolation.py::_direct_plan` 的 env 组装）。
本文件因此分两半，**两半都是真的**：

- 显式解释器那一半（`test_running_code_through_the_sandbox_layer_…`）证明
  "库在 + 交付链路通"，**不依赖**那一步；
- 裸 `python` 那一半（`test_bare_python_in_the_sandbox_can_import_the_chart_library`）
  就是那一步的验收：它现在会**红**，红了才说明"预装"这件事真的没生效完。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from app.core.exceptions import InvalidRequestError
from app.core.services import Services
from app.services import isolation
from app.services.api_key import Caller
from app.services.sandbox import sandbox_for
from app.services.tools import call_tool

PLOT_CODE = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "x = list(range(1, 11))\n"
    "fig, ax = plt.subplots(figsize=(6, 4), dpi=110)\n"
    "ax.plot(x, [n ** 2 for n in x], marker='o')\n"
    "ax.set_title('squares')\n"
    "fig.tight_layout()\n"
    "fig.savefig('squares.png')\n"
    "import os\n"
    "print('PNG_BYTES', os.path.getsize('squares.png'))\n"
)


@pytest.fixture
def services() -> Services:
    from app.core.services import get_services

    return get_services()


@pytest.fixture
def admin() -> Caller:
    return Caller(is_admin=True)


@pytest.fixture
def conversation(services: Services) -> str:
    """一条**没挂工作区**的会话——K-03 那个形状（产物落会话文件区）。"""
    return services.conversations.create(title="跑段代码把图给我").id


def _sandbox(services: Services, conversation: str) -> Path:
    return sandbox_for(services.runtime.data_dir, conversation).ensure()


def _run(services: Services, conversation: str, code: str, *, interpreter: str) -> str:
    """在**真的隔离层**里跑一段代码（direct 档：与这台机器上的实际形态一致）。

    走 `run_isolated` 而不是 `subprocess.run`：路径、cwd、超时、输出截断
    全是产品里那一条（`agent_exec.run_command` 也走它）。
    """
    box = _sandbox(services, conversation)
    result = isolation.run_isolated(
        [interpreter, "-c", code],
        workspace_root=box,
        sandbox_dir=box,
        timeout=60.0,
        isolation=isolation.direct_isolation(),
    )
    if not result.ok and "No module named 'matplotlib'" in result.stderr:
        pytest.fail(
            "这个解释器里没有 matplotlib —— 它是 `pyproject.toml` 的 `office` extra 依赖，"
            "装上它：`cd backend && uv sync --all-extras`（改的正是 K-03 的根因）。"
            f"\n解释器：{interpreter}\nstderr：{result.stderr}"
        )
    assert result.ok, f"沙箱里的代码没跑成：{result.stdout}\n{result.stderr}"
    return result.stdout


# --------------------------------------------------------------- 交付登记（与画图无关）


def test_export_file_turns_a_sandbox_file_into_an_artifact(
    services: Services, admin: Caller, conversation: str
) -> None:
    """沙箱里的一份文件 → 交付物 → **按产物 id 读回同一批字节**。

    三层各断言一件事：产物记录在（名字 + 字节数）、字节数是**文件真实的**大小、
    取回的就是那几个字节。只断言"工具返回成功"是不够的——那正是 G-03 的形状
    （它自认为做完了，而对方手上什么都没有）。
    """
    box = _sandbox(services, conversation)
    payload = b"\x89PNG\r\n\x1a\n" + b"kylab" * 512
    (box / "chart.png").write_bytes(payload)

    result = call_tool(
        services,
        "export_file",
        {"path": "chart.png"},
        caller=admin,
        conversation_id=conversation,
    )

    assert result["artifact_id"].startswith("art_")
    assert result["name"] == "chart.png"
    assert result["format"] == "png"
    assert result["size_bytes"] == len(payload)

    records = services.artifacts.list_for_conversation(conversation)
    assert [record.name for record in records] == ["chart.png"]
    content, name = services.artifacts.read_file(conversation, result["artifact_id"])
    assert (name, content) == ("chart.png", payload)


def test_export_file_can_rename_and_keep_a_subdirectory(
    services: Services, admin: Caller, conversation: str
) -> None:
    """文件在子目录里、名字也可以另给：两者都走同一条清洗。

    `path` 允许子目录（`out/chart.png` 是刚才那条命令真实写出来的形状），
    而**显示名里不许有路径**——名字要被下游拿去拼对象 Key 与落盘路径。
    """
    box = _sandbox(services, conversation)
    (box / "out").mkdir(parents=True, exist_ok=True)
    (box / "out" / "chart.png").write_bytes(b"PNG-BYTES")

    result = call_tool(
        services,
        "export_file",
        {"path": "out/chart.png", "filename": "月度销量.png"},
        caller=admin,
        conversation_id=conversation,
    )

    assert result["name"] == "月度销量.png"


# --------------------------------------------------------------- 路径与失败（安全）


def test_export_file_refuses_to_reach_outside_the_sandbox(
    services: Services, admin: Caller, conversation: str
) -> None:
    """**路径必须被约束在沙箱里。**

    这条路径由模型生成，它会写 `../../backend/.env` 或 `C:/Windows/...`——
    不是恶意，是它在猜这个项目的结构。判据复用 `sandbox.resolve_in` 的四道闸
    （绝对路径 / `..` / 解析后越界 / 敏感文件），这里一行都不另写。
    """
    box = _sandbox(services, conversation)
    # 沙箱**外面**放一份真的敏感文件：如果哪道闸漏了，这条用例读到的就是它
    secret = box.parent / f"secret_{uuid.uuid4().hex[:6]}.txt"
    secret.write_text("TOKEN=leaked", encoding="utf-8")
    (box / ".env").write_text("TOKEN=inside-the-box", encoding="utf-8")
    try:
        for path in (
            f"../{secret.name}",
            "../../backend/.env",
            str(secret),
            "C:/Windows/win.ini",
            ".env",
        ):
            with pytest.raises(InvalidRequestError):
                call_tool(
                    services, "export_file", {"path": path}, caller=admin,
                    conversation_id=conversation,
                )
        # 一次都不许登记成产物
        assert services.artifacts.list_for_conversation(conversation) == []
    finally:
        secret.unlink(missing_ok=True)


def test_export_file_says_which_failure_it_was(
    services: Services, admin: Caller, conversation: str
) -> None:
    """三种失败**分开说**：文件不存在 / 它是个目录 / 没有扩展名。

    糊成"导出失败"的话模型只能瞎试（G-03 的 80 步里有一半就是这种重试）。
    """
    box = _sandbox(services, conversation)
    (box / "out").mkdir(exist_ok=True)
    (box / "noext").write_bytes(b"x")

    with pytest.raises(InvalidRequestError, match="沙箱里没有这个文件"):
        call_tool(
            services, "export_file", {"path": "nope.png"}, caller=admin,
            conversation_id=conversation,
        )
    with pytest.raises(InvalidRequestError, match="是个目录"):
        call_tool(
            services, "export_file", {"path": "out"}, caller=admin,
            conversation_id=conversation,
        )
    with pytest.raises(InvalidRequestError, match="要有扩展名"):
        call_tool(
            services, "export_file", {"path": "noext"}, caller=admin,
            conversation_id=conversation,
        )


def test_export_file_needs_a_conversation(services: Services, admin: Caller) -> None:
    """外部 MCP 那条通道没有会话，也就没有沙箱——**明确说清**而不是猜一个目录。"""
    with pytest.raises(InvalidRequestError, match="只能在这条对话里用"):
        call_tool(services, "export_file", {"path": "chart.png"}, caller=admin)


# --------------------------------------------------------------- 真代码：画图 → 交付


def test_running_code_through_the_sandbox_layer_produces_a_deliverable(
    services: Services, admin: Caller, conversation: str
) -> None:
    """**K-03 那条路的最小复现**：跑真代码画图 → 交付 → 产物里真的有一张图。

    沙箱这一步显式用**项目自己的解释器**（`sys.executable`，跑用例的就是
    `.venv\\Scripts\\python.exe`）——这正是"预装生效"的样子：`matplotlib` 在
    `pyproject.toml` 的 `office` extra 里，`uv sync --all-extras` 之后它就在这个
    解释器里。裸 `python` 落到空解释器那件事由下面那条用例单独管。
    """
    out = _run(services, conversation, PLOT_CODE, interpreter=sys.executable)
    assert "PNG_BYTES" in out

    box = _sandbox(services, conversation)
    written = box / "squares.png"
    assert written.is_file(), "画图代码没有把 png 落到沙箱里"
    size = written.stat().st_size
    assert size > 5_000, f"png 只有 {size} 字节，画出来的不像一张真图"

    result = call_tool(
        services, "export_file", {"path": "squares.png"}, caller=admin,
        conversation_id=conversation,
    )

    assert result["name"] == "squares.png"
    assert result["format"] == "png"
    assert result["size_bytes"] == size
    # 走**产物那条既有路**读回来：字节与磁盘上那份逐字节相等
    content, _ = services.artifacts.read_file(conversation, result["artifact_id"])
    assert content == written.read_bytes()
    assert content[:8] == b"\x89PNG\r\n\x1a\n", "交付出去的不是一张真 PNG"


def test_bare_python_in_the_sandbox_can_import_the_chart_library(
    services: Services, conversation: str
) -> None:
    """**裸 `python` 也要能 import matplotlib**（direct 档的验收）。

    实测 K-03：沙箱里 `python -c "import matplotlib"` → `ModuleNotFoundError`，
    而那个 `python` 是 **uv 托管的裸解释器**（`site-packages` 里连 fastapi 都没有），
    `uv run` 并不把 `.venv\\Scripts` 放进 PATH。所以"把 matplotlib 加进依赖清单"
    只完成了一半——**另一半是让 direct 档的 `python` 落到 `.venv`**。

    这条用例就是那一半的验收：它现在**会红**（红得对），
    等 `isolation.py::_direct_plan` 把解释器/PATH 接上之后转绿。
    """
    box = _sandbox(services, conversation)
    result = isolation.run_isolated(
        ["python", "-c", "import matplotlib, numpy, pandas, openpyxl; print('LIBS_OK')"],
        workspace_root=box,
        sandbox_dir=box,
        timeout=45.0,
        isolation=isolation.direct_isolation(),
    )

    assert result.ok and "LIBS_OK" in result.stdout, (
        "沙箱里的裸 python 还是那个空解释器：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}\n"
        "（预装只完成了一半：库进了 .venv，而 direct 档的 python 没落到 .venv）"
    )
