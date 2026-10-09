"""进程级共享出站客户端（``app/core/http.py``）的语义。

这个模块的存在理由只有一个：**别在每次调用时重新握手**。
所以用例盯的也是这一条——同一个对象、关掉之后能重建、以及那个不能改的默认值。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.http import close_shared_client, shared_client


@pytest.fixture(autouse=True)
def _clean_shared_client() -> Iterator[None]:
    """每个用例前后各关一次。

    共享客户端是**进程级**的：不清理的话，某个用例把它关掉或换掉，
    后面的用例会拿到意外的状态，而那种失败看起来像"被测代码坏了"。
    """
    close_shared_client()
    yield
    close_shared_client()


def test_the_same_client_comes_back_on_every_call() -> None:
    """两次拿到的是**同一个对象**：连接池因此是共用的。"""
    assert shared_client() is shared_client()


def test_closing_allows_a_fresh_client() -> None:
    """关掉之后能重建（否则关停再启动的进程会拿到一个已关闭的客户端）。"""
    first = shared_client()
    close_shared_client()

    assert first.is_closed
    second = shared_client()
    assert second is not first
    assert not second.is_closed


def test_closing_twice_is_safe() -> None:
    """重复关停是安全的：lifespan 的收尾路径可能被走到两次（测试、异常退出）。"""
    shared_client()
    close_shared_client()
    close_shared_client()


def test_redirects_stay_off() -> None:
    """**不许跟随重定向**：联网抓取要求每一跳都重新校验地址（防 SSRF 绕过），
    在共享客户端上打开这个开关，会让 ``services/web.py`` 里那个逐跳检查形同虚设。
    """
    assert shared_client().follow_redirects is False
