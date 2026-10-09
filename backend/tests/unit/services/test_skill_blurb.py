"""技能简介的中文化（v0.28）。

技能描述基本都是英文，而这一页是给中文用户看的。这里盯的是三件事：

1. **一批只翻一次**（20 个技能一条 prompt，不是 20 次调用）；
2. **翻不成就退回原文**：没配模型、模型报错、回的不是 JSON——一律返回空，
   市场照常能用（中文化是增强，不是依赖）；
3. **解析要容忍走形**：模型把 JSON 包在代码块里、前后带一句解释，都得认。
"""

from __future__ import annotations

from app.services.skill_blurb import MAX_BLURB_CHARS, SkillBlurbService, parse_blurbs


class _Chat:
    """假的对话服务：记下收到的 prompt，回一段预置文本。"""

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    def ask_raw(self, messages, model_pk=None):  # type: ignore[no-untyped-def]
        self.prompts.append("\n".join(str(m.content) for m in messages))
        if self.error:
            raise self.error
        return self.reply


ITEMS = [("pdf", "Work with PDF files"), ("xlsx", "Work with spreadsheets")]


def test_one_call_for_the_whole_batch() -> None:
    """一批技能**只花一次调用**：把清单一起给模型，要它回一个数组。"""
    chat = _Chat(
        '[{"name": "pdf", "summary": "处理 PDF"}, {"name": "xlsx", "summary": "处理表格"}]'
    )

    got = SkillBlurbService(chat)(ITEMS)

    assert got == {"pdf": "处理 PDF", "xlsx": "处理表格"}
    assert len(chat.prompts) == 1
    assert "Work with PDF files" in chat.prompts[0]


def test_parsing_tolerates_the_usual_shapes() -> None:
    """模型把 JSON 包在 ``` 里、或前后多一句解释——都要认出来。"""
    wrapped = '好的，这是结果：\n```json\n[{"name": "pdf", "summary": "处理 PDF"}]\n```\n希望有用。'

    assert parse_blurbs(wrapped) == {"pdf": "处理 PDF"}


def test_parsing_rejects_what_it_cannot_trust() -> None:
    assert parse_blurbs("我不知道") == {}
    assert parse_blurbs("[不是 JSON") == {}
    assert parse_blurbs('{"name": "pdf"}') == {}  # 不是数组


def test_a_long_summary_is_clipped() -> None:
    """列表里那一行该短：超了截断（而不是铺满三行）。"""
    got = parse_blurbs(f'[{{"name": "pdf", "summary": "{"长" * 200}"}}]')

    assert len(got["pdf"]) == MAX_BLURB_CHARS


def test_a_failing_model_returns_nothing_instead_of_raising() -> None:
    """**翻不成就退回原文**：没配模型 / 模型报错时返回空字典，不是抛出去。

    中文化是增强：它坏了不该让"逛技能市场"这件事直接 500。
    """
    chat = _Chat(error=RuntimeError("还没配对话模型"))

    assert SkillBlurbService(chat)(ITEMS) == {}


def test_nothing_to_translate_costs_nothing() -> None:
    chat = _Chat('[{"name": "pdf", "summary": "处理 PDF"}]')

    assert SkillBlurbService(chat)([]) == {}
    assert chat.prompts == []
