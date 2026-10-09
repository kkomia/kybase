"""词面覆盖：候选与查询在**词**这一层的重合比例（相关度下限的词面那一半）。

**为什么需要它**：全文通道的原始分（PG 的 ``ts_rank_cd``）**没法当绝对下限用**。
本机四个真实库上实测（2026-09-24，bge-m3，见 ``docs`` 与本次标定记录）：

- 纯噪声查询也能拿到很高的分：医学库上「合唱团的排练时间安排」的全文最高分
  10.1，「真空镀膜机的靶材更换周期」9.9——因为 jieba 会把「的」「更换」这类词
  切出来，OR 查询命中一个高频词就够了，而 ``ts_rank_cd`` 只按覆盖密度与词频算；
- 真命中的全文最高分可以更低：同一批语料上「街道绿视率对青少年视力的影响」只有
  8.9，另一个库的「眼科」系列查询更是低到 2-5。

两个区间**重叠**，所以"绝对下限"选不出一个不误杀的数；"相对自身最高分"也没用
——噪声查询的最高分就是它自己的头部，比例永远接近 1。相对分（``score_threshold``）
因此只能剪尾巴，剪不掉头部，这正是"8 条假命中"的来源。

这一层换一个**跨查询、跨库可比**的量：查询里的实词有多少比例真的出现在这段正文里。
它是"词面证据"的归一化形态（分子分母都随查询长度走，所以长度不影响判定），
与向量余弦是**或**的关系（见 ``service._passes_relevance``）：语义够近也行、
词面够像也行，两条路各有各的证据。

**与索引的切法同源但**不同档**（``jieba.lcut`` vs 索引的 ``cut_for_search``）**：
索引那边要**宽召回**，所以 ``cut_for_search`` 会把长词再切出子词（「服务器」→ 服务/务器/服务器）；
判定这边要的是**证据**，子词会让"蹭上一个词头"也算命中（实测：「如何用 Rust 写一个 HTTP
服务器」在医学库上就靠「一个」「服务」蹭过了 0.5，留下一条完全无关的命中）。
两边同源（同一份词典、同一套分词器）保证不会漂成两种中文切法，
``tests/unit/services/retrieval/test_relevance.py`` 有一条用例盯着这个包含关系。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["MIN_TERM_CHARS", "content_terms", "term_coverage"]

#: 惰性拿到的 jieba（**故意不是模块级导入** ✗）。
_JIEBA: Any = None


def _jieba_cache_file() -> str | None:
    """jieba 的缓存该落在**数据目录**，而不是进程的 cwd。

    为什么必须显式设：jieba 的 ``cache_file`` 默认是 ``None``，此时它用
    ``os.path.join(tmp_dir or tempfile.gettempdir(), "jieba.cache")`` ——
    而本机沙箱**拒写 %TEMP%**，``tempfile.gettempdir()`` 会静默回退到 **cwd**，
    于是跑一次检索用例，仓库根就冒出一个 ``jieba.cache``（约 9 MB，实测过）。

    **行为不变**：缓存里只是前缀词典的概率表，谁写都一样 —— 切词结果一字不差，
    只是它不再落在 cwd。取不到数据目录时返回 ``None``（退回 jieba 自己的默认）：
    别为了一个缓存路径让切词挂掉。
    """
    try:
        from pathlib import Path

        from app.core.config import get_settings

        data_dir = Path(get_settings().data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / "jieba.cache")
    except Exception:
        return None


def _jieba() -> Any:
    """第一次用到时才导入 jieba（模块级导入会让**客户端运行时**凭空多背 40.9 MB ✗）。

    为什么惰性：这个模块挂在 `app.services.retrieval` 的导入链上 ✓，而"算词面覆盖"
    只有**服务器**那条检索链会用 ✓ —— 边车（客户端运行时）只跑循环 + 工具 + 沙箱 ✓。
    模块级 `import jieba` 的后果实测过（P4-3，2026-09-29）：`dist\\sidecar-runtime` 里
    `python -m app.sidecar` 直接启动失败 ✗（`ModuleNotFoundError: No module named 'jieba'`），
    而"精简集合够用"这件事就此不成立 ✓。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，jieba 真的不在时仍在**调用那一刻**
    抛 `ModuleNotFoundError` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _JIEBA
    if _JIEBA is None:
        import jieba

        # 缓存落哪儿：**数据目录**，不是 cwd（见 `_jieba_cache_file()` 的说明）
        cache = _jieba_cache_file()
        if cache:
            jieba.dt.cache_file = cache

        _JIEBA = jieba
    return _JIEBA

#: 参与判定的最短词元（字符数）。单字词在中文里绝大多数是虚词或黏着语素
#: （的／了／是／在／机／修），拿它们当"词面证据"只会把噪声算成命中——
#: 实测「真空镀膜机的靶材更换周期」在医学库上的噪声头，就是靠「的」命中的。
MIN_TERM_CHARS = 2


def content_terms(text: str) -> tuple[str, ...]:
    """查询里的实词（去重、大小写归一）。切法见模块头。"""
    terms: dict[str, None] = {}
    for word in _jieba().lcut(text or ""):
        term = word.strip()
        if len(term) < MIN_TERM_CHARS:
            continue
        # 大小写归一是为了与全文索引对齐：tsvector 对 ASCII 做折叠，
        # 查询「NDVI」命中的段落里写的是「ndvi」时，这一层也必须认。
        terms.setdefault(term.casefold(), None)
    return tuple(terms)


def term_coverage(terms: Sequence[str], text: str) -> float:
    """``terms`` 里有多少比例出现在 ``text`` 中（0.0–1.0）。

    ``terms`` 为空（查询里压根没有实词，例如只输入了「的」或一个标点）时返回 1.0：
    这一侧无从判断，不该由它来拦——**宁可少拦**。防误杀是这一层的第一原则，
    真要收紧的手感应该来自向量那一侧的下限。
    """
    if not terms:
        return 1.0
    haystack = (text or "").casefold()
    matched = sum(1 for term in terms if term in haystack)
    return matched / len(terms)
