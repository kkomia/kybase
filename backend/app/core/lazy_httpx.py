"""**惰性 httpx**：一个模块级代理，第一次访问属性时才 `import httpx`（P4-3）。

## 为什么需要它

`httpx/__init__.py` 里有 ``from ._main import main``（httpx 自带的 CLI 入口）——
于是**任何** ``import httpx`` 都会顺带把 ``click`` + ``pygments`` + ``rich`` 拉进来
（实测约 5.5 MB）。而**客户端运行时（本地边车）**只需要 httpx"发请求"那一部分能力，
不该为它背一套 CLI 依赖 ✗。

## 怎么用

```python
from app.core.lazy_httpx import httpx   # 不要在别处写 import httpx
...
httpx.Client(...)                        # 第一次访问属性时才真正导入
```

**调用点一个字都不用改**（属性名照旧），**行为不变**：
- 第一次访问 ``httpx.X`` 时才导入 ✓；
- httpx 真的不在时，仍在**第一次访问那一刻**抛 ``ModuleNotFoundError`` ✓
  （原先在导入模块那一刻抛 ✓）；
- ``except httpx.HTTPError`` / ``isinstance(exc, httpx.TimeoutException)`` 都照常工作 ✓
  （取到的是真类 ✓）。

## 判据

``python scripts/sidecar-closure.py``（只读）重跑，看 ``httpx`` / ``click`` / ``pygments``
是否从导入闭包里消失 ✓ —— "看代码觉得不该有"不算判据 ✗。
"""

from __future__ import annotations

from typing import Any

__all__ = ["httpx"]


class _LazyHttpx:
    """代理本身：自己没有 ``__dict__`` 里的 httpx 属性，全部转发到真模块。"""

    def __getattr__(self, name: str) -> Any:
        import httpx as module

        return getattr(module, name)

    def __dir__(self) -> list[str]:
        import httpx as module

        return dir(module)


#: 真属性（**不是**模块级 ``__getattr__``）：这样 ``from app.core.lazy_httpx import httpx``
#: 拿到的是代理本身 ✓，不会在导入那一刻就把 httpx 拉进来 ✓。
httpx = _LazyHttpx()
