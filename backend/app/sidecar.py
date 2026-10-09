r"""**边车入口**（Phase B · P3，2026-09-29）：把循环 + 工具 + 工作区 + 沙箱放进**本地进程**，
KB 经提供者客户端接出去 ✓、**模型本机直连** ✓ —— 同一份循环代码、两个装配点里的第二个 ✓。

## 跑法

```bash
# 在 backend/ 下（默认工作区 ~/.kylab/workspace，绝不写系统目录 ✗）
python -m app.sidecar --port 8765
```

## 装配点（方案 §4）

| 这一侧 | 边车模式 | 服务器模式（今天 ✓） |
| --- | --- | --- |
| KB | `KnowledgeProviderClient`（M3 起收编检索与入库 ✓） | `ChatService.retrieve_sources` ✓ |
| 模型 | **本机直连** `OpenAICompatChat` ✓（2026-10-04 拍板，key 在本机 ✓） | `OpenAICompatChat` ✓ |
| 工具 / 工作区 / 沙箱 | **本地** ✓（`isolation.py` / `sandbox.py` ✓； | 服务器进程内 ✓ |
| | **没有隔离就拒绝执行** ✗ 这条不绕过 ✓） | |

（KB 那一件的落点是 `services/knowledge_provider.py` ✓ —— 握手 / 检索 / 入库都在它身上。）

**模型这一格从 2026-10-04 起不是"远端"了**（产品负责人拍板：**推理搬回本机**，本机自己持
key 直连模型服务 ✗ 不再经 NAS 的 `/model-proxy` ✗）。落点是 :class:`_LocalModel`：它每建一次
客户端就问一次 `services.runtime.llm()`（模型身份与凭据都从**本机**的模型注册表来，
key 在本机档由 Windows 凭据管理器持有，见 `services/secrets.py`）。**服务器档一个字没动**：
`/model-proxy` 那条路照旧挂在那台 NAS 上（`api/v1/model_proxy.py` 与 `api/v1/router.py`
都没碰 ✓）——它今天仍服务"客户端不带 key"的那些部署。

`if` 只出现在 `build_clients()` 这一处 ✓ —— **循环不复制** ✗。

## 本轮做到哪（如实写）

- ✓ `GET /health`：版本 + **两端可达性**（真去打后端的 `/api/v1/health` ✓，
  打不通就**如实报不可达** ✗ 不假装健康 ✓）；
- ✓ `POST /turn`：吃一条用户消息 → 建**同一个 `ToolLoop`** ✓（循环本体一行没改 ✗）
  → 工具在**本机执行** ✓ → 结果回灌模型 → 返回 `{answer, sources, steps, notes, sse}` ✓；
- ✓ `steps` 现在是**真的**：`ToolLoop` 产出的 `StepEvent` 逐条转成 dict ✓；
- ✓ `POST /turn/stream`：SSE（`step` / `thinking` / `delta` / **`sources`** / **`approval`** /
  `done` / `error` ✓），事件形状与服务器那条链**逐字对齐** ✓（`sources` 是 M3 阶段 7
  真机验收补上的：提供者接上之后本机这一侧才第一次真的检索得出出处 ✓）；
- ✓ 审批的**确认入口**（2026-09-29 接上 ✓）：`ask` 档走到"要问"时发一条 `type=approval` ✓、
  循环停在 `ApprovalRegistry.wait_decision` 上等人 ✓；用户在界面上点的那一下走
  `POST /turn/approvals/{approval_id}` ✓（与服务器 `ChatApprovalIn/Out` 同形 ✓，
  复用**同一个登记表** ✓ 不另造一套 ✗）。**没有通道的链路**（`/turn` 那条非流式、
  以及脚本 / 单测这种没登记表的）照旧走 `UNAVAILABLE` —— 如实回"待确认"，
  **绝不静默当成用户拒绝** ✗✓。

## 工作区与沙箱

- 工作区默认 `~/.kylab/workspace` ✓（可用 `--workspace` 指定 ✓）；**系统目录一律拒绝** ✗
  （`_check_workspace` 挡 `C:\Windows`、`/etc`、`/usr` 这些 ✓）；
- 命令执行仍走既有两道闸 ✓（`sandbox.require_isolation` 默认**拒绝**裸跑 ✓ —— 这条不许绕 ✓）。

## 这一轮的账落在哪（M2 阶段 3，2026-10-01）

**从这一步起，桌面边车跑的对话账也落在本机**——不再是"跑在本机、账在服务器"✓：

| 件 | 落点 |
| --- | --- |
| 会话 / 消息 / 事件 / 产物 / 笔记 / 记忆 / 设置 | **本机**：`<data_dir>/kylab.db` |
| 知识库（检索、入库）| **NAS** ✗（M3 收成**提供者**：客户端在 `services/knowledge_provider.py` ✓）|
| 模型 | **本机** ✓（2026-10-04 起：本机持 key 直连模型服务，见上面的装配表）|
| 技能目录 / 插件目录 / MCP 配置 | **本机** ✓（2026-10-04 起工具面也接真服务，见下）|

（工作区记录同样在本机库里；产物与文件区的字节在本机对象存储 / 用户的真实目录。）

三件与"落到本机"配套的事，都在本模块里钉死：

1. **入口自己钉档位**（`_pin_local_deployment`）：`KYLAB_DEPLOYMENT=local` +
   `KYLAB_DATABASE_URL` 压成空串 + `KYLAB_DATA_DIR` 指向本次的数据目录。
   **不给环境继承的机会** ✗ ——"边车误连服务器库"是最糟的失败形态（不报错、写错库）；
2. **服务图就是本机档的组合根**（`build_local_services`）：会话 / 产物 / 笔记 / 记忆 /
   设置 / **技能 / 插件 / MCP** 全部走 `get_services()` 那一份（**与本机后端端点是同一个
   对象**——同一张审批登记表、同一个运行期配置、同一批会话、**同一份技能与 MCP 清单**）；
   知识库那条（检索 + 入库）的换线在**组合根一处**（M3 阶段 3，
   `core/services.py::build_services`），不在这里 —— 笔记与产物各持一份真
   `IngestService`，`dataclasses.replace` 换不到它们对象内部的引用（方案 §5.1）；
3. **写回落本机**（`_record_turn` → `ConversationService.record_turn`）：HTTP 写回那一半
   删掉了 ✗，`TurnOut.recorded` 的语义随之变成"**已落本机**"。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.v1.router import local_router
from app.api.v1.schemas import ChatSourceOut
from app.core.caller import Caller
from app.core.config import API_VERSION, get_settings
from app.core.exceptions import NotFoundError, register_exception_handlers
from app.core.logging import setup_logging
from app.core.services import Services, get_services, reset_services
from app.core.storage import LOCAL_DB_NAME, reset_stores
from app.services import agent_tools, plan_gate
from app.services.agent import (
    ApprovalEvent,
    DeltaEvent,
    DoneEvent,
    SourcesEvent,
    StepEvent,
    ThinkingEvent,
)
from app.services.knowledge_provider import (
    STATE_READY,
    KnowledgeProviderClient,
)
from app.services.llm import ChatError, ChatMessage, LLMDelta, OpenAICompatChat, ToolSpec
from app.services.mcp_client import split_qualified
from app.services.remote_clients import RemoteClientError
from app.services.runtime_config import RuntimeConfigService
from app.services.tool_loop import ToolLoop
from app.workers.local_worker import bind_local_scheduler, run_local_scheduler

logger = logging.getLogger(__name__)

#: 惰性拿到的 httpx（**模块级不 import 它** ✗）。
_HTTPX: Any = None


def _httpx() -> Any:
    """惰性拿 httpx —— 既是"不进导入闭包"的手法，也是**用例的注入接缝** ✓。

    为什么模块级不 import：`httpx/__init__.py` 里那句 `from ._main import main`
    （它自带的 CLI 入口）会把 `click` + `pygments` + `rich` 一起拉进来 ✗ ——
    而客户端运行时（边车）只需要它"发请求"那一部分能力 ✓。
    实测：这一处不改，导入闭包里就一直挂着 click/pygments（约 5.5 MB ✗）。

    **行为不变**：缺包时仍在**第一次调用那一刻**抛 `ModuleNotFoundError` ✓
    （原先在导入模块那一刻抛 ✓）；判据见 `scripts/sidecar-closure.py`（重跑闭包 ✓）。

    **为什么是函数而不是散落的函数内 import**：用例要能换掉这一侧的传输
    （`monkeypatch.setattr(sidecar, "_httpx", …)` ✓）—— 散着写 import，测试就只能去改
    真模块的全局属性 ✗（那种 patch 会漏、也会互相干扰）。
    """
    global _HTTPX
    if _HTTPX is None:
        import httpx

        _HTTPX = httpx
    return _HTTPX


__all__ = [
    "SIDECAR_VERSION",
    "build_clients",
    "build_local_services",
    "create_app",
    "default_workspace",
    "pin_local_deployment",
]

SIDECAR_VERSION = "0.2.0"

#: 本地这一侧的"会话 id"：**没带会话 id 的那一轮**（例如老调用方、烟测脚本）落在它上面 ✓，
#: 用来定位沙箱目录与计划门闸的格子 ✓。M2 阶段 3 起会话真的在本机库里，所以它**不再
#: 是"上报用的 id"** ✗ —— 带了这个 id 的一轮不会写进任何会话（`_record_turn` 会如实报
#: "没带会话 id，本轮未落库" ✓，见那里的说明）。
LOCAL_CONVERSATION = "sidecar"

#: 「本机工作区」在工作区表里的**替身 id**（见 `_LocalConversations` / `_LocalWorkspaces`）：
#: 边车那个 `--workspace` 目录不是一条工作区记录（壳没给它建记录），而
#: `resolve_roots` 要的是一个 id。带 `sidecar:` 前缀是为了**一眼看出它不是真记录**。
LOCAL_WORKSPACE_ID = "sidecar:workspace"

#: 默认工作区（用户目录下，**不写系统目录** ✗）。
DEFAULT_WORKSPACE = Path.home() / ".kylab" / "workspace"

#: 系统目录前缀：指定到这些下面一律拒绝 ✗（宁可起不来，也不让工具在系统目录里跑）。
FORBIDDEN_ROOTS = (
    "c:\\windows",
    "c:\\program files",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/system",
    "/var",
)


def pin_local_deployment(
    data_dir: Path,
    *,
    server_url: str = "",
    token: str = "",
    kb_url: str = "",
    kb_token: str = "",
    device_id: str = "",
) -> None:
    """**入口自己钉死档位**（M2 §4.1）——三个环境变量，一个都不能省。

    - ``KYLAB_DEPLOYMENT=local``：不给环境继承的机会 ✗。"边车误连服务器库"是最糟的失败
      形态（不报错，只是把数据写进**另一个库**），所以档位由入口说了算；
    - ``KYLAB_DATABASE_URL=""``（**设空串、不是 delenv** ✗）：本机 `.env` 里真的配了这条
      （开发机就是这么干的），而本机档**不接受**它——`Settings` 的校验器见到它会在启动的
      第一秒直接抛（"两个真相源"那条）。`Settings` 里环境变量优先于 `.env`，所以只有
      设成空串才压得住它；`delenv` 会让它从 `.env` 里"复活"（`tests/conftest.py` 的
      S3 那三个变量是同一手法）；
    - ``KYLAB_DATA_DIR`` / ``KYLAB_SERVER_URL`` / ``KYLAB_TOKEN``：本次这一档的落点与
      远端两头。**从入口的参数来**（壳传的 `--data-dir` / `--server` / `--token`），
      不给它们的话库会建在 cwd 下的 `./data`（与用户看到的"我的数据"不是一处）；
    - ``KYLAB_KB_URL`` / ``KYLAB_KB_TOKEN``（M3 §4.1）：**知识库提供者**那两头的覆盖
      （排障与多 NAS 入口）。**有值才设**——它们是"覆盖"，而空串会**顶掉**环境或
      `.env` 里已有的值（"没传"与"显式置空"在引导级不是一回事：后者要清掉得改 `.env`）。
      默认档一个字都不用填：提供者客户端按"权威 + 覆盖"解析，
      ``KYLAB_SERVER_URL`` / ``KYLAB_TOKEN`` 就是它的继承源。
    - ``KYLAB_DEVICE_ID``（M5 阶段 4）：**这台机器的设备身份**，壳生成、壳传
      （``--device-id``）。与 ``kb_url`` 同一条"**有值才设**"：它是引导级的事实，
      没传就是没有（本机档随后如实拒绝打快照，R12——**绝不自动编一个**）。

    顺带清掉两个单例缓存：档位是**进程启动时定一次**的东西（`get_settings` /
    `get_stores` / `get_services` 都是 `lru_cache`），而"先有人问过档位、再钉档"
    在测试里是常态 —— 不清缓存就会拿到按**旧**环境变量建出来的单例
    （那正是"边车误连服务器库"的同一条错误路径，只是发生在内存里）。
    """
    os.environ["KYLAB_DEPLOYMENT"] = "local"
    os.environ["KYLAB_DATABASE_URL"] = ""
    os.environ["KYLAB_DATA_DIR"] = str(data_dir)
    if server_url:
        os.environ["KYLAB_SERVER_URL"] = server_url
    if token:
        os.environ["KYLAB_TOKEN"] = token
    if kb_url:
        os.environ["KYLAB_KB_URL"] = kb_url
    if kb_token:
        os.environ["KYLAB_KB_TOKEN"] = kb_token
    if device_id:
        os.environ["KYLAB_DEVICE_ID"] = device_id
    reset_services()
    reset_stores()
    get_settings.cache_clear()


def default_workspace() -> Path:
    """默认工作区目录 —— **按可写性逐级回退** ✓（P3 烟测抓到：`~/.kylab` 建不出来 ✗）。

    实测（2026-09-29 真烟测）：在这台机器上 `Path.home()/".kylab"` 直接
    `PermissionError: [WinError 5]` ✗ —— 用户目录并不总是可写的（受限配置、
    受管终端、沙箱策略都会这样 ✓）。而边车**必须能起来** ✓，所以按顺序试：

    1. `~/.kylab/workspace`（首选 ✓，用户看得见、好备份 ✓）；
    2. `%LOCALAPPDATA%\\kylab\\workspace`（Windows）/ `$XDG_DATA_HOME`（*nix）✓；
    3. `%LOCALAPPDATA%\\Temp\\kylab-workspace`（Windows）/ 系统临时目录（*nix）✓
       —— **两个问题分开** ✗：`tempfile.gettempdir()` 在受管终端里可能指向**仓库目录**
       （本会话实测就是 `E:\\gitlab\\kylab\\backend` ✗）→ 落在仓库里的工作区会被 git 看见 ✗、
       也会跟着仓库被清掉 ✗，所以 Windows 上**显式用 `%LOCALAPPDATA%\\Temp`** ✓，
       并且**任何落点只要在仓库根之下就跳过** ✗（`_inside_repo` ✓）。

    三处都写不了才抛 ✗（那时如实报出来，而不是假装起来了 ✓）。
    """
    candidates: list[Path] = [DEFAULT_WORKSPACE]
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        candidates.append(Path(local) / "kylab" / "workspace")
    import tempfile

    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Temp" / "kylab-workspace")
    candidates.append(Path(tempfile.gettempdir()) / "kylab-workspace")

    problems: list[str] = []
    for candidate in candidates:
        # **绝不落在仓库里** ✗（临时兜底最容易踩：TEMP 常被指到工程目录 ✓）
        if _inside_repo(candidate):
            problems.append(f"{candidate}（在仓库根之下，跳过 ✗）")
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{candidate}（{type(exc).__name__}: {exc}）")
            continue
        return candidate
    raise RuntimeError("找不到可写的工作区目录：" + "；".join(problems))


def _inside_repo(path: Path) -> bool:
    """这个路径是不是落在**仓库根**之下（`backend/` 的上一级 ✓）。

    判据用 `app/sidecar.py` 自己的位置往上一级推 ✓ —— 不依赖 cwd ✗、
    也不依赖用户把仓库放在哪儿 ✓。
    """
    repo_root = Path(__file__).resolve().parents[2]
    try:
        return path.resolve().is_relative_to(repo_root)
    except OSError:
        return False


class _LocalModel:
    """**本机档的模型直连**（2026-10-04 产品负责人拍板：推理搬回本机）。

    在这之前本机档的对话链把模型请求转给 NAS 的 `/model-proxy`（`RemoteModelClient`：
    请求出本机、由 NAS 持 key 发出去）。现在**本机自己持 key 直连模型服务** ——
    用的就是进程内那条链一直在用的那份客户端（`llm.OpenAICompatChat` ✓，
    服务器档的 `/model-proxy` 也是它的包装，见 `api/v1/model_proxy.py::_chat`）：
    **不重写、不加依赖** ✗。

    **为什么是"每建一次客户端"而不是构造时建一个**：模型身份（地址 / key / 模型名）
    与采样参数都从 `runtime.llm()` 现取，而那份运行期配置是**设置页可改**的
    （本机档的设置就在本机库 `app_settings` 里）。构造时定一份会让"改完设置要重启边车"
    变成一条没人记得的隐含前提，与 `provider`（知识库那一头）同一条口径。
    建对象本身廉价，昂贵的是 HTTP 调用。

    **key 从哪来**：`model_providers` 那一行的凭据。本机档（Windows）上它由
    `services/secrets.use_keychain` 判定为"钥匙串是凭据的家"——注册表服务读的时候
    从 Windows 凭据管理器取（`kylab:model_provider:<id>`），**库里那一列是空的** ✗
    （见 `services/model_registry.py` 的 `_with_key` / `_for_db`）。
    所以这里一个字都不碰凭据：**没有把 key 带进日志、也没有第二处凭据来源** ✗。

    **失败路径如实报** ✗（两道，都带下一步动作）：
    - 还没配（没绑「对话生成」/没填 key）→ ``ChatError(reason="not_configured")``，
      与服务器那条链**同一句话**（`services/chat.py::_config`）；
    - 配了但上游拒（401 / 404 / 5xx）→ `OpenAICompatChat` 自己抛的 `ChatError`
      （"对话端点鉴权失败（401）：请检查 API Key"这类），照原样上抛。
    两者都被 `/turn` 与 `/turn/stream` 收下、变成一句人话 ✗ 不是空回答、也不是 500 ✗。
    """

    def __init__(self, runtime: RuntimeConfigService) -> None:
        self._runtime = runtime

    def _chat(self) -> OpenAICompatChat:
        """现取一次运行期配置 → 一个直连客户端（没配就在这一行报可读的错）。"""
        config = self._runtime.llm()
        if not config.is_configured:
            # 措辞与 `services/chat.py::_config` 逐字一致：同一个"没配"在两个装配点上
            # 必须说同一句话，否则用户会把同一种状态当成两种毛病
            raise ChatError(
                "尚未配置对话模型，请到设置 → 模型配置里填写 API Key 与模型 ID",
                reason="not_configured",
            )
        return OpenAICompatChat(config)

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        return self._chat().complete(messages)

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        return self._chat().stream(messages)

    def stream_events(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None
    ) -> Iterator[LLMDelta]:
        return self._chat().stream_events(messages, tools)


class _LocalConversations:
    """`resolve_roots` 会问"这条会话挂在哪个工作区"（`services/agent_files.py`）。

    M2 阶段 3 起会话**真的在本机库里**，所以真记录优先 ✓：用户在某个项目里开的会话，
    它的文件面就是那个项目（`services.workspaces` 是"项目在哪儿"的权威）。

    这一层只补**一个**缺口：**没挂工作区**的会话（桌面端不选项目也能开一轮）。
    照真记录走的话，`resolve_roots` 会给出"只有沙箱、没有工作区"——而在本机档，
    边车明明有一个用户看得见的本机工作区（`--workspace`，壳传的是 `<数据目录>/workspace`），
    P3 起它一直是"边车干活的地方"✗。真记录**没有工作区**时落回它 ✓：
    "没挂工作区 = 只有沙箱"在服务器档是对的（会话本来就该挂在项目上），
    在本机档却会让"读我工作区里的文件"这类请求全部失败 ✓ —— 那是**能力的回退**，
    不是本方案要改的东西。
    """

    def __init__(self, conversations: Any, fallback_workspace_id: str) -> None:
        self._conversations = conversations
        self._fallback = fallback_workspace_id

    def get(self, conversation_id: str) -> Any:
        try:
            record = self._conversations.get(conversation_id)
        except NotFoundError:
            # 这条会话本机库里没有（例如没带会话 id 那一轮用的 `LOCAL_CONVERSATION` 占位）
            # → 与"没挂工作区"同一支：这一轮老老实实落回本机工作区 ✓
            return SimpleNamespace(workspace_id=self._fallback)
        return record if record.workspace_id else SimpleNamespace(workspace_id=self._fallback)

    def __getattr__(self, name: str) -> Any:
        """其余方法**原样转给真服务** ✓（这一层只补 `get` 那一处，不当"影子服务"）。

        写库（`record_turn` / `ensure_title`）、列会话（`list`）都要走真实现 ——
        少转一个方法，表现就是"属性不存在"这类一眼看得见的错（比静默走错实现好 ✓）。
        下划线开头的一律不转（`_conversations` 自己就走这里，转下去会递归 ✗）。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._conversations, name)


class _LocalWorkspaces:
    """工作区记录（`resolve_roots` 只读 `root_path` ✓）。

    **本机工作区**（`--workspace`）那个替身 id 由这一层答（它不是一条记录）；
    其余工作区 id 走真服务 ✓ —— 那才是"用户选的项目在哪"的权威。
    """

    def __init__(self, workspaces: Any, root: Path, fallback_id: str) -> None:
        self._workspaces = workspaces
        self._root = root
        self._fallback = fallback_id

    def get(self, workspace_id: str, **kwargs: Any) -> Any:
        # **签名要跟着真服务走** ✗（2026-09-30 踩过）：v0.59 起 `get` 多了设备那一维 ✓，
        # 而 `resolve_roots` 是**服务器与边车共用**的那段代码 ✓ —— 这里少一个关键字
        # 参数，边车里每一次"按工作区读文件"都会 TypeError ✓，表现成工具
        # `outcome: failed`（用例：`test_turn_really_runs_a_tool_in_the_local_workspace` ✓）。
        # 所以这一层收 `**kwargs` 并**原样转给真服务** ✓（本机工作区那一条上没有可判的两维）。
        if workspace_id == self._fallback:
            return SimpleNamespace(root_path=str(self._root))
        return self._workspaces.get(workspace_id, **kwargs)


def build_local_services(base: Services, *, workspace: Path) -> Services:
    """边车这一侧的 `Services`：**就是本机档的组合根那一份**，只补两个截面（M2 阶段 3）。

    与旧版"影子 Services"（只有十几个属性、缺的一律没有）的区别在这里：现在
    **会话 / 消息 / 事件 / 产物 / 笔记 / 记忆 / 设置 / 工作区 / 技能 / 插件 / MCP
    全都是 `get_services()` 那一份真服务**（本机 SQLite + 本机目录），而它与本机后端
    端点是**同一个对象** —— 同一张审批登记表、同一份运行期配置、同一批会话、
    **同一份技能与 MCP 清单**。于是"边车这一轮跑出来的账"与"界面上读到的账"必然是
    同一份 ✓（旧版做不到这件事：会话在服务器上）。

    **2026-10-04 起这里不再盖掉任何东西**：技能目录、插件目录与 MCP 配置的权威
    本来就在本机（`<data_dir>/skills`、`<data_dir>/plugins`、仓库 `skills/`、
    `~/.agents/skills`，启停状态写在**本机** `app_settings`），而 `local_router`
    也早就把 `/skills`、`/plugins`、`/mcp-servers` 挂在了**同一个进程**上
    （见 `api/v1/router.py` 的 `local_router` 那一段）。所以边车的工具面这一侧
    不该再是"如实回空"：那会让界面里看得见、agent 手里摸不着的两副面孔并存。
    此前那两处（`_EmptySkills` / `_NoMcp`）已随这一条结论删除 ✗。

    `ingest` / `documents` 那两处**不在这一层**（M3 阶段 3）：本机档的入库
    整条换线在**组合根一处**（`core/services.py::build_services` 里 `ingest_for_kb` /
    `enqueue_documents` 那一对）——因为 `notes` / `artifacts` 各自持有一份真
    `IngestService`，`dataclasses.replace` 换不到它们对象内部的引用（方案 §5.1）。
    所以这一层现在只补"边车特有的两个截面"，接缝换线只有一个落点。

    那两处**不是"换掉"而是"补一侧"**（真记录仍优先，见各自的类说明）：

    - `conversations`：真会话优先；**没挂工作区**的会话落回本机工作区；
    - `workspaces`：本机工作区那个替身 id 由这一层答，其余走真服务。

    `api_keys` 不再是假的 ✓（旧版是个恒放行的 `_ApiKeySeam`）：本机档的调用主体是
    "本机主人"（`api_key.LOCAL_CALLER`，`is_admin=True`），而 `check_access` 对管理员
    就是直接放行（见那里的第一行）——用真服务比用一个"永远返回 None"的壳更老实，
    也不会有两套权限语义。
    """
    return dataclasses.replace(
        base,
        conversations=_LocalConversations(
            base.conversations, fallback_workspace_id=LOCAL_WORKSPACE_ID
        ),
        workspaces=_LocalWorkspaces(base.workspaces, workspace, fallback_id=LOCAL_WORKSPACE_ID),
    )


#: 这一轮边车**真的能服务**的工具 ✓（其余不摆给模型 ✗ —— 摆上去只会撞一句"内部错误" ✗）。
#:
#: - `search` 走**远端 KB** ✓（只有这一轮给了 `kb_ids` 才出现 ✓，与服务器同口径 ✓）；
#: - 文件三件 + `run_command` 是**本地执行的主体** ✓（沙箱与隔离探测都在本机 ✓）；
#: - 联网两件在 `tools.py` 里实现 ✓，不依赖仓储 ✓；
#: - 技能两件**走本机技能目录** ✓（2026-10-04 起：`list_skills` 列真的技能、
#:   `read_skill` 读得到正文；启停状态读本机 `app_settings` 的 `chat.disabled_skills` ✓）；
#: - **导出三件**（2026-09-30 起 ✓，**M2 阶段 3 改落本机** ✓）：产出物**本地生成、
#:   落本机对象存储**（`ArtifactService.save`：挂了工作区就落用户的真实目录）——
#:   这是"要一份文件"那一类请求在**桌面与本机跑**的链上唯一缺过的一环 ✓。
#:   阶段 3 之前它上传到服务器的会话文件区；现在**账与文件都在本机** ✓
#:   （文件的预览/下载走本机后端的 `/conversations/{id}/files*` ✓）。
#: - **笔记三件**（`create_note` / `attach_note_to_kb` / `list_notes` 2026-10-01 ✓，
#:   **阶段 3 改落本机** ✓）：笔记就是本机库里的笔记（`NotesService` ✓）——
#:   `create_note` / `list_notes` 读写本机；`attach_note_to_kb` 要**知识库**，
#:   而知识库在 NAS 上：正文从本机库读出来，**经提供者客户端上传**（multipart、
#:   `start=true`、NAS 回文档 id 再回填到本机笔记上，M3 阶段 3 打通，见方案 §5.2）✓。
#:   提供者没配/连不上时它**如实失败**（`KnowledgeBaseUnavailable` 那句，含原因与下一步）✗。
#: - **记忆两件**（`recall` / `remember` 2026-10-01 ✓，**阶段 3 改落本机** ✓）：
#:   记忆本体本来就在 `data_dir/memory`（本机）✓ —— 现在读写的也是本机那份
#:   `MemoryService`（`remember` 不看开关；`recall` 受记忆开关门控，默认关，
#:   关着时它**明确报错**而不是回空 ✓）。
#:   `read_memory` / `write_memory`（改人设文件那两个）**仍留给 P4** ✗。
#: - **`ingest_file`**（2026-10-01 ✓）：**边车读本机 → 上传进知识库**（走提供者客户端 ✓）——
#:   "把我这台机器上的某份文件放进库"本来只差一个上传口；字节恰好在这侧 ✓。
#:   受库开关门控（`_LOCAL_KB_TOOLS`：对话里没选库就不摆 ✓），
#:   **M3 阶段 2 起还受提供者状态门控**（`state != ready` 时与 `search` 一起摘掉 ✓）。
#:
#: 表格读取（`list_tables` / `query_table` 要服务器侧的结构化副本与 SQL 面）→
#: 仍**留给 P4** ✓。
SIDECAR_TOOL_NAMES = frozenset(
    {
        "search",
        "list_files",
        "read_file",
        "search_files",
        "run_command",
        "web_search",
        "web_fetch",
        "list_skills",
        "read_skill",
        "export_document",
        "export_table",
        "export_deck",
        "create_note",
        "attach_note_to_kb",
        "list_notes",
        "recall",
        "remember",
        "forget",
        "ingest_file",
    }
)

#: `mcp__<服务>__<工具>` 那一族**不在上面的名单里**（名字由外部服务定，写不进 frozenset），
#: 所以按**限定名的形状**放行（2026-10-04）：判据复用 `mcp_client.split_qualified`
#: ——"什么算外部工具名"只有那一处实现 ✗（在这里自己写一遍 `startswith("mcp__")`
#: 就是第二处规则，前缀一改就漏）。
#:
#: 这一条与 `build_local_services` 里"不再盖掉 `mcp`"是**同一件事的两半**：
#: 光把 `services.mcp` 换成真服务，`agent_tools._mcp_specs` 产出的工具仍会被上面那张
#: 白名单筛掉 ✗ —— 表现就是"配置页里那三台 MCP 服务好好的，agent 手里一个 `mcp__*` 都没有"。

#: 提供者**不 ready 时一个都不摆**的工具（方案 §3.2 的"失败降级"那一行，阶段 2 落地）。
#:
#: 判据是"它有没有真的用到知识库"：`search`（检索）、`attach_note_to_kb`（笔记入库）、
#: `ingest_file`（本机文件入库）——三件都要那台 NAS。不 ready 时按"这台机器没有这项能力"
#: 处理，与 `_KB_TOOLS` 的"关了就不摆"**同一条纪律**：给了又拒只会白花一个来回
#: （模型先看一眼有哪些库、再检索一次被拒）。
#:
#: 三个工具名与 `SIDECAR_TOOL_NAMES` 一起维护：改前者就要回头看这里（那份名单里
#: 也只有这三个真的碰知识库——`list_notes` / `create_note` 是纯本机写的笔记）。
KB_PROVIDER_TOOLS = frozenset({"search", "attach_note_to_kb", "ingest_file"})


#: 边车这一侧的**最小**系统提示词（**不是**服务器那一份 ✗）。
#:
#: 服务器那份要 `Services`（知识库范围、技能目录、人设、记忆 ✓），边车这一轮没有那些 ✓；
#: 但**必须有一句"直接动手"** ✗ —— 真烟测实测（2026-09-29）：没有它时模型回的是
#: 「我先读一下这个文件。」**一个工具都没调** ✗，于是"工具真的执行"在真模型上根本不发生 ✓。
#: 完整提示词与 P4 的会话口径一起接 ✓。
#:
#: **交付口那一句（2026-09-30 加）**：导出三件在工具表里之后，不点名它照样不用 ✗——
#: 实测同一句"生成 sales.xlsx"（表里已有 export_table）它仍回"要跑命令才能落盘"，
#: 而服务器那份提示词里对等的第 10 条是**点名**的 ✓。"网页交得了、桌面交不了"，
#: 差的就是这一句。
#:
#: **"缺素材按已知写"半句（2026-09-30 同批加）**：点名交付口之后仍有一次失败 ——
#: 一句"做一份 3 页 PPT"（原句，没别的提示）它**先去工作区翻素材**（搜文件、列目录、
#: 试图读一份 docx、最后 `run_command unzip` 撞上执行闸），整轮**没交付** ✗。
#: 把同一句改成"直接用你已知的知识写、不需要翻文件"就一次到位 ✓ —— 差的正是这半句。
#:
#: **"笔记两件"半句（2026-10-01 加）**：与交付口同一课——`create_note` 进了工具表，
#: 不点名它模型仍然想不到（"记笔记"是一件服务器上的事，它默认自己够不着 ✗）；
#: `attach_note_to_kb` 是同一句话的第二步（"记下来 → 以后 `search` 搜得到"），
#: 但它**受库开关门控**（`_KB_TOOLS`：对话里没选知识库时它根本不在表里 ✗）——
#: 所以那半句带前提（"对话里选了知识库时"）。实测（2026-10-01 L1 验收）：
#: 无条件点名时，没选库的会话里模型会花半段回答解释"我这里没有这个工具" ✗。
#:
#: **"记忆两件"半句（2026-10-01 同批加）**：同一课——`recall` / `remember` 进了工具表，
#: 不点名时模型会把"帮我记住 X"往 `create_note` 上带 ✗（"记住"听起来像记一条东西），
#: 或者干脆说这台机器上没有记忆 ✗。它们不受库开关门控（受记忆开关，而**开关在本机了**：
#: 阶段 3 起读写的都是本机那份 `MemoryService` ✓，恒摆 ✓）——所以这半句**不带前提** ✓。
#: ⚠️ `recall` 在**打包运行时**里会因为缺 jieba 而失败（阶段 3 发现的已知缺口，
#: 见 `requirements-sidecar.txt` 第 5 节）—— 这半句先留着，等那件事定了再一并调 ✗。
SIDECAR_SYSTEM_PROMPT = (
    "你是这台电脑上的本地 Agent。**需要本机信息时（读文件、列目录、搜文件、跑命令、查网页）"
    "必须先调用对应工具**：list_files / read_file / search_files / run_command / "
    "web_search / web_fetch（技能用 list_skills / read_skill）——"
    "**不许只用文字描述你将要做什么** ✗（「我这就去读」「我马上跑」都算没做）。"
    "拿到工具结果之后再作答。"
    "**对方要一份文件时**（「给我一份」「发我个 .docx / .xlsx」「能下载的」）**用交付口**："
    "export_document（正文类）/ export_table（表格）/ export_deck（幻灯）——"
    "文件会挂到对话里、他点一下就能拿到；**只把内容贴在正文里不算交付** ✗。"
    "**留档用 `create_note`**（存进对方的笔记列表，服务器上能看到）；"
    "**对话里选了知识库时，再调 `attach_note_to_kb` 让笔记能被检索到**——"
    "**这两个都不是交付**：对方要的是文件时仍然走交付口 ✗。"
    "**对方让你「记住」的偏好与约定用 `remember`**（之后每轮对话都会带上）、"
    "**翻过去的结论用 `recall`**——这两件是**长期记忆**，别用 `create_note` 顶 ✗。"
    "**缺素材就按你已知的写**，不要为了找素材去翻文件 / 扫盘 / 跑命令 ✗ ——"
    "先把东西交出去，再问他要不要按真实口径改。"
    "工具报错或被拒绝时**如实转述**（连同理由），不要假装成功、也不要编结果；"
    "本机没有你要的工具时**直说没有**，不要编造。"
    "回答用简体中文。"
)


def _sse(payload: dict[str, Any]) -> str:
    """一条 SSE 事件。**形状与服务器那条链逐字一致** ✓（见 `api/v1/chat.py` 模块头那五行）：

    ``data: {"type":"step",…}`` / ``{"type":"thinking","text":…}`` / ``{"type":"delta","text":…}``
    / ``{"type":"sources","items":[…]}`` / ``{"type":"done","answer":…}``
    / ``{"type":"error","message":…}`` ✓
    —— 前端解析那一侧**同一套** ✓，所以这里**不另创形状** ✗（也不带 `seq`：边车这一侧没有会话
    事件日志，没有可补发的地方 ✓）。

    `sources` 那一条是 **M3 阶段 7 真机验收补上的**（见 `_sources_payload` 的说明）：
    知识库提供者接上之后，本机这一侧才第一次真的会检索出出处 ✓ —— 在那之前
    `SourcesEvent` 根本不会产生，所以少这一条也没人看得出来 ✗。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


#: SSE 的媒体类型（与服务器/壳那侧同一口径 ✓）。
SSE_MEDIA_TYPE = "text/event-stream"


def _close_trailing_answer_step(steps: list[dict[str, Any]]) -> None:
    """把"回答"那一步收尾成 `done` ✓（**就地改** `steps`）。

    为什么（2026-09-29 烟测）：`tool_loop` **从不给"组织回答"这一步发 `done`**（服务器那条链路
    靠前端收敛 ✓）——所以 `/turn` 的响应里它一直是 `status:"running"` ✗，调用方会以为
    "还在跑" ✗。而 `/turn` 是**一次请求一次答复**：返回时这一轮**已经结束** ✓，
    所以这里把最后那一步如实收掉 ✓。只动边车这一侧的响应形状 ✗，循环本体一个字不改 ✓。
    """
    for step in reversed(steps):
        if step["phase"] == "answer":
            step["status"] = "done"
            return


def _step_payload(event: StepEvent) -> dict[str, Any]:
    """`StepEvent` → 响应里的 dict ✓（字段与服务器那条链路同一套名字 ✓）。

    **产出物要带出去**（2026-09-30，与导出三件同一批）：导出类工具把文件挂在
    `event.artifacts` 上，界面靠它画那张"点一下就能拿到"的卡片 —— 原来这里没抄这个字段 ✗，
    于是边车交付的文件在对话里**看得见结果、点不到卡片** ✗（服务器侧那份 `snapshot` 是带的 ✓）。
    `kind`（图标）/ `degraded`（降级横幅）/ `added`（资料条数）同理，一并对齐 ✓。
    """
    return {
        "phase": event.phase,
        "label": event.label,
        "detail": event.detail,
        "status": event.status,
        "tool": event.tool,
        "outcome": event.outcome,
        "args": event.args,
        "result": event.result,
        **({"kind": event.kind} if event.kind else {}),
        **({"degraded": True} if event.degraded else {}),
        **({"added": event.added} if event.added is not None else {}),
        **({"artifacts": [dict(item) for item in event.artifacts]} if event.artifacts else {}),
    }


def _sources_payload(refs: Sequence[Any]) -> list[dict[str, Any]]:
    """这一轮的**出处快照** → 线上形状（服务器那条链的 `_sources_out` 是同一件事）。

    **为什么要有它**（M3 阶段 7 真机验收抓到的真缺口）：本机这一侧原先**没有**
    `sources` 那一条事件 ✗ —— 与 M2 时期"本机档没有知识库可检索"是一致的（那时
    `SourcesEvent` 根本不会产生 ✓）。M3 把提供者接上之后这条链第一次真的会检索 ✓，
    于是表现变成：工具步里写着「命中 8 段原文」✓、模型的回答里也带 `[1]` ✓，
    而**界面的出处面板与那串编号点不动** ✗（前端 `chat.ts` 的 `sources` 分支收不到东西 ✓）。

    形状**不手抄**：过 `ChatSourceOut` 过一道 ✓ —— 它就是服务器那条链回给前端的模型
    （`ChatSourceOut.model_validate(source)` ✓，`SourceRef` 是 slots dataclass，
    `from_attributes` 直接吃得下 ✓）。手写一份 dict 的话，字段一改就是两处漂 ✗。

    `mode="json"`：与 `chat.py::_record_turn` 存快照那一份**逐字同一口径** ✓
    （存进本机会话的 `sources` 也是它 ✓），时间/枚举之类在这里一次定死 ✓。
    """
    return [ChatSourceOut.model_validate(ref).model_dump(mode="json") for ref in refs]


def _approval_payload(event: ApprovalEvent) -> dict[str, Any]:
    """一条待确认 → SSE 载荷（字段与**服务器那条链逐字对齐** ✓）。

    照 `api/v1/chat.py` 那段 `{"type": "approval", …}` ✓：`approval_id` / `tool` / `label` /
    `args` / `detail` / `rule` / `timeout_seconds` ✓ —— 界面那三个按钮（允许一次 / 这类都允许 /
    拒绝）与"这类都允许会写下哪条规则"全都靠这几个字段 ✓，少一个就得在前端另做推断 ✗。

    **不是"过程里的一步"** ✗：过程快照记的是"这一轮做过什么"，而这是一句**还没被回答**的问题 ✓
    （服务器那条链也是这么分的 ✓）。所以它**不进 `steps`** ✗ —— 进了就会跟着这一轮写回服务器，
    用户回看历史时会冒出一条永远等不到人点的确认 ✗。
    """
    return {
        "approval_id": event.approval_id,
        "tool": event.tool,
        "label": event.label,
        "args": event.args,
        "detail": event.detail,
        "rule": event.rule,
        "timeout_seconds": event.timeout_seconds,
    }


def _skills_note(clients: Clients) -> str:
    """技能目录那一条：**当场数**，数不出来就如实说数不出来 ✓。

    `SkillService.list()` 带上"被用户关掉的"（它们 `used_by_prompt=False`），
    所以这里把两个数都报出来 —— 只说一个"共 N 个"会让"关掉一个之后 agent 还看得见"
    这件事变得没法判断 ✗。**读失败不拦这一轮**（技能目录读不出来不该让对话起不来）：
    那句话说清楚"没数出来"并与"真是 0 个"分开 ✗（两者混起来正是最误导的形态）。
    """
    try:
        records = clients.services.skills.list()
    except Exception:  # 目录读不出来：如实说，不假装没有技能
        logger.warning("读本机技能目录失败（只影响这一句说明）", exc_info=True)
        return "本机技能目录这次没读出来（详情见边车日志）——不代表没有技能。"
    usable = [item for item in records if item.used_by_prompt]
    off = len(records) - len(usable)
    detail = f"，其中 {off} 个被用户停用" if off else ""
    return (
        f"技能目录在本机（{len(records)} 个技能{detail}）："
        "`<数据目录>/skills`、仓库自带的 `skills/`、`~/.agents/skills` 三处都扫，"
        "启停状态存在本机设置里（`chat.disabled_skills`）——agent 侧用 "
        "`list_skills` / `read_skill` 读的就是这一份。"
    )


def _mcp_note(clients: Clients) -> str:
    """外部 MCP 服务那一条（**只查本机库里的配置**，不去连它们 ✓）。

    为什么不在这里调 `available_tools`：那会把"这一句话"变成"给每台服务一次握手"，
    而没连上的服务每次都要等一个超时 ✗ —— `/turn` 的说明文字不该拖慢这一轮。
    工具表那边该连的已经连过了（`tool_specs` → `_mcp_specs`，它自带 5 分钟缓存）。
    """
    try:
        records = clients.services.mcp.list(user_id=None)
    except Exception:
        logger.warning("读本机 MCP 配置失败（只影响这一句说明）", exc_info=True)
        return "本机 MCP 服务清单这次没读出来（详情见边车日志）。"
    enabled = [item for item in records if item.enabled]
    if not records:
        return "本机没有登记外部 MCP 服务（配了就会以 `mcp__<服务>__<工具>` 出现在工具表里）。"
    return (
        f"外部 MCP 服务 {len(records)} 台（{len(enabled)} 台启用）："
        "启用的那些的工具以 `mcp__<服务>__<工具>` 摆进工具表，"
        "调用前仍受服务自己的策略（允许 / 要确认 / 拒绝）约束。"
    )


def _notes(clients: Clients) -> list[str]:
    """响应里那几句**如实说明** ✓（"这一侧有什么、没有什么" ✗ 不假装有 ✓）。

    三句话各自回答一个排障问题：**技能在哪**（`_skills_note`）、
    **外部工具有没有**（`_mcp_note`）、**数据和模型落在哪一头**。
    技能与 MCP 两句是 2026-10-04 接上真服务之后补的 —— 在那之前这里写的是
    "本机无内置技能目录"（那时确实没有）。
    """
    return [
        _skills_note(clients),
        _mcp_note(clients),
        f"本地执行：工作区 {clients.workspace}；沙箱在 {clients.data_dir / 'sandbox'} 下。",
        "模型在本机直连（key 在本机；本机没有可用的对话模型时会明确报「没配」）。"
        f"这一轮的账落在本机库（{clients.data_dir / LOCAL_DB_NAME}）；"
        "知识库（检索与入库）在 NAS 上。",
    ]


def _check_workspace(raw: str | None) -> Path:
    """解析工作区：**系统目录一律拒绝** ✗（用户目录下怎么放都行 ✓）。"""
    path = Path(raw).expanduser().resolve() if raw else default_workspace()
    text = str(path).replace("/", "\\").lower() if os.name == "nt" else str(path).lower()
    if os.name == "nt":
        text = text.replace("\\", "/")
        for root in FORBIDDEN_ROOTS:
            marker = root.replace("\\", "/")
            if text == marker or text.startswith(f"{marker}/"):
                raise ValueError(f"工作区不能是系统目录：{path}")
    else:
        for root in FORBIDDEN_ROOTS:
            if text == root or text.startswith(f"{root}/"):
                raise ValueError(f"工作区不能是系统目录：{path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


class Clients:
    """**装配点**：KB 远端 + 模型本机直连 + 本地那一侧（P3）✓

    （服务器模式的装配在 `core/services.py` ✓。）

    | 件 | 边车这一侧 | 怎么来 |
    | --- | --- | --- |
    | KB（检索 + 入库）| **远端** ✓ | `KnowledgeProviderClient` ✓（M3 阶段 3 起整条在组合根上）|
    | 模型 | **本机直连** ✓（2026-10-04 起）| `_LocalModel` → `OpenAICompatChat` ✓（key 在本机 ✓）|
    | 循环 / 工具 / 沙箱 / 审批 | **本地** ✓ | `ToolLoop` + `build_runner` ✓（同一份代码 ✓）|
    | 会话 / 产物 / 笔记 / 记忆 / 设置 | **本地** ✓（阶段 3）| `get_services()` 那一份 ✓ |
    | 技能目录 / 插件 / MCP | **本地** ✓（2026-10-04 起工具面也接上）| `get_services()` 那一份 ✓ |
    | 入库 | **远端** ✓ | 组合根换线那一处（`core/services.py`，知识库在 NAS）✓ |

    ⚠️ **本类手上那个 provider 就是服务图里跑着的那个**（M3 阶段 5 收成一个实例）：
    服务图的提供者客户端由**组合根**建（`core/services.py::build_services`，本机档那一段，
    挂在 `Services.provider` 上），`ChatService` 的检索、笔记与产物的入库、
    `Services.ingest`、`stores.meta.kb` 的 reader、`/local/provider` 端点与**本类**的
    `self.provider` / `self.knowledge` 全从**它**取。于是全进程只有一份 30s 握手缓存：
    工具表门控与状态端点不可能各答一半（"端点说 ready、工具表说不 ready"这类不一致
    在阶段 2/3 是能发生的——那时是两份缓存）。
    `provider=` 那个入参仍是**用例的注入接缝**（塞一个假实现进去验三态门控），
    给了它本类就用手上这一份、**换不动服务图里那一个**——那是用例的形态，不是运行形态。

    `model=` 同理是**用例的注入接缝**（假模型，避免打真网络）：给了就用它，
    不给就是 `_LocalModel`（本机直连）。**它不再默认打 NAS 的 `/model-proxy`** ✗
    —— 那一条（`RemoteModelClient`）留在 `services/remote_clients.py` 里，
    服务的是"客户端不带 key、由服务端代发"的那些部署，本机档不用它。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        workspace: Path,
        data_dir: Path,
        knowledge: Any = None,
        model: Any = None,
        provider: KnowledgeProviderClient | None = None,
        services: Services | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        #: 用户会话令牌：**知识库那一头**要它（模型这一头不要——key 在本机 ✓）。
        self.token = token
        #: 打服务器自己的健康端点（探活很便宜，不占用模型的额度 ✓）
        self.health_url = self.base_url.rsplit("/api/v1", 1)[0] + "/api/v1/health"
        self.workspace = workspace
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        # **本地那一侧 = 本机档的组合根那一份**（M2 阶段 3）：
        # 会话 / 消息 / 事件 / 产物 / 笔记 / 记忆 / 设置 / 技能 / 插件 / MCP 全是
        # 本机那份真服务 ✓，而且与挂在同一个 app 上的 `/api/v1/*` 端点是**同一个对象** ✓
        # —— 于是"边车跑完写下的账"与"界面读到的账"必然是同一份。
        # 装配要求：调用方**先**钉死档位（`pin_local_deployment`，见那里的说明），
        # 否则 `get_services()` 会按服务器档去连 PG（那是"误连服务器库"那条路）。
        base_services = services if services is not None else get_services()

        # 模型可注入（用例给假实现 ✓）；**默认是本机直连** ✓ —— 它每建一次客户端就问
        # 一次本机的运行期配置（模型身份与凭据都在本机，见 `_LocalModel` 的说明），
        # 所以放在 `base_services` 之后：那一份运行期配置就是它要读的东西。
        self.model = model if model is not None else _LocalModel(base_services.runtime)

        #: **知识库提供者的客户端**（M3 阶段 2 起有它）。**默认就是服务图上那一个**
        #: （`Services.provider`，阶段 5 收成一个实例）：地址与钥匙**每次调用现取**
        #: （`get_setting` 读的就是下面那份运行期配置），所以设置页改了地址不用重启边车；
        #: 工具表门控（`tool_specs`）问它一次，`/local/provider` 端点问的是同一个对象。
        #: `provider=` 是给用例塞一个假实现的口子（它同时也换了 `self.knowledge`）
        #: ——**它换不动服务图里那一个**，那一条只出现在用例里，见类说明。
        if provider is not None:
            self.provider = provider
        elif base_services.provider is not None:
            self.provider = base_services.provider
        else:
            # 兜底：手工构造的 `Services` 上没有那一格（脚本 / 老用例）。自己建一个，
            # 口径与组合根那一处逐字相同（同一份引导级配置 + 同一个运行期配置）。
            self.provider = KnowledgeProviderClient(get_setting=base_services.runtime.get)
        #: KB 那条接缝在本类这一侧的把手 = **提供者客户端**（它满足 `KnowledgeClient`
        #: 协议的 `retrieve_sources` 签名）。`knowledge=` 这个入参留给"用例塞一个假实现"，
        #: 给了就用它（与模型那一头同一个写法）。
        self.knowledge = knowledge if knowledge is not None else self.provider

        self.services = build_local_services(base_services, workspace=workspace)
        #: 本地运行期配置：**就是本机库 `app_settings` 那一份** ✓（旧版是个本地 JSON
        #: 临时物 —— 设置页改的值与本机后端读的值必须是同一个，见阶段 3 的收编表）。
        #: `sandbox.require_isolation` 的默认值仍是 `"true"` ✓ —— **那道闸没有被绕** ✗。
        self.runtime = self.services.runtime
        #: **整套 `ApprovalRegistry` 带过来** ✓（`ask` 档的行为与服务器逐条一致 ✓），
        #: 而且**就是组合根那一张表**（本机后端的 `/chat/approvals` 与这一侧共用一套）。
        self.approvals = self.services.approvals
        #: 下面这几个给"这一侧有什么"的说明与用例读（真服务可以从 `self.services` 上取）；
        #: `ingest` / `documents` 这两件**就是服务图上那一对**（M3 阶段 3 起）：本机档是
        #: 提供者给的两个窄视图（只有 `submit` / `enqueue_ingest` 那一件事），服务器档是
        #: 真 `IngestService` / `DocumentService`。用例要断言"入库接缝装配成了哪一件"，
        #: 看这两个引用就够（`is` 比身份，见 `tests/unit/services/test_client_seams.py`）。
        self.artifacts = self.services.artifacts
        self.notes = self.services.notes
        self.memory = self.services.memory
        self.ingest = self.services.kb.ingest
        self.documents = self.services.kb.documents

    def model_configured(self) -> bool:
        """**本机**有没有配好对话模型（`/health` 那一格读它，见那里的说明）。

        只看配置（`LLMConfig.is_configured`：有 key、有模型名），**不真发一次请求**：
        健康检查不该花用户的钱、也不该为了探活去撞一次限流。配了但上游会拒（key 无效）
        在真跑一轮时报出来（`ChatError` → `/turn` 的一句人话）。

        ⚠️ 这一条只对**本机直连**成立（2026-10-04 起 `_LocalModel` 是默认）：
        `model=` 注入了假实现（用例）时，这里报的还是本机注册表的状态 ——
        那个"假模型"不是本机配置的一部分。用例要断言这一格请直接看 `clients.model`。
        """
        return self.services.runtime.llm().is_configured

    def tool_specs(self, *, kb_ids: Sequence[str] = ()) -> list[ToolSpec]:
        """这一轮摆给模型的工具：**只摆本地真能服务的那些** ✓（见 `SIDECAR_TOOL_NAMES`）。

        两处门控叠在一起，判据不同、都要过：

        1. `agent_tools.tool_specs` 的**库开关**（`kb_ids` 为空 = 用户关了知识库那一侧，
           见 `_KB_TOOLS` / `_LOCAL_KB_TOOLS`）；
        2. M3 阶段 2 追加的**提供者状态**：``state != ready`` 时那三个真正要知识库的工具
           一个都不摆（方案 §3.2 的"失败降级"）——给了又拒只会白花一个来回。

        **技能与外部 MCP 走的是同一张表、同一个执行器**（2026-10-04）：
        `agent_tools.tool_specs` 已经把技能目录（`services.skills`）与外部服务
        （`services.mcp`，含启停与策略）算进去了，这里只做**白名单筛**——
        名单里的名字 + `mcp__<服务>__<工具>` 那族（外部名字写不进名单，
        按形状放行，见 `SIDECAR_TOOL_NAMES` 下面那段说明）。

        ⚠️ 第 2 条**只在 `scope` 非空时才去问提供者**（`kb_ids` 为空时那三个本来就已被
        第 1 条摘掉）：否则**每一轮对话**都会先探一次握手，而 R1 要的恰恰是
        "交互路径不被握手拖慢"（没选库的会话根本用不到提供者，不该为它等一次 NAS 往返）。

        ⚠️ 本机档**没有归属过滤**（`owner_id=None`）：这台机器只有一个主人，
        "别人登记的 MCP 服务"这个场景不存在（与 `/mcp-servers` 端点那条 `LOCAL_CALLER`
        同一口径）。而 `available_tools` 仍按 `record.enabled` 与 `policy` 收口 ✓。
        """
        scope = [str(item) for item in kb_ids if str(item).strip()]
        specs = agent_tools.tool_specs(self.services, owner_id=None, kb_ids=scope or None)
        specs = [
            spec
            for spec in specs
            if spec.name in SIDECAR_TOOL_NAMES or split_qualified(spec.name) is not None
        ]
        if scope and self.provider.status().state != STATE_READY:
            specs = [spec for spec in specs if spec.name not in KB_PROVIDER_TOOLS]
        return specs

    def tool_loop(
        self,
        *,
        kb_ids: Sequence[str] = (),
        #: 这一轮归属的会话：**产物要靠它**（`ArtifactService.save` 按会话落点、
        #: `_record_turn` 按它写库 ✓）。没带会话 id 的老调用方落回 `LOCAL_CONVERSATION`
        #: ——那时产物与这一轮都会**如实报**"没有这条会话"（不是静默丢掉 ✓）。
        conversation_id: str = LOCAL_CONVERSATION,
        #: **这一轮有没有"问用户"的通道** ✓。
        #:
        #: - `True`（默认，`/turn/stream` 用 ✓）：`ask` 档走到"要问"时**发一条 `type=approval`**
        #:   并停下来等人 ✓ —— 这一侧唯一的通道就是 SSE 那条流 ✓，所以只有流式那条路配它 ✓；
        #: - `False`（`/turn` 用 ✗）：整段响应发不出一句询问 ✗（调用方拿到响应前不知道
        #:   `approval_id` ✓）→ 交给循环的 `UNAVAILABLE` 那条路 ✓，回给模型的是"待确认" ✓。
        interactive: bool = True,
    ) -> ToolLoop:
        """把**两头的接缝 + 本地那一侧**拼成一个 `ToolLoop` ✓（循环本体一行不改 ✗）。

        三处口径与服务器那条链路逐条对齐：工具表（`tool_specs` ✓）、执行器
        （`build_runner` ✓）、档位（`chat.mode` / `chat.permission` 从**本机**运行期配置读 ✓，
        与 `chat.tool_loop` 同一读法 ✓）。

        `client_factory` 给的是 `self.model`（本机直连）——**工厂形式**而不是一个现成的
        客户端：循环每一步都会现要一个，而 `_LocalModel` 每建一次就问一次运行期配置，
        所以用户在设置页换了模型/填了 key，**下一轮就生效**（不必重启边车 ✓）。
        """
        scope = [str(item) for item in kb_ids if str(item).strip()]
        # `Caller(is_admin=True)`：这是**本机主人**在 `agent_exec` 那道闸上的形状
        # （"能不能在**这台机器**上执行代码"）——边车跑在用户自己的机器上，能起边车的
        # 就是这台机器的主人 ✓。**服务器那道 `require_admin` 没有被绕过** ✗：它管的是
        # 服务器上的执行，而边车这一侧的执行根本不经过服务器 ✓。
        # 隔离（`require_isolation` ✓）与权限档（`chat.permission` ✓）两道闸照旧生效 ✓。
        runner = agent_tools.build_runner(
            self.services,
            Caller(is_admin=True),
            kb_ids=scope,
            conversation_id=conversation_id,
        )
        return ToolLoop(
            client_factory=lambda: self.model,
            tools=self.tool_specs(kb_ids=kb_ids),
            runner=runner,
            # **通道就是这一件事**：登记表非空 → 循环走到"要问"时发事件、停下来等人 ✓；
            # 为空 → 直接按 `UNAVAILABLE` 回"待确认" ✓（**绝不静默当成用户拒绝** ✗）。
            # 复用**同一个** `ApprovalRegistry`（`self.services.approvals` ✓）——它是
            # `POST /turn/approvals/{id}` 与正在等它的那一步之间的唯一交接点 ✓，不另造一套 ✗。
            approvals=self.approvals if interactive else None,
            mode=self.runtime.get("chat.mode"),
            permission=self.runtime.get("chat.permission"),
            gate=plan_gate.gate_for(LOCAL_CONVERSATION),
        )


def build_clients(base_url: str, token: str, *, workspace: Path, data_dir: Path) -> Clients:
    """唯一的装配处 ✓（方案 §4：`if` 只允许出现在这里）。

    **装配之前先把档位钉死**（M2 §4.1）：`create_app` 也会调一次（用例直接拿 app 时
    只有那一次机会），这里再调一次是幂等的——两个入口都不许"忘了钉档"。
    """
    pin_local_deployment(data_dir, server_url=base_url, token=token)
    return Clients(base_url, token, workspace=workspace, data_dir=data_dir)


class HistoryIn(BaseModel):
    """历史里的一条（与前端 `ChatHistoryMessage` 同形 ✓：`role` + `content` ✓）。"""

    role: str = Field(description="user 或 assistant（其它角色一律忽略 ✓）")
    content: str = ""


#: 历史条数的**硬上限**：前端已经截过一次（最近 20 条 ✓），这里是服务端侧的兜底 ✓ ——
#: 别让"把整个会话灌进来"这件事靠调用方自觉 ✗（提示词预算是有限的 ✓）。
MAX_HISTORY_MESSAGES = 20


class TurnIn(BaseModel):
    """一条用户消息（**为 SSE 留位**：`stream` 打开时走 `text/event-stream` ✓）。"""

    message: str = Field(min_length=1, max_length=32_000)
    workspace: str | None = Field(default=None, description="留空用默认工作区（用户目录下）")
    kb_ids: list[str] = Field(
        default_factory=list,
        description="这一轮允许查的库；**留空 = 知识库关着** ✓（与服务器同口径 ✓）",
    )
    history: list[HistoryIn] = Field(
        default_factory=list,
        description=(
            "最近的对话历史（**可选**）。"
            "边车这一侧现在以**本机库里的历史**为准 ✓（M2 阶段 3）；这一项留给"
            "「库里没有这段历史」的老调用方与烟测脚本——不带上它就是**失忆的一轮** ✗"
            "（用户会立刻感觉到『它忘了上文』，而界面看不出来 ✗）。只取最近"
            " `MAX_HISTORY_MESSAGES` 条 ✓。"
        ),
    )
    stream: bool = Field(default=False, description="预留：P4 与前端一起做 SSE ✓")
    conversation_id: str = Field(
        default="",
        description=(
            "这一轮归属的会话（**可选，默认空**）。空 = **这一轮不落库** ✓ —— 但要在 `notes` 里"
            "如实说明「未带会话 id，本轮未落库」✗（**不许静默丢** ✓）。"
        ),
    )
    turn_id: str = Field(
        default="",
        description=(
            "这一轮的标识（可选，默认空 = 边车自己生成 uuid4 ✓）。"
            "**本机档不再当幂等键用** ✓：这一轮是直接写本机库的，重复发就是一个新请求"
            "（= 再问一遍）。这个字段留着是为了响应里那一行标识 ✓。"
        ),
    )


def _messages_of(payload: TurnIn) -> list[ChatMessage]:
    """`TurnIn` → 循环要的消息（**顺序**：system → 历史 → 本轮 user ✓）。

    两处与服务器那条链刻意对齐 ✓：
    - 历史放在 system 之后、本轮 user 之前 ✓（顺序错了模型会把历史当"新指令" ✗）；
    - **只认 `user` / `assistant` 两个角色** ✓ —— 别让调用方从这里塞进第二条 system ✗
      （那等于绕过我们这条链的提示词 ✓）。
    """
    messages = [ChatMessage(role="system", content=SIDECAR_SYSTEM_PROMPT)]
    for item in payload.history[-MAX_HISTORY_MESSAGES:]:
        if item.role not in ("user", "assistant") or not item.content.strip():
            continue
        messages.append(ChatMessage(role=item.role, content=item.content))
    messages.append(ChatMessage(role="user", content=payload.message))
    return messages


class TurnOut(BaseModel):
    """回答 + 本轮步骤（`steps` 现在是 `ToolLoop` 真的产出的 ✓）+ 出处快照 + 如实说明 ✓。"""

    answer: str
    sources: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "这一轮检索到的**出处快照**（M3 阶段 7 补：与流式那条 `sources` 事件同一份形状，"
            "与服务器那条链的 `ChatSourceOut` 逐字同形 ✓）。本机档没有知识库时是空列表 ✓。"
        ),
    )
    steps: list[dict[str, Any]] = Field(default_factory=list)
    workspace: str
    notes: list[str] = Field(default_factory=list, description="这一侧有什么/没有什么（如实写 ✓）")
    sse: bool = Field(default=False, description="预留：true 时可用 /turn/stream 取 SSE ✓")
    turn_id: str = Field(default="", description="这一轮的标识（本机档不再当幂等键用 ✓）")
    recorded: bool | None = Field(
        default=None,
        description=(
            "**已落本机**：`True` 已写进本机库 ✓ / `False` 写库失败 ✗ / `None` 没写"
            "（请求没带 `conversation_id` ✓）。"
            "**它不影响 `answer`** ✗ —— 落库失败时答案照旧返回，原因写在 `notes` 里 ✓。"
        ),
    )
    error: str = Field(
        default="",
        description=(
            "非空 = **这一轮没有正常作答**（可判定的标记 ✓）：知识库那头不可用/被拒、"
            "本机模型没配或被上游拒、或模型没产出正文。"
            "**空 `answer` 绝不等于成功** ✗ —— 调用方据此区分「成功」与「空」✓。"
        ),
    )


class HealthOut(BaseModel):
    """**如实报**两个"能用吗" ✗（不许假装健康 ✓）。

    - `kb_reachable`：NAS 的知识库这一头真探一次（打不通会带原因进 `note`）；
    - `model_reachable`：**本机**有没有配好对话模型（2026-10-04 起模型不走 NAS，
      所以这一格不再等于上面那一格，见 `/health` 的说明）。
    """

    version: str
    workspace: str
    kb_reachable: bool
    model_reachable: bool
    note: str = ""


class ApprovalDecisionIn(BaseModel):
    """界面在确认条上点的那一下（形状与服务器 `ChatApprovalIn` **逐字对齐** ✓）。

    **只有三个取值** ✓，都是用户明确点出来的：允许一次 / 这类都允许 / 拒绝 ✓。
    "超时"与"这条链路没人可问"**不是请求参数** ✗ —— 它们是执行侧自己的结论 ✓
    （能从外面伪造就等于开了一条绕过"等用户点头"的路 ✗，见 `services/approvals.py`）。
    """

    decision: Literal["allow_once", "allow_always", "deny"] = Field(
        description="允许一次 / 这类都允许（写进放行清单）/ 拒绝"
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="拒绝时给模型的一句话（可空 ✓）；空 = 与没有这个输入框时一字不差 ✓",
    )


class ApprovalDecisionOut(BaseModel):
    """决定有没有真的交到**正在等它的那一步** ✓（照服务器 `ChatApprovalOut` ✓）。"""

    accepted: bool = Field(description="true = 那一头已经收到它，会立刻接着往下跑 ✓")
    detail: str = Field(default="", description="给人看的一句话 ✓")


def _record_turn(
    clients: Clients,
    *,
    conversation_id: str,
    turn_id: str,
    question: str,
    answer: str,
    steps: list[dict[str, Any]],
    thinking: str,
    sources: Sequence[dict[str, Any]] = (),
) -> tuple[bool | None, str]:
    """把这一轮写进**本机库**（M2 阶段 3）。返回 ``(recorded, 原因)``。

    从这一步起，"跑在本机、账在服务器"那半截没有了 ✗：这一轮落在
    ``<data_dir>/kylab.db`` 的 `chat_messages` 里（消息与事件同一个事务，见
    `ConversationService.record_turn`），首轮提问顺手把标题定下来（`ensure_title`，
    **只在还没有标题时**——用户改过名字的会话不该被后续提问覆盖 ✓）。

    四条口径：

    1. **失败绝不许影响回答** ✗ —— 调用方拿到的 `answer` 原样返回 ✓，
       原因进 `notes` / SSE 那条 `phase="note"` 的 step ✓（与旧版同一条纪律）；
    2. **没带 `conversation_id` 就跳过** ✓，但**必须说明** ✗（静默丢一轮 = 数据丢失 ✓）；
    3. **会话不存在也是如实报** ✗（`recorded=False` + 原因）：本机库没有这条会话时
       写不进去，而这与"写成功"必须在响应里分得开；
    4. `recorded` 的语义随之改为"**已落本机**" ✓（旧版是"服务器已入库"）：
       `True` 已落库 / `False` 落库失败 / `None` 没带会话 id。

    **`turn_id` 不再是幂等键**（服务器那条链上它是）：本机库这两张表没有"轮"这个键，
    而"同一轮重复发"在本机就是一个新请求（客户端重发 = 再问一遍）。这条差别**如实登记**在
    阶段 3 的偏离点里（要幂等就得给 `chat_messages` 加一列并落一次 schema 迁移，M2 不做）。
    它仍然回给调用方做标识用 ✓。

    ``sources``（M3 阶段 7 补）是这一轮检索到的**出处快照**：与服务器那条链
    （`chat.py::_record_turn` 的 `sources=[item.model_dump(mode="json") …]` ✓）
    **同一份形状、同一个落点**（assistant 那条消息的 `sources`）。不存的话，
    界面上那串 `[1][2]` 只在**这一轮还在流的时候**点得动 ✗ —— 刷新回看就没了 ✗。
    """
    del turn_id  # 只作标识，不再参与写库（见 docstring 最后一段）
    if not conversation_id:
        return None, (
            "未带会话 id（conversation_id），本轮**未落库**（刷新后这一轮不会留在会话里）"
        )
    try:
        clients.services.conversations.record_turn(
            conversation_id,
            question=question,
            answer=answer,
            sources=list(sources),
            steps=steps,
            thinking=thinking,
        )
        # 首轮提问落库后定标题；`ensure_title` 自己只在"还没有标题"时动它。
        # 它失败**不影响这一轮已经落库**（标题是装饰，消息是事实）——单独兜一层。
        clients.services.conversations.ensure_title(conversation_id, question)
    except Exception as exc:  # 写库/会话不存在/库被锁：都归"这一轮没记上"，原因带出去
        logger.warning("这一轮没能落本机库：%s", type(exc).__name__, exc_info=True)
        return False, f"这一轮没能落本机库（答案不受影响）：{type(exc).__name__}: {exc}"
    return True, ""


def _probe_health(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """探活 → ``(可达?, 说明)`` ✓ —— **失败原因必须带出来** ✗（P3 烟测抓到：吞成 False ✗）。

    烟测现场：`/health` 说"后端不可达" ✗，可**同一份 base/token** 的 `/turn` 却成功了 ✓ ——
    探针把原因吞了，于是"不可达"三个字既不可信、也没法排障 ✗。现在分两类如实报：

    - **网络层不可达**（连不上/超时/DNS）→ 带上异常原文 ✓；
    - **端点答了但不是 2xx** → 带上**状态码 + 正文前 200 字** ✓（那条才是真正要看的 ✓）。
    """
    httpx = _httpx()  # 按需导入 + 可注入接缝（理由见 `_httpx()` 的说明）

    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"网络不可达：{type(exc).__name__}: {exc}（url={url}）"
    if response.status_code >= 400:
        return False, (f"端点返回 {response.status_code}（url={url}）：{response.text[:200]}")
    return True, ""


def _seed_local_files(clients: Clients) -> None:
    """启动时把**缺的记忆/人设文件**补上模板（幂等，只补缺的 ✓）。

    与 `app/main.py` 的 lifespan 那一段**同一件事、同一口径**（v0.1.1 起服务器每次
    启动都铺一遍）：本机档的「记忆」页也是本机后端那一页（`local_router` 挂了
    `memory.router` ✓），新装好的桌面第一次打开它就该有东西可看 —— 而边车**没有
    lifespan 那些步骤**（它只造 app），不在这里补一次就会比服务器那侧少铺这一遍。

    **只补缺的，绝不覆盖已有的**（那是用户写了几天的东西，见 `MemoryService.seed_persona`）；
    写不出来只警告不拦启动 —— 与 main 那条同一个口径（建不出模板不该让边车起不来）。
    """
    try:
        created = clients.services.memory.seed_persona()
    except OSError:
        logger.warning(
            "记忆/人设模板建不出来：%s", clients.services.memory.workspace, exc_info=True
        )
    else:
        if created:
            logger.info("记忆/人设模板已就位：%s", "、".join(created))


def _lifespan(clients: Clients) -> Any:
    """边车进程的生命周期（2026-10-04 起有内容：**本机消费者**）。

    与 `app/main.py` 的 lifespan 同一件事、同一把开关（`KYLAB_RUN_WORKER`）：
    起那个消费者——定时任务到点跑 + 本机库空闲维护，落点与安全边界写在
    `workers/local_worker.py` 的模块头。

    **为什么这里也要起一份**：桌面壳起的是**这个进程**
    （`python -m app.sidecar`，见 `desktop/src-tauri/src/sidecar.rs`），而界面打的
    `/api/v1/scheduled-tasks*` 就是打在它上面的。不在这儿起，"到点跑"在本机永远不会
    发生，而 `POST /scheduled-tasks/{id}/run` 会撞上 NAS 的队列表（本机档没有那张表）
    ——那正是"摆出来的端点点不通"那条老毛病。

    两个进程同时起来也只会跑一遍：认领是一次 CAS（`MetaStore.arm_scheduled_task`），
    第二个人要么认领失败，要么在下一轮看到 `next_run_at` 已经推到下一个周期。

    **摄取那条消费者不在这里**：它领的活全在 NAS 上（队列表 / 文档 / 切块 / 向量），
    本机起了只会每隔几秒撞一次不可用的库（M2 §4.1 那条结论对**它**仍然成立）。
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if get_settings().run_worker:
            scheduler = bind_local_scheduler(clients.services)
            tasks = [asyncio.create_task(run_local_scheduler(scheduler, stop))]
            logger.info("边车：已启动本机消费者（定时任务到点跑 + 本机库空闲维护）")
        else:
            logger.warning("KYLAB_RUN_WORKER=false：边车未起消费者，定时任务到点不会跑")
        try:
            yield
        finally:
            # 置位即可退出（两条循环都在 `wait_for(stopping.wait(), …)` 上，
            # 立刻醒来返回，不会多等一个间隔）——与 `main.py` 的收尾同一套语义
            stop.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    return lifespan


def create_app(
    base_url: str,
    token: str,
    workspace: Path,
    *,
    data_dir: Path | None = None,
    kb_url: str = "",
    kb_token: str = "",
    device_id: str = "",
) -> FastAPI:
    """造边车应用（入口只做参数解析与 `uvicorn.run` ✓，方便用例直接拿 app ✓）。

    ``data_dir`` 是**本地**运行期数据的落点 ✓（本机库、沙箱、记忆都在它下面 ✓）：
    默认取工作区的上一级 `…/data` ✓ —— 与工作区同处一个用户目录，备份时一起拿走 ✓。

    ``kb_url`` / ``kb_token`` 是**知识库提供者那两头**的覆盖（M3 §4.1，排障与多 NAS
    入口）。**留空 = 继承** ``base_url`` / ``token``（壳里那台 NAS），所以默认档
    一个字都不用传 ✓ —— 它们只往 ``pin_local_deployment`` 的"有值才设"那条路走。

    ``device_id``（M5 阶段 4）是**这台机器的设备身份**：壳在登录那一刻生成一次、
    之后每次起边车都传下来。它同样是"有值才设"，**留空就是没有** —— 本机档随后
    如实拒绝打快照（R12：绝不自动编一个 id），而不是悄悄用一台匿名机器备份。

    **先把档位钉死**（`pin_local_deployment`）再建任何东西：`Clients` 会走本机档的
    组合根（`get_services()`），而那是按环境变量建单例的——钉晚了就会按服务器档
    去连 PG（"边车误连服务器库"那条路，不报错、只是写错库）。用例直接调本函数时
    也只有这一次机会 ✓。
    """
    data_dir = data_dir or (workspace.parent / "data")
    pin_local_deployment(
        data_dir,
        server_url=base_url,
        token=token,
        kb_url=kb_url,
        kb_token=kb_token,
        device_id=device_id,
    )
    clients = build_clients(base_url, token, workspace=workspace, data_dir=data_dir)
    _seed_local_files(clients)
    app = FastAPI(title="kylab sidecar", version=SIDECAR_VERSION, lifespan=_lifespan(clients))

    # **壳的页面要跨源直连边车**（2026-09-30 实测补上）：界面从 `http://app.localhost`
    # （自定义 scheme 在 Windows 上的映射，见 `resources.rs::app_url`）调本机边车，
    # 浏览器先做 CORS 预检——不挂这个中间件时 `fetch` 直接 `TypeError: Failed to fetch`，
    # `resolveTurnTarget()` 于是**永远回退到服务器**：页面上只留一条 console.warn，
    # "对话在本机跑"就这么静默地从来没生效过（服务端日志里能看到工具循环还在服务器上跑）。
    # 只放行三个源（壳 + vite dev 的两种写法）：别的源照旧读不到响应。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://app.localhost",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # **本机后端面**（M2 §4.1 / §4.3）：会话 / 笔记 / 设置 / 记忆 / 工作区……那批端点
    # 就挂在这个进程上（`/api/v1/*`）—— 桌面壳里的界面**直连边车**打它们
    # （`http://127.0.0.1:<port>/api/v1/…`，CORS 上面已经放行）。
    #
    # 为什么挂在这里而不是"让壳把 `/api/**` 转发到边车"：本机权威面必须是**本机进程**
    # 直接答的（转发会多一跳、也把"这一份数据到底在不在本机"搅浑）。白名单见
    # `api/v1/router.py` 的 `local_router`（服务器专属的那些端点**一个都没挂**）。
    #
    # **异常映射也要挂上**（与 `main.py` 一字不差，2026-10-03 补）：这些端点抛的是
    # `KylabError` 那一族（404 找不到、409 冲突、503 知识库不可用……），不注册处理器
    # 就成了 500 或裸异常 —— 而前端读的是 `{code, message}` 那个信封（见
    # `core/exceptions.py`）。边车以前没有业务端点，所以这件事一直没有暴露出来。
    app.include_router(local_router, prefix=f"/api/{API_VERSION}")
    register_exception_handlers(app)

    @app.get("/health", response_model=HealthOut, summary="健康 + 知识库可达性 + 本机模型配置")
    def health() -> HealthOut:
        """**两个"能用吗"分开答** ✓（都不许假装健康 ✗）。

        - `kb_reachable`：真去打一次 NAS 的知识库健康端点 ✓ —— 打不通就如实报不可达，
          **带上原因** ✗（网络层不可达 vs 端点非 2xx 分开，见 `_probe_health`）；
        - `model_reachable`：**本机**有没有配好对话模型（2026-10-04 起模型不走 NAS ✗，
          所以这里不再拿知识库的可达性当模型的健康 —— 那是两个独立的失败面：
          "NAS 连不上但本机模型好好的"与"NAS 好好的但本机没配模型"都要能一眼看出来）。
          判的是**配置**（`is_configured`）而不是真发一次请求：探活不该花用户的钱、
          也不该为了一个健康检查去撞一次上游限流 ✓。配好了但上游会拒（key 无效）
          在真跑一轮时才报出来（见 `_LocalModel` 的失败路径）。
        """
        kb_ok, why = _probe_health(clients.health_url)
        model_ok = clients.model_configured()
        if not model_ok:
            hint = "本机还没有配好对话模型：「设置 → 模型注册」里登记供应商、填 Key、绑定对话生成"
            why = f"{why}；{hint}" if why else hint
        return HealthOut(
            version=SIDECAR_VERSION,
            workspace=str(workspace),
            kb_reachable=kb_ok,
            model_reachable=model_ok,
            # **如实报原因** ✗（网络不可达 vs 端点非 2xx 分开 ✓ —— 别吞成一句"不可达" ✗）
            note=why,
        )

    @app.post("/turn", response_model=TurnOut, summary="走一轮（模型本机直连；工具在本机跑）")
    def turn(payload: TurnIn) -> TurnOut:
        """**同一个 `ToolLoop`** ✓：本机直连模型 + 远端 KB + 本地工具/沙箱/审批 ✓。

        顺序与服务器那条链路逐条对齐（见 `api/v1/chat.py` 的同名循环）：
        取正文以收尾那条 `DoneEvent` 为准 ✓、步骤逐条收 ✓、失败**如实报** ✗。
        """
        target = _check_workspace(payload.workspace) if payload.workspace else workspace
        notes = _notes(clients)
        # **非流式这条没有通道**（`interactive=False` ✓）：响应是**整段**回来的，
        # 询问发不出去（调用方在拿到响应之前根本不知道 `approval_id` ✗）→ 让人等 120 秒
        # 是白等 ✓。所以按"这条链路上没人可以问"走 `UNAVAILABLE` ✓ —— 回给模型的是
        # "待确认"，**不是**"用户拒绝了" ✗（分档见 `services/approvals.py` 模块头 ✓）。
        # 要真正弹确认条就用 `/turn/stream` ✓（它把询问当成一条 SSE 发出去 ✓）。
        loop = clients.tool_loop(
            kb_ids=payload.kb_ids,
            conversation_id=payload.conversation_id or LOCAL_CONVERSATION,
            interactive=False,
        )
        messages = _messages_of(payload)
        answer = ""
        steps: list[dict[str, Any]] = []
        reasoning: list[str] = []
        sources: list[dict[str, Any]] = []
        try:
            for event in loop.run(messages=messages):
                if isinstance(event, StepEvent):
                    steps.append(_step_payload(event))
                elif isinstance(event, SourcesEvent):
                    # 出处（M3 阶段 7 补）：非流式这条也要收着 ✓ —— 它既进响应
                    # （调用方/脚本能核对"这一轮依据的是哪几段"），也跟着这一轮落进
                    # 本机会话的快照 ✓（与服务器那条链同一处口径 ✓）。
                    sources = _sources_payload(event.sources)
                elif isinstance(event, DeltaEvent):
                    answer += event.text
                elif isinstance(event, ThinkingEvent):
                    # 思考过程收着**只为解释"为什么一个字都没有"** ✓——它不进 `answer` ✗：
                    # 那是模型的思考，不是给用户的回答 ✓。
                    reasoning.append(event.text)
                elif isinstance(event, DoneEvent):
                    # 收尾那条带的是拼好的全文，**以它为准** ✓（个别增量丢了也不会与步骤对不上 ✓）
                    answer = event.answer or answer
                elif isinstance(event, ApprovalEvent):
                    # 这条路上**没有通道**（`interactive=False` ✓）→ 循环不会走到这里 ✗。
                    # 真出现了就说明"通道接上了而这条链路没接" ✗ —— 如实记一条 step（不吞 ✗），
                    # 因为静默丢掉一句"要用户点头"的询问，用户看到的就是"命令被拒" ✗。
                    steps.append(
                        {
                            "phase": "tool",
                            "label": f"待确认：{event.label}",
                            "detail": (
                                f"{event.args}（非流式这条没有确认通道："
                                f"要弹确认条请用 /turn/stream ✓）"
                            ),
                            "status": "done",
                            "tool": event.tool,
                            "outcome": "awaiting",
                            "args": event.args,
                            "result": "",
                        }
                    )
        except (RemoteClientError, ChatError) as exc:
            # **失败分档照旧** ✓：两头都要如实说出来 ✗（不当成"空回答" ✓）。
            # `RemoteClientError` = 知识库那一头的接缝（远端不可用/被拒）；
            # `ChatError` = 模型这一头（2026-10-04 起模型在本机直连）：
            # "还没配模型"（reason=not_configured）与"上游拒了"（401/404/5xx）
            # 都从它来，文案里带着下一步动作（见 `_LocalModel`）。
            # 两条都不许变成空回答 ✗、也不该冒成 500 ✗ —— 前者用户看不出出了什么事，
            # 后者会把这句人话吞进"服务端出错了"里。
            return TurnOut(
                answer=f"（边车报告：{exc}）",
                workspace=str(target),
                notes=notes,
                error=str(exc),
            )
        _close_trailing_answer_step(steps)
        # **写回这一轮**（best-effort ✓）：跑在本机、账在服务器 ✓。
        # 失败**绝不影响回答** ✗ —— 原因进 `notes`，`answer` 原样返回 ✓。
        turn_id = payload.turn_id or uuid4().hex
        recorded, why = _record_turn(
            clients,
            conversation_id=payload.conversation_id,
            turn_id=turn_id,
            question=payload.message,
            answer=answer,
            steps=steps,
            thinking="".join(reasoning),
            sources=sources,
        )
        if why:
            notes = [*notes, why]
        if not answer.strip():
            # **绝不返回"空成功"** ✗✗（2026-09-29 烟测现场：`answer:""` + `steps:[]` + HTTP 200 ✓
            # 是这条链路里最危险的形态 ✗ —— 调用方以为成功 ✓、用户拿到空白 ✓）。
            # 两种成因都要写清：模型只给了思考（推理模型常见 ✓）、或者什么都没给 ✓。
            detail = (
                f"模型只返回了思考过程、没有正文与工具调用（思考 {len(reasoning)} 段）"
                if reasoning
                else "模型没有返回任何内容"
            )
            hint = (
                "这一轮没有执行任何工具。要本机动作就把要读/要跑的东西说具体些再试；"
                "本机没有隔离后端时命令会被拒绝，拒绝原因会写在回答里。"
            )
            return TurnOut(
                answer=f"（边车报告：{detail}。{hint}）",
                steps=steps,
                workspace=str(target),
                notes=notes,
                error=f"empty-answer: {detail}",
            )
        if not any(step["tool"] for step in steps):
            # 有正文但**一次工具都没调**（模型直接作答）：如实标出来 ✓ ——
            # 不许把"只宣布意图"当成"做了" ✗（从 notes 就看得出这一轮没有本机动作 ✓）。
            notes = [
                *notes,
                "这一轮没有调用任何工具（模型直接作答；需要本机动作时请把要求说得更具体）。",
            ]
        return TurnOut(
            answer=answer,
            sources=sources,
            steps=steps,
            workspace=str(target),
            notes=notes,
            turn_id=turn_id,
            recorded=recorded,
        )

    @app.post("/turn/stream", summary="走一轮（SSE：步骤 / 思考 / 正文增量 / 收尾 / 失败）")
    def turn_stream(payload: TurnIn) -> StreamingResponse:
        """与 `/turn` **同一个循环、同一套语义** ✓，只是把过程**边跑边发** ✓。

        为什么要有它（P4 前置）：前端对话是**流式**的 ✓（服务器那条 `/api/v1/chat` 就是 SSE ✓）；
        边车这一侧不先把流做实 ✗，前端切过来就会**丢流** ✗（整段等完才出字 ✓ 体验断档）。

        事件形状**照服务器那条链**（`api/v1/chat.py` 模块头那五行 ✓，逐条对齐 ✗ 不另创 ✓）::

            data: {"type":"step", phase,label,detail,status,tool,outcome,args,result}
            data: {"type":"thinking","text":"…"}
            data: {"type":"delta","text":"…"}
            data: {"type":"approval", approval_id,tool,label,args,detail,rule,timeout_seconds}
            data: {"type":"done","answer":"…"}
            data: {"type":"error","message":"…"}

        `approval` 那一条是**要用户点头**的询问 ✓（v0.41）：发出去之后这一轮的循环就停在
        `ApprovalRegistry.wait_decision` 上 ✓，等 `POST /turn/approvals/{approval_id}` 那一下 ✓。
        所以它必须**原样、立刻**发出去 ✗（攒着不发 = 两边一起等死 ✓）。

        护栏与 `/turn` **一字不差** ✓：
        - **正文为空绝不当成功** ✗✗ → 先发 `error`（`empty-answer: …` ✓）再发 `done`（如实说明 ✓）；
        - 知识库那头不可用/被拒、或模型这头没配/被上游拒 → `error` + 如实的 `answer` ✓
          （不当成"空回答" ✗）；
        - `done.answer` 与流出去的 `delta` 拼起来的是**同一份** ✓（前端兜底不会与增量打架 ✓）。

        与 `/turn` 唯一有意的差别：`/turn` 末尾会往 `notes` 里加一句"这一轮没有调用任何工具" ✗，
        流式这条路**不加** ✓ —— 那件事在事件流里**本来就看得见**（一个 `tool` 步都没发过 ✓），
        再塞一句反而要发明一个形状 ✗（`done` 只有 `answer` ✓）。
        """

        def gen() -> Iterator[str]:
            target = _check_workspace(payload.workspace) if payload.workspace else workspace
            loop = clients.tool_loop(
                kb_ids=payload.kb_ids,
                conversation_id=payload.conversation_id or LOCAL_CONVERSATION,
            )
            messages = _messages_of(payload)
            answer = ""
            steps: list[dict[str, Any]] = []
            reasoning: list[str] = []
            sources: list[dict[str, Any]] = []
            try:
                for event in loop.run(messages=messages):
                    if isinstance(event, StepEvent):
                        step = _step_payload(event)
                        steps.append(step)
                        yield _sse({"type": "step", **step})
                    elif isinstance(event, SourcesEvent):
                        # **出处照发**（M3 阶段 7 补的真缺口，见 `_sources_payload`）✓：
                        # 循环那边每检索一次就发一条**累计**列表 ✓（服务器那条链同一语义 ✓），
                        # 这里原样转发、不攒批 ✗ —— 界面的 `[1][2]` 要靠它才点得动 ✓。
                        sources = _sources_payload(event.sources)
                        yield _sse({"type": "sources", "items": sources})
                    elif isinstance(event, DeltaEvent):
                        # **边到边发** ✓：这里不做任何缓冲/攒批 ✗（攒了就等于没做流式 ✓）
                        answer += event.text
                        yield _sse({"type": "delta", "text": event.text})
                    elif isinstance(event, ThinkingEvent):
                        # 思考也照发 ✓（前端的过程面板用它 ✓）；但它**不进 `answer`** ✗
                        reasoning.append(event.text)
                        yield _sse({"type": "thinking", "text": event.text})
                    elif isinstance(event, DoneEvent):
                        # 收尾那条带拼好的全文，**以它为准** ✓（与 `/turn` 同一口径 ✓）
                        answer = event.answer or answer
                    elif isinstance(event, ApprovalEvent):
                        # **问用户**（v0.41）：这一条发出去之后，循环那边就停在
                        # `approvals.wait_decision` 上了 ✓ —— 所以必须**原样、立刻**发出去 ✗
                        # （攒着不发 = 两边一起等死 ✓，见 `services/approvals.py` 模块头第 1 条）。
                        #
                        # **不进 `steps`** ✗：这是一句还没被回答的问题，不是"做过的一步" ✓
                        # （服务器那条链同样把它排除在快照之外 ✓）。
                        yield _sse({"type": "approval", **_approval_payload(event)})
            except (RemoteClientError, ChatError) as exc:
                # **失败如实报** ✗（分档照旧：KB 那头不可用 / 被拒、模型这头没配或上游拒了 ✓，
                # 不伪装成空回答 ✓）。两族异常的含义见 `/turn` 那一处的说明。
                yield _sse({"type": "error", "message": str(exc)})
                yield _sse({"type": "done", "answer": f"（边车报告：{exc}）"})
                return

            # 收尾那一步要**再发一次**：它先前以 `running` 出去过 ✓，而循环从不给它 `done` ✗
            # （与 `/turn` 同一个 `_close_trailing_answer_step` ✓）—— 不发这一次，
            # 前端会一直显示"正在组织回答" ✗，而这一轮其实已经结束了 ✓。
            before = [dict(step) for step in steps]
            _close_trailing_answer_step(steps)
            for old, new in zip(before, steps, strict=True):
                if old != new:
                    yield _sse({"type": "step", **new})

            # **写回这一轮**（best-effort ✓）：失败绝不影响已经流出去的答案 ✗ ——
            # 用一条 `phase="note"` 的 step 如实说出来 ✓（不发明新的 `type` ✗：
            # 事件形状照服务器那条链那几个 `type` ✓）。
            turn_id = payload.turn_id or uuid4().hex
            recorded, why = _record_turn(
                clients,
                conversation_id=payload.conversation_id,
                turn_id=turn_id,
                question=payload.message,
                answer=answer,
                steps=steps,
                thinking="".join(reasoning),
                sources=sources,
            )
            if why:
                yield _sse(
                    {
                        "type": "step",
                        "phase": "note",
                        "label": "写回服务器",
                        "detail": why,
                        "status": "done",
                        "tool": "",
                        "outcome": "recorded" if recorded else "not-recorded",
                        "args": "",
                        "result": turn_id,
                    }
                )

            if not answer.strip():
                # **绝不发"空成功"的 done** ✗✗（与 `/turn` 同一段判据与同一套措辞 ✓）
                detail = (
                    f"模型只返回了思考过程、没有正文与工具调用（思考 {len(reasoning)} 段）"
                    if reasoning
                    else "模型没有返回任何内容"
                )
                hint = (
                    "这一轮没有执行任何工具。要本机动作就把要读/要跑的东西说具体些再试；"
                    "本机没有隔离后端时命令会被拒绝，拒绝原因会写在回答里。"
                )
                yield _sse({"type": "error", "message": f"empty-answer: {detail}"})
                yield _sse({"type": "done", "answer": f"（边车报告：{detail}。{hint}）"})
                return

            del target  # 工作区已在校验时定下 ✓（响应里不再回它：流式的载荷形状照服务器那条链 ✓）
            yield _sse({"type": "done", "answer": answer})

        return StreamingResponse(gen(), media_type=SSE_MEDIA_TYPE)

    @app.post(
        "/turn/approvals/{approval_id}",
        response_model=ApprovalDecisionOut,
        summary="对一条待确认做出决定（允许一次 / 这类都允许 / 拒绝）",
    )
    def decide_turn_approval(approval_id: str, payload: ApprovalDecisionIn) -> ApprovalDecisionOut:
        """把界面点的那一下，交给**正在等它的那一步** ✓（v0.41 同一套语义 ✓）。

        与 `/turn/stream` 的关系就是这条协议的全部要点：那条流**还开着**、停在
        `ApprovalRegistry.wait_decision` 上 ✓（见 `services/approvals.py` 模块头），
        这一条请求只是把决定送回它手里 ✓。所以**不另造一套** ✗：登记表与判定都复用
        `clients.approvals`（`Clients.__init__` 里那一份 ✓，`tool_loop` 拿的也是它 ✓）。

        两处口径与**服务器那条链逐条对齐**（`api/v1/chat.py` 的 `decide_approval` ✓）：

        - **送到了** → `accepted=true` ✓；
        - **已经失效**（超时 / 已经点过一次）→ **409** ✗，而不是回一句"已记录" ✓ ——
          否则用户以为命令跑了 ✗，而那一头其实早按超时处理完了 ✓（同一个登记表的
          `decide` 就是靠"条目还在、还没过期"来回答这件事的 ✓）。

        鉴权：边车**只监听本机** ✓（`--host` 默认 127.0.0.1 ✓），能打到这个端口的就是
        这台机器的主人 ✓ —— 与服务器那条要 `require_admin` 的口径同源 ✓
        （服务器管的是"服务器上的执行" ✓，而这里执行的是**用户自己机器上**的命令 ✓）。
        """
        if not clients.approvals.decide(approval_id, payload.decision, payload.reason):
            raise HTTPException(
                status_code=409,
                detail=(
                    "这条确认已经失效了（可能等得太久超时了，或者已经点过一次）："
                    "那一轮会按「没有批准」继续，模型那边会收到这个结论。请让它再来一次。"
                ),
            )
        return ApprovalDecisionOut(accepted=True, detail="已经交给正在等它的那一步")

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="KYLAB 本地边车（循环 / 工具 / 模型都在本机；知识库在 NAS）"
    )
    parser.add_argument("--host", default="127.0.0.1", help="**只监听本机**（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--server",
        default=os.environ.get("KYLAB_SERVER_URL", "http://127.0.0.1:8000/api/v1"),
        help="服务器 API 基址（**知识库**那一头在它下面；模型不走它）",
    )
    parser.add_argument("--token", default=os.environ.get("KYLAB_TOKEN", ""), help="用户会话令牌")
    parser.add_argument(
        "--kb-url",
        default=os.environ.get("KYLAB_KB_URL", ""),
        help="知识库提供者的地址覆盖（默认继承 --server，见设置页「知识库连接」）",
    )
    parser.add_argument(
        "--kb-token",
        default=os.environ.get("KYLAB_KB_TOKEN", ""),
        help="知识库提供者的凭据覆盖（默认继承 --token；不落库、不进日志）",
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("KYLAB_DEVICE_ID", ""),
        help="这台机器的设备身份（壳生成并传入；没有它不自动编，见 M5 §3.1）",
    )
    parser.add_argument(
        "--workspace", default=None, help="本地工作区目录（默认 ~/.kylab/workspace）"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="本地运行期数据目录（沙箱与设置落在它下面；默认取工作区上一级的 data/）",
    )
    args = parser.parse_args(argv)

    workspace = _check_workspace(args.workspace)
    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else None
    # **控制台日志要配一次**（2026-10-04 补，起本机消费者那一批）：不配的话根日志器是
    # WARNING，`logger.info` 全部丢掉——"定时任务到点跑了一条"这类**正常发生**的事
    # 在壳抓的那份日志里一个字都看不到，只剩失败时才有痕迹（而"它到底跑没跑"正是
    # 排障时第一个要回答的问题）。与 `app/main.py` 的 lifespan 同一个口径与同一个函数，
    # 级别也取同一份设置（`KYLAB_LOG_LEVEL`）。
    # **不挂文件处理器**：壳已经把这一个进程的 stdout 收进它自己的日志文件了
    # （`desktop/src-tauri/src/logfile.rs`），再挂一份会变成两个日志文件。
    setup_logging(get_settings().log_level)
    import uvicorn  # 局部导入：用例 import 本模块时不必拉起 uvicorn ✓

    uvicorn.run(
        create_app(
            args.server,
            args.token,
            workspace,
            data_dir=data_dir,
            kb_url=args.kb_url,
            kb_token=args.kb_token,
            device_id=args.device_id,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
