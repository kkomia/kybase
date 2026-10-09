"""笔记 AI 处理（v20.2）的单元测试。

镜像同构：``app/services/note_ai.py`` → 本文件。
"""

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.llm import ChatMessage
from app.services.note_ai import NoteAiService


class FakeChat:
    """假的对话服务：记录 messages，返回固定文本。"""

    def __init__(self, answer: str = "处理后的正文") -> None:
        self.answer = answer
        self.received: list[list[ChatMessage]] = []

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        self.received.append(list(messages))
        return self.answer


def test_format_prompt_forbids_rewriting() -> None:
    """排版只动结构：提示词里必须写死"不得改写"。"""
    chat = FakeChat()
    NoteAiService(chat).transform(action="format", content_md="一段很长的文字")

    system = chat.received[0][0].content
    assert "只做排版与分段" in system
    assert "不得改写" in system


def test_polish_prompt_keeps_structure() -> None:
    chat = FakeChat()
    NoteAiService(chat).transform(action="polish", content_md="这段话有错别字")

    system = chat.received[0][0].content
    assert "只做文字润色" in system
    assert "段落划分与标题结构不变" in system


def test_both_prompt_does_both() -> None:
    chat = FakeChat()
    NoteAiService(chat).transform(action="both", content_md="内容")

    system = chat.received[0][0].content
    assert "先润色文字，再做排版分段" in system


def test_user_message_is_the_note_body() -> None:
    chat = FakeChat()
    NoteAiService(chat).transform(action="format", content_md="  笔记正文  ")

    assert chat.received[0][1] == ChatMessage(role="user", content="笔记正文")


def test_code_fence_is_stripped() -> None:
    """模型很爱把整篇包在```里；不剥掉就会把围栏当正文存进笔记。"""
    chat = FakeChat("```markdown\n# 标题\n\n正文\n```")

    result = NoteAiService(chat).transform(action="format", content_md="x")

    assert result == "# 标题\n\n正文"


def test_plain_text_passes_through() -> None:
    chat = FakeChat("# 标题\n正文")
    assert NoteAiService(chat).transform(action="format", content_md="x") == "# 标题\n正文"


def test_empty_content_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        NoteAiService(FakeChat()).transform(action="format", content_md="   ")


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        NoteAiService(FakeChat()).transform(action="translate", content_md="内容")
