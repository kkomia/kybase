"""流水线状态机的单元测试。

镜像同构：``app/pipeline/state_machine.py`` → ``tests/unit/pipeline/test_state_machine.py``。
"""

import itertools

import pytest

from app.models.enums import PIPELINE_STAGE_ORDER, DocumentStage
from app.pipeline.state_machine import (
    ALLOWED_TRANSITIONS,
    RETRYABLE_STAGES,
    InvalidTransition,
    assert_transition,
    can_transition,
    is_terminal,
    next_stage,
)


def test_main_chain_advances_one_step_at_a_time() -> None:
    """架构 §4 的主链路：每一步只能走到下一步。"""
    for current, target in itertools.pairwise(PIPELINE_STAGE_ORDER):
        assert can_transition(current, target) is True


def test_skipping_stages_is_rejected() -> None:
    with pytest.raises(InvalidTransition, match="uploaded → indexed"):
        assert_transition(DocumentStage.UPLOADED, DocumentStage.INDEXED)


def test_backward_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(DocumentStage.CHUNKED, DocumentStage.PARSING)


def test_self_transition_is_allowed() -> None:
    """断点续跑时任务可能重设同一状态，这不该算非法。"""
    for stage in DocumentStage:
        assert can_transition(stage, stage) is True


def test_any_stage_can_fail() -> None:
    for stage in DocumentStage:
        if stage in (DocumentStage.FAILED, DocumentStage.CANCELED):
            # 取消是终态且不指向失败：用户叫停的东西不该在任务中心里长成一条红色记录
            continue
        assert can_transition(stage, DocumentStage.FAILED) is True


def test_canceled_is_terminal_and_resumable() -> None:
    """取消不是死路：可以重新摄入，但不能"失败"。"""
    assert can_transition(DocumentStage.UPLOADED, DocumentStage.CANCELED) is True
    assert can_transition(DocumentStage.PARSING, DocumentStage.CANCELED) is True
    assert can_transition(DocumentStage.CANCELED, DocumentStage.FAILED) is False
    for stage in RETRYABLE_STAGES:
        assert can_transition(DocumentStage.CANCELED, stage) is True


def test_indexed_cannot_be_canceled() -> None:
    """都跑完了，没有"取消解析"这回事。"""
    assert can_transition(DocumentStage.INDEXED, DocumentStage.CANCELED) is False


def test_failed_can_resume_from_each_step() -> None:
    """架构 §4：失败要能定位到具体步骤并从那里续跑，而不是整篇重来。"""
    for stage in RETRYABLE_STAGES:
        assert can_transition(DocumentStage.FAILED, stage) is True


def test_failed_cannot_jump_to_a_terminal_stage() -> None:
    assert can_transition(DocumentStage.FAILED, DocumentStage.INDEXED) is False


def test_enhancement_branch_is_reachable_from_indexed() -> None:
    """图谱/Wiki 增强是可选分支，默认关闭但状态可达（架构 §4、§11）。"""
    assert can_transition(DocumentStage.INDEXED, DocumentStage.ENRICHING) is True
    assert can_transition(DocumentStage.ENRICHING, DocumentStage.ENRICHED) is True


def test_every_stage_has_a_transition_row() -> None:
    """新增状态却忘了写迁移规则，会在这里变红。"""
    assert set(ALLOWED_TRANSITIONS) == set(DocumentStage)


def test_next_stage_walks_the_main_chain() -> None:
    assert next_stage(DocumentStage.UPLOADED) is DocumentStage.PROBING
    assert next_stage(DocumentStage.EMBEDDING) is DocumentStage.INDEXED
    assert next_stage(DocumentStage.INDEXED) is None


def test_next_stage_outside_main_chain_returns_none() -> None:
    assert next_stage(DocumentStage.ENRICHING) is None
    assert next_stage(DocumentStage.FAILED) is None


def test_terminal_stages() -> None:
    assert is_terminal(DocumentStage.INDEXED) is True
    assert is_terminal(DocumentStage.FAILED) is True
    assert is_terminal(DocumentStage.PARSING) is False


def test_error_message_lists_allowed_targets() -> None:
    """报错要能直接告诉人"现在能去哪"，而不是只说"不行"。"""
    with pytest.raises(InvalidTransition) as excinfo:
        assert_transition(DocumentStage.UPLOADED, DocumentStage.INDEXED)
    message = str(excinfo.value)
    assert DocumentStage.PROBING.value in message
    assert DocumentStage.FAILED.value in message
