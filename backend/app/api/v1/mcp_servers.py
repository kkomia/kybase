"""MCP 服务端点（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.2）。

接外部 MCP 服务进来，把它们的工具变成 Agent 的能力。这一组端点做四件事：
**登记 / 改 / 删 / 用**（探活与调用）。

**凭据绝不回显**：``env`` 与 ``headers`` 的值只在写入时上行，读出时只给
"配过哪几个 key"（``secret_keys``）。这与设置页对模型 api_key 的处理一致——
回显一次，日志、截图、浏览器缓存里就各留一份。

**调用要过策略闸**：默认 ``ask``。策略命中时接口回 **409**（而不是 403）：
那不是"你不能做"，是"要先确认一下"——界面据此弹确认框，确认后带
``approved=true`` 再调一次。把闸放在服务层（而不是这里），是因为调用点将来
不止一个，放在能被绕过的地方等于没有闸。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import require_read, require_write
from app.api.v1.schemas import (
    MCPCallIn,
    MCPCallOut,
    MCPServerCreateIn,
    MCPServerListOut,
    MCPServerOut,
    MCPServerUpdateIn,
    MCPToolOut,
)
from app.core.caller import Caller
from app.core.exceptions import ForbiddenError
from app.core.services import Services, get_services
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    append_allow_rule,
    rules_from_runtime,
    suggest_rule,
)
from app.services.mcp_client import tool_qualified_name

router = APIRouter(prefix="/mcp-servers", tags=["mcp"])


def _owner(caller: Caller) -> str | None:
    """归属口径与工作区/知识库/会话一致：普通成员只看自己的（``Caller.owner_id``）。"""
    return caller.owner_id


def _out(record) -> MCPServerOut:  # type: ignore[no-untyped-def]
    return MCPServerOut(
        id=record.id,
        name=record.name,
        transport=record.transport,
        target=record.target,
        args=list(record.args),
        policy=record.policy,
        enabled=record.enabled,
        # **只给 key，不给值**：见模块头。
        secret_keys=sorted(list(record.env.keys()) + list(record.headers.keys())),
        has_secrets=bool(record.env or record.headers),
        tool_prefix=tool_qualified_name(record.name, ""),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("", response_model=MCPServerListOut, summary="MCP 服务列表")
def list_servers(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> MCPServerListOut:
    records = services.mcp.list(user_id=_owner(caller))
    return MCPServerListOut(items=[_out(item) for item in records])


@router.post(
    "",
    response_model=MCPServerOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记一个 MCP 服务",
)
def create_server(
    payload: MCPServerCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> MCPServerOut:
    """``stdio`` 服务会**起一个本地子进程**，所以这是"用户明确配置了才发生"的动作——
    没有自动发现、没有扫描，只有这里登记的才会被连。"""
    record = services.mcp.create(
        name=payload.name,
        transport=payload.transport,
        target=payload.target,
        args=payload.args,
        env=payload.env,
        headers=payload.headers,
        policy=payload.policy,
        user_id=_owner(caller),
    )
    return _out(record)


@router.get("/{server_id}", response_model=MCPServerOut, summary="单个 MCP 服务")
def get_server(
    server_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> MCPServerOut:
    """取一个服务的登记信息（**不含凭据值**）。"""
    return _out(services.mcp.get(server_id, user_id=_owner(caller)))


@router.patch("/{server_id}", response_model=MCPServerOut, summary="改 MCP 服务")
def update_server(
    server_id: str,
    payload: MCPServerUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> MCPServerOut:
    """凭据字段是**整份替换**：合并语义下"删掉一个 header"与"没传"分不开。"""
    record = services.mcp.update(
        server_id, user_id=_owner(caller), **payload.model_dump(exclude_unset=True)
    )
    return _out(record)


@router.delete(
    "/{server_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除 MCP 服务"
)
def delete_server(
    server_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    services.mcp.delete(server_id, user_id=_owner(caller))


@router.post("/{server_id}/probe", response_model=MCPServerOut, summary="测试连接并发现工具")
def probe_server(
    server_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> MCPServerOut:
    """真连一次、问它有哪些工具。

    **连不上不报 5xx**：它是「测试连接」，报错就是它的产出。这里把结果
    （``reachable`` / ``detail`` / ``tools``）一并回给界面，而不是抛异常——
    抛了的话前端只能拿到一句"请求失败"，看不到"是连不上还是没工具"。
    """
    ok, detail, tools = services.mcp.probe(server_id, user_id=_owner(caller))
    record = services.mcp.get(server_id, user_id=_owner(caller))
    # **要 exclude 掉 reachable**：`_out()` 按列表语义把它填成默认值，
    # 这里再传一次就是 "got multiple values for keyword argument"，
    # 而这个 TypeError 会让探活**永远 500**（实测：接口测试一跑就炸）。
    return MCPServerOut(
        **_out(record).model_dump(exclude={"reachable", "detail", "tools"}),
        reachable=ok,
        detail=detail,
        tools=[
            MCPToolOut(
                name=item.name,
                qualified=item.qualified,
                description=item.description,
                server_id=item.server_id,
                server_name=item.server_name,
            )
            for item in tools
        ],
    )


@router.post("/{server_id}/call", response_model=MCPCallOut, summary="调用一个外部工具")
def call_tool(
    server_id: str,
    payload: MCPCallIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> MCPCallOut:
    """按策略闸调用。``ask`` 且未确认时回 409，界面确认后带 ``approved=true`` 重调。"""
    record = services.mcp.get(server_id, user_id=_owner(caller))
    # **判定只有一处**（`MCPClientService.decide`）：会话里的工具闸与这个端点共用它，
    # 两处各写一套就会出现"这边能拦、那边拦不住"。这里只做本端点特有的事：
    # 拒绝时给 403、用户勾了"以后都允许"时把规则写进清单。
    qualified = tool_qualified_name(record.name, payload.tool)
    decision = services.mcp.decide(
        record, payload.tool, rules=rules_from_runtime(services.runtime, source="外部工具")
    )
    if decision.action == ACTION_DENY:
        raise ForbiddenError(f"这个外部工具被拦下：{decision.reason}")
    if payload.remember and decision.action == ACTION_ASK:
        append_allow_rule(services.runtime, suggest_rule(qualified, ""))
    text = services.mcp.call(
        record,
        payload.tool,
        payload.arguments,
        # 这一档是**这个端点特有**的：界面上确认过（``approved``）就算问过了。
        # 会话那条链路没有这个往返，所以它那边 ``ask`` 只能不执行（见 agent_tools）。
        approved=payload.approved or decision.action == ACTION_ALLOW,
    )
    return MCPCallOut(server_id=server_id, tool=payload.tool, text=text)


@router.get("/tools", response_model=list[MCPToolOut], summary="所有已登记服务的工具")
def list_all_tools(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> list[MCPToolOut]:
    """把每个**启用中**的服务的工具汇总起来（带限定名），给界面与提示词用。

    某个服务连不上时**跳过它并继续**：一个外部服务挂了不该让整张能力清单消失——
    那正是"接外部依赖"最不该有的耦合。

    从缓存读（``available_tools``），**与对话那侧读的是同一份**：两处各自去连一遍
    不但慢（stdio 会起子进程），还会出现"能力页显示有、对话里却调不动"这种
    极其难查的不一致。
    """
    return [
        MCPToolOut(
            name=tool.name,
            qualified=tool.qualified,
            description=tool.description,
            server_id=tool.server_id,
            server_name=tool.server_name,
        )
        for _record, tool in services.mcp.available_tools(user_id=_owner(caller))
    ]
