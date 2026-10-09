"""插件包端点（v0.43，设计见 ``docs/设计/插件与技能-v0.1.md``）。

**本地市场 = 目录本身**：插件是磁盘上的"一个目录 + 一份 ``plugin.json``"，
放进 ``<data_dir>/plugins/`` 就是上架，删掉目录就是下架——没有清单服务器、
没有下载、没有在线源（这一轮不做）。这一组端点只做两件事：
**列出来**（含每个插件提供了什么、加载失败的原因）与**启用/停用**。

两条刻意的设计：

1. **列表是只读的**：接口不往插件目录写任何东西，启停写在 ``app_settings``
   （状态与内容分离，照 ZCode）。所以界面上的"重新扫描"永远安全。
2. **加载失败的插件照样列出**（``loaded=false`` + ``error``），照 DSH 的
   "失败的 preset 也列出"。静默藏掉会让用户以为插件没装上，而"为什么它不生效"
   就成了一个查不出的问题。

启停**要管理员**：插件是"会不会有代码被执行"的那一档入口（与 MCP、技能市场同一档）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.auth import require_admin, require_read
from app.api.v1.schemas import PluginComponentOut, PluginListOut, PluginOut
from app.core.caller import Caller
from app.core.services import Services, get_services

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _out(record) -> PluginOut:  # type: ignore[no-untyped-def]
    """插件记录 → 界面形状。字段逐个抄进来（协议层不 import 存储/服务实现）。"""
    return PluginOut(
        name=record.name,
        version=record.version,
        description=record.description,
        author=record.author,
        homepage=record.homepage,
        source=record.source,
        path=record.path,
        manifest_path=record.manifest_path,
        enabled=record.enabled,
        blocked=record.blocked,
        loaded=record.loaded,
        error=record.error,
        components=[
            PluginComponentOut(
                kind=item.kind,
                name=item.name,
                description=item.description,
                path=item.path,
                status=item.status,
            )
            for item in record.components
        ],
        kinds=list(record.kinds),
        user_config=list(record.user_config),
    )


@router.get("", response_model=PluginListOut, summary="插件列表")
def list_plugins(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> PluginListOut:
    """**每次都重新扫磁盘**（与技能同一条理由）：用户可能刚往插件目录里丢了一个。

    ``user_dir`` / ``builtin_dir`` 一起回给界面：它们是这一页的"市场在哪"——
    用户要能看见该把目录放到哪儿，而不是读文档才知道。
    """
    items = services.plugins.list()
    return PluginListOut(
        items=[_out(item) for item in items],
        total=len(items),
        enabled=sum(1 for item in items if item.enabled and item.loaded),
        failed=sum(1 for item in items if not item.loaded),
        user_dir=str(services.plugins.user_dir),
        builtin_dir=str(services.plugins.builtin_dir),
    )


@router.post("/{plugin_id}/enable", response_model=PluginOut, summary="启用一个插件")
def enable_plugin(
    plugin_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> PluginOut:
    """``plugin_id`` 就是插件的 ``name``（manifest 里那个，ZCode / QwenPaw 同源）。

    内置插件启用时会**解除屏蔽**——启用的意思就是"随代码发布的那份我又要了"。
    """
    return _out(services.plugins.enable(plugin_id))


@router.post("/{plugin_id}/disable", response_model=PluginOut, summary="停用一个插件")
def disable_plugin(
    plugin_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> PluginOut:
    """**不碰插件目录**：停用只写一条状态（内置的另记一条屏蔽标记，照 ZCode）。

    于是"停用"永远可逆，也不会把用户自己放进来的东西删掉——
    卸载是用户在文件系统里的事。
    """
    return _out(services.plugins.disable(plugin_id))
