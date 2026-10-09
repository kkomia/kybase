"""**模型调用**这条接缝的显式接口（Phase B 第一刀，2026-09-29）。

## 为什么需要它

目标形态（已拍板）是"**循环 / 工具 / 沙箱在本地，模型代理在服务器**"：
本机跑 agent 循环，模型请求走服务器的代理（**key 不下发** ✗）。
今天这条链是**进程内直连**（`llm.OpenAICompatChat` 自己发 httpx ✗），
于是"本地循环 + 远端模型"这件事**没有可插的地方** —— 这一层就是那个插口。

## 一处定义、两种装配

- **今天的实现（默认）**：`llm.OpenAICompatChat` ✓ —— 进程内直接打模型端点（服务器模式 ✓）；
- **边车模式的实现（将来）**：把同样的入参转成到"模型代理"的一条请求 ✓
  （流式语义不变：`stream` 逐块吐、`stream_events` 带工具调用 ✓）。
  **两份装配点共用同一个循环** ✓：循环只认这个 Protocol ✗ 不认 httpx ✗ —— 这是"同一份代码、
  两个装配点"那条纪律的落点（见 `docs/规范/本地边车-迁移方案-v0.1.md`）。

## 边界（本轮）

**纯声明 + 一处标注**：不改任何行为 ✗（`OpenAICompatChat` 本来就是这么三个方法 ✓）。
`chat.py` 里那个 `_chat_factory` 的注入点**将来**改读本协议 ✓ —— 本轮不动它
（`agent_tools.py` / `prompt.py` 正被另一条 lane 改 ✓，见交卷）。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

from app.services.llm import ChatMessage, LLMDelta, ToolSpec

__all__ = ["ModelClient"]


@runtime_checkable
class ModelClient(Protocol):
    """循环要的**全部**模型能力（一个都不能少：少了就装不进服务器代理 ✗）。

    三个方法对应三条用法（与 `llm.OpenAICompatChat` 逐字同名同签名 ✓）：

    - ``complete``：一次性补全（子 Agent、压缩摘要、技能简介这类小任务 ✓）；
    - ``stream``：只要文本增量（普通对话 ✓）；
    - ``stream_events``：要**工具调用**的那条（工具循环 ✓）——返回值是
      ``Iterator[LLMEmit]``（文本增量与工具调用两种 emit ✓）。

    ``runtime_checkable`` 是为了让用例能直接断言
    "``OpenAICompatChat`` 确实满足这份契约" ✓（结构性类型：不必改类的继承 ✗）。
    """

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        """一次性补全，返回完整文本。"""
        ...

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        """流式补全：逐个文本增量。"""
        ...

    def stream_events(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None
    ) -> Iterator[LLMDelta]:
        """流式补全 + 工具调用事件（碎片原样转出，拼装与执行在工具循环那边 ✓）。"""
        ...
