"""``app/models/enums.py`` 的单元测试。

镜像同构：``app/models/enums.py`` → ``tests/unit/models/test_enums.py``（工程规范 §5.1）。
本文件里对状态序列的断言就是《架构设计 v0.2》§4 的契约快照——改枚举必须同时改架构文档。
"""

from app.models.enums import (
    PIPELINE_STAGE_ORDER,
    TERMINAL_STAGES,
    ApiKeyPermission,
    DataSourceKind,
    DocumentStage,
    TaskKind,
    TaskState,
    TrashKind,
)


def test_pipeline_stage_order_matches_architecture() -> None:
    """主链路顺序必须与架构 §4 逐字一致。"""
    assert tuple(stage.value for stage in PIPELINE_STAGE_ORDER) == (
        "uploaded",
        "probing",
        "parsing",
        "parsed",
        "chunking",
        "chunked",
        "embedding",
        "indexed",
    )


def test_terminal_stages_are_indexed_failed_and_canceled() -> None:
    assert {DocumentStage.INDEXED, DocumentStage.FAILED, DocumentStage.CANCELED} == TERMINAL_STAGES


def test_enhancement_branch_is_outside_main_chain() -> None:
    """图谱/Wiki 增强分支默认关闭且不在主链路上（架构 §4、§11）。"""
    assert DocumentStage.ENRICHING not in PIPELINE_STAGE_ORDER
    assert DocumentStage.ENRICHED not in PIPELINE_STAGE_ORDER


def test_api_key_permission_has_exactly_two_levels() -> None:
    """架构 §3.2：只读 / 读写两种权限，无第三种。"""
    assert {permission.value for permission in ApiKeyPermission} == {"readonly", "readwrite"}


def test_data_source_kinds_cover_p0_and_reserved() -> None:
    """P0：本地上传 / HTML / RSS；WebDAV 为框架预留（架构 §10、§14）。"""
    assert {kind.value for kind in DataSourceKind} == {"upload", "html", "rss", "webdav"}


def test_task_kinds_cover_each_pipeline_stage() -> None:
    """每个摄入阶段都能独立成任务，才能按阶段重试（架构 §4）。"""
    stage_like = {
        TaskKind.PROBE.value,
        TaskKind.PARSE.value,
        TaskKind.CHUNK.value,
        TaskKind.EMBED.value,
    }
    assert stage_like <= {kind.value for kind in TaskKind}
    assert TaskKind.DELETE.value in {kind.value for kind in TaskKind}


def test_task_states_include_running_for_lease_recovery() -> None:
    """RUNNING 是租约回收的前提：进程崩溃后超时任务回到 PENDING 续跑。"""
    assert TaskState.RUNNING in set(TaskState)
    assert {state.value for state in TaskState} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "canceled",
    }


def test_trash_kinds_cover_original_and_image() -> None:
    assert {kind.value for kind in TrashKind} == {"original", "image"}


def test_enums_are_string_valued() -> None:
    """用 StrEnum：可直接序列化进 SQLite / JSON，无需额外转换。"""
    assert isinstance(DocumentStage.INDEXED, str)
    assert DocumentStage.INDEXED == "indexed"
