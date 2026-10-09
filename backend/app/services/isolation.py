"""内核级执行隔离（v0.16，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §4）。

``services/sandbox.py`` 管的是**路径与策略**（能不能碰、要不要确认）；
这个模块管的是**内核级隔离**（真跑起来之后，它到底能不能碰）。

三件事分层，缺一层都不够：

| 层 | 回答 | 在哪 |
| --- | --- | --- |
| 策略 | 要不要问用户 | ``sandbox.ExecutionPolicy`` |
| 路径 | 参数里能写哪些路径 | ``sandbox.resolve_in`` |
| **内核** | **进程真跑起来之后能碰什么** | 本模块 |

**为什么必须有第三层**：前两层都作用在"我们替它构造的参数"上。一条
``python -c "open('/etc/passwd').read()"`` 里没有任何路径参数要校验——
它会直接读到我们根本不知道它要读什么的地方。只有内核级的文件系统视图
（bwrap 的 bind mount、Seatbelt 的 profile、容器的 mount namespace）
能管住这一类。

**三个后端**（照 QwenPaw 的做法，一个平台一个）：

- Linux：**bubblewrap**——把整个 ``/`` 只读挂进来，再把工作区与沙箱**读写**挂上，
  ``/tmp`` 换成 tmpfs；需要断网时加 ``--unshare-net``。用户态、免 root、启动快；
- macOS：**sandbox-exec**——Seatbelt profile，``deny default`` 之后按目录放行读写；
- 任意平台：**Docker**——真正的内核隔离（namespace + cgroup），只要装了 docker 就能用。
  Windows 上没有前两个的原生等价物（AppContainer 要写一堆 Win32 调用，且不覆盖
  文件系统之外的边界），所以 **Docker 是 Windows 上唯一的真隔离路径**——
  这一点如实报出来，而不是假装"策略层就够了"。

**探测而不是假设**：``detect()`` 真去盘上找这些可执行文件（并检查 docker daemon
是否活着——装了 CLI 但 daemon 没起是最常见的假阳性）。界面与端点据此告诉用户
"这台机器上现在有什么"。**没有隔离时明确拒绝要隔离的执行**，不回退成裸跑：
回退会让"我们以为它在沙箱里"成为一个静默的假象，而这个假象比拒绝危险得多。

**v0.55 的降级档**：上面那条纪律在"这台机器上永远没有隔离"时会变成一道死闸——
Windows 上没有 bwrap / sandbox-exec 的原生等价物，容器里也常没挂 docker，
于是**本地源码启动与容器部署两边都跑不了命令**（用户报的正是这条："明明指定了工作区，
为啥还是不能执行工具"）。所以补一个**显式的降级后端** ``direct``（``direct_isolation``）：
没有真隔离时**直接在本机执行**，但**如实标注"未隔离"**，并由设置项
``sandbox.require_isolation`` 决定要不要退回严格拒绝（默认关 = 允许降级）。
这与同行通行做法一致：Kimi Work 桌面端就是宿主直接执行 + 审批；Claude Code
在没有沙箱时默认"警告并降级"，并把提示标题写成 ``unsandboxed``。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.core.exceptions import InvalidRequestError, UnsupportedContentError

__all__ = [
    "BACKEND_BWRAP",
    "BACKEND_DIRECT",
    "BACKEND_DOCKER",
    "BACKEND_NONE",
    "BACKEND_SEATBELT",
    "DEFAULT_BIND_RO",
    "ExecutionResult",
    "Isolation",
    "IsolationPlan",
    "bind_paths_from",
    "build_plan",
    "detect",
    "direct_isolation",
    "run_isolated",
]

logger = logging.getLogger(__name__)

BACKEND_BWRAP = "bwrap"
BACKEND_SEATBELT = "sandbox-exec"
BACKEND_DOCKER = "docker"
BACKEND_NONE = "none"
#: **无隔离直接执行**（降级档，v0.55）。它不是"沙箱"，是"承认这台机器上现在没有沙箱"：
#: 命令原样在本机跑，安全由工作区根 + 命令策略 + 用户确认承担。见 `direct_isolation`。
BACKEND_DIRECT = "direct"

#: Linux 上**只读挂载**的目录：只挂"能把命令跑起来"的那些。
#:
#: 这是 QwenPaw 说的 *restricted filesystem view*：与其把整个 ``/`` 只读挂进来
#: （那样 ``/etc/passwd``、``~/.ssh`` 都还在视野里，只是不能写），
#: 不如**只挂运行时要用的**——没挂上的路径在沙箱里**根本不存在**。
#:
#: 抄这份清单时保留了"少而够用"的取舍：``/usr`` 与 ``/lib*`` 是解释器与依赖，
#: ``/bin`` ``/sbin`` 是系统命令，``/etc/ssl`` 给 TLS 证书（很多 CLI 起不来是因为它）。
#: **故意不含 ``/etc`` 整体**：口令文件、sudoers 都在那里，而它们与"跑一条命令"无关。
DEFAULT_BIND_RO: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/ssl",
    "/etc/alternatives",
    "/etc/ld.so.cache",
)

#: Docker 镜像。**刻意用一个最小且可预测的**：隔离层不该顺带引入一个我们
#: 没审过的用户态环境。需要别的工具链时由部署方改这里。
DOCKER_IMAGE = "python:3.12-slim"

#: 单次执行的默认超时与输出上限。**两个都必须有**：
#: 一个 `yes` 能把内存吃光，一个死循环能挂到天荒地老。
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class Isolation:
    """这台机器上现在能用的隔离后端。"""

    backend: str
    available: bool
    detail: str
    """人话说明：为什么可用/不可用。界面直接显示它。"""

    @property
    def key(self) -> str:
        return self.backend


@dataclass(frozen=True, slots=True)
class IsolationPlan:
    """一次隔离执行的**命令行构造结果**（纯数据，可断言）。

    把它与"真的去跑"分开：argv 的构造是纯函数，能在任何平台上测——
    而"这台机器上真的隔离住了吗"只能在那台机器上测。
    """

    backend: str
    argv: list[str]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    available: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    backend: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def detect(*, prefer: str | None = None) -> Isolation:
    """探测可用的隔离后端。**真去盘上找，不假设**。

    ``prefer`` 可以指定优先用哪个（部署方知道自己的机器）；
    指定的那个不可用时**回落到自动探测**，而不是直接报"没有隔离"——
    但回落这件事要出现在 ``detail`` 里，好让界面能显示出来。
    """
    candidates = [prefer] if prefer else []
    candidates += [BACKEND_BWRAP, BACKEND_SEATBELT, BACKEND_DOCKER]
    tried: list[str] = []
    for name in candidates:
        if not name or name == BACKEND_NONE:
            continue
        if name in tried:
            continue
        tried.append(name)
        found = _probe(name)
        if found.available:
            return found
        logger.debug("隔离后端 %s 不可用：%s", name, found.detail)

    return Isolation(
        backend=BACKEND_NONE,
        available=False,
        detail=(
            "这台机器上没有可用的内核级隔离（Linux 需要 bubblewrap、macOS 需要 "
            "sandbox-exec、任意平台都可以用 Docker）。执行会被拒绝——"
            "不会退化成不带隔离地裸跑，那样「我们以为它在沙箱里」就成了一个假象。"
        ),
    )


def direct_isolation() -> Isolation:
    """**无隔离直接执行**这一档（降级用，v0.55，见模块头末段）。

    什么时候用它由调用方决定（读 ``sandbox.require_isolation``，默认关 = 用它）：
    ``detect()`` 找不到任何真隔离时，调用方要么拒绝、要么换上这一档。

    **它不是沙箱**，所以 ``detail`` 里必须把这一点说白：命令原样在本机跑，
    工作区根与路径越界仍由 ``sandbox.resolve_in`` 挡着，但**进程跑起来之后能碰什么，
    这一档管不住**（网络也不受限）。把它包装成"沙箱"就是模块头警告的那个静默假象。
    """
    return Isolation(
        backend=BACKEND_DIRECT,
        available=True,
        detail=(
            "这台机器上没有可用的内核级隔离，当前**直接在本机执行（未隔离）**："
            "命令以**你的工作区目录**为 cwd（未隔离时就是那个项目文件夹）、工作区路径越界仍会被拒，"
            "但进程本身能碰到本机的东西，网络也不受限。"
            "要开启隔离：Linux 装 bubblewrap、macOS 用 sandbox-exec、任意平台装 Docker。"
            "要让它在无隔离时严格拒绝而不是降级，把设置里的「无隔离时拒绝执行」打开。"
        ),
    )


def _probe(name: str) -> Isolation:
    if name == BACKEND_BWRAP:
        path = shutil.which("bwrap")
        if not path:
            return Isolation(name, False, "没有找到 bwrap（Linux 上装 bubblewrap 即可）")
        return Isolation(name, True, f"bubblewrap 就位：{path}")

    if name == BACKEND_SEATBELT:
        path = shutil.which("sandbox-exec")
        if not path:
            return Isolation(name, False, "没有找到 sandbox-exec（仅 macOS 有）")
        return Isolation(name, True, f"Seatbelt 就位：{path}")

    if name == BACKEND_DOCKER:
        path = shutil.which("docker")
        if not path:
            return Isolation(name, False, "没有找到 docker")
        # **装了 CLI 但 daemon 没起是最常见的假阳性**，所以真的问一次它。
        try:
            # S603：这里的 argv 是**我们自己拼的常量**（docker + 固定子命令），
            # 不含任何用户输入，也没有 shell=True —— bandit 只是看到 subprocess 就报。
            #
            # **编码必须显式给**（`text=True` 不够，它用 locale）：这台机器（中文
            # Windows）的 locale 是 GBK，而 docker CLI 的输出里只要有一个非 GBK 的
            # 字节（本地化的报错、带中文的路径…），读线程就抛 `UnicodeDecodeError`，
            # 于是"探测一个隔离后端"这条本该无害的路把整个调用带崩。
            probe = subprocess.run(  # noqa: S603
                [path, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return Isolation(name, False, f"docker CLI 在，但连不上 daemon：{exc}")
        if probe.returncode != 0:
            reason = (probe.stderr or probe.stdout or "").strip().splitlines()
            return Isolation(
                name, False, f"docker daemon 没在跑：{reason[-1] if reason else '未知原因'}"
            )
        return Isolation(name, True, f"docker 就位（server {probe.stdout.strip()}）")

    return Isolation(name, False, f"未知的隔离后端：{name}")


def build_plan(
    argv: list[str],
    *,
    workspace_root: Path,
    sandbox_dir: Path,
    isolation: Isolation | None = None,
    allow_network: bool = False,
    bind_ro: tuple[str, ...] | None = None,
) -> IsolationPlan:
    """把一条命令包进隔离后端。

    **纯函数**（除了探测那一步），所以"argv 里到底有没有把工作区挂成读写、
    有没有断网"是能写成断言的——而这些恰恰是最容易写错、又最不容易发现的地方
    （少挂一个 ``--bind``，进程就只读；忘了 ``--unshare-net``，它就还能联网）。
    """
    chosen = isolation or detect()
    if not argv:
        raise InvalidRequestError("缺少要执行的命令")

    if chosen.backend == BACKEND_BWRAP:
        return _bwrap_plan(argv, workspace_root, sandbox_dir, allow_network, bind_ro)
    if chosen.backend == BACKEND_SEATBELT:
        return _seatbelt_plan(argv, workspace_root, sandbox_dir, allow_network)
    if chosen.backend == BACKEND_DOCKER:
        return _docker_plan(argv, workspace_root, sandbox_dir, allow_network)
    if chosen.backend == BACKEND_DIRECT:
        return _direct_plan(argv, sandbox_dir)
    return IsolationPlan(
        backend=BACKEND_NONE,
        argv=list(argv),
        workdir=str(sandbox_dir),
        available=False,
        detail=chosen.detail,
    )


def _bwrap_plan(
    argv: list[str],
    workspace_root: Path,
    sandbox_dir: Path,
    allow_network: bool,
    bind_ro: tuple[str, ...] | None = None,
) -> IsolationPlan:
    """bubblewrap：**限定文件系统视图** + 工作区与沙箱读写 + ``/tmp`` 是 tmpfs。

    挂载策略是这一层最要紧的决定，而它有两种写法，差别很大：

    - ``--ro-bind / /``（整个根只读）：命令一定跑得起来，但 ``/etc/passwd``、
      ``~/.ssh``、别人的项目都**还在视野里**——只是不能写。而"能读"本身就可能
      是事故（读走凭据不需要写权限）。
    - **只挂运行所需的目录**（采用）：没挂上的路径在沙箱里**根本不存在**。
      代价是可能缺某个工具要的文件——那时命令跑不起来，用户看到的是
      "缺文件"，而不是"文件被读了"。**两害相权，取"跑不起来"**。

    这正是 QwenPaw 说的 *restricted filesystem view*。清单在
    ``DEFAULT_BIND_RO``，可通过设置扩展（见 ``sandbox.bind_ro``）。
    """
    paths = bind_ro if bind_ro is not None else DEFAULT_BIND_RO
    parts = ["bwrap", "--dev", "/dev", "--proc", "/proc"]
    # 只挂**存在**的那些：清单是跨发行版通用的，某个目录在某台机器上不存在很正常，
    # 而 bwrap 遇到不存在的源会直接失败（那会让整个沙箱不可用）。
    for path in paths:
        if Path(path).exists():
            parts += ["--ro-bind", path, path]
    parts += [
        "--tmpfs",
        "/tmp",  # noqa: S108 - 这是沙箱**里面**的 tmpfs，不是宿主机的临时目录
        # 工作区读写：这是"在哪儿干活"
        "--bind",
        str(workspace_root),
        str(workspace_root),
        # 沙箱读写：这是"试错的地方"
        "--bind",
        str(sandbox_dir),
        str(sandbox_dir),
        "--die-with-parent",
        "--chdir",
        str(sandbox_dir),
    ]
    if not allow_network:
        # 断网：默认就断。Agent 跑的命令绝大多数不需要网络，
        # 而"能联网"是数据外泄那条路上最省事的一环。
        parts.append("--unshare-net")
    parts += ["--", *argv]
    return IsolationPlan(
        backend=BACKEND_BWRAP,
        argv=parts,
        workdir=str(sandbox_dir),
        available=True,
        detail="bubblewrap：只挂运行所需目录（限定视图），工作区与沙箱读写"
        + ("，已断网" if not allow_network else "，允许联网"),
    )


def _seatbelt_plan(
    argv: list[str], workspace_root: Path, sandbox_dir: Path, allow_network: bool
) -> IsolationPlan:
    """macOS Seatbelt：``deny default`` 之后按目录放行。

    规则是**白名单**：默认全拒，逐条放行。写成黑名单（默认放行、禁掉几个目录）
    等于没有隔离——总会有没想到的路径。
    """
    allow = [
        "(allow file-read*)",
        f'(allow file-write* (subpath "{workspace_root}") (subpath "{sandbox_dir}"))',
        "(allow process-exec)",
        "(allow sysctl-read)",
        # 只读的 /dev 与 /tmp 下的临时文件：没有它们连解释器都起不来
        "(allow file-read* file-write* (subpath \"/private/tmp\"))",
    ]
    if allow_network:
        allow.append("(allow network*)")
    profile = "(version 1)(deny default)" + "".join(allow)
    return IsolationPlan(
        backend=BACKEND_SEATBELT,
        argv=["sandbox-exec", "-p", profile, *argv],
        workdir=str(sandbox_dir),
        available=True,
        detail="Seatbelt：默认全拒，只放行工作区与沙箱的写"
        + ("，允许联网" if allow_network else "，已断网"),
    )


def _docker_plan(
    argv: list[str], workspace_root: Path, sandbox_dir: Path, allow_network: bool
) -> IsolationPlan:
    """Docker：挂载工作区与沙箱，``--network none`` 断网。

    两处刻意的参数：

    - ``--read-only`` + ``--tmpfs``：容器根文件系统只读，可写的只有明确挂进来的
      两个目录与一个内存 tmpfs。不给 ``--read-only`` 的话，进程能在容器里乱写，
      虽然出不去，但"哪里是它该写的地方"就模糊了；
    - ``--network none``：与 bwrap 的 ``--unshare-net`` 同理。
    """
    parts = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--tmpfs",
        "/tmp",  # noqa: S108 - 同上：容器内的 tmpfs
        # 工作区读写：与 bwrap 那份一一对应，三种后端的行为要一致
        "-v",
        f"{workspace_root}:{workspace_root}",
        "-v",
        f"{sandbox_dir}:{sandbox_dir}",
        "-w",
        str(sandbox_dir),
        "--network",
        "none" if not allow_network else "bridge",
        DOCKER_IMAGE,
        *argv,
    ]
    return IsolationPlan(
        backend=BACKEND_DOCKER,
        argv=parts,
        workdir=str(sandbox_dir),
        available=True,
        detail=f"docker（{DOCKER_IMAGE}）：根只读，工作区与沙箱读写"
        + ("，已断网" if not allow_network else "，允许联网"),
    )


#: 项目虚拟环境里放解释器的那个目录名与可执行文件名（Windows 与 POSIX 两套）。
_VENV_LAYOUTS = (("Scripts", "python.exe"), ("bin", "python"))

#: 裸名字的别名：Windows 上**没有** ``python3`` 这个可执行文件，而模型十有八九会写它
#: （2026-09-29 实测 Z-04 那两次 `[WinError 2] 系统找不到指定的文件` 就是这么来的）。
#: 只在"项目虚拟环境里真有那个东西"时才替，命中不到照旧原样返回。
_VENV_ALIASES: dict[str, tuple[str, ...]] = {"python3": ("python.exe", "python")}


def _project_scripts_dir() -> Path | None:
    """项目**自己**的虚拟环境里放解释器的那个目录；找不到就 ``None``。

    **按本文件的位置推导**（``<repo>/backend/app/services/isolation.py`` → 往上三层是
    ``<repo>/backend``），不写死机器上的绝对路径：这段代码要跟着克隆走，
    别人的机器、CI 容器里都得成立。

    找不到就返回 ``None``（装在 site-packages 里跑、或者这台机器没建 ``.venv``）——
    调用方据此**原样退回**，不许因为"推导不到 venv"把用户的命令弄成起不来。
    """
    backend_dir = Path(__file__).resolve().parents[2]
    for folder, binary in _VENV_LAYOUTS:
        candidate = backend_dir / ".venv" / folder
        if (candidate / binary).exists():
            return candidate
    return None


def _direct_env() -> dict[str, str]:
    """direct 档要用的环境：**继承当前进程环境**，再把项目解释器目录放进 ``PATH`` **最前面**。

    两件事各有理由，缺一件这条就是错的：

    - **继承**：Windows 上少了 ``PATH`` / ``PATHEXT`` / ``SystemRoot``，连 ``python`` 都
      找不到（v0.55 那条记录）。所以这里拷一份当前环境，**只动 ``PATH`` 这一项**。
    - **放最前面**（而不是追加在后面）：服务是 ``uv run`` 起的，``PATH`` 里排在前面的
      ``python`` 是 **uv 托管的裸解释器**（``…\\uv\\python\\cpython-3.12-…\\python.exe``），
      它的 ``site-packages`` 是空的——于是沙箱里任何一句 ``python -c "import numpy"``
      都是 ``ModuleNotFoundError``（2026-09-29 能力实测 K-03 的"画图 60 步交白卷"就是这么来的）。
      追加在后面赢不过它，必须放最前；**原有的 PATH 项一个都不动**（是追加，不是覆盖）。

    推导不到项目虚拟环境时返回 ``{}`` = 什么都不改（见 ``_project_scripts_dir``）。
    """
    scripts = _project_scripts_dir()
    if scripts is None:
        return {}
    env = dict(os.environ)
    existing = env.get("PATH", "")
    env["PATH"] = f"{scripts}{os.pathsep}{existing}" if existing else str(scripts)
    return env


def _direct_argv(argv: list[str]) -> list[str]:
    """direct 档的 argv：**裸名字先按项目虚拟环境解析一遍**。

    只动 ``argv[0]``，而且只有它**不带目录**（``python``、``pip`` 这种）时才试：
    带路径的（``E:\\…\\python.exe``、``./x``）照旧；项目虚拟环境里没有这个名字的
    （``ls``、``git``、``node``）**原样返回**——一个都不动。

    ``python3`` 另算（见 ``_VENV_ALIASES``）：Windows 上没有这个可执行文件，而模型
    经常写它，于是那条命令会以"起不了进程"收场（Z-04 实测两次）。这里把它接到项目
    解释器上——只在**接得到**的时候，接不到仍然原样抛出，不假装成功。

    **为什么光给子进程 env 里的 PATH 不够**（2026-09-29 能力实测 K-03 的根因）：
    Windows 的 ``CreateProcess`` 找可执行文件**不看**我们传下去的那份 ``env``
    （实测：``subprocess.run(["python", …], env={"PATH": "<venv>\\Scripts;…"})`` 仍旧落到
    uv 那只裸解释器上——``sys.prefix == sys.base_prefix`` 为真、``import numpy`` 失败）。
    把解析结果**写成绝对路径**才是两边都成立的做法：POSIX 那边本来就会按 ``env`` 的
    PATH 解析（见 ``_direct_env``），Windows 这边靠这一手把行为对齐。
    """
    if not argv:
        return list(argv)
    head = argv[0]
    if os.path.dirname(head):
        return list(argv)
    scripts = _project_scripts_dir()
    if scripts is None:
        return list(argv)
    for name in (head, *_VENV_ALIASES.get(head, ())):
        for candidate in (scripts / name, scripts / f"{name}.exe"):
            if candidate.exists():
                return [str(candidate), *argv[1:]]
    return list(argv)


def _direct_plan(argv: list[str], sandbox_dir: Path) -> IsolationPlan:
    """无隔离：**命令原样跑**，只是 cwd 落在这次会话的沙箱目录。

    与另外三个后端的差别必须写在明处：它们各自限定了"进程能碰什么"（bind mount /
    Seatbelt profile / 容器 namespace），这一个**什么都不限定**——它是降级档
    （见 `direct_isolation`）。所以 ``detail`` 里如实写"未隔离"，界面与给模型的话照它说。

    "原样跑"里有**两处只属于这一档的加工**，都是为了同一件事——沙箱里那个裸 ``python``
    必须是**项目自己的解释器**（带依赖），而不是 uv 托管的那只裸的：

    - ``argv``：裸名字先按项目虚拟环境解析（见 ``_direct_argv``）；
    - ``env``：继承当前进程环境，并把项目解释器目录放进 ``PATH`` 最前面（见 ``_direct_env``）。
    """
    return IsolationPlan(
        backend=BACKEND_DIRECT,
        argv=_direct_argv(argv),
        workdir=str(sandbox_dir),
        available=True,
        detail="未隔离：直接在本机执行（cwd 是这次会话的沙箱目录）",
        env=_direct_env(),
    )


def run_isolated(
    argv: list[str],
    *,
    workspace_root: Path,
    sandbox_dir: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    allow_network: bool = False,
    isolation: Isolation | None = None,
    bind_ro: tuple[str, ...] | None = None,
) -> ExecutionResult:
    """在隔离里跑一条命令。

    **没有可用隔离时拒绝执行**（抛 ``UnsupportedContentError``），不回退成裸跑：
    回退会把"我们以为它在沙箱里"变成一个静默的假象，而那个假象比拒绝危险得多
    ——用户会放心地让 Agent 跑东西，而它其实能碰整台机器。
    """
    plan = build_plan(
        argv,
        workspace_root=workspace_root,
        sandbox_dir=sandbox_dir,
        isolation=isolation,
        allow_network=allow_network,
        bind_ro=bind_ro,
    )
    if not plan.available:
        raise UnsupportedContentError(
            f"这台机器上没有可用的内核级隔离，拒绝执行：{plan.detail}"
        )

    sandbox_dir.mkdir(parents=True, exist_ok=True)
    # 环境变量分两档：
    #
    # - **直接执行那一档**由 `_direct_plan` 组装（继承当前进程环境 + 把项目解释器放进
    #   PATH 最前面，见 `_direct_env` 里那段"为什么"）；推导不到项目虚拟环境时它是空的
    #   ——那就照旧 `None` = **继承**，因为 Windows 上少了 PATH / PATHEXT / SystemRoot，
    #   连 `python` 都找不到（v0.55）；
    # - 前三个后端是"限定视图"，给一份固定的 POSIX env 就够（它们自己把需要的目录挂进去）。
    env = plan.env or (
        None
        if plan.backend == BACKEND_DIRECT
        else {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(sandbox_dir)}
    )
    try:
        # S603：整条链路就是为了"在隔离里执行用户要跑的命令"而存在的，
        # 而且传的是**数组**（没有 shell 解析），并被隔离后端包着——
        # 这正是本模块的职责，不是注入面。见模块头的三层说明。
        #
        # **编码必须显式给**（`text=True` 只说明"要文本"，它挑的是 locale）：
        # 这台机器（中文 Windows）的 locale 是 GBK，而命令吐出 UTF-8 是常态
        # （`git log`、`pytest`、带中文的文件名…）。不带 `encoding` 时读线程会抛
        # `UnicodeDecodeError`，`Popen` 那一侧的 `stdout/stderr` 变成 `None`，
        # 下面 `_clip(None)` 再抛 `TypeError` —— 用户看到的是"这个工具这次没能跑起来
        # （内部错误）"，而真正的原因只是"输出不是 GBK"。
        # `errors="replace"`：真遇到非 UTF-8 的（例如 cmd 自己在 GBK 下吐的中文）
        # 也要把结果交出去（乱码可辨认），而不是整条命令失败。
        finished = subprocess.run(  # noqa: S603
            plan.argv,
            # cwd **按档分**（2026-09-30 用户裁定，对齐 Kimi Work 的口径：
            # "所有命令的工作目录默认就是该项目文件夹" ✓）：
            # - 直接执行（无内核隔离 ✓ 本机直接跑）→ cwd = **工作区根目录** ✓，
            #   于是命令就在用户指定的文件夹里跑、产物也直接写回那里 ✓；
            # - 隔离档（bwrap/seatbelt/docker）→ 仍落在这次会话的沙箱目录 ✓
            #   （那是隔离的容器；它与工作区根之间的可见性由各后端自己的挂载规则决定 ✓）。
            cwd=str(workspace_root if plan.backend == BACKEND_DIRECT else sandbox_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            exit_code=-1,
            stdout=_cap(exc.stdout),
            stderr=f"执行超过 {timeout:g} 秒，已终止" + _tail(exc.stderr),
            truncated=False,
            backend=plan.backend,
            timed_out=True,
        )
    except OSError as exc:
        # **两类失败必须分开报**（D16，2026-09-29 用户点名会话 `conv_a5f4628f405f`）：
        # 现场那条 `ls -a /` 在 Windows 上的报错是「起不了隔离进程（direct）：
        # [WinError 2] 系统找不到指定的文件」✗ —— 读的人会以为"隔离坏了"，
        # 而真正的原因是**这台机器上没有 `ls`**（同一个会话里后面 `cmd /c dir` 全都跑得动 ✓）。
        # 一句错话让模型与用户都走错方向，所以这里按 errno/winerror 判：
        # 找不到可执行文件 → 说是"命令不存在"（并给这台机器上能用的替代）；
        # 其余 OSError → 才是真的"隔离/进程起不来"。
        first = plan.argv[0] if plan.argv else ""
        missing_file = isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) == 2
        if missing_file:
            detail = (
                f"命令没找到：`{first}` 在这台机器上不存在（这不是隔离的问题）。"
                "换一个本机有的命令：Windows 上列目录用 `dir`、找文件用 `dir /s /b` 或 "
                "`where`，Linux/macOS 上用 `ls` / `find`。"
            )
        else:
            detail = f"隔离后端不可用（{plan.backend}）：{exc}"
        raise UnsupportedContentError(detail) from exc

    stdout, cut_out = _clip(finished.stdout)
    stderr, cut_err = _clip(finished.stderr)
    return ExecutionResult(
        exit_code=finished.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=cut_out or cut_err,
        backend=plan.backend,
    )


def _cap(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def _tail(value: object) -> str:
    text = _cap(value)
    return f"\n{text[-2000:]}" if text else ""


def _clip(text: str) -> tuple[str, bool]:
    """输出截断。**必须截**：一条 `ls -R /` 能产出几百 MB，
    而它们会原样进上下文（那是要按 token 付费的）。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS] + f"\n…（已截断，共 {len(text)} 字）", True


def bind_paths_from(raw: str) -> tuple[str, ...] | None:
    """把设置里那串逗号/换行分隔的路径解析成只读挂载清单。

    空字符串 → ``None``（用 :data:`DEFAULT_BIND_RO` 的默认清单）。
    **不给"挂整个 /"这个选项**：那正是这一层想避免的形态；
    真需要某个目录时把它加进来（如 ``/opt/toolchain``），
    那是一次明确的、看得见的放权。
    """
    if not raw or not raw.strip():
        return None
    parts = [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]
    return tuple(parts) or None
