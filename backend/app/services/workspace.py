"""工作区（Agent 的项目，v0.15）。

见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §3。**工作区回答"在哪儿干活"**，
与**沙箱**（"试错的地方在哪"，见 ``services/sandbox.py``）是两个概念：

| | 工作区 | 沙箱 |
| --- | --- | --- |
| 生命周期 | 长期，用户拥有 | 一次会话，可丢弃 |
| 位置 | 用户指定的路径 | `data/sandbox/<conversation_id>/` |
| 内容 | 真实项目文件 | 临时脚本、中间产物 |

**归属有两个维度，各自只有一份判定**（v0.59）：

- **账号**（``owner_id``）：与知识库 / 会话完全一致（`owner_id`）。管理员与 API Key
  通道（``user is None``）看得到全部，普通成员只看自己的；
- **设备**（``device_id``）：桌面壳每次请求带 ``X-Kylab-Device: <uuid>``，于是
  "这台机器的项目"与"那台机器的项目"互相看不见。**不带设备头**（网页版/直连 API）
  只看到 ``device_id IS NULL`` 的那批——语义是**服务器端**：``root_path`` 在服务器的
  盘上。管理员另有一条 ``device=all`` 的额外通道（跨机清理用）。

两维都判、都只判一次（``_visible`` 与 ``_device_visible``），并且都收敛在
:meth:`WorkspaceService.get` 里——``update`` / ``delete`` / ``bind_conversation``
先走它，不会出现"某一条路忘了判"。**越权一律 404 而不是 403**
——后者会暴露"这个 id 存在"。

`root_path` 的三道校验都在 ``validate_root_path`` 里，那是本模块唯一的安全边界：
它决定了 Agent 的文件操作能落到磁盘的哪个位置。

**浏览与写盘不是一回事**（v0.41）。浏览列的是整台机器，但"能看见"从来不等于"能写"。
所以有一块**专用区域** ``<data_dir>/workspaces``（见 :func:`workspace_area`）：它是
选择器的**默认落脚点**，也是**改名**的唯一范围。

**新建目录不再限区域**（v0.58，用户点名："在 D:\\ 下也得能建"）：除了**数据目录树**
（那里是服务端自己的库、原件与其它账号的数据，见 :func:`create_problem`）哪儿都能建，
而"这儿到底能不能写"**不做写性探测**——由真实 ``mkdir`` 的 ``OSError`` 当场如实回答。
改名没有跟着放开（它动的是别人的既有目录，风险另算，见 :func:`rename_problem`）。

三件事（能不能选、能不能建、能不能改名）各自只有一份判定，浏览时的标记与真去动手时的
拒绝**用的是同一个函数**，不会出现"按钮亮着、点了却报错"。
"""

from __future__ import annotations

import logging
import os
import string
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.storage.base import StoreBundle, WorkspaceRecord

__all__ = [
    "MAX_DIRECTORY_NAME_CHARS",
    "WORKSPACE_AREA_NAME",
    "BrowseView",
    "DirectoryEntry",
    "WorkspaceService",
    "create_problem",
    "rename_problem",
    "root_path_problem",
    "validate_root_path",
    "workspace_area",
]

logger = logging.getLogger(__name__)

#: 名字长度上限。与知识库同一个量级（120），界面上都是单行标题。
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 500

#: 一次浏览最多列多少个子目录。**与工具面那些列表同一个量级**：真在一个有几万个子目录的
#: 目录里（比如某个媒体库）全列出来既慢又没用，而"前 N 个 + 还剩多少"足够让人判断
#: "对着不对"——他要找的东西要么在前面，要么他该换个起点（起点里有路径输入）。
MAX_BROWSE_ENTRIES = 300

#: 起点清单里专用区域那一项的显示名。**不叫"工作区"**：那个词在这套界面里已经指
#: "一个具体的项目"（侧栏那一栏），而这是"放这些项目的那个地方"。
AREA_ROOT_LABEL = "工作区区域"

#: 起点最多给几个。家目录、盘符、已有工作区目录加起来可能很多（工作区多时），
#: 而起点是"跳到某个熟悉的地方"用的，七八个足够；再多就成了一份目录清单。
MAX_BROWSE_ROOTS = 8


#: 专用可写区域的名字：``<data_dir>/workspaces/``（v0.41）。
#:
#: **为什么要有它**：区域是选择器的**默认落脚点**（新建的工作区放哪儿有个可预期的地方），
#: 也是**改名**的唯一范围（改名动的是别人的既有目录，得有块说得清归属的地方）。
#: **新建目录不止这一处**（v0.58 起除数据目录树外哪儿都能建，见 :func:`create_problem`）。
#:
#: 它跟着 ``data_dir`` 走，所以**已经是可配的**：``KYLAB_DATA_DIR`` 指到哪，
#: 区域就在哪，不另开一个只在 docker 里有意义的配置项。
WORKSPACE_AREA_NAME = "workspaces"


def workspace_area(data_dir: Path) -> Path:
    """专用区域：``<data_dir>/workspaces``（只算路径，**不创建**；返回解析后的绝对路径）。

    它落在**数据目录里**（数据随备份/迁移一起走，一个部署只有一处要备份），
    所以 ``root_path_problem`` 里"数据目录不能当工作区"那一条必须给它让路——
    区域里的目录正是最该能当工作区的地方。这个例外**只开一次、只开给这一个前缀**：
    除了它，数据目录里的一切照旧不可选、也不可写。

    ``data_dir`` 的默认值是相对路径（``./data``），所以这里必须 ``resolve()``：
    不然拿一个没解析过的 ``./data/workspaces`` 去和浏览回来的绝对路径比，
    区域会被判成"不在区域里"——那正是最不该出错的地方。
    """
    return (data_dir / WORKSPACE_AREA_NAME).resolve()


def root_path_problem(path: Path, *, data_dir: Path) -> str | None:
    """``path`` 能不能当工作区：能回 ``None``，不能回**一句给人看的原因**。

    **只有一份判定**（``validate_root_path`` 也走它），因为这件事有两个消费者：
    建/改工作区时抛异常，以及浏览目录时把不可选的目录标出来（灰掉 + 说明）。
    两份判定迟早会出现"能选但建不出来"（或者反过来），而那种不一致最难查。

    ``path`` 应当是**已经解析过**的绝对路径（``resolve()`` 之后）：
    存在性、``~`` 展开、软链的真实位置都由调用方先处理掉。
    """
    if not path.is_dir():
        return f"这个路径不是目录：{path}"

    # 第 1 道：文件系统根。`path.parent == path` 是"它是自己的父目录"，也就是根
    # （`/` 或 `C:\`）——比字符串比较可靠。
    if path.parent == path:
        return "不能把文件系统根目录作为工作区"

    # 第 2 道：专用区域**自身**。区域是所有工作区的容器，拿它当工作区等于
    # 把别人的项目一起交出去（成员也能直接填这个路径，而它在部署里很好猜）。
    # 里面的子目录不受影响——那正是"新建一个目录再用它"的正常用法。
    area = workspace_area(data_dir)
    if path == area:
        return (
            "「工作区」区域本身不能当工作区：它是所有工作区的容器，"
            "指过去等于把里面每一个项目一起交出去。在里面新建一个目录再用它"
        )
    # 上面的 == 分支已经拦掉了区域自身，走到这里就是"区域里的某个目录"——正是
    # 该能当工作区的地方（下面那道数据目录的规则要跳过它）
    if path.is_relative_to(area):
        return None

    # 第 3 道：数据目录（区域除外，见上）。**必须是 resolve 之后比较**：
    # `data/link-to-elsewhere` 这种软链能绕过纯字符串前缀判断。
    guarded = data_dir.resolve()
    if path == guarded or path.is_relative_to(guarded):
        return (
            "不能把数据目录（或它里面的目录）作为工作区："
            "那里存着数据库、原件与其它账号的数据，指向它等于绕过了账号隔离"
        )
    return None


def create_problem(path: Path, *, data_dir: Path) -> str | None:
    """``path`` 这一层**能不能新建目录**：能回 ``None``，不能回一句给人看的原因。

    与 :func:`root_path_problem` 一样，"只有一份判定"：浏览时把一行标成
    ``creatable=False`` 用的就是它，真去新建时被拒的也是同一句话——所以界面上灰掉的
    那一行，点下去一定建不出来，反过来也一定建得出来。

    规则本身很短（v0.58，用户点名改的）：**除了数据目录树，哪儿都能建**。文件系统根、
    区域外的任意目录、用户在自己盘上挑的自定义路径，全都放行（工作区本来就该能开在
    用户自己想放的地方）。唯一拒的是**数据目录**（``<data_dir>/workspaces`` 区域除外，
    见 :func:`workspace_area`）：那里存着数据库、原件、记忆与其它账号的数据，往里建
    目录没有意义，而那句原因比"这里只读"具体得多。

    **不做写性探测**（不先试写一个临时文件问"这儿能不能写"）：能不能写由真实 ``mkdir``
    的 ``OSError`` 当场回答（见 :meth:`WorkspaceService.create_directory` 的上抛）。
    探测是**投机**——它答的是"此刻能不能写"，而用户点的是"待会儿在那里面建"，两者在
    只读分区、权限收紧、软链目标掉了这些情况下并不一致；多一份可能与真实结果不同的
    判定，就多一处会漂的地方，而"建失败时说清为什么"本来就已经有了。

    ``path`` 应当是**已经解析过**的绝对路径（``resolve()``，见 :func:`_resolved`）：
    判定按真实位置做，`<区域>/link -> 数据目录` 这种绕过拦得住。
    """
    if _in_area(path, workspace_area(data_dir)):
        return None
    guarded = data_dir.resolve()
    if path == guarded or path.is_relative_to(guarded):
        return _inside_data_directory_problem(action="新建目录")
    return None


def rename_problem(
    path: Path, *, data_dir: Path, workspace_names: Mapping[str, str]
) -> str | None:
    """``path`` 这个目录**能不能改名**：能回 ``None``，不能回一句给人看的原因。

    **改名仍只在专用区域里**（v0.41；v0.58 放开的是新建，改名没跟着放——它动的是
    **别人的既有目录**，改错了名字那个目录就不见了，风险与"新建一个空目录"不是一回事）。
    除区域外，另外四条拒绝各有各的理由：

    - **文件系统根**：`/` 或 `C:\\` 不能改名（那是整台机器的根）；
    - **专用区域本身**：它是选择器的默认落脚点，改了名字就没地方落脚了；
    - **数据目录的祖先**：改了名字，运行中的服务就找不到自己的库了；
    - **某个工作区的根目录**：改了名字那条工作区记录就指向一个不存在的位置，
      表现为"这个工作区突然什么都读不到"。

    ``workspace_names`` 是"解析后的根目录 → 工作区名"，由调用方一次查好传进来：
    浏览时一屏几十行都用得上，逐行去查库是不能按数据量增长的查询。
    ``path`` 应当是**已经解析过**的绝对路径（``resolve()``，见 :func:`_resolved`）。
    """
    # 文件系统根：它永远在区域外，下面那条规则也拦得住，但"根目录不能改名"比
    # "只有「工作区」区域里能改名"更该说给人听——他对 `C:\` 做的事和别的目录不是一回事
    if path.parent == path:
        return "文件系统根目录不能改名"
    area = workspace_area(data_dir)
    if path == area:
        return (
            "「工作区」区域本身不能改名：它是选择器的默认落脚点，"
            "改了名字（或挪走）就找不回来了"
        )
    if not _in_area(path, area):
        guarded = data_dir.resolve()
        # 数据目录的**祖先**：这是只有"改名"才会踩到的一条（改了名字，运行中的服务
        # 就找不到自己的库了），在开发机上很常见——数据目录就在仓库里
        if path != guarded and guarded.is_relative_to(path):
            return (
                f"「{path.name}」里面有服务端的数据目录，改名之后服务就找不到自己的库了。"
                "要改的话先把数据目录挪到别处（`KYLAB_DATA_DIR`）"
            )
        return _outside_area_problem(path, data_dir=data_dir, action="改名")
    owner = workspace_names.get(str(path))
    if owner is not None:
        return (
            f"「{path.name}」是工作区「{owner}」的根目录，改名会让那条工作区失联。"
            "先在界面上改那个工作区（或删掉它），再来改目录名"
        )
    return None


def _in_area(path: Path, area: Path) -> bool:
    """``path`` 是不是专用区域自身或它里面的东西。

    **"在不在区域里"只有这一处判定**：三条规则各自的区域分支都从它出发——
    ``create_problem``（区域在数据目录里面，得先让开那一条）、``rename_problem``、
    以及 ``root_path_problem``（区域自身不能当工作区）。

    v0.58 起"能不能建"不再等于"在不在区域里"（区域外也能建，见 ``create_problem``），
    所以别再拿它当"可写"的同义词——它是**专用区域这个前缀本身**。

    ``path`` 必须是**解析过**的（见 :func:`_resolved`）：纯前缀比较在软链面前
    两个方向都骗得过——`<区域>/link -> 区域外` 会被当成"在区域里"。
    """
    return path == area or path.is_relative_to(area)


def _resolved(path: Path) -> Path:
    """解析软链与 ``..``；解析不了（断链、环、权限）就退回字面路径。

    **所有判定都应当用它**（写盘那两条路本来就是 ``resolve(strict=True)``）：
    ``is_relative_to`` 这种前缀比较对软链是纸糊的，而"界面标着能建、真去建被拒"
    正是这一轮要消灭的那种不一致。退回字面路径是保守的：断链本来也不该被选中。
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _inside_data_directory_problem(*, action: str) -> str:
    """数据目录树里为什么不能写盘。

    **这一句是新建与改名共用的**（v0.58 起只剩"新建"走得到这里，改名那边也仍会用到
    数据目录那一条，见 :func:`_outside_area_problem`）：它比"这里只读"具体得多——
    说清了服务端自己的数据在哪儿、该去哪儿放项目。两句共用一套模板，免得同一件事
    出现两种说法。
    """
    return (
        f"数据目录里不能{action}：那里是服务端自己的数据（数据库、原件、记忆）。"
        "要放项目用里面的「工作区」区域"
    )


def _outside_area_problem(path: Path, *, data_dir: Path, action: str) -> str:
    """区域外**改名**为什么被拒：数据目录里与别处各说各的。

    **一条规则，两句解释**（不是两套规则）：数据目录里那句比"这里只读"具体得多
    （它告诉用户服务端自己的数据在哪儿），而这两种输入在界面上都会遇到，
    所以宁可在这里分一次支，也不要让用户看着一句通用的话猜。

    v0.58 起**只有改名走这条路**（新建已经在 :func:`create_problem` 里直接放行区域外
    的路径），所以"别处"那一句说的就是"改名只在区域里"。

    ``action`` 是"这一层想做的事"（改名）：两句共用一套模板，
    免得同一件事在这里出现两种说法。
    """
    guarded = data_dir.resolve()
    if path == guarded or path.is_relative_to(guarded):
        return _inside_data_directory_problem(action=action)
    return (
        f"只有「工作区」区域里能{action}：区域外那些目录的归属不归这个服务管"
        "（改了名字影响的是谁，这里判不出来）。"
        "起点里有「工作区区域」那一项，点它就能进去"
    )


def validate_root_path(raw: str, *, data_dir: Path) -> str:
    """校验并规范化工作区根目录，返回**绝对路径字符串**。

    三道校验，缺一不可：

    1. **必须存在且是目录**。允许用户填一个还没建的路径听起来友好，但那样
       "工作区建好了却什么都读不到"会变成一个要查很久的现象——不如当场说清。
    2. **拒绝文件系统根与系统关键目录**。把工作区设成 ``/`` 或 ``C:\\`` 等于
       把整台机器交给 Agent 的文件操作，而这不是"配置错了"，是**权限事故**。
    3. **拒绝指向数据目录自身**（含其内部任意层级）——**但专用区域除外**（v0.41）。
       那里放着数据库文件、原件、记忆与其它账号的数据，让一个工作区指过去，等于
       绕过了所有归属隔离。而 ``<data_dir>/workspaces`` 正是"给工作区用的地方"，
       它里面的目录必须可选（见 :func:`root_path_problem` 的第 2 道）。

    符号链接用 ``resolve()`` 之后的真实路径判断，避免"链到数据目录"这种绕过。

    后两道判定在 :func:`root_path_problem` 里——**浏览目录那条路用的是同一份**。
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidRequestError("缺少参数：root_path（工作区的根目录）")

    target = Path(text).expanduser()
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidRequestError(f"这个路径不存在或不可访问：{text}") from exc
    if not resolved.is_dir():
        raise InvalidRequestError(f"这个路径不是目录：{text}")

    problem = root_path_problem(resolved, data_dir=data_dir)
    if problem:
        raise InvalidRequestError(problem)
    return str(resolved)


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """浏览目录时的一行：一个子目录，或一个"起点"。

    **三个问题、三对字段**（v0.41）：能不能拿它当工作区（``selectable``）、能不能在
    它里面新建目录（``creatable``）、能不能给它改名（``renamable``）。合成一个
    ``reason`` 会让人分不清是"不能选"还是"不能建"——而这两件事的下一步动作不一样。
    每一对都是"判定 + 原样显示的原因"，三份判定各自只有一处（见模块顶部三个函数），
    浏览时的标记与真去动手时的拒绝共用它们。
    """

    name: str
    """显示用。子目录就是目录名；起点是"家目录""D:\\"这类人话标签。"""
    path: str
    selectable: bool
    """能不能直接拿它当工作区。**判定与建工作区时同一份**（见 ``root_path_problem``），
    所以界面上灰掉的那些，点"选择"也一定建不出来。"""
    reason: str = ""
    """不能选的原因（原样显示给用户）。"""
    creatable: bool = True
    """能不能在**它里面**新建目录（判定见 ``create_problem``）。

    **除了数据目录树都是真**（v0.58）：文件系统根、区域外的任意目录、用户自己挑的
    自定义路径都能建，"写不进去"由真实 ``mkdir`` 的 ``OSError`` 当场回答（见
    ``WorkspaceService.create_directory``），不做事先的写性探测。默认 ``True`` 是给
    "不是从浏览来的"那一小撮构造用的（比如建完目录后返回的那一行），浏览给的行一律
    显式带上真值。"""
    create_reason: str = ""
    """不能在它里面新建目录的原因（原样显示）。"""
    renamable: bool = True
    """能不能给它改名（判定见 ``rename_problem``）。"""
    rename_reason: str = ""
    """不能改名的原因（原样显示）。"""


@dataclass(frozen=True, slots=True)
class BrowseView:
    """一次浏览的结果。"""

    path: str
    current: DirectoryEntry
    """**当前这一层自己**能不能选。

    服务端给而不是让界面自己判：那条判定（数据目录 / 文件系统根 / 专用区域）只有一份，
    界面再去猜一次就会出现"按钮亮着但建不出来"。它也从侧面回答了
    "我现在站的这一层是什么"（名字 + 完整路径）。
    """
    parent: str | None
    """上一级；已经是这个根的最上层时为 ``None``（界面上把"上一级"置灰）。"""
    entries: list[DirectoryEntry]
    roots: list[DirectoryEntry]
    """起点清单（专用区域 / 家目录 / 盘符 / 已有工作区的目录）：路径很长时不用从根一路点下来。"""
    note: str = ""
    """一句人话说明（"共有 N 个，只列了前 M 个"这类）。空串 = 没什么要说的。"""
    area: str = ""
    """专用可写区域（``<data_dir>/workspaces``）：选择器的默认落脚点，也是**改名**的唯一范围
    （v0.58 起新建不限区域，见 :func:`create_problem`）。

    界面用它认出"现在站的这一层就是区域"（给一句"在这里可以新建/改名"的提示）。
    **由服务端给而不是界面自己拼**：数据目录在哪只有服务端知道，
    前端拼一次就会在换了 ``KYLAB_DATA_DIR`` 的部署里错位。
    """


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    """给界面用的工作区视图：记录本身 + 会话计数。

    计数由存储层单独查（一条 ``count(*)``），**不是把会话全拉回来再数**——
    侧栏每次渲染都要这些数字，那是不能按数据量增长的查询。
    """

    record: WorkspaceRecord
    conversation_count: int


class WorkspaceService:
    def __init__(self, stores: StoreBundle, data_dir: Path) -> None:
        self._stores = stores
        self._data_dir = data_dir

    # ------------------------------------------------------------------ 读

    def list(
        self,
        *,
        user_id: str | None,
        archived: bool = False,
        device_id: str | None = None,
        any_device: bool = False,
    ) -> list[WorkspaceView]:
        """列出可见的工作区。``user_id=None`` = 管理员/API Key 通道，看全部。

        ``archived`` 与会话列表同口径（``services/conversation.py::list``）：默认
        （``False``）只列**未归档**的，``True`` 时列**已归档**的——归档视图是一个
        单独的视图，不是"多出来的一组"。归属过滤与归档过滤都在这里做，
        存储层不认识调用者身份（同会话那条取舍）。

        **设备维度是三态**（v0.59）：``any_device=True`` 全都要（管理员的跨机清理
        通道，见 ``api/v1/workspaces.py`` 的 ``device=all``）；否则只列
        ``device_id`` 与参数相等的那一批，其中 ``None`` 是**服务器端**——不带设备头
        的网页版/直连 API 看到的就是它。设备那一层过滤交给存储（一条 ``WHERE``），
        与归属那一层在 Python 里过滤不是一回事：后者要顺带给出"管理员看全部"的语义。
        """
        records = self._stores.meta.list_workspaces(device_id=device_id, any_device=any_device)
        visible = [
            item
            for item in records
            if self._visible(item, user_id) and (item.archived_at is not None) == archived
        ]
        return [
            WorkspaceView(
                record=item,
                conversation_count=self._stores.meta.count_workspace_conversations(item.id),
            )
            for item in visible
        ]

    def conversation_count(self, workspace_id: str) -> int:
        """这个工作区下有多少会话（侧栏那个计数）。

        **不校验归属**：它只被 ``get()`` 已经校验过的路径调用（见 api 层），
        内部再查一次等于把同一条规则写两遍。
        """
        return self._stores.meta.count_workspace_conversations(workspace_id)

    def get(
        self,
        workspace_id: str,
        *,
        user_id: str | None,
        device_id: str | None = None,
        any_device: bool = False,
    ) -> WorkspaceRecord:
        """取一个工作区。**越权与不存在都回 404**：403 会暴露"这个 id 存在"。

        **两个维度、两道闸**：账号（``_visible``）与设备（``_device_visible``），
        各自只有一份判定，而 ``update`` / ``delete`` / ``bind_conversation`` 都先走
        这里——所以只要这一处判了，那三条路不会漏。``any_device`` 是管理员那条
        ``device=all`` 通道（跨机清理），它只放开**设备**那一维，归属照旧。
        """
        record = self._stores.meta.get_workspace(workspace_id)
        if (
            record is None
            or not self._visible(record, user_id)
            or not self._device_visible(record, device_id, any_device=any_device)
        ):
            raise NotFoundError(f"工作区不存在：{workspace_id}")
        return record

    # -------------------------------------------------------------- 浏览目录

    def browse(self, path: str | None = None) -> BrowseView:
        """列一个目录下的**子目录**，供界面"选一个目录当工作区"。

        **为什么这件事必须由服务端做**：工作区根目录是**服务器上**的路径
        （后端跑在 NAS 上），而浏览器拿不到、也不该拿到服务器上的绝对路径
        （`showDirectoryPicker` 给的是客户端本机的一个句柄，指向的还是错误的那台机器）。
        所以"选择"只能是"服务端列给你看"。

        四条规则：

        - **只列目录**（工作区根必须是目录，列出文件只会让人点错）；
        - **数据目录会出现但点不动**：它标着不可选与原因。**不藏起来**是有意的——
          静默省略会让人以为"这里没有这个目录"，而他要找的可能正是它旁边那个；
        - **可选性用的是建工作区那一份判定**（``root_path_problem``），所以灰掉的
          一定也建不出来，不会出现"能选但是建失败"；
        - **能不能建 / 能不能改名也一并标出来**（``creatable`` / ``renamable``，v0.41）：
          这两件事的判定与真去动手时同一份，界面因此能把"不能建 / 不能改"说在点下去
          之前——原因得摆在他要点的那个位置旁边，而不是等他点完再弹一个错。

        ``path`` 为空时落在**专用区域**（见 :func:`workspace_area`），并在 ``roots`` 里
        给出专用区域 / 家目录 / 盘符 / 已有工作区目录这几个起点——路径很深时不用从根
        一路点下来。默认从区域起步是有意的：家目录在容器里往往只读甚至不存在，
        而区域是选择器的落脚点（新建的工作区多半就放这儿）。
        """
        target = self._browse_root(path)
        entries, note = self._subdirectories(target)
        return BrowseView(
            path=str(target),
            current=self._describe(target, workspace_names=self._workspace_root_names()),
            parent=None if target.parent == target else str(target.parent),
            entries=entries,
            roots=self._browse_roots(),
            note=note,
            area=str(workspace_area(self._data_dir)),
        )

    def _describe(
        self,
        target: Path,
        *,
        label: str | None = None,
        workspace_names: Mapping[str, str] | None = None,
    ) -> DirectoryEntry:
        """把一个目录描述成一行（名字 + 三件事的判定与原因）。

        三个 ``*_problem`` 就是三份判定本身，这里只把它们摆到一行上——
        界面据此灰按钮、显示原因，**不自己判**（自己判一次就多一份会漂的规则）。

        **判定拿解析后的路径**：写盘那两条路（``_writable_parent`` / ``_renameable``）
        都是先 ``resolve`` 再判，两边必须拿同一个东西去判——否则软链会让界面说
        "这儿能建"而真去建时被拒（`<区域>/link -> 数据目录` 就是这种）。
        ``path`` 那一栏仍给字面路径：它是用户点出来的那个位置，导航与提交都按它走。
        """
        real = _resolved(target)
        select = root_path_problem(real, data_dir=self._data_dir)
        create = create_problem(real, data_dir=self._data_dir)
        rename = rename_problem(
            real, data_dir=self._data_dir, workspace_names=workspace_names or {}
        )
        return DirectoryEntry(
            name=label or target.name or str(target),
            path=str(target),
            selectable=select is None,
            reason=select or "",
            creatable=create is None,
            create_reason=create or "",
            renamable=rename is None,
            rename_reason=rename or "",
        )

    def _workspace_root_names(self) -> dict[str, str]:
        """已有工作区的根目录：解析后的路径 → 工作区名。

        **一次查好、整屏复用**（见 ``rename_problem`` 的参数说明）：浏览一屏几十行，
        逐行去查库就是一次不能按数据量增长的查询。解析不了的记录跳过——
        它已经指向一个不存在的位置，不该让整次浏览跟着失败。

        **跨设备取全部**（``any_device=True``，v0.59）：这条查的是**服务器上的路径**
        与工作区记录的对应关系（浏览本来就是管理员专属的服务器视角），
        少取一批只会让"某个工作区指着这个目录"被漏判，改名就会把它的根目录改走。
        """
        found: dict[str, str] = {}
        for record in self._stores.meta.list_workspaces(any_device=True):
            try:
                found[str(Path(record.root_path).resolve())] = record.name or "未命名"
            except (OSError, RuntimeError):
                continue
        return found

    # ------------------------------------------------------- 建 / 改目录名

    def create_directory(self, *, parent: str, name: str) -> DirectoryEntry:
        """在 ``parent`` 下新建一个目录，返回它的描述。

        **这是"在服务器上写东西"**，所以比浏览严一档：只建**一层**（不替你递归造父目录——
        那等于按一个手滑的名字造出一串目录）、重名当场拒（不覆盖、不合并不提示）、
        名字按可移植的那一套校验（见 :func:`_clean_directory_name`）。

        **除了数据目录树，哪儿都能建**（v0.58，用户点名："在 D:\\ 下也得能建"）。
        判定走 :func:`create_problem`，与浏览时标 ``creatable=False`` 的是同一份；
        而"这儿到底写不写得进去"**不事先探测**——真去 ``mkdir``，失败了把那个
        ``OSError`` 原样说给人听（``建不了这个目录：…``）。只读分区、权限不够、
        名字被系统规则挡住，走的都是这一条。
        """
        target = self._writable_parent(parent)
        clean = _clean_directory_name(name)
        created = target / clean
        if created.exists():
            raise InvalidRequestError(f"「{clean}」已经存在了，换一个名字")
        try:
            created.mkdir()
        except OSError as exc:
            raise InvalidRequestError(f"建不了这个目录：{exc}") from exc
        return self._describe(created, workspace_names=self._workspace_root_names())

    def rename_directory(self, *, path: str, name: str) -> DirectoryEntry:
        """把一个目录改名（**只改名字，不搬位置**）。

        拒绝的规则都在 :func:`rename_problem` 里（**只有那一份**）：文件系统根、
        专用区域本身、数据目录的祖先、区域外的任何目录（**改名只在区域里**，
        v0.58 放开的是新建，不是改名）、以及某个工作区的根目录——每条都带具体理由，
        和浏览时把一行标成 ``renamable=False`` 用的是同一句话。
        """
        clean = _clean_directory_name(name)
        source = self._renameable(path)
        target = source.parent / clean
        if target == source:
            return self._describe(source)  # 名字没变：当作成功（幂等）
        if target.exists():
            raise InvalidRequestError(f"「{clean}」已经存在了，换一个名字")
        try:
            source.rename(target)
        except OSError as exc:
            raise InvalidRequestError(f"改不了名字：{exc}") from exc
        return self._describe(target, workspace_names=self._workspace_root_names())

    def _writable_parent(self, parent: str) -> Path:
        """新建目录时的父目录：必须在、必须是目录、**且这一层允许建**。

        可建判定只有一处（:func:`create_problem`）：浏览时那一行标着
        ``creatable=False`` 的（只有数据目录树），走到这里就会拿到同一句话并 422 掉。
        "真的写不写得进去"不在这里管——那由下面那次 ``mkdir`` 的 ``OSError`` 回答。
        """
        target = self._browse_root(parent)
        problem = create_problem(target, data_dir=self._data_dir)
        if problem:
            raise InvalidRequestError(problem)
        return target

    def _renameable(self, path: str) -> Path:
        """要改名的那个目录：存在、是目录、且通过 :func:`rename_problem`。

        存在性与"是不是目录"在这里处理（它们是"这个路径本身对不对"），
        "该不该拒"全部交给那个函数——浏览时的标记与这里的拒绝必须是同一份。
        """
        text = (path or "").strip()
        if not text:
            raise InvalidRequestError("缺少参数：path（要改名的目录）")
        try:
            source = Path(text).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidRequestError(f"这个路径不存在或不可访问：{text}") from exc
        if not source.is_dir():
            raise InvalidRequestError(f"这是一个文件，不是目录：{text}")

        problem = rename_problem(
            source, data_dir=self._data_dir, workspace_names=self._workspace_root_names()
        )
        if problem:
            raise InvalidRequestError(problem)
        return source

    def _browse_root(self, path: str | None) -> Path:
        """把请求的路径解析成一个**可以列**的目录。

        与 ``validate_root_path`` 的区别：这里**允许列数据目录本身**——
        列出来无害（界面标着不可选），而"点进去看看"是人的正常动作。
        真正不能做的是把它当工作区，那条规则在 ``root_path_problem`` 里。

        ``path`` 为空时落在**专用区域**（顺手把它准备好，见 :func:`_ensure_area`）：
        那是选择器的落脚点（新建的工作区多半放那儿），而家目录在容器里常常是只读的
        甚至不存在——打开选择器却落在一个"什么都做不了"的目录里，正是这一轮要修的
        那个体感。
        """
        text = (path or "").strip()
        if not text:
            area = self._ensure_area()
            if area is not None:
                return area
            home = Path.home()
            if home.is_dir():
                return home
            # 家目录都读不到（极少见）时退回数据目录的父目录：至少是个能列的地方
            return self._data_dir.parent.resolve()
        try:
            resolved = Path(text).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidRequestError(f"这个路径不存在或不可访问：{text}") from exc
        if not resolved.is_dir():
            # 说清它的父目录是哪儿：用户多半是把文件拖进来了，下一步该去哪一目了然
            raise InvalidRequestError(
                f"这是一个文件，不是目录：{text}。它的父目录是 {resolved.parent}"
            )
        return resolved

    def _ensure_area(self) -> Path | None:
        """把专用区域准备好（**幂等**），返回它；建不出来时返回 ``None``。

        由浏览顺手做：区域是这个功能的落点，第一次打开选择器时它还不存在，
        而"先去 shell 里建一个目录"是把配置问题推给用户。GET 里写盘不常见，
        但这里写的只是一个固定位置的空目录（``mkdir(exist_ok=True)`` 天然幂等）；
        失败也不抛——只读分区上退回家目录，浏览照样能用，只是少一个默认落脚点。
        """
        area = workspace_area(self._data_dir)
        try:
            area.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("建不了工作区专用区域 %s：%s", area, exc)
            return None
        return area

    def _subdirectories(self, target: Path) -> tuple[list[DirectoryEntry], str]:
        """列出 ``target`` 下的子目录（数量封顶，并把"截断了"如实说出来）。"""
        found: list[Path] = []
        skipped = 0
        try:
            children = list(target.iterdir())
        except OSError as exc:
            raise InvalidRequestError(f"这个目录读不了：{exc}") from exc
        for child in children:
            try:
                # **符号链接跟着走**（`is_dir()` 会解引用）：链到别处的目录是合法的工作区，
                # 而"链到数据目录"那种绕过后面的 `root_path_problem` 会拦住
                if not child.is_dir():
                    continue
            except OSError:
                skipped += 1  # 断链 / 权限不足：跳过，不因为一条坏链让整次浏览失败
                continue
            found.append(child)
        # **隐藏目录排后面**（而不是藏起来）：家目录里 `.cache` / `.cargo` 这类东西一堆，
        # 排前面会把真正的项目挤下去；但它们是合法的工作区，藏起来就成了
        # "我明明有那个目录，为什么选不到"
        found.sort(key=lambda item: (item.name.startswith("."), item.name.casefold()))

        # 三个判定都在 `_describe` 里（各只有一份）：灰掉的一定也建不出来 / 改不了名。
        # 工作区根目录的名字**查一次**给整屏用，不给每一行各查一次
        workspace_names = self._workspace_root_names()
        entries = [
            self._describe(child, workspace_names=workspace_names)
            for child in found[:MAX_BROWSE_ENTRIES]
        ]
        notes = []
        if len(found) > MAX_BROWSE_ENTRIES:
            notes.append(f"这个目录里共有 {len(found)} 个子目录，只列了前 {MAX_BROWSE_ENTRIES} 个")
        if skipped:
            notes.append(f"有 {skipped} 个目录读不了（权限或断链），没有列出来")
        return entries, "；".join(notes)

    def _browse_roots(self) -> list[DirectoryEntry]:
        """起点清单：**专用区域**、家目录、盘符（Windows）、已有工作区的目录。

        **专用区域排第一**（v0.41）：它是选择器的默认落脚点（新建的工作区多半放这儿），
        也是改名的唯一范围——起点里排在最后会让人先看到家目录、点进去、然后才想起来
        还有那么一项。**已有工作区的目录也放进来**是有用的：真实用法里"再建一个旁边的
        项目"比"从根一路点下去"常见得多。

        **跨设备取全部**（``any_device=True``，v0.59）：这是管理员的**服务器目录视角**，
        起点按"服务器上真实存在的目录"给；只取服务器端那批会让管理员少看到几处
        磁盘上确实存在、又正被某个工作区用着的位置。
        """
        candidates: list[tuple[str, Path]] = []
        area = self._ensure_area()
        if area is not None:
            candidates.append((AREA_ROOT_LABEL, area))
        home = Path.home()
        if home.is_dir():
            candidates.append(("家目录", home))
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:/")
                if drive.exists():
                    candidates.append((f"{letter}:", drive))
        else:
            candidates.append(("/", Path("/")))
        for record in self._stores.meta.list_workspaces(any_device=True):
            candidates.append((record.name or "工作区", Path(record.root_path)))

        roots: list[DirectoryEntry] = []
        seen: set[str] = set()
        workspace_names = self._workspace_root_names()
        for name, item in candidates:
            try:
                resolved = item.resolve()
            except (OSError, RuntimeError):
                continue
            key = str(resolved)
            if key in seen or not resolved.is_dir():
                continue
            seen.add(key)
            roots.append(self._describe(resolved, label=name, workspace_names=workspace_names))
        return roots[:MAX_BROWSE_ROOTS]

    # ------------------------------------------------------------------ 写

    def create(
        self,
        *,
        name: str,
        root_path: str,
        user_id: str | None,
        description: str = "",
        kb_ids: list[str] | None = None,
        device_id: str | None = None,
        device_name: str = "",
    ) -> WorkspaceRecord:
        """建一个工作区。

        ``device_id``（v0.59）由请求头 ``X-Kylab-Device`` 来：带头 = 这个项目在**那台
        机器**上（``root_path`` 是那台机器的路径），不带 = **服务器端**（``None``）。
        ``device_name`` 只作显示（``X-Kylab-Device-Name``，可空），判定一律按 id。
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise InvalidRequestError("缺少参数：name（工作区名字）")
        if len(clean_name) > MAX_NAME_CHARS:
            raise InvalidRequestError(f"工作区名字最多 {MAX_NAME_CHARS} 字")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise InvalidRequestError(f"描述最多 {MAX_DESCRIPTION_CHARS} 字")

        return self._stores.meta.create_workspace(
            WorkspaceRecord(
                id=f"ws_{uuid.uuid4().hex[:12]}",
                name=clean_name,
                root_path=validate_root_path(root_path, data_dir=self._data_dir),
                owner_id=user_id,
                description=description.strip(),
                # 去重保序：同一个库勾两次没有意义，而顺序是用户勾选的顺序
                kb_ids=tuple(dict.fromkeys(kb_ids or ())),
                device_id=device_id,
                device_name=device_name or "",
            )
        )

    def update(
        self,
        workspace_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        root_path: str | None = None,
        description: str | None = None,
        kb_ids: list[str] | None = None,
        archived: bool | None = None,
        device_id: str | None = None,
        any_device: bool = False,
    ) -> WorkspaceRecord:
        """改工作区。**只改传进来的字段**（``None`` = 不动）。

        归属**不可改**：`owner_id` 不在参数里。把一个工作区转给别人，
        连带的是"里头会话里的 Agent 行为"——那是另一个功能，不该顺手做掉。
        设备同理：**换了机器就该在新机器上新建**，把一条记录搬来搬去会让 ``root_path``
        指向一个那台机器上不存在的目录。

        ``archived``（v0.55）与会话归档同一口径：**不是删除**，且**不推 ``updated_at``**
        （归档是一次整理动作，不该把这个项目顶到"最近更新"的最前面）。
        """
        record = self.get(workspace_id, user_id=user_id, device_id=device_id, any_device=any_device)
        if name is not None:
            clean = name.strip()
            if not clean:
                raise InvalidRequestError("工作区名字不能为空")
            if len(clean) > MAX_NAME_CHARS:
                raise InvalidRequestError(f"工作区名字最多 {MAX_NAME_CHARS} 字")
            record.name = clean
        if root_path is not None:
            record.root_path = validate_root_path(root_path, data_dir=self._data_dir)
        if description is not None:
            if len(description) > MAX_DESCRIPTION_CHARS:
                raise InvalidRequestError(f"描述最多 {MAX_DESCRIPTION_CHARS} 字")
            record.description = description.strip()
        if kb_ids is not None:
            record.kb_ids = tuple(dict.fromkeys(kb_ids))
        # 只有"别的字段真的改了"才走 `update_workspace`——它会推 updated_at，
        # 而**纯归档**那一下不该推（见 docstring）。与 API 的 PATCH 只带 archived 时对应。
        touched = any(value is not None for value in (name, root_path, description, kb_ids))
        if touched:
            record = self._stores.meta.update_workspace(record)
        if archived is not None:
            self._stores.meta.set_workspace_archived(workspace_id, archived)
            record = self._stores.meta.get_workspace(workspace_id) or record
        return record

    def delete(
        self,
        workspace_id: str,
        *,
        user_id: str | None,
        device_id: str | None = None,
        any_device: bool = False,
    ) -> None:
        """删工作区。**里面的会话退回未归档**，不跟着删（见存储层协议说明）。"""
        self.get(workspace_id, user_id=user_id, device_id=device_id, any_device=any_device)
        self._stores.meta.delete_workspace(workspace_id)

    # ------------------------------------------------------------- 会话归属

    def bind_conversation(
        self,
        conversation_id: str,
        workspace_id: str | None,
        *,
        user_id: str | None,
        device_id: str | None = None,
        any_device: bool = False,
    ) -> None:
        """把会话挂到工作区下（``workspace_id=None`` = 退回未归档）。

        先校验工作区可见：否则任何人都能把会话"挂进"别人的工作区
        （挂进去之后，那个工作区的主人就会在侧栏看到它）。**设备那一维同样要过**：
        网页版把一个会话挂进"某台桌面的项目"里，与挂进别人的项目是同一类越界。

        ``workspace_id=None``（退回未归档）**不看设备**：那是取消归属，
        与"这个会话属于哪台机器"无关。
        """
        if workspace_id is not None:
            self.get(workspace_id, user_id=user_id, device_id=device_id, any_device=any_device)
        self._stores.meta.set_conversation_workspace(conversation_id, workspace_id)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _visible(record: WorkspaceRecord, user_id: str | None) -> bool:
        """**账号**维度的可见性：无归属过滤的通道（管理员/API Key）看全部；成员只看自己的。

        **老数据（``owner_id is None``）对成员不可见**——与知识库同一口径：
        无主的东西不该自动落到某个成员名下。

        设备那一维不在这里（见 :meth:`_device_visible`）：两维各自只有一份判定，
        合起来判只会让"到底哪一维没过"变成需要调试的事。
        """
        if user_id is None:
            return True
        return record.owner_id == user_id

    @staticmethod
    def _device_visible(
        record: WorkspaceRecord, device_id: str | None, *, any_device: bool
    ) -> bool:
        """**设备**维度的可见性：要求 ``record.device_id`` 与请求设备**完全相等**。

        ``device_id=None`` 是**服务器端**那一档（网页版/直连 API 不带设备头），
        不是"没条件"——所以它与 ``device_id IS NULL`` 的记录相等、与桌面端的都不等。
        ``any_device=True`` 是管理员的额外通道（``?device=all``，跨机清理），
        此时不过滤设备（归属那一维照旧由 :meth:`_visible` 判）。
        """
        if any_device:
            return True
        return record.device_id == device_id


#: 目录名里不允许出现的字符。取的是**跨平台的那一套**：Windows 禁 `< > : " / \ | ? *`，
#: Linux 只禁 `/` 与 NUL——但一个在 Linux 上合法的名字（`a:b`）到了 Windows 上就建不出来，
#: 而工作区目录经常是要两边互拷的。所以按严格的那一档拒。
_BAD_NAME_CHARS = frozenset('<>:"/\\|?*')

#: Windows 的保留设备名（大小写不敏感，带扩展名也算：`CON.txt` 同样打不开）。
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

MAX_DIRECTORY_NAME_CHARS = 80


def _clean_directory_name(raw: str) -> str:
    """校验一个目录名，返回清洗后的名字。

    **只校验名字，不碰路径**：分隔符一律拒（`a/b` 这种"顺手带上路径"的写法在这里
    是歧义——它到底是想建 `b`，还是想建 `a/b`？拒掉，让它分两步做）。
    首尾的空格与点也要拒：Windows 会静默把它们去掉，于是"我建的名字"与
    "盘上的名字"不是同一个——那种不一致事后极难查。
    """
    name = (raw or "").strip()
    if not name:
        raise InvalidRequestError("缺少参数：name（文件夹名）")
    if len(name) > MAX_DIRECTORY_NAME_CHARS:
        raise InvalidRequestError(f"名字最多 {MAX_DIRECTORY_NAME_CHARS} 个字")
    if any(char in _BAD_NAME_CHARS for char in name):
        raise InvalidRequestError(f"名字里不能有这些字符：{' '.join(sorted(_BAD_NAME_CHARS))}")
    # 开头/结尾的点要拒：**`.` 与 `..` 在路径里不是名字、是导航**（见 sandbox.sandbox_for
    # 里同一个坑），而 `x.` 这种在 Windows 上会被静默改成 `x`——盘上的名字就不是你写的那个
    if name.startswith(".") or name.endswith("."):
        raise InvalidRequestError("名字不能以点开头或结尾（. 与 .. 在路径里是导航，不是名字）")
    if any(ord(char) < 32 for char in name):
        raise InvalidRequestError("名字里不能有控制字符")
    if name.split(".")[0].casefold() in _RESERVED_NAMES:
        raise InvalidRequestError(f"「{name}」是系统保留的名字（Windows 上打不开），换一个")
    return name
