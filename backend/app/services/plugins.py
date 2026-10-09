"""插件包与本地市场（v0.43，设计见 ``docs/设计/插件与技能-v0.1.md``）。

技能（``services/skills.py``）是能力层的一档：磁盘上一个 ``SKILL.md``。
插件是它**外面那一档**：一个目录 + 一份 ``plugin.json``，里面可以装技能、命令、
钩子、工具四类东西。格式照 ZCode 的 manifest 与 QwenPaw 的 ``plugins/``，
能力面收窄到四类——每一条都写在调研报告《Agent-与对话架构对标调研 v0.1》§2.4。

**抄来的三条硬约定**（改之前先读这三条的理由）：

1. **manifest 只强制 ``name``**（``^[a-z0-9][a-z0-9._-]{0,127}$``，其余全可选），
   组件**不在 manifest 里写路径**，靠目录约定发现（``skills/``、``commands/``、
   ``hooks/hooks.json``）。ZCode 就是这么定的：写路径的 manifest 一改目录结构就烂，
   而"按约定找"让一个插件的最小形态只需要一个 ``plugin.json``。
2. **状态与内容分离**：插件目录**只读**（这个类一个字节都不往里写），
   启用/停用与屏蔽写在 ``app_settings``（``plugins.enabled.<name>`` /
   ``plugins.blocked.<name>``）。于是"重新扫描"永远安全，插件目录也可以被别的工具
   （git / 同步盘 / 手工）管理而不打架。
3. **校验失败的要列出来**（照 DSH 的"失败的 preset 也列出"）：缺 ``name``、
   名字不合法、组件路径逃逸插件根 —— 这些插件**不加载**，但要带着原因出现在列表里。
   静默藏掉会让用户以为插件没装上，而"为什么它不生效"就成了查不出的问题。

**本地市场 = 目录本身**：发现源固定两条、first match wins ——
``<data_dir>/plugins/``（用户装的）在前，仓库自带 ``plugins/``（内置）在后；
内置那个目录**不存在就跳过**（不是错误）。这一轮不做在线市场（下载、来源、
版本锁那一套是 ``services/skill_market.py`` 在技能侧已经做过的事，
插件的在线市场要等"插件里的代码怎么过闸"这个问题答完）。

**内置插件的"物化"我们没做拷贝**：ZCode 的做法是把内置插件拷进用户目录、
版本变了才刷新。KYLAB 的内置插件与技能一样是**仓库里那一份**（部署时是只读的），
发现顺序（用户 → 内置）已经覆盖了它；再拷一份进数据目录只会产生两个真相。
所以这里照抄的是它**能观察到的两条语义**：① 扫描是幂等的，内置插件每次都以
同样的一条记录出现，不因为它来自仓库就"重新安装"一遍；② 被用户停用的内置插件
记一条**屏蔽**标记（``plugins.blocked.<name>``）而不是从磁盘上抹掉——
"伪装卸载"，但它仍然在列表里看得见。

**能力面四类都还没接执行**（这是有意写在最显眼处的）：
skill 只在插件里被**发现**（技能的装载仍走 ``skills/`` 那两条来源）、
command / hook / tool 只列出。界面上每一类都带着"未实现"的说明，
不假装支持——做不到的要说出来（用户对这三家的共同要求）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.memory_files import parse_frontmatter
from app.services.skills import MAX_DESCRIPTION_CHARS, SKILL_FILE
from app.storage.base import StoreBundle

__all__ = [
    "BLOCKED_PREFIX",
    "BUILTIN_DIR_ENV",
    "COMPONENT_KINDS",
    "COMPONENT_STATUS",
    "DEFAULT_VERSION",
    "ENABLED_PREFIX",
    "NAME_PATTERN",
    "PLUGIN_FILE",
    "PluginComponent",
    "PluginManifest",
    "PluginRecord",
    "PluginService",
]

logger = logging.getLogger(__name__)

#: 插件清单的文件名（照 ZCode / QwenPaw：都在插件根下）。
PLUGIN_FILE = "plugin.json"

#: 用户装的插件目录（数据目录下）。
PLUGINS_DIR = "plugins"

#: ``<data_dir>/plugins/`` 之外的**内置**插件目录从哪来。与 ``KYLAB_SKILLS_DIR``
#: 同一套三种来源（见 ``services/skills.py`` 的模块头）：显式注入 > 本环境变量 >
#: 按代码位置推仓库根。加这一条是因为"数层数"在容器里不成立
#: （§12.224 第 9 条：代码在 ``/app/app/services``，往上四层是 ``/``）。
BUILTIN_DIR_ENV = "KYLAB_PLUGINS_DIR"

#: 插件名的形状。**与 ZCode 逐字一致**（调研 §2.4）：它同时是标识符与设置键的一部分
#: （``plugins.enabled.<name>``），所以不允许空格、大写与斜杠。
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: 没写版本时的默认值（照 ZCode 的 ``0.0.0``）。
DEFAULT_VERSION = "0.0.0"

#: 状态键前缀。**状态与内容分离的落点**：插件目录只读，这两样写在 app_settings。
ENABLED_PREFIX = "plugins.enabled."
BLOCKED_PREFIX = "plugins.blocked."

#: 目录约定（照 ZCode：组件靠约定发现，manifest 里不写路径）。
SKILL_DIR = "skills"
COMMAND_DIR = "commands"
HOOKS_DIR = "hooks"

#: 能力面四类（照 QwenPaw 的 ``register(api)`` 四类：provider / 生命周期 hook /
#: 控制命令 / 工具配置，收窄成 KYLAB 这一轮能讲清楚的四种）。
COMPONENT_KINDS = ("skill", "command", "hook", "tool")

#: 每一类**现在到底能不能用**。这是本模块唯一允许说"未实现"的地方，
#: 也是界面上那一行说明的来源：宁可有这一行，也不要让用户以为命令能被执行。
COMPONENT_STATUS: dict[str, str] = {
    "skill": "已发现（技能的装载仍走 skills/ 那两条来源，插件内的技能还没并进去）",
    "command": "未实现：本轮只列出，不执行",
    "hook": "未实现：本轮只列出，不触发",
    "tool": "未实现：本轮只列出，不注册成工具",
}


@dataclass(frozen=True, slots=True)
class PluginComponent:
    """插件提供的一样东西。``path`` 相对插件根（``tools`` 那一类是 manifest 里的 entry）。"""

    kind: str
    name: str
    description: str = ""
    path: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """``plugin.json`` 的解析结果。**只有 ``name`` 是必填的**（照 ZCode）。

    解析失败抛 ``InvalidRequestError``（人话原因），调用方把它变成
    "加载失败 + 原因"的一条记录——校验失败的插件也要在列表里出现。
    """

    name: str
    version: str = DEFAULT_VERSION
    description: str = ""
    author: str = ""
    homepage: str = ""
    user_config: tuple[str, ...] = ()
    """``userConfig`` 里声明的字段名。**本轮只登记名字**：值要落在 app_settings
    的 ``plugins.config.<name>.<键>``，而录入的端点与界面还没做，所以界面上会明说
    "声明了但还没有录入入口"，不假装能配（调研 §2.4 的反面教材：ZCode 的
    ``userConfig.sensitive`` 就是这个下场）。"""
    tools: tuple[PluginComponent, ...] = ()
    """manifest 里的 ``tools``（QwenPaw 四类能力面之一）。"""

    @classmethod
    def parse(cls, raw: Any, *, where: str = PLUGIN_FILE) -> PluginManifest:
        if not isinstance(raw, dict):
            raise InvalidRequestError(f"{where} 不是一个 JSON 对象（需要 {{'name': …}}）")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise InvalidRequestError(f"{where} 缺 name（这是唯一必填的字段）")
        if not NAME_PATTERN.match(name):
            raise InvalidRequestError(
                f"name 不合法：{name}"
                "（只允许小写字母、数字与 . _ -，要以字母或数字开头，最多 128 个字符）"
            )
        return cls(
            name=name,
            version=str(raw.get("version") or "").strip() or DEFAULT_VERSION,
            description=_text(raw.get("description")),
            author=_text(raw.get("author")),
            homepage=_text(raw.get("homepage")),
            user_config=_user_config(raw.get("userConfig")),
            tools=_tools(raw.get("tools")),
        )


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """一个插件。**加载失败的也在里面**（``loaded=False`` 且 ``error`` 非空）。"""

    name: str
    version: str = DEFAULT_VERSION
    description: str = ""
    author: str = ""
    homepage: str = ""
    source: str = "user"
    """``user`` = 数据目录里用户放的（本地市场装的）；``builtin`` = 随代码发布。"""
    path: str = ""
    """插件目录的绝对路径（"这个插件其实在哪"是排错第一问）。"""
    manifest_path: str = ""
    enabled: bool = True
    blocked: bool = False
    """内置且被用户屏蔽（照 ZCode 的"屏蔽标记"）：文件还在，只是不要再启用它。"""
    loaded: bool = True
    """校验过了没有。``False`` 时 ``error`` 一定非空。"""
    error: str = ""
    components: tuple[PluginComponent, ...] = ()
    user_config: tuple[str, ...] = ()

    @property
    def kinds(self) -> tuple[str, ...]:
        """提供了哪几类能力（去重，顺序按 ``COMPONENT_KINDS``）。"""
        present = {item.kind for item in self.components}
        return tuple(kind for kind in COMPONENT_KINDS if kind in present)


class PluginService:
    """发现并登记插件。**插件目录只读**：这个类不往里写任何东西。

    **无状态**（与技能同一条理由）：插件是目录，用户可能刚往里丢了一个，
    所以每次调用重新扫磁盘；缓存要处理失效，为省这点开销不划算。
    """

    def __init__(
        self,
        data_dir: Path,
        stores: StoreBundle,
        *,
        builtin_dir: Path | None = None,
    ) -> None:
        #: 状态（启停 / 屏蔽）的落点。**只写 app_settings，不写插件目录**（见模块头）。
        self._stores = stores
        self._data_dir = data_dir
        # 仓库自带的那批：显式注入 > KYLAB_PLUGINS_DIR > 按代码位置推。
        # 显式注入排最前是为了测试能指到临时目录（断言的对象不该被"机器上恰好设了
        # 环境变量"改掉），这条与常驻技能那一档逐字一致。
        self._builtin_dir = builtin_dir if builtin_dir is not None else _default_builtin_dir()

    # ------------------------------------------------------------------ 发现源

    @property
    def user_dir(self) -> Path:
        """用户装的插件目录——**本地市场就是它**：放进来的目录就是"上架"。"""
        return self._data_dir / PLUGINS_DIR

    @property
    def builtin_dir(self) -> Path:
        """随代码发布的插件目录。**不存在就跳过**（空目录也是跳过，不是错误）。"""
        return self._builtin_dir

    # ------------------------------------------------------------------ 读

    def list(self) -> list[PluginRecord]:
        """全部插件（含加载失败的）。顺序：用户装的在前，其余按名字。

        **first match wins**：同名时以**先出现的那条**为准（用户目录在前）——
        用户放进数据目录的那份是"他现在要的这个版本"，内置那份是随代码发布的兜底。
        这条与技能那一档（内置优先）**正好相反**，是用户指定要照 ZCode 的顺序。
        """
        found: dict[str, PluginRecord] = {}
        for source, root in (("user", self.user_dir), ("builtin", self.builtin_dir)):
            for directory in _plugin_dirs(root):
                record = self._load(directory, source=source)
                found.setdefault(record.name, record)
        return sorted(found.values(), key=lambda item: (item.source != "user", item.name))

    def get(self, name: str) -> PluginRecord:
        wanted = (name or "").strip()
        for record in self.list():
            if record.name == wanted:
                return record
        raise NotFoundError(f"没有这个插件：{name}")

    # ------------------------------------------------------------------ 写状态
    #
    # 只写两件事：启用/停用与屏蔽。**都不碰插件目录**——用户把插件目录删了、
    # 换成 git 管理、或者重新扫一遍，状态都还在。

    def enable(self, name: str) -> PluginRecord:
        """启用。内置的顺带**解除屏蔽**：启用的意思就是"随代码发布的那份我又要了"。"""
        record = self.get(name)
        settings = self._stores.app_settings
        settings.set_setting(f"{ENABLED_PREFIX}{record.name}", "true")
        if record.source == "builtin":
            settings.delete_setting(f"{BLOCKED_PREFIX}{record.name}")
        return self.get(record.name)

    def disable(self, name: str) -> PluginRecord:
        """停用。内置的**记一条屏蔽**而不是删（见模块头对"物化"的说明）。"""
        record = self.get(name)
        settings = self._stores.app_settings
        settings.set_setting(f"{ENABLED_PREFIX}{record.name}", "false")
        if record.source == "builtin":
            # 屏蔽标记的值写版本号：将来"内置的那份升级了、要不要再问一次用户"
            # 需要它（ZCode 的物化也是按版本决定刷不刷新）
            version = record.version or DEFAULT_VERSION
            settings.set_setting(f"{BLOCKED_PREFIX}{record.name}", version)
        return self.get(record.name)

    # ------------------------------------------------------------------ 内部

    def _load(self, directory: Path, *, source: str) -> PluginRecord:
        """读一个插件目录。**坏文件不抛错**：一个插件写坏了不该让整个列表 500。

        这与技能那一档同一条纪律，处置也一样：失败的那条**留在列表里**，
        带着原因（静默跳过等于把"为什么不生效"变成一个查不出的问题）。
        """
        base = {
            "name": directory.name,
            "path": str(directory),
            "manifest_path": str(directory / PLUGIN_FILE),
            "source": source,
        }
        manifest_path = directory / PLUGIN_FILE
        if not manifest_path.is_file():
            return self._failed(base, f"没有 {PLUGIN_FILE}（插件 = 一个目录 + 一份 {PLUGIN_FILE}）")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            return self._failed(base, f"读不到 {PLUGIN_FILE}：{_read_reason(error)}")
        except ValueError as error:
            return self._failed(base, f"{PLUGIN_FILE} 不是合法 JSON：{error}")
        try:
            manifest = PluginManifest.parse(raw)
        except InvalidRequestError as error:
            return self._failed(base, str(error))
        components, escape = self._components(directory, manifest)
        if escape:
            return self._failed(base, escape)
        return self._record(manifest, base, components)

    def _failed(self, base: dict[str, str], reason: str) -> PluginRecord:
        """加载失败的一条。**状态照读**：加载失败与启停是两件事，别把它们搅在一起
        （用户改好 plugin.json 之后点一下"重新扫描"，状态还在原处）。"""
        return PluginRecord(
            **base,
            loaded=False,
            error=reason,
            enabled=self._is_enabled(base["name"]),
            blocked=self._is_blocked(base["name"]),
        )

    def _record(
        self,
        manifest: PluginManifest,
        base: dict[str, str],
        components: tuple[PluginComponent, ...],
    ) -> PluginRecord:
        # 名字以 **manifest 里的** 为准（目录名只是它在磁盘上的位置）：
        # 于是"把 pack 改名成 pack-2 目录"不会变成另一个插件
        payload = {**base, "name": manifest.name}
        return PluginRecord(
            **payload,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            homepage=manifest.homepage,
            loaded=True,
            enabled=self._is_enabled(manifest.name),
            blocked=self._is_blocked(manifest.name),
            components=components,
            user_config=manifest.user_config,
        )

    def _is_enabled(self, name: str) -> bool:
        """没写过这个键 = 启用：把它放进插件目录这件事本身就是"我要用它"。"""
        return self._setting(f"{ENABLED_PREFIX}{name}").lower() not in ("0", "false", "no")

    def _is_blocked(self, name: str) -> bool:
        return bool(self._setting(f"{BLOCKED_PREFIX}{name}"))

    def _setting(self, key: str) -> str:
        return (self._stores.app_settings.get_setting(key) or "").strip()

    # ------------------------------------------------------------------ 组件

    def _components(
        self, root: Path, manifest: PluginManifest
    ) -> tuple[tuple[PluginComponent, ...], str]:
        """按目录约定发现组件。返回 ``(组件, 逃逸原因)``——原因非空则整条拒载。

        **逃逸一律拒载而不是"只跳过越界那一个"**：一个插件的组件表要么是它声明的
        那一套，要么它就是个坏包。只跳过一个的话，列表上看起来一切正常，
        而"少了一个组件"没人会发现。
        """
        out: list[PluginComponent] = []
        for path in _skill_dirs(root / SKILL_DIR):
            escape = _escape_reason(root, path)
            if escape:
                return (), escape
            out.append(
                PluginComponent(
                    kind="skill",
                    name=_skill_name(path),
                    description=_skill_description(path),
                    path=_relative(root, path),
                    status=COMPONENT_STATUS["skill"],
                )
            )
        for path in _command_files(root / COMMAND_DIR):
            escape = _escape_reason(root, path)
            if escape:
                return (), escape
            out.append(
                PluginComponent(
                    kind="command",
                    name=path.stem,
                    description=_command_description(path),
                    path=_relative(root, path),
                    status=COMPONENT_STATUS["command"],
                )
            )
        hooks, escape = self._hooks(root)
        if escape:
            return (), escape
        out.extend(hooks)
        for tool in manifest.tools:
            if tool.path:
                # entry 是 manifest 里**唯一**的路径声明（ZCode 的约定是"组件不写路径"，
                # 而工具配置本来就要指向一段代码）。它由别人写的包决定，所以必须过闸。
                escape = _escape_reason(root, root / tool.path)
                if escape:
                    return (), escape
            out.append(tool)
        return tuple(out), ""

    def _hooks(self, root: Path) -> tuple[list[PluginComponent], str]:
        """``hooks/hooks.json``（照 ZCode 的位置）。**只列出**：不触发、不解析动作。

        坏的 hooks.json **不拒载整个插件**：manifest 是好的，坏的是它的一个组件，
        而"这个组件坏了"本身就是要在界面上说出来的一件事。
        """
        path = root / HOOKS_DIR / "hooks.json"
        if not path.is_file():
            return [], ""
        escape = _escape_reason(root, path)
        if escape:
            return [], escape
        status = COMPONENT_STATUS["hook"]
        relative = _relative(root, path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            return [_broken_hooks(relative, f"读不出来：{_read_reason(error)}", status)], ""
        try:
            raw = json.loads(text)
        except ValueError as error:
            return [_broken_hooks(relative, f"不是合法 JSON：{error}", status)], ""
        events = _hook_events(raw)
        if not events:
            return [_broken_hooks(relative, "没有声明任何钩子", status)], ""
        return [
            PluginComponent(
                kind="hook",
                name=event,
                description=f"{count} 条",
                path=relative,
                status=status,
            )
            for event, count in events
        ], ""


# --------------------------------------------------------------------- 工具函数


def _default_builtin_dir() -> Path:
    """内置插件目录的三条来源，优先级见模块头。

    目录不存在**只警告不报错**：插件是增强不是依赖，少一批内置插件不该让服务起不来。
    但警告不能省——这条路径配错的表现就是"内置插件一个都不出现"，而那看起来
    和"仓库本来就没带插件"一模一样。
    """
    raw = (os.environ.get(BUILTIN_DIR_ENV) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_dir():
            logger.warning(
                "%s 指向的目录不存在：%s（内置插件会一个都扫不到）", BUILTIN_DIR_ENV, path
            )
        return path
    return Path(__file__).resolve().parents[3] / "plugins"


def _plugin_dirs(root: Path) -> list[Path]:
    """``<根>/<插件名>/plugin.json``。**只走一层**：一个插件就是一个目录。

    跳过隐藏目录与 ``_`` 开头（照技能那一档）：``.git`` / ``_draft`` 是用户自己
    的杂物，不该被算成"加载失败"。

    **没有 plugin.json 的目录也返回**：它不是插件，但"我放进去了它却不出现"
    正是要靠一条带原因的失败记录来回答（见 ``_load``）。
    """
    if not root.is_dir():
        return []
    return [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    ]


def _skill_dirs(root: Path) -> list[Path]:
    """``<插件>/skills/<技能>/SKILL.md``（也认 ``skills/<组>/<技能>/SKILL.md``）。

    **只走两层**，与 ``services/skills.py`` 同一条约定：再深就是"把别人的仓库整个
    拷进来"，那会让扫描变成遍历。
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    for first in sorted(root.iterdir()):
        if not first.is_dir() or first.name.startswith((".", "_")):
            continue
        if (first / SKILL_FILE).is_file():
            out.append(first)
            continue
        for second in sorted(first.iterdir()):
            if not second.is_dir() or second.name.startswith((".", "_")):
                continue
            if (second / SKILL_FILE).is_file():
                out.append(second)
    return out


def _command_files(root: Path) -> list[Path]:
    """``<插件>/commands/*.md``（照 ZCode：md 文件即命令，文件名就是命令名）。"""
    if not root.is_dir():
        return []
    return [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_file()
        and entry.suffix.lower() == ".md"
        and not entry.name.startswith((".", "_"))
    ]


def _broken_hooks(relative: str, reason: str, status: str) -> PluginComponent:
    """hooks.json 读不出来时的那一条（**照样列出**，说明写在 description 里）。"""
    return PluginComponent(
        kind="hook", name="hooks.json", description=reason, path=relative, status=status
    )


def _skill_name(directory: Path) -> str:
    """技能名取 ``SKILL.md`` 的 frontmatter，退回目录名（与技能那一档一致）。"""
    meta, _body = _frontmatter(directory / SKILL_FILE)
    return str(meta.get("name") or directory.name).strip() or directory.name


def _skill_description(directory: Path) -> str:
    meta, _body = _frontmatter(directory / SKILL_FILE)
    return _text(meta.get("description"))


def _command_description(path: Path) -> str:
    meta, _body = _frontmatter(path)
    return _text(meta.get("description"))


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """尽力读一份带 frontmatter 的文本。**读不出来就当没有**（描述只是界面上那一行）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("读组件描述失败（当作没有描述）：%s", path, exc_info=True)
        return {}, ""
    return parse_frontmatter(text)


def _hook_events(raw: Any) -> list[tuple[str, int]]:
    """``hooks.json`` 里有哪些事件、各几条。

    **形状容错**是刻意的：三家都没有统一的 hooks.json 协议，我们只做"列出"，
    所以认 ``{"hooks": {事件: [条目…]}}``（ZCode 形状）、``{事件: […]}}``、
    以及"条目列表"三种。认不出来的**当一条无名钩子**报出来，
    而不是说"没有钩子"——后者是在替一个别人写的文件下结论。
    """
    body = raw
    if isinstance(body, dict) and isinstance(body.get("hooks"), (dict, list)):
        body = body["hooks"]
    out: list[tuple[str, int]] = []
    if isinstance(body, dict):
        for event, value in body.items():
            out.append((str(event), len(value) if isinstance(value, list) else 1))
    elif isinstance(body, list):
        for index, item in enumerate(body):
            if isinstance(item, dict):
                name = item.get("event") or item.get("type") or f"第 {index + 1} 条"
            else:
                name = f"第 {index + 1} 条"
            out.append((str(name), 1))
    return out


def _user_config(raw: Any) -> tuple[str, ...]:
    """``userConfig`` 里声明的字段名。两种写法都认：字段列表（ZCode 形状）与
    ``{键: {…}}``。

    **形状不对要报出来**：写成 ``userConfig: "token"`` 时静默当没有，界面上就少一行，
    而写它的人以为声明生效了。
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        keys = [str(key).strip() for key in raw]
        if any(not key for key in keys):
            raise InvalidRequestError("userConfig 里有空的字段名")
        return tuple(keys)
    if not isinstance(raw, list):
        raise InvalidRequestError("userConfig 要么是字段列表，要么是 {键: {…}} 的对象")
    out: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            key = item.strip()
        elif isinstance(item, dict):
            key = str(item.get("name") or "").strip()
        else:
            key = ""
        if not key:
            raise InvalidRequestError(f"userConfig 第 {index + 1} 项没有名字")
        out.append(key)
    return tuple(out)


def _tools(raw: Any) -> tuple[PluginComponent, ...]:
    """manifest 里的 ``tools``（QwenPaw 四类能力面之一）。**本轮只列出**。

    ``entry`` 是"这段代码在哪"（本轮的约定），它的越界判定在 ``_components`` 里做——
    因为那需要知道插件根，而解析 manifest 时还不知道。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidRequestError("tools 要是列表（每项 {name, description, entry}）")
    out: list[PluginComponent] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InvalidRequestError(f"tools 第 {index + 1} 项不是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise InvalidRequestError(f"tools 第 {index + 1} 项缺 name")
        entry = item.get("entry")
        if entry is not None and not isinstance(entry, str):
            raise InvalidRequestError(f"tools 第 {index + 1} 项的 entry 要是字符串路径")
        out.append(
            PluginComponent(
                kind="tool",
                name=name,
                description=_text(item.get("description")),
                path=(entry or "").strip(),
                status=COMPONENT_STATUS["tool"],
            )
        )
    return tuple(out)


def _escape_reason(root: Path, target: Path) -> str:
    """``target`` 有没有跑出插件根（**含符号链接**）。空串 = 没跑出去。

    这是这一层唯一的安全边界：插件目录是别人给的，一个 ``../../.ssh/id_rsa``
    的 entry、或一条指到仓库外的符号链接，不该因为我们"按约定找组件"就被读进来。
    ZCode 对跨市场依赖要显式放行、DSH 的层栈只认显式声明，同一个道理。
    """
    try:
        base = root.resolve()
        resolved = target.resolve()
    except OSError:  # 解析不了（路径太长、权限不足）就交给后面真正的读失败去报
        return ""
    if resolved == base or base in resolved.parents:
        return ""
    return f"组件路径逃逸插件根：{target.name} 指向 {resolved}"


def _relative(root: Path, path: Path) -> str:
    """相对插件根的路径（界面上要的是"在插件里的哪儿"，绝对路径只在排错时有用）。"""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _text(raw: Any, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """把 manifest/frontmatter 里的一个值取成短文本（``None`` → 空串）。

    截断上限与技能那一档共用：这些东西都会出现在列表里，
    一个几万字的 description 会把界面撑成正文。
    """
    text = str(raw).strip() if raw is not None else ""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _read_reason(error: Exception) -> str:
    """读文件失败的人话说明。非法 UTF-8 单独说——它的英文原文看不出来是编码问题。"""
    if isinstance(error, UnicodeDecodeError):
        return "文件不是 UTF-8 编码"
    return f"{error}"
