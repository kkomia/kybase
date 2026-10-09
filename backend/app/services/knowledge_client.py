"""**KB 检索**这条接缝的显式接口（Phase B 第一刀，2026-09-29）。

## 为什么需要它

目标形态里 **KB（解析/切块/嵌入/向量/检索）留在服务器** ✓，而**循环与工具在本地** ✓。
今天这条链是**进程内调用**：`agent_tools.py` 直接拿 `services.chat.retrieve_sources(...)`
（**全仓只有这一处调用点** ✓，`agent_tools.py:1314` ✓）。把它显式化成协议之后，
"本地循环 + 远端 KB"才有可插的地方 ✓，而且**出本机的只有"问题 + 命中片段"** ✓
（工作区文件永不上传 ✗ —— 除非用户明确要求入库 ✓）。

## 一处定义、两种装配

- **今天的实现（默认）**：`services.chat.ChatService.retrieve_sources` ✓（进程内检索 ✓）；
- **边车模式的实现（将来）**：把同一个入参打成一条到服务器的检索请求 ✓，
  返回**同一形状**的 `SourceRef` 列表 ✓（界面、引用、出处卡片一个字节都不用改 ✗）。

## 边界（本轮）

**只声明，不接线** ✗：`agent_tools.py` 与 `prompt.py` 此刻正被另一条 lane 改 ✓
（`git status` 显示两者都在途 ✓），所以把 `agent_tools.py:1314` 改成走本协议那一步
**留到下一轮** ✓（改法与验收写在 `docs/规范/本地边车-迁移方案-v0.1.md` ✓）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.chat import SourceRef

__all__ = ["KnowledgeClient"]


@runtime_checkable
class KnowledgeClient(Protocol):
    """循环要的**全部** KB 能力。

    签名与 `ChatService.retrieve_sources` **逐字一致** ✓（将来换实现时调用点不用改 ✗）：

    - ``query`` 是**关键字参数** ✓（它已经是关键字专用 ✓ —— 别写成位置参数，
      那正是子 Agent 那条链曾经踩过的坑，见 `tests/unit/services/test_subagent.py`）；
    - ``kb_ids`` 为空 = 这一轮不查库 ✓（调用点在检索层直接返回空 ✓）；
    - 返回的是带编号的 ``SourceRef`` 列表 ✓（出处、引用卡片、过程面板三处共用它 ✓）。
    """

    def retrieve_sources(
        self,
        *,
        query: str,
        kb_ids: list[str],
        top_k: int | None = None,
        candidate_k: int = 40,
        reader: object | None = None,
    ) -> list[SourceRef]:
        """检索出处（与进程内实现同一形状）。"""
        ...
