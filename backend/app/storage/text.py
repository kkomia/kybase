"""检索用的中文切词（存储层共享）。

**为什么单独一个模块**：全文检索的写入与查询必须用**同一个**分词器，否则
"写入时切成 A、查询时切成 B"就是必然的召回缺失。现在有两个全文实现
（sqlite FTS5 与 pgvector 那套 tsvector），把分词放在实现里就会出现第二份副本，
迟早漂成两种切法。

**与 ``services/embedding/deterministic.py::tokenize`` 不是一回事，别合并**：
那个是哈希嵌入用的，会补中文单字并截断到 512，返回 ``list[str]``；
这里返回的是"空格连接的词元串"，直接喂给索引。用途不同，判定标准也不同。

中文分词不依赖数据库扩展：**写入前用 jieba 切好**，查询时对 query 做同样处理。
这样只用标准 FTS5 / PG 内建 tsvector 就能拿到可用的中文召回，不引入 C 扩展编译。
"""

from __future__ import annotations

from typing import Any

_SPACE = " "

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

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"中文切词"只有**服务器**
    那条检索链会用 ✓ —— 边车（客户端运行时）只跑循环 + 工具 + 沙箱 ✓，从不切词。
    模块级 ``import jieba`` 的后果实测过（P4-3，2026-09-29）：``dist\\sidecar-runtime`` 里
    ``python -m app.sidecar`` 起不来 ✗（``ModuleNotFoundError: No module named 'jieba'``）。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，jieba 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
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


def cut(text: str) -> list[str]:
    """切词并过滤纯空白，返回词元列表。

    ``cut_for_search`` 会额外给出长词的子词（如"向量检索"→ 向量/检索/向量检索），
    检索场景要的正是这种"宽召回"，精度交给 RRF 融合与可选 rerank。
    """
    jieba = _jieba()
    return [word for word in jieba.cut_for_search(text) if word.strip()]


def tokenize(text: str) -> str:
    """切词并空格连接，供写入索引使用。

    大小写不归一是有意的：FTS5 的 unicode61 与 PG 的文本解析器都会对 ASCII
    做大小写折叠，两边在这一层自动一致，多写一次 lower 反而不一致（中文不受影响）。
    """
    return _SPACE.join(cut(text))
