"""工作区按**设备**隔离（v0.59）。

镜像同构：``app/services/workspace.py`` 的 ``list`` / ``get`` / ``create`` /
``update`` / ``delete`` / ``bind_conversation`` → 本文件。

**为什么是第二维归属**：桌面壳在每条请求上带 ``X-Kylab-Device``，于是"这台电脑的项目"
与"那台电脑的项目"必须互相看不见；而网页版/直连 API 不带这个头，它们看到的是
``device_id IS NULL`` 的那批——语义是**服务器端**（``root_path`` 在服务器的盘上），
不是"没有归属"。这一组用例钉四件事：

1. **列表三态**：指定设备 / 只看服务器端（``device_id=None``）/ 跨设备全都要
   （管理员的额外通道，服务层对应 ``any_device=True``）；
2. **创建打戳**：带设备落的记录带 ``device_id`` + ``device_name``，不带就落 ``NULL``
   （``device_name`` 空串）——打戳错了，整台机器的项目会在下一次启动时集体消失；
3. **设备闸在四条路上**：``get`` / ``delete`` / ``bind`` 拿到别的设备的记录时，
   与"越权"和"不存在"回**逐字同一句话**（``工作区不存在：{id}``）——措辞不一样
   就等于承认"这个 id 存在"；
4. **两维各自只有一份判定**：设备那一维不替归属那一维说话——同一台机器上，
   普通成员仍然只看得到自己的。

这一层用假存储，钉的是**服务层的判定**；真存储的 ``WHERE`` 由集成用例
（``tests/integration/api/test_agent_api.py``）对着真库钉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import NotFoundError
from app.services.workspace import WorkspaceService
from app.storage.base import WorkspaceRecord

#: 两台机器 + 一批服务器端的记录。两个 id 都是 UUID v4 形状（真值是壳生成的）。
DEVICE_A = "11111111-1111-4111-8111-111111111111"
DEVICE_B = "22222222-2222-4222-8222-222222222222"


class _FakeMeta:
    """够 ``list`` / ``create`` / ``get`` / ``delete`` / ``bind`` 用的一小撮存储。

    设备三态过滤**照真存储写一份**：``any_device`` 不过滤，否则精确比 ``device_id``
    （``None`` 这一档就是服务器端）。假存储不过滤的话，用例钉住的就只是
    "服务层与假存储恰好都不判"，而不是真的隔离。
    """

    def __init__(self, records: list[WorkspaceRecord] | None = None) -> None:
        self.records = list(records or [])
        self.bound: dict[str, str | None] = {}
        self.deleted: list[str] = []

    def list_workspaces(
        self, *, device_id: str | None = None, any_device: bool = False
    ) -> list[WorkspaceRecord]:
        if any_device:
            return list(self.records)
        return [item for item in self.records if item.device_id == device_id]

    def create_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord:
        self.records.append(record)
        return record

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        return next((item for item in self.records if item.id == workspace_id), None)

    def delete_workspace(self, workspace_id: str) -> None:
        self.deleted.append(workspace_id)
        self.records = [item for item in self.records if item.id != workspace_id]

    def set_conversation_workspace(self, conversation_id: str, workspace_id: str | None) -> None:
        self.bound[conversation_id] = workspace_id

    def count_workspace_conversations(self, workspace_id: str) -> int:
        return 0


class _FakeStores:
    def __init__(self, meta: _FakeMeta) -> None:
        self.meta = meta


def _service(data_dir: Path, records: list[WorkspaceRecord] | None = None):  # type: ignore[no-untyped-def]
    """一个数据目录 + 一批记录；返回 ``(服务, 假存储)`` 供断言用。"""
    meta = _FakeMeta(records)
    return WorkspaceService(_FakeStores(meta), data_dir), meta


def _three_records() -> list[WorkspaceRecord]:
    """服务器端 / A 机 / B 机 各一条（``root_path`` 只是字符串，这一层不碰盘）。"""
    return [
        WorkspaceRecord(id="ws_server", name="服务器上的项目", root_path="/srv/proj"),
        WorkspaceRecord(
            id="ws_a",
            name="A 机的项目",
            root_path="C:/proj",
            device_id=DEVICE_A,
            device_name="台式机",
        ),
        WorkspaceRecord(
            id="ws_b",
            name="B 机的项目",
            root_path="D:/proj",
            device_id=DEVICE_B,
            device_name="笔记本",
        ),
    ]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    target.mkdir()
    return target


# ------------------------------------------------------------------ 列表三态


def test_list_defaults_to_the_server_side_only(tmp_path: Path, data_dir: Path) -> None:
    """不带设备头（``device_id=None``）看到的是 ``device_id IS NULL`` 那批。

    **不是"没有归属所以全都能看"**——那会让网页版看见所有桌面端的项目，
    而它们的 ``root_path`` 根本不在服务器的盘上。
    """
    service, _ = _service(data_dir, _three_records())

    listed = service.list(user_id=None)

    assert [view.record.id for view in listed] == ["ws_server"]


def test_list_filters_by_device(tmp_path: Path, data_dir: Path) -> None:
    service, _ = _service(data_dir, _three_records())

    assert [view.record.id for view in service.list(user_id=None, device_id=DEVICE_A)] == ["ws_a"]
    assert [view.record.id for view in service.list(user_id=None, device_id=DEVICE_B)] == ["ws_b"]
    # 没见过的设备 id（比如重装后换了壳的 uuid）看到的是空，不是全部
    assert service.list(user_id=None, device_id="33333333-3333-4333-8333-333333333333") == []


def test_list_can_take_every_device(tmp_path: Path, data_dir: Path) -> None:
    """``any_device=True`` 是管理员那条 ``?device=all`` 通道（跨机清理）。"""
    service, _ = _service(data_dir, _three_records())

    listed = service.list(user_id=None, any_device=True)

    assert [view.record.id for view in listed] == ["ws_server", "ws_a", "ws_b"]


# ------------------------------------------------------------------ 创建打戳


def test_create_stamps_the_device(tmp_path: Path, data_dir: Path) -> None:
    service, _ = _service(data_dir)
    folder = tmp_path / "proj"
    folder.mkdir()

    made = service.create(
        name="桌面上的项目",
        root_path=str(folder),
        user_id=None,
        device_id=DEVICE_A,
        device_name="台式机",
    )

    assert (made.device_id, made.device_name) == (DEVICE_A, "台式机")
    # 打了戳之后，别的设备（包括不带头的网页版）都看不到它
    assert service.list(user_id=None) == []
    assert [view.record.id for view in service.list(user_id=None, device_id=DEVICE_A)] == [made.id]


def test_create_without_device_leaves_it_server_side(tmp_path: Path, data_dir: Path) -> None:
    service, _ = _service(data_dir)
    folder = tmp_path / "proj"
    folder.mkdir()

    made = service.create(name="服务器上的项目", root_path=str(folder), user_id=None)

    assert made.device_id is None
    # `device_name` 是 `str` 字段：没给就是空串，不是 `None`（存储层也归一到空串）
    assert made.device_name == ""
    assert [view.record.id for view in service.list(user_id=None)] == [made.id]


# --------------------------------------------------------------- 设备闸（三条路）


def _call(action: str, service: WorkspaceService, workspace_id: str, device_id: str | None) -> None:
    """按名字走一遍四条带设备闸的路（``get`` / ``delete`` / ``bind``）。"""
    if action == "get":
        service.get(workspace_id, user_id=None, device_id=device_id)
    elif action == "delete":
        service.delete(workspace_id, user_id=None, device_id=device_id)
    else:
        service.bind_conversation("conv_1", workspace_id, user_id=None, device_id=device_id)


@pytest.mark.parametrize("action", ["get", "delete", "bind"])
def test_other_device_looks_exactly_like_not_found(
    action: str, tmp_path: Path, data_dir: Path
) -> None:
    """拿到别的设备的记录 = 拿到不存在的 id：**回逐字同一句话**。

    这正是"不暴露存在性"的落点——若设备不匹配单独给一句"这是另一台电脑的项目"，
    就等于确认了"这个 id 存在"（而这台机器本来不该知道别的机器有哪些项目）。
    """
    service, meta = _service(data_dir, _three_records())

    with pytest.raises(NotFoundError) as caught:
        _call(action, service, "ws_a", DEVICE_B)

    assert str(caught.value) == "工作区不存在：ws_a"
    # 而且**什么都没发生**：记录还在、没被删、也没被挂到会话上
    assert meta.get_workspace("ws_a") is not None
    assert meta.deleted == []
    assert meta.bound == {}


@pytest.mark.parametrize("action", ["get", "delete", "bind"])
def test_server_side_only_sees_server_side(action: str, tmp_path: Path, data_dir: Path) -> None:
    """不带设备头时，桌面端的记录同样回 404（隔离是**双向**的）。"""
    service, _ = _service(data_dir, _three_records())

    with pytest.raises(NotFoundError):
        _call(action, service, "ws_a", None)


@pytest.mark.parametrize("action", ["get", "delete", "bind"])
def test_own_device_passes(action: str, tmp_path: Path, data_dir: Path) -> None:
    """自己的设备照常能用（不然上面那三条"该拒的拒了"就只是"全拒了"）。"""
    service, meta = _service(data_dir, _three_records())

    _call(action, service, "ws_a", DEVICE_A)

    if action == "delete":
        assert meta.get_workspace("ws_a") is None
    if action == "bind":
        assert meta.bound == {"conv_1": "ws_a"}


def test_unbinding_a_conversation_ignores_the_device(tmp_path: Path, data_dir: Path) -> None:
    """``workspace_id=None``（退回未归档）不看设备：那是**取消**归属，
    与"这条会话属于哪台机器"无关——拿设备去卡它会让桌面端没法把会话挪出项目。
    """
    service, meta = _service(data_dir, _three_records())

    service.bind_conversation("conv_1", None, user_id=None, device_id=DEVICE_B)

    assert meta.bound == {"conv_1": None}


# --------------------------------------------------------------- 两维互不代替


def test_member_still_sees_only_their_own_on_the_same_device(
    tmp_path: Path, data_dir: Path
) -> None:
    """设备那一维**不替**归属那一维说话：同一台机器上，成员仍只看自己的。

    两维都过才算可见——合起来判会让"到底哪一维没过"变成需要调试的事，
    所以 ``_visible``（账号）与 ``_device_visible``（设备）各自只有一份判定。
    """
    records = [
        WorkspaceRecord(
            id="ws_mine", name="我的", root_path="C:/a", owner_id="user_1", device_id=DEVICE_A
        ),
        WorkspaceRecord(
            id="ws_theirs", name="别人的", root_path="C:/b", owner_id="user_2", device_id=DEVICE_A
        ),
    ]
    service, _ = _service(data_dir, records)

    mine = service.list(user_id="user_1", device_id=DEVICE_A)
    assert [view.record.id for view in mine] == ["ws_mine"]

    # 归属那一维先拦：另一条记录即使是同一台设备也不可见
    with pytest.raises(NotFoundError):
        service.get("ws_theirs", user_id="user_1", device_id=DEVICE_A)
    # 管理员（user_id=None）在同一台设备上两条都看得到
    assert service.get("ws_theirs", user_id=None, device_id=DEVICE_A).id == "ws_theirs"


def test_admin_can_reach_another_device_for_cleanup(tmp_path: Path, data_dir: Path) -> None:
    """管理员的 ``device=all`` 通道一路通到 ``get``/``delete``（跨机清理要用）。"""
    service, meta = _service(data_dir, _three_records())

    assert service.get("ws_b", user_id=None, any_device=True).id == "ws_b"
    service.delete("ws_b", user_id=None, any_device=True)

    assert meta.get_workspace("ws_b") is None
