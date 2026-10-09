"""续跑的交接说明（§12.212）。

这一组的重点是**说明里必须有的三样东西**，缺一样续跑就会退化：
1. **已经做过什么**（不写，模型会把同一个检索再做一遍——那是真花钱的）；
2. **材料编号**（不写，它引用出来的 [n] 会指向别的资料，用户点过去看到的是另一段原文）；
3. **停在哪**（不写，它不知道自己上次没答完，可能从头再想一遍）。
"""

from __future__ import annotations

from app.services.resume import (
    MAX_NOTE_CHARS,
    ResumeMaterial,
    degraded_reason,
    resume_note,
)


def _material(**overrides: object) -> ResumeMaterial:  # type: ignore[arg-type]
    base: dict[str, object] = {
        "question": "眼轴怎么监测",
        "answer": "眼轴是主要参数[1]，建议",
        "steps": [
            {
                "phase": "tool",
                "label": "检索知识库",
                "detail": "检索词：眼轴，命中 8 段",
                "status": "done",
            },
            {
                "phase": "tool",
                "label": "本轮时间已用尽",
                "detail": "本轮最多 300 秒，已用 312 秒，按现有信息作答",
                "status": "done",
                "degraded": True,
            },
        ],
        "sources": [
            {
                "index": 1,
                "document_name": "中国干眼共识（2024）.pdf",
                "heading_path": "4 黏蛋白",
                "preview": "眼轴长度是主要监测参数。",
            }
        ],
    }
    base.update(overrides)
    return ResumeMaterial(**base)  # type: ignore[arg-type]


def test_reason_comes_from_the_degraded_step() -> None:
    """降级原因取那一步的 `detail`——它是服务端写给人看的一句话，界面与说明共用一份。"""
    material = _material()
    assert degraded_reason(material.steps) == "本轮最多 300 秒，已用 312 秒，按现有信息作答"


def test_a_normal_turn_has_no_reason() -> None:
    """正常跑完的一轮没有可续的地方——**这一点决定端点返回 422 而不是硬续**。"""
    assert degraded_reason([{"phase": "tool", "label": "检索知识库", "status": "done"}]) == ""


def test_reason_falls_back_to_the_label_when_detail_is_missing() -> None:
    """老快照里可能只有 label：宁可给一句"这一轮没跑完"，也不要空着。"""
    assert degraded_reason([{"label": "工具步数已达上限", "degraded": True}]) == "工具步数已达上限"


def test_note_carries_done_work_material_and_the_stop() -> None:
    note = resume_note(_material(), reason="本轮时间已用尽，已用 312 秒")

    # 停在哪
    assert "本轮时间已用尽" in note
    # 做过什么 + 别再重复（这是最贵的一条）
    assert "检索知识库" in note and "不要重复做" in note
    # 材料与编号
    assert "[1] 中国干眼共识（2024）.pdf §4 黏蛋白" in note
    assert "编号沿用" in note
    # 写了一半的回答
    assert "眼轴是主要参数[1]" in note
    # 收尾指令
    assert "接着把这一轮做完" in note


def test_degraded_step_is_not_listed_as_an_action() -> None:
    """降级那一条写在"停在哪"里就够，再混进动作清单会让模型以为"停"是一次动作。"""
    note = resume_note(_material(), reason="本轮时间已用尽")
    actions = note.split("已经做过这些事")[1].split("已经拿到")[0]
    assert "检索知识库" in actions
    assert "本轮时间已用尽" not in actions


def test_source_numbering_follows_the_stored_index() -> None:
    """编号**按快照里的 `index` 走**，不按它在列表里的位置：

    账本可能去重过（同一次检索里重复的片段只留一条），位置与编号因此不一定相等；
    说明里给错编号，模型就会引用到另一段原文。
    """
    note = resume_note(
        _material(
            sources=[
                {"index": 3, "document_name": "指南.pdf", "preview": "第三段"},
                {"index": 5, "document_name": "共识.pdf", "preview": "第五段"},
            ]
        ),
        reason="原因",
    )
    assert "[3] 指南.pdf" in note and "[5] 共识.pdf" in note
    # 只看**材料清单那一段**（半截回答里本来就可能写着 [1]，那是上一轮自己的引用）
    material_block = note.split("已经拿到")[1].split("上一次写到这里")[0]
    assert "[1]" not in material_block


def test_note_is_capped() -> None:
    """说明是要进提示词的，不能比资料还长。"""
    note = resume_note(_material(answer="很长" * 5000), reason="原因")
    assert len(note) <= MAX_NOTE_CHARS


def test_missing_pieces_do_not_leave_holes() -> None:
    """没有材料、没有半截回答时，说明仍然读得通（不能出现"已经拿到并引用过的原文："后面空着）。"""
    note = resume_note(
        ResumeMaterial(question="问", answer="", steps=[], sources=[]), reason="原因"
    )
    assert "已经拿到" not in note
    assert "上一次写到这里" not in note
    assert "接着把这一轮做完" in note
