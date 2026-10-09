"""工具目录端点（v0.61）：管理界面据此渲染"这个服务对外有哪些能力"。

**为什么需要一个端点**：工具面（``services/tools.py`` 那张表）原先只有 MCP 客户端
的 ``tools/list`` 看得到，管理界面拿不到；而"这把 API Key 能调哪些工具"要在页面上
勾选（v0.60 的 ``allowed_tool_names``）——勾选清单必须与真实工具面**同源**，
在界面上再抄一份必然相漂，而漂出来的形态是"勾了却调不动"或反过来。

**三份数据各管一段，这里只负责拼**：

- 有哪几个工具 = ``TOOL_NAMES``（对外面的名单，只有一处定义）；
- 参数是什么 = ``tool_definitions()`` 的 ``inputSchema``（写给模型看的描述也在这里）；
- 给人看的名字与一句话 = ``TOOL_META``（**短**，因为它会渲染到屏幕上）。

所以这个端点里**没有任何一张自己的清单**：加一个工具时，上面三处跟着走，
这里一行都不用改。

**指纹也来自同一处**（``surface_fingerprint()``）：它是那张表本身的摘要，控制台把它
显示出来，用户对着两次部署的值一比就知道契约有没有变过——版本号做不到这件事，
理由写在 ``services/tools.py`` 那个函数里。这里只转发，不自己算。

**它不注入 ``Services``**：目录是纯元数据，读它不查库——一旦这个端点开始依赖库，
"目录"就会跟着数据长（比如按库过滤工具），而工具面与库范围是两回事。

**鉴权用只读档**：受限的 API Key 也看得到全量目录——目录不是"你能用什么"，
是"这个服务有什么"；能不能调由 ``check_tool`` 在调用那一刻判。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.auth import ReadDep
from app.api.v1.schemas import SkillOut, ToolCatalogOut, ToolOut, ToolParamOut
from app.core.config import get_settings
from app.services.tools import TOOL_META, TOOL_NAMES, surface_fingerprint, tool_definitions

router = APIRouter(tags=["tools"])

#: 随服务一起发出去的那份技能（``skills/kylab-knowledge-base/``）。
#:
#: 只登记身份与路径，**不读那个目录**：服务进程跑在容器里、技能文件在仓库里，
#: 读它就得猜部署时的路径——而猜错的表现是"这个端点偶尔 500"。
SKILL_NAME = "kylab-knowledge-base"
SKILL_PATH = "skills/kylab-knowledge-base"
SKILL_SUMMARY = "在你的知识库里检索原文段落，给出可追溯出处的回答"
SKILL_TRANSPORTS = ["mcp", "rest"]


def _params(schema: dict[str, Any]) -> list[ToolParamOut]:
    """从工具的 ``inputSchema`` 推参数列表。

    ``required`` 在 schema 里是**列表**，而响应里要逐参数标——所以转成集合再查。
    参数的 ``summary`` 取 schema 里的 ``description``：多数参数没写，那就是空串
    （界面只显示名字与必填标记，而不是显示一句我们现编的说明）。
    """
    properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    required = set(schema.get("required", ()))
    return [
        ToolParamOut(
            name=name,
            required=name in required,
            summary=str(spec.get("description") or ""),
        )
        for name, spec in properties.items()
    ]


def _mcp_endpoint(url: str | None) -> str | None:
    """MCP 接入地址：``KYLAB_MCP_PUBLIC_URL`` 的整条值。

    **不在 REST 基址后面接 ``/mcp``**：MCP 是另一个监听（默认 8765），两个端口、
    也可能两个域名。拼出来的地址端口是错的，而"服务在跑却连不上"最难排——所以这一项
    要么由部署方整条给，要么就回 ``None``（不猜、不拼）。
    """
    return (url or "").strip().rstrip("/") or None


@router.get("/tools", response_model=ToolCatalogOut, summary="对外能力目录（工具 + 技能）")
def list_tools(_caller: ReadDep) -> ToolCatalogOut:
    """这个服务对外暴露的那些能力：工具清单 + 随附的技能。

    ``items`` 的顺序跟着 ``TOOL_NAMES``（登记顺序）：界面上摆出来的顺序与 MCP 客户端
    看到的应当一致，两部分本来就是一回事。
    """
    specs = {item["name"]: item for item in tool_definitions()}
    return ToolCatalogOut(
        items=[
            ToolOut(
                name=name,
                label=TOOL_META[name].label,
                summary=TOOL_META[name].summary,
                requires_write=TOOL_META[name].requires_write,
                params=_params(specs[name]["inputSchema"]),
            )
            for name in TOOL_NAMES
        ],
        # 指纹只**转发**，不在这里自己算：它必须与 MCP 那侧（serverInfo.version 与
        # `--list-tools` 打出来的那个）是同一个数，两处各算一遍迟早对不上。
        surface_fingerprint=surface_fingerprint(),
        mcp_endpoint=_mcp_endpoint(get_settings().mcp_public_url),
        skill=SkillOut(
            name=SKILL_NAME,
            summary=SKILL_SUMMARY,
            path=SKILL_PATH,
            transports=list(SKILL_TRANSPORTS),
        ),
    )
