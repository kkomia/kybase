"""进提示词的文本卫生（`app/core/text_hygiene.py` + 两个边界）。

背景（D16 P0，2026-09-29）：上游把 emoji 写成**字面转义**（`"\\ud83e\\udd16"`），
YAML 解出来是两个孤立代理项；它一进请求体，httpx 编码就
`UnicodeEncodeError: surrogates not allowed` —— **整句话都问不出去**（两条真会话实测）。

两种口径分开钉：技能那一侧"非法就丢弃 + 理由"；请求组装那一侧"只清洗、不拒绝"。
"""

from __future__ import annotations

from app.core.text_hygiene import recombine_surrogates, sanitize_prompt_text, text_problem
from app.services.llm import ChatMessage, _message_wire


def test_paired_escapes_are_recombined() -> None:
    """**合法的一对转义 → 一个正常码位**（那是"转义写坏了"，不是非法内容）。"""
    broken = "\ud83e\udd16"  # 上游写成 "\ud83e\udd16" 时 YAML 解出来的样子
    fixed = recombine_surrogates(broken)

    assert fixed == "\U0001f916"
    assert text_problem(fixed) == ""
    # 幂等：再合并一次不变
    assert recombine_surrogates(fixed) == fixed


def test_lone_surrogate_is_a_problem_with_a_chinese_reason() -> None:
    """**合并后仍孤立的 → 非法**，理由里要说清"为什么"（技能那一侧据此丢弃）。"""
    problem = text_problem(recombine_surrogates("x\ud83ey"))

    assert problem.startswith("已丢弃：")
    assert "孤立代理项" in problem and "编码失败" in problem
    assert recombine_surrogates("x\ud83ey") == "x\ud83ey"  # 合并不掉它


def test_control_chars_and_multiline_names_are_problems() -> None:
    assert "控制字符" in text_problem("a\x07b")
    assert "换行" in text_problem("a\nb", single_line=True)
    # 正文里的换行是正常的——只有技能名那种"一行一个"的地方才禁
    assert text_problem("a\nb") == ""


def test_sanitize_never_lets_a_surrogate_through() -> None:
    """**边界用例（①的验收）**：清洗只清洗、不拒绝，且任何来源都过不去。"""
    cleaned = sanitize_prompt_text("前\ud83e\udd16中\ud83e后\x00")

    assert "\ud83e\udd16" not in cleaned  # 成对的已合并
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in cleaned)  # 孤立的不见了
    assert "\ufffd" in cleaned  # 用替换字符留痕，而不是悄悄删掉
    assert "\x00" not in cleaned
    assert cleaned == sanitize_prompt_text(cleaned)  # 幂等


def test_message_wire_is_the_choke_point() -> None:
    """**请求体组装那一处就是咽喉**：消息里带孤立代理项，序列化出来一个都不剩。

    这是 ① 的落点（`services/llm.py::_message_wire`）：历史 / 记忆 / 技能 / 工具返回
    全都得从它过，所以不必逐个上游去堵。
    """
    message = ChatMessage(role="user", content="问一句\ud83e的话")

    body = _message_wire(message)

    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in body["content"])
    assert body["role"] == "user"
    # 普通消息（没有代理项）序列化出来与以前逐字一样
    assert _message_wire(ChatMessage(role="user", content="普通一句"))["content"] == "普通一句"
