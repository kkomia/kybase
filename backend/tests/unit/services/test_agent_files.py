"""Agent 的文件面（v0.33）：两个根、三种动作。

镜像同构：``app/services/agent_files.py`` → 本文件。

这一组盯的是**四件容易做错的事**：

1. **路径不能越界**（``..``、绝对路径、凭据文件、软链）——判定在 ``sandbox.resolve_in`` 里，
   但"文件工具真的用了它"必须在这里钉住：从工具面绕过它，等于前面所有隔离都白做
   （模型写 ``../../backend/.env`` 不是恶意，是它在猜项目结构）；
2. **两个根各自指对地方**：工作区是会话挂的那个目录，沙箱是这条会话的试错目录，
   不给 ``where`` 时的默认值要合理（有工作区就用工作区，没有就用沙箱）——写死成
   工作区会让没挂工作区的会话白撞一次错；
3. **读得完、读得懂**：按行分页、告诉它还剩多少行、二进制文件明确拒绝
   （读一半乱码比读不了更糟：模型会照样基于乱码往下推）；
4. **列目录要跳过依赖与缓存**（node_modules / .git / __pycache__），
   并把"跳过了几个"如实说出来——不说的话用户会以为那些文件不存在。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.agent_files import (
    Roots,
    list_files,
    read_bytes,
    read_file,
    search_files,
)


@pytest.fixture
def roots(tmp_path):  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    sandbox = tmp_path / "sandbox"
    workspace.mkdir()
    sandbox.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (workspace / "README.md").write_text("# 项目\n第一行\n第二行\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "big.js").write_text("x" * 10, encoding="utf-8")
    (sandbox / "notes.txt").write_text("试错的产物\n", encoding="utf-8")
    return Roots(workspace=workspace, sandbox=sandbox)


# ------------------------------------------------------------------ 根的选择


def test_default_root_prefers_workspace(roots: Roots) -> None:
    payload = list_files(roots)
    names = [item["path"] for item in payload["entries"]]
    assert payload["where"] == "workspace"
    assert "README.md" in names


def test_default_root_falls_back_to_sandbox(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """没挂工作区时会话仍然能用（用沙箱），而不是报错。"""
    box = tmp_path / "box"
    box.mkdir()
    (box / "a.txt").write_text("hi", encoding="utf-8")
    payload = list_files(Roots(workspace=None, sandbox=box))
    assert payload["where"] == "sandbox"
    assert [item["path"] for item in payload["entries"]] == ["a.txt"]


def test_asking_for_workspace_without_one_is_an_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """显式要工作区而没有 → 报错并说清怎么走（而不是悄悄换成沙箱）。"""
    box = tmp_path / "box"
    box.mkdir()
    with pytest.raises(InvalidRequestError, match="没有挂工作区"):
        list_files(Roots(workspace=None, sandbox=box), where="workspace")


def test_unknown_where_is_rejected(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError, match="where 只能是"):
        list_files(roots, where="everywhere")


# ------------------------------------------------------------------ 列


def test_list_skips_dependency_dirs_and_says_so(roots: Roots) -> None:
    payload = list_files(roots)
    names = [item["path"] for item in payload["entries"]]
    assert "node_modules" not in names
    assert payload["skipped_dirs"] == 1
    assert "跳过了 1 个" in str(payload["note"])


def test_list_puts_dirs_first(roots: Roots) -> None:
    names = [item["path"] for item in list_files(roots)["entries"]]
    assert names[0] == "src"


def test_list_filters_by_pattern(roots: Roots) -> None:
    names = [item["path"] for item in list_files(roots, pattern="*.md")["entries"]]
    assert names == ["README.md"]


def test_list_reports_truncation(roots: Roots) -> None:
    payload = list_files(roots, limit=1)
    assert len(payload["entries"]) == 1
    assert payload["total"] == 2
    assert "只列了前 1 条" in str(payload["note"])


def test_list_on_a_file_tells_you_to_read_it(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError, match="这不是目录"):
        list_files(roots, path="README.md")


# ------------------------------------------------------------------ 读


def test_read_file_returns_content_and_line_numbers(roots: Roots) -> None:
    payload = read_file(roots, path="src/main.py")
    assert payload["text"] == "print('hello')"
    assert payload["total_lines"] == 1
    assert payload["lines"] == 1


def test_read_file_pages_by_lines(roots: Roots) -> None:
    payload = read_file(roots, path="README.md", offset=2, limit=1)
    assert payload["text"] == "第一行"
    assert payload["offset"] == 2
    assert "共 3 行" in str(payload["note"])
    assert "offset=3" in str(payload["note"])


def test_read_file_reads_from_sandbox(roots: Roots) -> None:
    payload = read_file(roots, path="notes.txt", where="sandbox")
    assert payload["text"] == "试错的产物"
    assert payload["where"] == "sandbox"


# ------------------------------------------------------------------ 读原始字节（v0.55）


def test_read_bytes_returns_raw_content_and_name(roots: Roots) -> None:
    """给"把它加进知识库"那条路用：它要的是**字节**，不是给模型看的文本。"""
    content, name = read_bytes(roots, path="README.md")
    assert name == "README.md"
    assert content.decode("utf-8").startswith("# 项目")


def test_read_bytes_allows_binary(roots: Roots) -> None:
    """同一份文件 ``read_file`` 会拒（二进制），``read_bytes`` **必须放行**——
    否则"把这张图 / 这份 PDF 加进知识库"这一步永远走不通。"""
    workspace = roots.workspace
    assert workspace is not None
    blob = b"\x89PNG\r\n\x1a\n\x00\x00"
    (workspace / "shot.png").write_bytes(blob)
    content, name = read_bytes(roots, path="shot.png")
    assert content == blob
    assert name == "shot.png"


def test_read_bytes_enforces_the_size_cap(roots: Roots) -> None:
    """先看大小再读：上限由调用方给（摄入那条路有自己的上限）。"""
    with pytest.raises(InvalidRequestError, match="文件太大"):
        read_bytes(roots, path="README.md", max_bytes=2)


def test_read_bytes_uses_the_same_bounds_as_read_file(roots: Roots) -> None:
    """越界、不存在、指到目录：三个都要拒——这条路最终会把字节交给解析器，
    闸门一道都不能少（与 ``read_file`` 共用 ``resolve_in``）。"""
    with pytest.raises(InvalidRequestError):
        read_bytes(roots, path="../../.env")
    with pytest.raises(InvalidRequestError, match="没有这个文件"):
        read_bytes(roots, path="nope.txt")
    with pytest.raises(InvalidRequestError, match="这是一个目录"):
        read_bytes(roots, path="src")


def test_read_file_refuses_a_binary_file(roots: Roots) -> None:
    """二进制读不了要**当场说清**，而不是回一屏替换字符让人拿它当正文用。"""
    (roots.workspace / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    with pytest.raises(InvalidRequestError, match="二进制"):
        read_file(roots, path="logo.png")


def test_read_file_refuses_a_directory(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError, match="这是一个目录"):
        read_file(roots, path="src")


def test_read_file_missing_file(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError, match="没有这个文件"):
        read_file(roots, path="nope.txt")


# ------------------------------------------------------------------ 搜


def test_search_reports_file_and_line(roots: Roots) -> None:
    payload = search_files(roots, pattern="第二行")
    assert payload["total"] == 1
    hit = payload["hits"][0]
    assert hit["path"] == "README.md"
    assert hit["line"] == 3


def test_search_ignores_case_by_default(roots: Roots) -> None:
    assert search_files(roots, pattern="PRINT")["total"] == 1
    assert search_files(roots, pattern="PRINT", ignore_case=False)["total"] == 0


def test_search_accepts_regex(roots: Roots) -> None:
    assert search_files(roots, pattern=r"第.行")["total"] == 2


def test_search_rejects_a_broken_regex(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError, match="不是合法的正则"):
        search_files(roots, pattern="([")


def test_search_never_walks_into_node_modules(roots: Roots) -> None:
    assert search_files(roots, pattern="x" * 5)["total"] == 0


# ------------------------------------------------------------------ 越界


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "C:/Windows/win.ini", ".env"],
)
def test_paths_outside_the_root_are_rejected(roots: Roots, path: str) -> None:
    """**这一条是本模块的底线**：路径由模型生成，它写出来的几乎都是"猜的"。"""
    with pytest.raises(InvalidRequestError):
        read_file(roots, path=path)


def test_list_rejects_escaping_paths(roots: Roots) -> None:
    with pytest.raises(InvalidRequestError):
        list_files(roots, path="..")


# ---------------------------------------------------------------- 沙箱根的类型（D33）

def test_the_sandbox_root_is_a_path_so_the_file_tools_work(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """**没挂工作区的会话里，`Roots.sandbox` 必须是一个 `Path`**（2026-09-28 走查 D33）。

    它是 `sandbox_for(...).ensure()` 的返回值。原先 `resolve_roots` 漏了 `.ensure()`，
    拿到的是 `Sandbox` 数据类，而 `list_files` 会直接拿这个根调 `.exists()` —— 于是
    「没挂项目的会话里列文件」100% 抛
    `AttributeError: 'Sandbox' object has no attribute 'exists'`，
    异常原文还被工具层写进了上下文（用户看到天书、模型顺着源码乱猜）。

    这条用例同时钉住**类型**与**行为**：类型错了会当场红，行为错了（比如又抛别的）
    也跑不过去。注意它必须走**真正的 `resolve_roots`**：既有的 `roots` fixture 直接
    构造 `Roots`（sandbox 已是 Path），`test_agent_exec.py` 那条还把 `resolve_roots`
    monkeypatch 掉了——两处都恰好绕开了这个 bug，所以它一直没被抓住。
    """
    from pathlib import Path
    from types import SimpleNamespace

    from app.services.agent_files import describe_roots, resolve_roots
    from app.services.api_key import Caller

    services = SimpleNamespace(
        runtime=SimpleNamespace(data_dir=tmp_path),
        # 没挂项目的会话：workspace_id 为空
        conversations=SimpleNamespace(get=lambda _id: SimpleNamespace(workspace_id=None)),
        workspaces=SimpleNamespace(),
    )

    roots = resolve_roots(services, conversation_id="conv_1", caller=Caller(is_admin=True))

    assert roots.workspace is None
    assert isinstance(roots.sandbox, Path), (
        "sandbox 必须是 Path：它是 list_files / read_file 直接 .exists() 的那个根"
    )
    # 空沙箱 = 0 项，不抛；而且沙箱目录被真的建出来了（否则会报"没有这个目录"）
    payload = list_files(roots)
    assert payload["where"] == "sandbox"
    assert payload["total"] == 0
    assert roots.sandbox.is_dir()
    # 给模型看的"你在哪儿"里也要能印出这个根（它要拿绝对路径去 run_command）
    assert str(roots.sandbox) in describe_roots(roots)
