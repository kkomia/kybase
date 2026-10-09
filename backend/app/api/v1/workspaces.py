"""工作区端点（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md``）。

工作区是 **Agent 的项目**：一个用户指定的根目录 + 一组知识库。
它把"这个 Agent 在哪儿干活、能查哪些资料"这两件事绑在一起。

**归属口径与知识库 / 会话完全一致**（``api/auth.py`` 的 ``check_kb_scope`` 同一套）：
普通成员只看得到自己的工作区，越权与不存在都回 **404**——403 会暴露"这个 id 存在"。

**v0.59 起还有第二维：设备**（见下面 :func:`device_from_headers`）。桌面壳注入
``X-Kylab-Device: <uuid>``，于是"这台机器的项目"与"那台机器的项目"互相看不见；
不带这个头（网页版/直连 API）只看到 ``device_id IS NULL`` 的那批——语义是
**服务器端**（``root_path`` 在服务器的盘上）。管理员另有一条 ``?device=all`` 的
额外通道，用于跨机清理。

`root_path` 是这一组端点里唯一的安全边界（Agent 的文件操作会落在那里），
它的校验在 ``services/workspace.py::validate_root_path``，这里只做协议层转发。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.api.auth import require_admin, require_read, require_write
from app.api.v1.schemas import (
    DirectoryCreateIn,
    DirectoryEntryOut,
    DirectoryRenameIn,
    WorkspaceBrowseOut,
    WorkspaceCreateIn,
    WorkspaceListOut,
    WorkspaceOut,
    WorkspaceUpdateIn,
)
from app.core.caller import Caller
from app.core.exceptions import InvalidRequestError
from app.core.services import Services, get_services

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

#: 桌面壳注入的设备标识（uuid）。**没有它就等于"服务器端"**：网页版与直连 API
#: 看不到任何桌面端的项目，它们看到的是 ``device_id IS NULL`` 的那批。
DEVICE_HEADER = "X-Kylab-Device"

#: 设备名（人话，可空）。**只用于显示**，判定一律按 id。
DEVICE_NAME_HEADER = "X-Kylab-Device-Name"

#: ``GET /workspaces?device=`` 唯一接受的值（管理员专属）。
DEVICE_QUERY_ALL = "all"


@dataclass(frozen=True, slots=True)
class Device:
    """一次请求所属的设备。``id`` 是桌面壳生成的 uuid，``name`` 只给人看。"""

    id: str
    name: str = ""


def device_from_headers(
    device_id: Annotated[
        str | None,
        Header(
            alias=DEVICE_HEADER,
            description=(
                "可选。桌面壳注入的设备标识（uuid）。带上它 = 只看到/只操作落在"
                "这台机器上的工作区；不带 = 服务器端的工作区（路径在服务器的盘上）"
            ),
        ),
    ] = None,
    device_name: Annotated[
        str | None,
        Header(
            alias=DEVICE_NAME_HEADER,
            description="可选。设备名（人话），随工作区一起记下、界面显示用；不参与判定",
        ),
    ] = None,
) -> Device | None:
    """从请求头解析出设备；**没有这个头就返回 ``None``**（= 服务器端）。

    做成依赖而不是在每个端点里手取：五个端点要用同一个口径，而"空串当头"这种
    边界（curl 传了但没给值）只该判一次。空串按**没带**处理——它与"带了但没值"
    在语义上是一回事，而把它当一个设备 id 会让记录落进一个永远没人再来的桶里。
    """
    clean = (device_id or "").strip()
    if not clean:
        return None
    return Device(id=clean, name=(device_name or "").strip())


def _device_id(device: Device | None) -> str | None:
    """请求的设备 id；``None`` = 服务器端（不带设备头）。"""
    return device.id if device is not None else None


def _owner(caller: Caller) -> str | None:
    """归属过滤用：只有**普通成员**会话才有归属；管理员与 API Key 通道没有。

    与 ``api/v1/notes.py::_owner``、``api/v1/conversations.py::_caller_owner``
    同一口径。三处各写一份是因为它们分属三个模块，但判定必须一致——
    `test_visibility_api` 那组用例正是钉这件事。

    这是**账号**那一维；设备那一维见 :func:`_device_id`，两者互不代替。
    """
    if caller.user is not None and not caller.is_admin:
        return caller.user.id
    return None


def _out(record, conversation_count: int) -> WorkspaceOut:  # type: ignore[no-untyped-def]
    return WorkspaceOut(
        id=record.id,
        name=record.name,
        root_path=record.root_path,
        description=record.description,
        kb_ids=list(record.kb_ids),
        conversation_count=conversation_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
        archived_at=record.archived_at,
        device_id=record.device_id,
        device_name=record.device_name,
    )


@router.get("", response_model=WorkspaceListOut, summary="工作区列表")
def list_workspaces(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
    archived: bool = Query(
        default=False,
        description="false（默认）= 未归档的项目；true = 已归档的项目（v0.55）",
    ),
    device: str | None = Query(
        default=None,
        description=(
            "**管理员专属的额外通道**：传 all 返回**全部设备**的工作区（跨机清理用）。"
            "不传 = 按请求头 X-Kylab-Device 隔离（不带那个头就是服务器端的项目）"
        ),
    ),
) -> WorkspaceListOut:
    """默认只列**未归档**的项目；``archived=true`` 列出**已归档**的（归档视图）。

    与会话列表同一口径：归档的项目不进默认视图，要看它们得显式要——
    这样"收起来"才真的把侧栏腾干净，而找回来也有一个明确的地方。

    **设备维度是三态**（v0.59）：``device=all``（管理员）跨设备全都要；否则按请求头
    ``X-Kylab-Device`` 隔离——带了只看那台机器的，不带只看**服务器端**的。
    非管理员传 ``device=all`` 回 422 并**如实说明只有管理员能跨设备看**：
    这不是 404 那类"不告诉你有没有"，它是一条明确的权限口径。
    """
    any_device = _any_device_requested(device=device, caller=caller)
    views = services.workspaces.list(
        user_id=_owner(caller),
        archived=archived,
        device_id=_device_id(request_device),
        any_device=any_device,
    )
    return WorkspaceListOut(items=[_out(view.record, view.conversation_count) for view in views])


def _any_device_requested(*, device: str | None, caller: Caller) -> bool:
    """解析 ``?device=`` 这个**额外通道**；返回是否要跨设备。

    只认 ``all`` 一个值，别的值**当场拒**而不是忽略：静默忽略会让"我明明传了
    ``device=xxx`` 却没按它过滤"变成一个要查很久的现象。管理员之外传 ``all``
    也拒（422，而不是悄悄退化成"只看自己的"）：口径要如实说，
    否则调用方会以为拿到了跨设备的清单。
    """
    if device is None:
        return False
    if device != DEVICE_QUERY_ALL:
        raise InvalidRequestError(
            f"device 只接受 {DEVICE_QUERY_ALL}（跨设备查看，管理员专属）：{device}"
        )
    if not caller.is_admin:
        raise InvalidRequestError(
            "只有管理员能跨设备查看工作区（device=all）：普通成员只看得到"
            "自己那台机器（或服务器端）的项目"
        )
    return True


@router.get(
    "/browse",
    response_model=WorkspaceBrowseOut,
    summary="浏览服务器上的目录（选工作区根目录用）",
)
def browse_directories(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
    path: str | None = Query(default=None, description="要看哪个目录；留空 = 落在「工作区」区域"),
) -> WorkspaceBrowseOut:
    """**管理员专属**：它列的是**服务器上**的目录树。

    这条与"设置页只认管理员"同一档：目录名本身就是信息（谁的项目叫什么、
    备份放在哪、有哪些账号的家目录），而成员建工作区本来就只需要填一个路径。
    换句话说是**不给它扩权**——能浏览不改变"能不能当工作区"的判定，
    那条判定只有一份（``workspaces.root_path_problem``）。

    只列**目录**；数据目录会出现在列表里但标着不可选与原因（不藏起来：
    静默省略会让人以为"这里没有它"，而他找的可能正是它旁边那个）。

    **每一行都带上"能不能在它里面新建目录 / 能不能改名"**（v0.41）：这两个判定与真去
    动手时同一份，所以界面能把"不能建 / 不能改"说在点下去之前，而不是等点完再弹错。
    v0.58 起**新建不限区域**（除了数据目录树哪儿都能建），改名仍只在「工作区」区域里。
    """
    view = services.workspaces.browse(path)
    return WorkspaceBrowseOut(
        path=view.path,
        current=DirectoryEntryOut(**asdict(view.current)),
        parent=view.parent,
        # `dataclasses.asdict` 而不是 `vars`：`DirectoryEntry` 是 slots 数据类，没有 `__dict__`
        entries=[DirectoryEntryOut(**asdict(item)) for item in view.entries],
        roots=[DirectoryEntryOut(**asdict(item)) for item in view.roots],
        note=view.note,
        area=view.area,
    )


@router.post(
    "/dirs",
    response_model=DirectoryEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="在服务器上新建一个目录（选工作区时用）",
)
def create_directory(
    payload: DirectoryCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> DirectoryEntryOut:
    """**这是"在服务器上写东西"**，比浏览严一档（与浏览同一道管理员闸）：

    只建一层、重名当场拒（不覆盖也不合并）、名字按**可移植的那一套**校验——目录名常要在
    Windows 与 NAS 之间互拷，而在 Linux 上合法的 `a:b` 到了 Windows 上根本建不出来。

    **除了数据目录树，哪儿都能建**（v0.58）：判定与浏览时标 ``creatable`` 的是同一份，
    所以界面上灰着的那些位置，这里也一定拒——反过来，亮着的一定建得出来。而"这儿到底
    写不写得进去"**不事先探测**：真去 ``mkdir``，写不进去时把那句 ``OSError`` 原样回给
    调用方（`建不了这个目录：…`）。
    """
    entry = services.workspaces.create_directory(parent=payload.parent, name=payload.name)
    return DirectoryEntryOut(**asdict(entry))


@router.patch(
    "/dirs",
    response_model=DirectoryEntryOut,
    summary="给服务器上的目录改名（选工作区时用）",
)
def rename_directory(
    payload: DirectoryRenameIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> DirectoryEntryOut:
    """只改名不搬位置。四类目录会被拒，各自都有具体理由（见服务层）：

    文件系统根、**「工作区」区域本身**、**区域外的任何目录**（v0.58 放开的是新建，
    **改名仍在区域里**——它动的是别人的既有目录）、以及**某个工作区的根目录**
    （改了那条工作区就失联）。"""
    entry = services.workspaces.rename_directory(path=payload.path, name=payload.name)
    return DirectoryEntryOut(**asdict(entry))


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建工作区（指定根目录）",
)
def create_workspace(
    payload: WorkspaceCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> WorkspaceOut:
    """`root_path` 必须是**已存在的目录**，且不能指向数据目录或文件系统根
    （见 ``validate_root_path`` 的三道校验）。

    **设备在这一步打戳**（v0.59）：带了 ``X-Kylab-Device`` 就把 ``device_id`` /
    ``device_name`` 一起记下（这台机器的项目），不带就落 ``NULL``（服务器端）。
    之后这条记录只对同一台设备可见——改机器请在新机器上新建，
    因为 `root_path` 是那台机器上的路径。
    """
    record = services.workspaces.create(
        name=payload.name,
        root_path=payload.root_path,
        description=payload.description,
        kb_ids=payload.kb_ids,
        user_id=_owner(caller),
        device_id=_device_id(request_device),
        device_name=request_device.name if request_device is not None else "",
    )
    return _out(record, 0)


@router.get("/{workspace_id}", response_model=WorkspaceOut, summary="工作区详情")
def get_workspace(
    workspace_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> WorkspaceOut:
    """**设备不匹配与越权、不存在一样回 404**：能区分就等于承认"这个 id 存在"。"""
    record = services.workspaces.get(
        workspace_id, user_id=_owner(caller), device_id=_device_id(request_device)
    )
    return _out(record, services.workspaces.conversation_count(workspace_id))


@router.patch("/{workspace_id}", response_model=WorkspaceOut, summary="改工作区")
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> WorkspaceOut:
    """名字 / 根目录 / 描述 / 知识库 / 归档都可选，只改传了的那些。

    **归属不可改**：把一个工作区转给别人，连带的是"里头会话的 Agent 行为"，
    那是另一个功能，不该顺手做掉。**设备同样不可改**：它是这条记录的"在哪儿"，
    换机器该新建（`root_path` 是那台机器上的路径）。
    """
    record = services.workspaces.update(
        workspace_id,
        user_id=_owner(caller),
        name=payload.name,
        root_path=payload.root_path,
        description=payload.description,
        kb_ids=payload.kb_ids,
        archived=payload.archived,
        device_id=_device_id(request_device),
    )
    return _out(record, services.workspaces.conversation_count(workspace_id))


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除工作区（里面的会话退回未归档）",
)
def delete_workspace(
    workspace_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
    request_device: Annotated[Device | None, Depends(device_from_headers)],
) -> None:
    """**不删会话**：它们变成未归档，在侧栏的"未归档会话"那一栏继续存在。

    这是刻意的：会话里有用户问过的内容，误删不可恢复；而"失去归属"是可恢复的
    （重新挂一个工作区就行）。
    """
    services.workspaces.delete(
        workspace_id, user_id=_owner(caller), device_id=_device_id(request_device)
    )
