"""执行工具（v0.33）：三道闸一层不省。

镜像同构：``app/services/agent_exec.py`` → 本文件。

`api/v1/sandbox.py` 是"用户自己点一条命令去跑"，这里是"模型想跑"。
两者的闸是同一套（权限 / 策略 / 内核隔离），差别只有一处，而那一处是本文件的重点：

**``ask`` 这一档在这里是"真的会问"**（v0.41）：登记一条待确认、把决定交给
工具循环去问，拿到 ``approval=…`` 之后**再跑一遍**同一个函数。所以这里必须钉住：

- 默认档下**一次都不执行**（`run_isolated` 一次都不能被调用）；
- 拿到 ``allow_once`` 才执行、``allow_always`` 还要写下放行规则、
  ``deny`` / ``timeout`` / 没有人可问时**不执行并说清是哪一种**——
  三者的措辞不一样，因为模型下一轮该讲的话不一样；
- 回给模型的"怎么放开"那两条出路**在任何一条拒绝对话里都要在**，
  否则模型只能反复重试同一条命令。

另外三条：不是管理员就拒绝（与端点同一档）、没有隔离时**默认降级为直接执行**
（v0.55；只有开了「无隔离时拒绝执行」才拒绝，且**排在"问用户"之前**——问了也跑不了的事
不该让对方白点一次）、命令拆得出 argv（``command`` 字符串也认，但不走 shell）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.exceptions import InvalidRequestError
from app.services import agent_exec
from app.services import approvals as approval_service
from app.services import isolation as isolation_service
from app.services.agent_exec import MAX_TIMEOUT_SECONDS, run_command
from app.services.api_key import Caller
from app.services.approvals import ApprovalRegistry
from app.storage.base import UserRecord


class _FakeRuntime:
    """运行期配置的假形状（只用到 ``get`` / ``set``）。"""

    def __init__(self, **values: object) -> None:
        self._values = {key: str(value) for key, value in values.items()}
        self.data_dir = Path(self._values.get("data_dir", "."))
        self.writes: list[dict[str, str]] = []

    def get(self, key: str) -> str:
        return self._values.get(key, "")

    def get_bool(self, key: str, default: bool = False) -> bool:
        # 与真实实现同一口径：**字符串要按布尔解析**（"false" 是假 ✗ 不是真）
        raw = self._values.get(key)
        if raw is None:
            return default
        return raw.strip().lower() not in ("", "0", "false", "no", "off")

    def set(self, values: dict[str, str], **_kwargs: object) -> None:
        """与真实实现同一个语义：**写完立刻生效**（它那边是清掉读缓存）。"""
        self.writes.append(dict(values))
        self._values.update({key: str(value) for key, value in values.items()})


class _FakeServices:
    def __init__(self, **values: object) -> None:
        self.runtime = _FakeRuntime(**values)
        # 真实那条链路里它挂在 Services 上（见 core/services.py），
        # 执行器通过 ``services.approvals`` 登记待确认
        self.approvals = ApprovalRegistry()


def _admin() -> Caller:
    return Caller(is_admin=True)


def _member() -> Caller:
    return Caller(is_admin=False, user=UserRecord(id="u_1", name="甲", username="jia"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """一个真实的工作区目录 + 一个假的数据目录（沙箱落在它下面）。"""
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("hi", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    services = _FakeServices(data_dir=str(data))
    monkeypatch.setattr(
        agent_exec, "resolve_roots", lambda *a, **k: agent_exec.Roots(root, data / "sandbox")
    )
    return services


def _allow_all(services: _FakeServices) -> None:
    """把权限扳到**完全访问**：命令直接跑（这是"允许"这一档在权限轴上的名字）。

    2026-09-27："命令执行策略"那一项已折进权限轴（仅查看 / 工作区内编辑 / 完全访问），
    所以这里写的是 `chat.permission` 而不是 `sandbox.exec_policy`。
    """
    services.runtime._values["chat.permission"] = "full"


def _manual(services: _FakeServices) -> None:
    """把权限扳到**会问**的那一档（``manual``）。

    为什么每条"要看到审批"的用例都得显式写这一行：权限轴的默认档从"问"换成了
    ``smart``（工作区内的命令不再逐条问，见 ``modes.DEFAULT_PERMISSION``），
    于是"没配置过 → 应当登记一条待确认"这件事**只在 manual 档成立**。
    这些用例钉的是安全不变量（没有许可一次都不许执行 / 拒绝与超时都如实说 /
    未知取值绝不等于允许），所以**钉住那一档**而不是放松断言。
    """
    services.runtime._values["chat.permission"] = "manual"


def _no_run(monkeypatch) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """盯着"有没有真的起进程"。返回一个列表，每真跑一次就多一条 argv。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: (
            calls.append(list(argv))
            or isolation_service.ExecutionResult(0, "ok\n", "", False, "bwrap")
        ),
    )
    return calls


def _available(monkeypatch, *, backend: str = "bwrap") -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        agent_exec,
        "_detect",
        lambda force=False: isolation_service.Isolation(backend, True, "测试里假装就位"),
    )


# ------------------------------------------------------------------ 闸 0：权限


def test_members_cannot_run_commands(workspace) -> None:  # type: ignore[no-untyped-def]
    """执行是"在这台机器上跑代码"，与端点同属管理员档——成员账号的模型不该绕过它。"""
    outcome = run_command(workspace, _member(), conversation_id=None, args={"command": "ls"})
    assert outcome.ran is False
    assert "只对管理员开放" in outcome.text


# ------------------------------------------------------------------ 闸 1：策略


def test_default_policy_asks_instead_of_running(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**默认档是 ask**：没配置过就执行，等于替用户点了那个头。

    v0.41 起这一档不再"拒绝并说清"，而是**登记一条待确认**停下来问用户；
    所以这里钉的是"什么都没跑，只多了一条等答案的登记"。
    """
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # 默认档是 smart（不问直接跑）；这条钉的是"问"那一档
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert calls == [], "没有许可时一次都不许执行"
    assert outcome.ran is False
    request = outcome.approval
    assert request is not None, "ask 档要交给工具循环去问（见 tool_loop._resolve_approvals）"
    # 界面上要看见的：命令原文、写清在哪跑、以及"这类都允许"会落下哪条规则
    assert request.args == "ls"
    assert request.rule == "Bash(ls:*)"
    assert "隔离" in request.detail
    assert request.tool == "run_command"
    # 登记表里真的有这一条（端点会按这个 id 把决定送回来）
    assert workspace.approvals.decide(request.approval_id, approval_service.ALLOW_ONCE) is True


def test_allow_once_runs_the_command(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """用户点了「允许一次」：这一条真的跑起来，结果照常回给模型。"""
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"argv": ["ls", "-la"]},
        approval=approval_service.ALLOW_ONCE,
    )
    assert calls == [["ls", "-la"]]
    assert outcome.ran is True and outcome.ok is True
    assert "退出码：0" in outcome.text
    # 「允许一次」不该往清单里写任何东西：那正是它与「这类都允许」的区别
    assert workspace.runtime.writes == []


def test_allow_always_remembers_the_rule_and_stops_asking(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """「这类都允许」：**规则落进放行清单**，之后同类命令不再问。

    下半段（"不再问"）才是这个按钮的意义所在——只跑这一次的话它与「允许一次」没差别。
    """
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # 规则只在"会问"那一档才写得进去（smart 档压根不问）
    first = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "git status --short"},
        approval=approval_service.ALLOW_ALWAYS,
    )
    assert first.ran is True
    # 建议的是"这一族"而不是完整命令：下次参数不同也该认出它（suggest_rule 的口径）
    assert workspace.runtime._values["sandbox.rules_allow"] == "Bash(git:*)"
    # 第二条同类命令：**没有 approval，也不会冒出待确认**
    second = run_command(
        workspace, _admin(), conversation_id=None, args={"command": "git status --porcelain"}
    )
    assert second.approval is None
    assert second.ran is True
    assert len(calls) == 2
    # 别的族照旧要问（规则是"这一族"，不是"这个工具"）
    other = run_command(workspace, _admin(), conversation_id=None, args={"command": "rm -rf x"})
    assert other.approval is not None


def test_deny_refuses_with_the_same_doorway_out(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """用户点了「拒绝」：不执行，回给模型的话**仍要带那两条出路**。

    与老行为一致的部分：「设置 → 聊天」这个人话路径、以及建议的放行规则。
    摘要（过程面板那一行）现在**点明是哪一道闸**（"对方没批准：拒绝"）——
    用户点开轨迹要能分清"我自己点了拒绝"与"策略拦的、我得去改设置"。
    多出来的还有一句"对方拒绝了"——那句话必须准确，模型才知道别再重试这条。
    """
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # "拒绝"要有意义，得先走到"问"那一步
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "ls"},
        approval=approval_service.DENY,
    )
    assert calls == []
    assert outcome.ran is False
    assert outcome.summary == "没有执行（对方没批准：拒绝）"
    assert "拒绝" in outcome.text
    assert "chat.permission" not in outcome.text  # 给用户看的是界面路径，不是配置键
    assert "设置 → 聊天" in outcome.text
    assert "Bash(ls" in outcome.text


def test_each_refusal_names_its_own_gate(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**每一种"没执行"都要报出是谁拦的**——这一行是用户判断"该去改哪里"的唯一依据。

    原先四种原因（拒绝规则 / 权限档不让跑 / 需确认但没人可问 / 对方没批准）
    共用一句"没有执行（策略拦下）"：用户在过程面板上看到它，分不清是规则的锅
    还是自己刚才点错了。这条用例把四句钉在一起——任一处退回泛泛而谈就会红。
    """
    _available(monkeypatch)
    _no_run(monkeypatch)
    runtime = workspace.runtime
    runtime._values["chat.permission"] = "manual"  # 规则与权限档都只在"问"那一档生效

    runtime._values["sandbox.rules_deny"] = "Bash(rm:*)"
    denied = run_command(workspace, _admin(), conversation_id=None, args={"command": "rm -rf x"})
    assert denied.summary == "没有执行（拒绝规则拦下）"

    runtime._values.pop("sandbox.rules_deny", None)
    runtime._values["chat.permission"] = "view"
    switched_off = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert switched_off.summary == "没有执行（权限档为「仅查看」）"

    # 旧的 `sandbox` 那一档（折进权限轴之前是"照跑但不过审批"）现在按"完全访问"认：
    # 命令真的跑了——这一档的差别已经被权限轴吸收，不该再留一条什么都不做的中间态
    runtime._values["chat.permission"] = "sandbox"
    legacy = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert legacy.ran is True

    # 没有界面的链路（定时任务）：确实没执行，而且**如实回「待确认」**——
    # 新语义（见 test_a_link_without_a_ui_says_pending_instead_of_silently_refusing）：
    # 没人可问时说"待确认"，**不**替用户说"没批准/拒绝"。
    # ⚠️ 这里必须**把档位钉回 manual**：上面第 256 行刚把它设成 "sandbox"（旧档，
    # 现在按"完全访问"认），不钉回去的话这条会真的把命令跑掉——那是另一件事。
    runtime._values["chat.permission"] = "manual"
    nobody = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "ls"},
        approval=approval_service.UNAVAILABLE,
    )
    assert "待确认" in (nobody.summary + nobody.text)
    assert "没有批准" not in nobody.text and "没批准" not in nobody.text


def test_timeout_is_a_refusal_and_says_nobody_answered(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**等不到人就按拒绝处理，而且要如实说是"没有回应"**。

    说成"对方拒绝了"会让模型以为对方看过并否了——它下一轮就会用完全错的措辞
    向用户复述（"你拒绝了这条命令"），而用户根本没看到那个问题。
    """
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # 超时那条只有在"会问"的档上才等得到
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "ls"},
        approval=approval_service.TIMEOUT,
    )
    assert calls == []
    assert outcome.ran is False
    assert "没有回应" in outcome.text
    assert "拒绝" not in outcome.text  # 不把"没等到"说成"对方拒绝了"
    assert "Bash(ls" in outcome.text


def test_a_link_without_a_ui_says_pending_instead_of_refusing(  # type: ignore[no-untyped-def]
    workspace, monkeypatch
) -> None:
    """没有人可以问的链路（定时任务）：**如实回「待确认」，不静默当拒绝**。

    旧行为：没界面就按"没批准"当场拒绝 ✗ ——那等于**替用户做了决定**（他根本没见过
    这条命令，却被告知"对方没批准"）。新行为：如实回一句"待确认" ✓，让调用方决定
    怎么处置；措辞里**不许**出现"没批准 / 拒绝"这类"有人拒绝过"的说法 ✓。

    这一条是本轮语义的要害：**没通道 ≠ 被拒绝**。
    """
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # 到"该问"那一步才谈得上"没人可问"
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "ls"},
        approval=approval_service.UNAVAILABLE,
    )
    assert calls == [], "没有人确认时一次都不许执行（安全不变量不变）"
    assert outcome.ran is False
    assert "待确认" in (outcome.summary + outcome.text), "要如实说是「待确认」"
    # 反向断言：没人拒绝过，就不许替用户说"被拒绝/没批准"
    assert "没有批准" not in outcome.text and "没批准" not in outcome.text
    assert "Bash(ls" in outcome.text, "出路（建议的放行规则）照旧要给"


def test_unknown_decision_never_means_allow(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """没见过的取值**一律当没许可**：这条路放行必须是明确的。"""
    _available(monkeypatch)
    calls = _no_run(monkeypatch)
    _manual(workspace)  # 未知取值那条也要先走到"问"这一步，否则测的是"直接跑"
    outcome = run_command(
        workspace, _admin(), conversation_id=None, args={"command": "ls"}, approval="yes-please"
    )
    assert calls == []
    assert outcome.ran is False


def test_isolation_is_checked_before_asking(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """严格模式下没有隔离就**不问了**：问了也跑不了的事不该让对方白点一次。

    这一条钉的是顺序（隔离排在"问用户"之前）——反过来的话用户会点一次同意，
    然后拿到的还是"这台机器上跑不了"。严格模式 = 设置里打开「无隔离时拒绝执行」。
    """
    workspace.runtime._values["sandbox.require_isolation"] = "true"
    monkeypatch.setattr(
        agent_exec, "_detect", lambda force=False: isolation_service.detect(prefer="none")
    )
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert outcome.approval is None
    assert "没有可用的内核级隔离" in outcome.text


def test_deny_rule_beats_a_broader_allow(workspace) -> None:  # type: ignore[no-untyped-def]
    workspace.runtime._values["sandbox.rules_allow"] = "Bash(git:*)"
    workspace.runtime._values["sandbox.rules_deny"] = "Bash(git push:*)"
    outcome = run_command(
        workspace, _admin(), conversation_id=None, args={"command": "git push origin main"}
    )
    assert outcome.ran is False
    assert "拒绝规则" in outcome.text


def test_view_permission_stops_everything(workspace) -> None:  # type: ignore[no-untyped-def]
    """「仅查看」这一档：命令跑不跑由**权限档**决定（原来读的是 `sandbox.exec_policy`，
    那一项已折进权限轴）。"""
    workspace.runtime._values["chat.permission"] = "view"
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert outcome.ran is False
    assert "仅查看" in outcome.text


# ------------------------------------------------------------------ 闸 2：隔离


def test_without_isolation_it_refuses_instead_of_running_naked(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**严格模式**下不回退成裸跑：回退会把"我们以为它在沙箱里"变成一个静默的假象。

    严格 = 设置里打开「无隔离时拒绝执行」（v0.55 那一项）。默认是降级直执，
    见下面 `test_without_isolation_degrades_to_direct_by_default`。
    """
    _allow_all(workspace)
    workspace.runtime._values["sandbox.require_isolation"] = "true"
    monkeypatch.setattr(
        agent_exec, "_detect", lambda force=False: isolation_service.detect(prefer="none")
    )
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert outcome.ran is False
    assert "没有可用的内核级隔离" in outcome.text


def test_without_isolation_refuses_by_default(  # type: ignore[no-untyped-def]
    workspace, monkeypatch
) -> None:
    """**默认拒绝**没有隔离的执行（D16，2026-09-29 翻转了 v0.55 那条默认）。

    为什么翻转：用户点名的会话 `conv_a5f4628f405f` 里，降级直执的表现是
    "上一步 `ls` 被拦 → 下一步 `find / -maxdepth 7` **整机跑起来**、网络不受限" ✗ ——
    那与"没有隔离就不执行"这条纪律正好相反。裸跑现在是**显式开关**：
    关掉 `sandbox.require_isolation` 才降级，而且降级时如实报 ``direct``（未隔离）✓。
    """
    _allow_all(workspace)
    monkeypatch.setattr(
        agent_exec, "_detect", lambda force=False: isolation_service.detect(prefer="none")
    )
    seen: dict[str, object] = {}

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return isolation_service.ExecutionResult(
            exit_code=0, stdout="hello\n", stderr="", truncated=False, backend="direct"
        )

    monkeypatch.setattr(isolation_service, "run_isolated", _fake_run)

    # ① 默认：拒绝执行（连进程都不起）
    refused = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})
    assert not refused.ran
    assert "没有可用的内核级隔离" in refused.text
    assert seen == {}

    # ② 显式关掉那一项（= 明确同意裸跑）才降级
    workspace.runtime._values["sandbox.require_isolation"] = "false"
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "ls"})

    assert outcome.ran and outcome.ok
    isolation = seen["isolation"]
    assert getattr(isolation, "backend", "") == "direct"
    assert "未隔离" in outcome.text


def test_utf8_command_output_comes_back_instead_of_a_decode_crash(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """命令吐 UTF-8 中文时，`run_command` 要**拿到输出**（不是"命令没能跑起来"）。

    这条钉的是野外实测到的那一批（`.shots/cases-api/G-03.json` 21 处、`K-03.json` 17 处，
    原始报错都是 `命令没能跑起来：object of type 'NoneType' has no len()`）：出事的命令
    一类是 `print(open('.../artifacts.py', encoding='utf-8').read())`（源文件里全是中文注释），
    一类是 `uv pip install`（uv 的进度框/勾是 UTF-8）——**输出里只要有一个非 GBK 的字节**，
    `run_isolated` 里 `text=True`（挑的是 locale = cp936）的读线程就抛 `UnicodeDecodeError`，
    `stdout/stderr` 变成 `None`，`_clip(None)` 再抛 `TypeError`，报出来的就是那句话。
    没出事的那一批（`os.listdir`、`os.path.getsize`、版本号、sqlite 查询）**输出全是 ASCII**。

    这里走的是**真链路**（不再 monkeypatch `run_isolated`）：权限扳到完全访问、
    探测结果装成"没有隔离"（于是降级成 direct 真跑），命令用 `sys.stdout.buffer`
    写**字节**，免得通过 locale 的子进程编码被"顺手救活"。

    D16 之后**默认拒绝**这种执行，所以要显式关掉那一项开关（= 同意裸跑）✓。
    """
    _allow_all(workspace)
    workspace.runtime._values["sandbox.require_isolation"] = "false"
    monkeypatch.setattr(
        agent_exec, "_detect", lambda force=False: isolation_service.detect(prefer="none")
    )
    child = (
        "import sys;"
        "sys.stdout.buffer.write('中文输出\\n'.encode('utf-8'));sys.stdout.flush()"
    )
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"argv": [sys.executable, "-c", child]},
    )

    assert outcome.ran is True, outcome.text
    assert outcome.ok is True, outcome.text
    assert "中文输出" in outcome.text
    assert "NoneType" not in outcome.text


def test_runs_when_policy_and_isolation_are_both_ready(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _allow_all(workspace)
    _available(monkeypatch)
    seen: dict[str, object] = {}

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen["argv"] = argv
        seen.update(kwargs)
        return isolation_service.ExecutionResult(
            exit_code=0, stdout="hello\n", stderr="", truncated=False, backend="bwrap"
        )

    monkeypatch.setattr(isolation_service, "run_isolated", _fake_run)
    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "python -c 'print(1)'", "timeout_seconds": 5},
    )
    assert outcome.ok and outcome.ran
    assert seen["argv"] == ["python", "-c", "print(1)"]
    assert seen["timeout"] == 5
    assert seen["allow_network"] is False
    # 结果里必须有**命令原文、退出码、标准输出**：少了任何一样，模型就没法判断
    # 这次到底发生了什么（"没输出"与"失败了"在它眼里会变成同一件事）
    assert "python -c print(1)" in outcome.text or "python -c 'print(1)'" in outcome.text
    assert "退出码：0" in outcome.text
    assert "hello" in outcome.text
    # 它得知道自己在哪跑（下一步要让命令去碰那些文件）
    assert str(workspace.runtime.data_dir) in outcome.text


def test_the_result_says_which_executable_actually_runs(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """⑥：结果里要写出**这条命令实际启动的可执行文件**（§12.341）。

    现场（`conv_a5f4628f405f`）：模型在回答里说"Python 环境是
    `E:\\gitlab\\kylab\\backend\\.venv`，里面没装 pip"——它验的是沙箱里解析到的那个
    python，`.venv` 是它自己**外推**的（结果里从来没有解释器路径）。这类外推只能靠
    "把事实摆出来"堵：写清 `argv[0]` 在本机 PATH 上解析到哪个绝对路径。
    """
    _allow_all(workspace)
    _available(monkeypatch)
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: isolation_service.ExecutionResult(
            exit_code=0,
            stdout="3.12.11\n",
            stderr="",
            truncated=False,
            backend=isolation_service.BACKEND_DIRECT,
        ),
    )

    outcome = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "python -c 'import sys; print(sys.version)'"},
    )

    assert "本条命令启动的可执行文件" in outcome.text
    assert "python" in outcome.text
    # 直连执行时那个路径就是**真启动的进程**；结果里必须点明"不是项目虚拟环境"
    assert "不是" in outcome.text

    # 本机没有的可执行文件：如实说"PATH 上找不到"，而不是编一个路径
    missing = run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "definitely-not-a-real-binary-xyz --version"},
    )
    assert "本机 PATH 上找不到" in missing.text


def test_the_result_does_not_claim_the_host_path_inside_isolation(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """隔离里跑时**不许**把本机解析到的路径说成"隔离里的解释器"（那是新的外推）。"""
    _allow_all(workspace)
    _available(monkeypatch)
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: isolation_service.ExecutionResult(
            exit_code=0, stdout="", stderr="", truncated=False, backend="bwrap"
        ),
    )

    outcome = run_command(
        workspace, _admin(), conversation_id=None, args={"command": "python -c pass"}
    )

    assert "隔离" in outcome.text
    assert "不是一回事" in outcome.text or "由那个后端自己的 PATH 决定" in outcome.text


def test_network_is_off_unless_asked(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _allow_all(workspace)
    _available(monkeypatch)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: (
            seen.update(kwargs) or isolation_service.ExecutionResult(0, "", "", False, "bwrap")
        ),
    )
    run_command(
        workspace, _admin(), conversation_id=None, args={"command": "ls", "allow_network": True}
    )
    assert seen["allow_network"] is True


def test_failure_is_reported_not_hidden(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _allow_all(workspace)
    _available(monkeypatch)
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: isolation_service.ExecutionResult(2, "", "boom", False, "bwrap"),
    )
    outcome = run_command(workspace, _admin(), conversation_id=None, args={"command": "false"})
    assert outcome.ran is True and outcome.ok is False
    assert "退出码：2" in outcome.text
    assert "boom" in outcome.text


# ------------------------------------------------------------------ 参数


def test_argv_array_is_preferred_and_untouched(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _allow_all(workspace)
    _available(monkeypatch)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: (
            seen.update(argv=argv) or isolation_service.ExecutionResult(0, "", "", False, "bwrap")
        ),
    )
    run_command(workspace, _admin(), conversation_id=None, args={"argv": ["ls", "-la", "my dir"]})
    assert seen["argv"] == ["ls", "-la", "my dir"]


def test_empty_command_is_rejected(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError, match="缺少参数"):
        run_command(workspace, _admin(), conversation_id=None, args={})


def test_timeout_is_clamped(workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """超时给到上限为止：一轮里同一批命令是并发的，一条跑几百秒会拖住整轮。"""
    _allow_all(workspace)
    _available(monkeypatch)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        isolation_service,
        "run_isolated",
        lambda argv, **kwargs: (
            seen.update(kwargs) or isolation_service.ExecutionResult(0, "", "", False, "bwrap")
        ),
    )
    run_command(
        workspace,
        _admin(),
        conversation_id=None,
        args={"command": "sleep 999", "timeout_seconds": 9999},
    )
    assert seen["timeout"] == MAX_TIMEOUT_SECONDS


def test_non_numeric_timeout_is_rejected(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError, match="要是数字"):
        run_command(
            workspace,
            _admin(),
            conversation_id=None,
            args={"command": "ls", "timeout_seconds": "很久"},
        )
