"""技能简介的中文化（v0.28）。

技能生态里绝大多数描述是英文（Anthropic / Google / Vercel / MiniMax 那些官方仓
都是英文），而这一页的用户是中文用户——**"这个技能是干什么的"看不懂，市场就白逛了**。

三件刻意的事：

1. **一批只翻一次**：把这一批技能的（名字, 描述）一起给模型，要它回一个 JSON 数组。
   20 个技能一条 prompt，而不是 20 次调用（浏览一个源本来就只该等一次）。
2. **翻不成就退回原文**：没配模型、模型报错、回的不是 JSON——一律返回空字典，
   界面上继续显示英文原描述。中文化是**增强，不是依赖**：它坏了不该让市场打不开。
3. **原文一个字都不改**：新的简介只是界面上多显示一行，技能的 ``SKILL.md`` 不动
   ——``description`` 是**模型判断"何时该用"的触发文本**（见 services/skills.py），
   替作者改它有真实的功能风险，而我们只是想让**人**看懂。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from app.services.llm import ChatMessage

__all__ = ["MAX_BLURB_CHARS", "SkillBlurbService", "Translator", "parse_blurbs"]

logger = logging.getLogger(__name__)

#: 一条中文简介最多多少字。列表里那一行本来就该短（描述是"什么时候该用它"的整句话）。
MAX_BLURB_CHARS = 60

#: 一次最多翻多少条：一屏能看的量。再多该分页，而不是把 prompt 撑大。
MAX_BATCH = 40

_SYSTEM_PROMPT = (
    "你是技术文档译者。用户会给你一批「技能」的英文名与英文描述，"
    "请为每一条写一句**简体中文简介**，让中文用户一眼看出这个技能是干什么的。\n"
    "要求：\n"
    f"1. 每条不超过 {MAX_BLURB_CHARS} 个汉字，一到两句，不要客套话；\n"
    "2. **说清「做什么、什么时候用」**，不要逐字直译；保留专有名词的原文"
    "（如 PDF、Excel、MCP、TDD 这类该照写）；\n"
    "3. 只回一个 JSON 数组，形如 "
    '[{"name": "<原样照抄的名字>", "summary": "<中文简介>"}]；\n'
    "4. 不要输出 JSON 之外的任何文字（不要解释、不要代码块标记）。"
)


def _user_prompt(items: Sequence[tuple[str, str]]) -> str:
    lines = []
    for name, description in items:
        text = " ".join((description or "").split())[:1200]
        lines.append(f"- name: {name}\n  description: {text or '（原描述为空）'}")
    return "技能清单：\n" + "\n".join(lines)


def parse_blurbs(text: str) -> dict[str, str]:
    """从模型回复里取出 ``名字 → 中文简介``。

    容忍两种常见的走形（这两种实测都会出现）：被包在 ``` 代码块里、
    前后带一句解释。做法是**往后找第一个 ``[``、往前找最后一个 ``]``**——
    比"只认纯 JSON"稳，也不会因为模型多说一句话就整批作废。
    """
    raw = (text or "").strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        data: Any = json.loads(raw[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        summary = " ".join(str(item.get("summary") or "").split())
        if name and summary:
            out[name] = summary[:MAX_BLURB_CHARS]
    return out


#: ``[(名字, 原描述)] -> {名字: 中文简介}``。组合根注入，服务自己不认识 ChatService。
Translator = Callable[[Sequence[tuple[str, str]]], dict[str, str]]


class SkillBlurbService:
    """把英文技能描述翻成中文简介。

    它是 ``Translator`` 的一种实现（也可以给技能源服务注入别的实现，测试里就是一个
    返回固定文案的 lambda）。**失败一律返回空字典**，见模块头第 2 条。
    """

    def __init__(self, chat: Any, *, model_pk: str | None = None) -> None:
        self._chat = chat
        self._model_pk = model_pk

    def __call__(self, items: Sequence[tuple[str, str]]) -> dict[str, str]:
        return self.summaries(items)

    def summaries(self, items: Sequence[tuple[str, str]]) -> dict[str, str]:
        wanted = [(name, text) for name, text in items if name][:MAX_BATCH]
        if not wanted:
            return {}
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=_user_prompt(wanted)),
        ]
        try:
            reply = self._chat.ask_raw(messages, model_pk=self._model_pk)
        except Exception:  # 模型不可用/超时/没配：中文化是增强，不该把市场带崩
            logger.warning("技能简介翻译失败，退回原文", exc_info=True)
            return {}
        return parse_blurbs(reply)


#: 句末的中文标点也去掉：列表里那一行不需要句号收尾。
_TRAILING = re.compile(r"[。；;\s]+$")


def tidy(summary: str) -> str:
    """收尾清理（给出/存库前统一走一遍）。"""
    return _TRAILING.sub("", (summary or "").strip())
