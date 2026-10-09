"""笔记的 AI 处理：智能排版 / 内容润色 / 两者一起（v20.2）。

为什么单独一个服务而不是塞进 ``NotesService``：那个模块的模块注释里写死了
"不引入任何对 LLM 的依赖"——列表、改名、删除在没有配模型的部署上也要能用。
AI 处理是**可选增强**，单独放能保住那条边界：没配模型时笔记照常，只是这个入口报错。

三个动作的措辞要求收在提示词里，核心是**别越权**：
- 排版只动结构（分段、标题层级、列表、空行），**逐字保留原文**；
- 润色只动措辞（错别字、病句、标点），**保持段落结构**；
- 两者才同时做。

模型很爱在正文外面加"好的，这是处理后的内容："和 ``` 围栏，所以出口统一剥一层。
"""

from __future__ import annotations

import logging
import re

from app.core.exceptions import InvalidRequestError
from app.services.llm import ChatMessage

__all__ = ["NOTE_AI_ACTIONS", "NoteAiService"]

logger = logging.getLogger(__name__)

_BASE = (
    "你是 Markdown 笔记的编辑助手。只输出处理后的 Markdown 正文："
    "不要写解释、不要写前言后语、不要用 ``` 代码围栏包住整篇。"
    "不得编造或删除原文中的事实、数字、人名。"
)

#: 动作 → 提示词。键同时是接口接受的动作名。
PROMPTS: dict[str, str] = {
    "format": (
        _BASE
        + "\n任务：**只做排版与分段**。合理分段、规范标题层级（# / ##）、"
        "把并列的内容整理成列表、统一空行与中英文之间的空格。"
        "逐字保留原文用词与顺序，**不得改写、增删、翻译任何文字**。"
    ),
    "polish": (
        _BASE
        + "\n任务：**只做文字润色**。修正错别字、病句与标点，让表达通顺、专业。"
        "保持原有的段落划分与标题结构不变，不新增也不删减信息。"
    ),
    "both": (
        _BASE
        + "\n任务：**先润色文字，再做排版分段**。"
        "润色时修正错别字、病句与标点；排版时合理分段、规范标题层级、"
        "把并列内容整理成列表、统一空行。不得编造或删除事实。"
    ),
}

NOTE_AI_ACTIONS = frozenset(PROMPTS)

#: 模型可能套的 ``` 围栏（有时带语言标记）。
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


class NoteAiService:
    """把一条笔记交给对话模型做排版/润色。"""

    def __init__(self, chat) -> None:  # type: ignore[no-untyped-def]
        self._chat = chat

    def transform(self, *, action: str, content_md: str, model_pk: str | None = None) -> str:
        """返回处理后的 Markdown（不落库，由调用方决定要不要写回）。"""
        if action not in PROMPTS:
            raise InvalidRequestError(f"不支持的 AI 处理类型：{action}")
        body = (content_md or "").strip()
        if not body:
            raise InvalidRequestError("笔记内容为空，没有可处理的内容")
        messages = [
            ChatMessage(role="system", content=PROMPTS[action]),
            ChatMessage(role="user", content=body),
        ]
        # 走 ``ask_raw``：不检索、不拼资料，要的是模型的语言能力本身
        text = self._chat.ask_raw(messages, model_pk=model_pk)
        return _strip_fence((text or "").strip())


def _strip_fence(text: str) -> str:
    """剥掉整篇的 ``` 围栏；没有就原样返回。"""
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text
