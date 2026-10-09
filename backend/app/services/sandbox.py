"""沙箱与工作区的**执行边界**（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §4）。

这是本轮里最容易被做错的一层——因为"给 Agent 一个目录"听起来是一件事，
其实是两件，它们回答的问题不同：

| | 工作区（Workspace） | 会话沙箱（Sandbox） |
| --- | --- | --- |
| 回答 | **在哪干活** | **试错的地方在哪** |
| 生命周期 | 长期，用户拥有，路径由他指定 | 一次会话，可随时丢弃 |
| 位置 | 用户的目录（如 ``E:/code/proj``） | ``data/sandbox/<conversation_id>/`` |
| 内容 | 真实的代码 / 文档 / 项目文件 | 临时脚本、下载、中间产物 |
| 清理 | 用户自己 | 超期后自动回收 |
| 可否被引用 | 是（产物就落在这里） | 否（只许诺"会话期间有效"） |

**为什么必须有沙箱**：Agent 要跑命令、试脚本、下东西。这些若直接落在用户的工作区里，
一次失败的尝试就会在他的真实项目里留下垃圾——而"我什么都没做，它自己多了一堆文件"
是最让人不敢再用一个 Agent 的原因。沙箱把**探索的痕迹**与**真实的产物**分开：
想留下的，由用户（或 Agent 明确地）从沙箱挪进工作区。

本模块提供三件事，都是**策略与路径**层面的（不做内核级隔离，见设计文档 §7 的"明确不做"）：

1. ``Sandbox``：一个会话的沙箱目录，随取随建、可整份清掉；
2. ``resolve_in(scope, path)``：**把请求的路径约束在一个根之内**，越界一律拒绝；
3. ``ExecutionPolicy``：四档准入（``allow`` / ``ask`` / ``deny`` / ``sandbox``），
   照 QwenPaw 的 Governance 那层做。

**为什么第 2 条是本模块的核心**：Agent 的文件工具会拿到"模型生成的路径"。
模型会写出 ``../../.env``、``C:/Windows/...``、``~/secrets``——**不是恶意，
而是它在推测这个项目的结构**。这一条不严，前面所有账号隔离都白做：
一次 ``cat ../../backend/.env`` 就把数据库口令读出来了。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import ForbiddenError, InvalidRequestError

__all__ = [
    "POLICY_ALLOW",
    "POLICY_ASK",
    "POLICY_DENY",
    "POLICY_SANDBOX",
    "REQUIRE_ISOLATION_KEY",
    "ExecutionPolicy",
    "Sandbox",
    "require_isolation",
    "resolve_in",
    "sandbox_root",
]

logger = logging.getLogger(__name__)

POLICY_ALLOW = "allow"
POLICY_ASK = "ask"
POLICY_DENY = "deny"
POLICY_SANDBOX = "sandbox"

#: 设置键：**没有内核级隔离时是否拒绝执行**（v0.55，默认关）。
#:
#: **默认开** = 没有隔离就**拒绝执行**（D16，2026-09-29 用户点名会话 `conv_a5f4628f405f` 后翻转）。
#:
#: 为什么翻转：降级档（`isolation.direct_isolation`）在真实会话里的表现是
#: "上一步 `ls` 被拦 → 下一步 `find / -maxdepth 7` **整机跑起来**、网络不受限" ✗ ——
#: 那正是我们一直写的纪律"没有隔离就不执行"被静默绕过的样子。
#: 现在裸跑是**显式开关**：打开它（= 关掉本项）才会降级直执，
#: 且设置页要写清"命令会在你本机直接执行、可访问网络与全部文件"。
REQUIRE_ISOLATION_KEY = "sandbox.require_isolation"


def require_isolation(runtime: Any) -> bool:
    """读上面那一项设置（**默认 ``True``** = 没有隔离就拒绝）。

    用鸭子类型接 ``runtime``（只要求它有 ``get_bool``）：策略层不依赖
    ``services/runtime_config`` 的具体实现，免得这一层多背一个 import。
    """
    return bool(runtime.get_bool(REQUIRE_ISOLATION_KEY, default=True))


#: 沙箱目录在数据目录下的位置。
SANDBOX_DIRNAME = "sandbox"

#: 明显指向系统或凭据的路径片段。命中就拒，**不看它是否在根之内**——
#: 因为这些名字出现在"被约束的根之内"时，通常说明根设错了（例如根设成了家目录）。
_SENSITIVE = (
    re.compile(r"(^|/)\.ssh(/|$)", re.I),
    re.compile(r"(^|/)\.aws(/|$)", re.I),
    re.compile(r"(^|/)\.env$", re.I),
    re.compile(r"(^|/)\.env\.", re.I),
    re.compile(r"(^|/)id_(rsa|ed25519|ecdsa)", re.I),
    re.compile(r"(^|/)\.git-credentials$", re.I),
    re.compile(r"(^|/)credentials(\.json)?$", re.I),
)


def sandbox_root(data_dir: Path) -> Path:
    return data_dir / SANDBOX_DIRNAME


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """一次执行的准入判定（照 QwenPaw 的 allow / deny / ask / sandbox 四档）。

    ``mode`` 决定"要不要先问用户"，``workspace_root`` 决定"能碰哪儿"。
    **两者都要**：只问不约束（用户点了同意，然后它去读了别处）与只约束不问
    （它默默改了用户的真实项目）都是不完整的设计。
    """

    mode: str = POLICY_ASK
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        if self.mode not in (POLICY_ALLOW, POLICY_ASK, POLICY_DENY, POLICY_SANDBOX):
            raise InvalidRequestError(f"未知的执行策略：{self.mode}")

    @property
    def needs_approval(self) -> bool:
        """``ask`` 要问；其余三档都不问（各自已由别的机制兜住）。"""
        return self.mode == POLICY_ASK

    def require_allowed(self, *, approved: bool = False, what: str = "这个操作") -> None:
        """不通过就抛。**消息要能指导下一步**：只说"不允许"的报错等于没说。"""
        if self.mode == POLICY_DENY:
            raise ForbiddenError(
                f"{what}被策略拒绝（当前为「拒绝执行」）。"
                "要放行请在能力设置里把执行策略改成「需要确认」或「允许」"
            )
        if self.needs_approval and not approved:
            from app.core.exceptions import ConflictError

            # 409 而不是 403：不是"你不能做"，是"要先确认一下"（与 MCP 那层同一口径）
            raise ConflictError(
                f"{what}需要你确认后才会执行。"
                "Agent 会在你的工作区里改动文件、或起子进程，确认前不会真的动手"
            )


def resolve_in(root: Path, candidate: str) -> Path:
    """把 ``candidate`` 解析成 **``root`` 之内**的绝对路径；越界一律拒绝。

    这是整套账号/数据隔离里最要紧的一道判定：文件工具的路径**由模型生成**
    （严格说是"模型推测这个项目的结构"），它会写出 ``../../backend/.env``
    这类路径——**不是恶意，是它在猜**。这一条不严，一次读口令就把前面所有隔离绕过去了。

    四道检查，顺序不能换：

    1. **绝对路径直接拒**（而不是"把它当相对路径"）：模型给绝对路径时，
       它想指的地方和我们理解的通常不是一个地方，猜错比拒掉危险。
    2. ``..`` 段直接拒：`resolve()` 之后再看当然也能发现，但那时已经无法区分
       "用户真的想指那里"与"它绕出去了"——早早拒掉，报错也更清楚。
    3. **解析后必须在根之内**（``is_relative_to``）。这一道是兜底：
       Windows 的 ``C:foo``、符号链接、以及各种编码花样只有真解析一遍才确认得了。
    4. **敏感文件一律拒**（``.env`` / ``.ssh`` / 私钥 / 凭据文件），
       即使它确实在根之内。根设错的时候（比如设成了家目录）只有这一道能拦住。
    """
    if not candidate or not candidate.strip():
        raise InvalidRequestError("缺少路径")
    text = candidate.strip()

    # 1. 绝对路径。**三种写法都要拦**，而且不能只靠 `is_absolute()`：
    #    Windows 上 `Path("/etc/passwd").is_absolute()` 是 **False**（没有盘符的
    #    路径是"驱动器相对"的），于是它会被当成相对路径拼到根下面——
    #    结果不越界、但也不是用户/模型想指的地方，属于"猜错"。
    #    所以额外拦前导分隔符与盘符。
    if text[0] in "/\\" or Path(text).is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise InvalidRequestError(
            f"只接受相对工作区的路径（收到绝对路径：{text}）。"
            "绝对路径会让 Agent 能碰到工作区之外的地方"
        )
    # 2. ``..`` 段
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise InvalidRequestError("路径不能包含 ..（不允许跳出工作区）")

    # 4. 敏感文件（在解析之前先按字面看一眼，报错更直接）
    normalized = "/".join(parts)
    for pattern in _SENSITIVE:
        if pattern.search("/" + normalized):
            raise InvalidRequestError(
                f"不允许访问这个文件（{normalized}）：它通常存放凭据。"
                "需要模型用某个凭据时，请通过设置页配置，而不是把文件读进上下文"
            )

    # 3. 解析后复查（兜底）
    base = root.resolve()
    target = (base / Path(*parts)).resolve()
    if target != base and not target.is_relative_to(base):
        raise InvalidRequestError("路径超出工作区")
    return target


@dataclass(slots=True)
class Sandbox:
    """一个会话的沙箱目录。

    ``root`` 是**会话级**的（``data/sandbox/<conversation_id>/``）：
    同一个会话里的多次探索共享它（这有用：第一步下的脚本第二步要用），
    换一个会话就是全新的一份（上一个会话的临时产物不该突然出现在新的里）。
    """

    root: Path

    def path(self, candidate: str = "") -> Path:
        """沙箱内的路径（同样受 ``resolve_in`` 约束）。"""
        if not candidate:
            return self.root
        return resolve_in(self.root, candidate)

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def clear(self) -> None:
        """整份清掉。**沙箱里的东西不承诺持久**——这正是它与工作区的区别。"""
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
            logger.info("会话沙箱已清理：%s", self.root)

    def usage(self) -> tuple[int, int]:
        """``(文件数, 字节数)``。界面上要能看出"这个会话试了多少东西"。"""
        if not self.root.exists():
            return 0, 0
        count = 0
        total = 0
        for item in self.root.rglob("*"):
            if item.is_file():
                count += 1
                try:
                    total += item.stat().st_size
                except OSError:  # pragma: no cover - 并发删除时的竞态
                    continue
        return count, total


def sandbox_for(data_dir: Path, conversation_id: str) -> Sandbox:
    """取某个会话的沙箱。

    **id 会变成目录名，所以必须清洗**：带 ``../`` 的 id 会把沙箱指到别处去
    （"目录名注入"）。两件事都要做，缺一不可：

    1. 非法字符折成 ``_``；
    2. **掐掉首尾的点**——只做第 1 步的话，id ``".."`` 会原样留下，
       而 ``sandbox_root/".."`` 正好是数据目录本身（实测踩到）。
       ``"."`` / ``".."`` 这类名字在路径里不是名字、是导航。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conversation_id or "default")[:120].strip("._")
    if not safe:
        safe = "default"
    return Sandbox(root=sandbox_root(data_dir) / safe)
