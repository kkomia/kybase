"""进提示词之前的**文本卫生**（纯函数，不依赖任何服务）。

## 为什么单独一个模块

这一族判断有两个不同口径的用处，落点分别在服务层的两头：

- **技能扫描**（`services/skills.py`）：来源可控 → 非法就**丢弃它并给中文理由**；
- **请求组装**（`services/llm.py` 的 `_message_wire`）：来源不可控（历史 / 记忆 /
  技能 / 工具返回都算）→ **只清洗、不拒绝**，因为"整句话都问不出去"是绝不能接受的。

两边都要用同一套判断，而 `llm.py` 与 `skills.py` 互相 import 会成环（真踩过：
`llm → skills → runtime_config → llm`），所以放到这里——`core/` 这一层不认识任何服务。

## 背景（D16 P0，2026-09-29）

上游把 emoji 写成**字面转义**（`"\\ud83e\\udd16"`），YAML 解出来是两个**孤立代理项**；
它一旦进请求体，httpx 编码时抛 `UnicodeEncodeError: surrogates not allowed`，
**整句对话全废**（两条真会话实测，用户看到的就是"服务端出错了"）。
"""

from __future__ import annotations

import re

__all__ = ["recombine_surrogates", "sanitize_prompt_text", "text_problem"]

#: 提示词里不能出现的字符：控制字符（``\t``/``\n``/``\r`` 不算，正文与描述里正常）。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def recombine_surrogates(text: str) -> str:
    """把成对的代理项合并回正常码位（``"\\ud83e\\udd16"`` → 一个 🤖）。

    成对合起来就正常了——那是**转义写坏**，不是非法内容；只剩一个的才叫真非法。
    """
    if not text:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(text):
            low = ord(text[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                index += 2
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


def text_problem(text: str, *, single_line: bool = False) -> str:
    """这段字能不能进提示词；不能就说清为什么（空串 = 没问题）。

    判之前**先 `recombine_surrogates`**（调用方负责），剩下的才叫真的非法：
    孤立代理项与控制字符。``single_line=True`` 时换行也算非法——那用在技能名上，
    因为技能目录是"一条技能一行"，名字里的换行会凭空多出一个"技能名"。
    """
    for index, char in enumerate(text or ""):
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            return (
                f"已丢弃：第 {index} 个字符是孤立代理项（U+{code:04X}）"
                "——它会让整句提示词编码失败"
            )
        if single_line and char in "\n\r":
            return f"已丢弃：第 {index} 个字符是换行——技能名必须是一行"
    matched = _CONTROL_CHARS.search(text or "")
    if matched:
        return f"已丢弃：第 {matched.start()} 个字符是控制字符（U+{ord(matched.group()):04X}）"
    return ""


def sanitize_prompt_text(text: str) -> str:
    """**进请求体前的最后一道**：合并成对代理项 → 仍孤立的换 U+FFFD → 去掉控制字符。

    只清洗、不拒绝；**幂等**（重复跑不会越改越坏）。理由见模块头：
    到了这一层，"拒绝"已经不是选项了——要么洗干净，要么整句对话失败。
    """
    if not text:
        return text
    out: list[str] = []
    for char in recombine_surrogates(text):
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            out.append("\ufffd")
        elif (code < 0x20 and char not in "\n\t\r") or code == 0x7F:
            continue
        else:
            out.append(char)
    return "".join(out)
