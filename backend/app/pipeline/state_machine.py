"""摄入流水线状态机（M2 T2.1）。

状态序列与《架构设计 v0.2》§4 逐字对齐：

``uploaded → probing → parsing → parsed → chunking → chunked → embedding → indexed``

两个刻意的设计：

- **允许原地自转**（``X → X``）：断点续跑时任务可能重设同一状态，这不该算非法迁移；
- **``failed`` 可以回到任意中间态**：实现"从失败的那一步续跑"，而不是整篇重来（架构 §4
  "每步可重试、可断点续跑、失败定位到具体步骤"）。
"""

from __future__ import annotations

from app.models.enums import TERMINAL_STAGES, DocumentStage

__all__ = [
    "ALLOWED_TRANSITIONS",
    "RETRYABLE_STAGES",
    "InvalidTransition",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "next_stage",
]

RETRYABLE_STAGES: frozenset[DocumentStage] = frozenset(
    {
        DocumentStage.PROBING,
        DocumentStage.PARSING,
        DocumentStage.CHUNKING,
        DocumentStage.EMBEDDING,
    }
)
"""可以从 ``failed`` 直接重新进入的阶段——对应四个可重试的流水线步骤。"""

ALLOWED_TRANSITIONS: dict[DocumentStage, frozenset[DocumentStage]] = {
    DocumentStage.UPLOADED: frozenset(
        {DocumentStage.PROBING, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.PROBING: frozenset(
        {DocumentStage.PARSING, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.PARSING: frozenset(
        {DocumentStage.PARSED, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.PARSED: frozenset(
        {DocumentStage.CHUNKING, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.CHUNKING: frozenset(
        {DocumentStage.CHUNKED, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.CHUNKED: frozenset(
        {DocumentStage.EMBEDDING, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.EMBEDDING: frozenset(
        {DocumentStage.INDEXED, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    # 可选增强分支：从终态进入，默认关闭，失败不影响主链路（架构 §4、§11）
    #
    # ``INDEXED → CHUNKING`` 是**"重新摄入"**：改完切分参数、或打开"分段出题"之后，
    # 已入库的文档要重新切分 + 重新向量化才生效。回到 CHUNKING 而不是 UPLOADED——
    # 后者会连解析一起重来，而解析可能是收费的云端服务（解析产物还在，
    # `_reuse_parse_result` 会直接复用）。`_advance` 允许自转（CHUNKING → CHUNKING）。
    DocumentStage.INDEXED: frozenset(
        {DocumentStage.CHUNKING, DocumentStage.ENRICHING, DocumentStage.FAILED}
    ),
    DocumentStage.ENRICHING: frozenset(
        {DocumentStage.ENRICHED, DocumentStage.FAILED, DocumentStage.CANCELED}
    ),
    DocumentStage.ENRICHED: frozenset({DocumentStage.FAILED}),
    DocumentStage.FAILED: RETRYABLE_STAGES,
    # 取消后可以重新摄入：这不是"坏掉了"，只是"我不想让它继续了"。
    # 允许直接回到四个可重试步骤，与 failed 同一条续跑语义。
    DocumentStage.CANCELED: RETRYABLE_STAGES,
}
"""允许的迁移。

**取消（``canceled``）只从"还在跑"的阶段可达**：已经 ``indexed`` 的文档没有
"叫停解析"这回事，给它开一条迁移只会让状态机多一个说不通的入口。
"""

_MAIN_CHAIN: tuple[DocumentStage, ...] = (
    DocumentStage.UPLOADED,
    DocumentStage.PROBING,
    DocumentStage.PARSING,
    DocumentStage.PARSED,
    DocumentStage.CHUNKING,
    DocumentStage.CHUNKED,
    DocumentStage.EMBEDDING,
    DocumentStage.INDEXED,
)


class InvalidTransition(Exception):
    """非法状态迁移。宁可硬失败，也不要让文档停在一个语义不明的状态上。"""

    def __init__(self, current: DocumentStage, target: DocumentStage) -> None:
        allowed = "、".join(sorted(stage.value for stage in ALLOWED_TRANSITIONS[current]))
        super().__init__(
            f"非法状态迁移：{current.value} → {target.value}（允许：{allowed}）"
        )
        self.current = current
        self.target = target


def can_transition(current: DocumentStage, target: DocumentStage) -> bool:
    """是否允许迁移；原地自转视为允许。"""
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: DocumentStage, target: DocumentStage) -> None:
    """不合法则抛 :class:`InvalidTransition`。"""
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def next_stage(current: DocumentStage) -> DocumentStage | None:
    """主链路上的下一步；已是终态（``indexed`` / ``failed`` / 增强分支）返回 None。"""
    if current not in _MAIN_CHAIN:
        return None
    index = _MAIN_CHAIN.index(current)
    return _MAIN_CHAIN[index + 1] if index + 1 < len(_MAIN_CHAIN) else None


def is_terminal(stage: DocumentStage) -> bool:
    """终态：不会再沿主链路自动推进。"""
    return stage in TERMINAL_STAGES
