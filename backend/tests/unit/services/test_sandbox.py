"""沙箱与工作区的执行边界（v0.15）。

镜像同构：``app/services/sandbox.py`` → 本文件。

这一层错了不会有任何"功能异常"——它会安静地放行一些不该放行的路径，
而**账号隔离与数据隔离全压在这一道上**（文件工具的路径是模型生成的，
它会写出 ``../../backend/.env`` 这类路径：不是恶意，是它在推测项目结构）。
所以这里逐条钉住，且用**真实路径**而不是纯字符串断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, InvalidRequestError
from app.services.sandbox import (
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    POLICY_SANDBOX,
    ExecutionPolicy,
    Sandbox,
    resolve_in,
    sandbox_for,
    sandbox_root,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个像工作区那样的目录：里面有子目录、有个 .env、还有指向外面的软链。"""
    workspace = tmp_path / "workspace"
    (workspace / "src" / "深" / "层").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / ".env").write_text("KYLAB_DATABASE_URL=postgresql://u:p@h/db\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("外面的秘密\n", encoding="utf-8")
    return workspace


# --------------------------------------------------------------- 路径约束


def test_resolves_a_normal_relative_path(root: Path) -> None:
    assert resolve_in(root, "src/main.py") == (root / "src" / "main.py").resolve()
    # 中文目录名与多余的分隔符都要正常
    assert resolve_in(root, "./src//深/层/") == (root / "src" / "深" / "层").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "../../backend/.env",
        "src/../../outside.txt",
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "C:foo.txt",
        "\\\\server\\share\\x.txt",
        "",
        "   ",
    ],
)
def test_rejects_absolute_and_escaping_paths(root: Path, bad: str) -> None:
    """**绝对路径直接拒**，不是"当成相对路径处理"：模型给绝对路径时，
    它想指的地方和我们理解的通常不是一个地方，猜错比拒掉危险。"""
    with pytest.raises(InvalidRequestError):
        resolve_in(root, bad)


@pytest.mark.parametrize(
    "secret",
    [".env", "src/.env", ".ssh/id_rsa", "config/credentials.json", "keys/id_ed25519"],
)
def test_rejects_sensitive_files_even_inside_the_root(root: Path, secret: str) -> None:
    """即使文件**确实在根之内**也拒。

    这一道是给"根设错了"兜底的：用户把工作区设成了家目录、或者设成了项目根
    而项目根里就有 ``.env``——那时前几道检查全都通不过拦截，只有这一道能拦住。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        resolve_in(root, secret)

    assert "凭据" in str(excinfo.value)


def test_rejects_symlink_escaping_the_root(tmp_path: Path) -> None:
    """符号链接：**只有真解析一遍才确认得了**，字符串前缀判断拦不住它。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    link = workspace / "peek.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限时跳过
        pytest.skip("这个环境不允许建符号链接")

    with pytest.raises(InvalidRequestError):
        resolve_in(workspace, "peek.txt")


def test_missing_path_is_allowed_for_new_files(root: Path) -> None:
    """还不存在的文件要能解析出来（Agent 要新建文件）。"""
    target = resolve_in(root, "src/新文件.py")
    assert not target.exists()
    assert target.parent.name == "src"


# --------------------------------------------------------------- 策略四档


def test_allow_runs_without_asking() -> None:
    ExecutionPolicy(mode=POLICY_ALLOW).require_allowed()


def test_deny_says_how_to_change_it() -> None:
    """拒绝的报错要**指导下一步**——只说"不允许"等于没说。"""
    with pytest.raises(ForbiddenError) as excinfo:
        ExecutionPolicy(mode=POLICY_DENY).require_allowed()

    assert "能力设置" in str(excinfo.value)


def test_ask_returns_conflict_until_approved() -> None:
    """``ask`` 未确认时是 **409**（不是 403）：不是"你不能做"，是"要先确认"。"""
    policy = ExecutionPolicy(mode=POLICY_ASK)

    with pytest.raises(ConflictError):
        policy.require_allowed()

    policy.require_allowed(approved=True)  # 确认之后放行


def test_sandbox_mode_does_not_ask() -> None:
    """``sandbox`` 档不需要确认：它的安全性由"只能在沙箱里做"保证，而不是由用户点头。"""
    ExecutionPolicy(mode=POLICY_SANDBOX).require_allowed()


def test_unknown_mode_is_rejected_at_construction() -> None:
    with pytest.raises(InvalidRequestError):
        ExecutionPolicy(mode="whatever")


def test_policy_carries_the_workspace_root() -> None:
    """策略与根**绑在一起**：只问不约束（用户同意了，然后它去读了别处）
    与只约束不问（它默默改了用户的真实项目）都是不完整的设计。"""
    policy = ExecutionPolicy(mode=POLICY_ASK, workspace_root=Path("E:/code/proj"))

    assert policy.workspace_root == Path("E:/code/proj")
    assert policy.needs_approval is True


# --------------------------------------------------------------- 沙箱


def test_sandbox_lives_under_the_data_dir(tmp_path: Path) -> None:
    box = sandbox_for(tmp_path / "data", "conv_abc123")

    assert box.root == sandbox_root(tmp_path / "data") / "conv_abc123"
    assert box.ensure().is_dir()


def test_sandbox_is_per_conversation(tmp_path: Path) -> None:
    """同一会话共享一份（第一步下的脚本第二步要用），换会话就是全新一份。"""
    data = tmp_path / "data"
    first = sandbox_for(data, "conv_a")
    second = sandbox_for(data, "conv_b")
    first.ensure().joinpath("tmp.sh").write_text("echo 1", encoding="utf-8")

    assert first.root != second.root
    assert second.usage() == (0, 0)
    assert first.usage() == (1, 6)


@pytest.mark.parametrize("bad", ["../../../etc", "..", ".", "a/../../b", "C:/Windows"])
def test_sandbox_id_is_sanitised(tmp_path: Path, bad: str) -> None:
    """会话 id 会变成目录名，所以必须清洗——带 ``../`` 或盘符的 id
    会把沙箱指到别处去（"目录名注入"）。

    ``".."`` 单独钉一下：只把非法字符折成下划线是不够的，
    ``sandbox_root/".."`` 正好是**数据目录本身**（实测踩到）。
    """
    box = sandbox_for(tmp_path / "data", bad)

    assert box.root.parent == sandbox_root(tmp_path / "data")
    assert box.root.name not in ("", ".", "..")
    assert "/" not in box.root.name and "\\" not in box.root.name


def test_sandbox_paths_are_confined() -> None:
    box = Sandbox(root=Path("E:/tmp/sandbox-x"))

    with pytest.raises(InvalidRequestError):
        box.path("../outside.txt")


def test_clear_removes_everything_and_is_idempotent(tmp_path: Path) -> None:
    """沙箱**不承诺持久**：清掉是它的正常归宿，重复清也不能报错。"""
    box = sandbox_for(tmp_path / "data", "conv_x")
    box.ensure().joinpath("a.txt").write_text("x", encoding="utf-8")

    box.clear()
    assert not box.root.exists()
    box.clear()  # 再清一次不该炸


def test_usage_counts_files_and_bytes(tmp_path: Path) -> None:
    box = sandbox_for(tmp_path / "data", "conv_y")
    root = box.ensure()
    (root / "a").mkdir()
    (root / "a" / "one.bin").write_bytes(b"1234")
    (root / "two.bin").write_bytes(b"12")

    assert box.usage() == (2, 6)


def test_missing_sandbox_reports_zero(tmp_path: Path) -> None:
    assert sandbox_for(tmp_path / "data", "never-used").usage() == (0, 0)
