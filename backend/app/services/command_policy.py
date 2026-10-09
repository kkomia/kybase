"""命令与工具准入规则（v0.17）。

**这一层是照抄成熟方案的，不是我们自己发明的**：规则语法、三档动作与优先级
都取自 Claude Code 的权限模型（`Bash(git status:*)` 那样写规则、deny / ask / allow
三张清单），QwenPaw 的 Governance 用的是同一套词表（allow / deny / ask / sandbox）。
抄它的理由很直接：这套东西已经被大量用户和大量命令磨过，边界情况
（前缀匹配怎么写、deny 能不能被更宽的 allow 覆盖、没匹配上算哪一档）
都有既成的、被验证过的答案；自己设计一套只会在细节上反复踩坑。

**三条照抄的规则，一条都不能改**：

1. **语法是 ``Tool(specifier)``**：``Bash(git status:*)``、``mcp__github__create_issue``。
   裸工具名（不带括号）表示"这个工具整体"。
2. **优先级 ``deny > ask > allow``**。**deny 永远优先**，而且**先判 deny**：
   这是整套东西里最要紧的一条——没有它，"我加了一条 deny" 会被一条更宽的 allow
   静默盖掉，而用户以为自己已经禁掉了。
3. **没匹配上就按默认档**（我们这里是 ``ask``，与 Claude Code 的默认模式一致）。
   不是 allow：默认放行等于规则表形同虚设。

**前缀通配 ``:*``** 只在**词边界**上生效：``Bash(git status:*)`` 匹配
``git status`` 与 ``git status --short``，**不匹配** ``git statuses``。
这一点是手写 glob 最容易错的地方（``git status*`` 会匹配到 ``git statuses``，
而那是一个完全不同的命令）。

**为什么把它与"沙箱"分开**：沙箱管"跑起来之后能碰什么"，这一层管"哪些根本不用问"。
两者是**独立的**：允许免确认不等于允许越界，沙箱该拦的还是拦。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import InvalidRequestError

__all__ = [
    "ACTIONS",
    "ACTION_ALLOW",
    "ACTION_ASK",
    "ACTION_DENY",
    "Decision",
    "Rule",
    "RuleSet",
    "append_allow_rule",
    "format_rule",
    "parse_rule",
    "parse_rules",
    "rules_from_runtime",
    "suggest_rule",
]

logger = logging.getLogger(__name__)

ACTION_ALLOW = "allow"
ACTION_ASK = "ask"
ACTION_DENY = "deny"
ACTIONS = (ACTION_ALLOW, ACTION_ASK, ACTION_DENY)

#: 工具名与限定名的合法字符。MCP 的限定名是 ``mcp__server__tool``，
#: 服务器名里可能有非 ASCII（我们的限定名不折非 ASCII）——所以字符集放宽到"非空白非括号"。
_RULE = re.compile(r"^\s*(?P<tool>[^\s()]+)\s*(?:\(\s*(?P<spec>.*?)\s*\))?\s*$")

#: 一条规则里 specifier 的长度上限。规则是给人看的，不该是一篇文本。
MAX_SPECIFIER_CHARS = 200


@dataclass(frozen=True, slots=True)
class Rule:
    """一条准入规则：``工具`` + 可选的**参数前缀**。

    ``specifier`` 为 ``None`` 表示"这个工具整体"（不带括号的写法）。
    """

    tool: str
    specifier: str | None = None

    @property
    def is_wildcard(self) -> bool:
        """``specifier`` 以 ``:*`` 结尾 = 前缀匹配（``git status:*``）。"""
        return bool(self.specifier) and self.specifier.endswith(":*")

    @property
    def prefix(self) -> str:
        return (self.specifier or "")[:-2] if self.is_wildcard else (self.specifier or "")

    def matches(self, tool: str, arguments: str) -> bool:
        """这条规则管不管这次调用。

        ``arguments`` 是**已经拼好的参数串**（命令是空格拼的 argv，
        MCP 工具是空串）：规则匹配的是"用户看到的那一行命令"，
        而不是某个内部结构——用户写规则时想的就是那一行。
        """
        if tool != self.tool:
            return False
        if self.specifier is None:
            return True
        if self.is_wildcard:
            # **词边界**：`git status:*` 匹配 `git status --short`，不匹配 `git statuses`。
            # 前缀匹配要么在末尾、要么后面跟一个空白——少这一条就成了"以它开头的都放行"。
            head = self.prefix
            if not arguments.startswith(head):
                return False
            rest = arguments[len(head) :]
            return rest == "" or rest[0].isspace()
        return arguments == self.specifier

    def describe(self) -> str:
        return format_rule(self)


@dataclass(frozen=True, slots=True)
class Decision:
    """判定结果。``action`` 是最终档位，``rule`` 是**命中的那条**。

    带上命中的规则是为了能解释"为什么是这个结论"——用户加错一条 deny 时，
    界面能直接指出是哪一条在起作用，而不是让他自己一行行排查。
    """

    action: str
    rule: Rule | None = None
    reason: str = ""

    @property
    def needs_approval(self) -> bool:
        return self.action == ACTION_ASK

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_DENY


class RuleSet:
    """三张清单 + 默认档。**判定顺序固定**：deny → ask → allow → 默认。

    顺序是这套方案的核心，所以要写成代码而不是"约定"：先判 allow 的话，
    一条宽泛的 allow 会把后面的 deny 全部盖掉，而用户以为自己已经禁掉了。
    """

    def __init__(
        self,
        *,
        allow: list[Rule] | None = None,
        ask: list[Rule] | None = None,
        deny: list[Rule] | None = None,
        default: str = ACTION_ASK,
    ) -> None:
        if default not in ACTIONS:
            raise InvalidRequestError(f"默认档只能是 {'/'.join(ACTIONS)}")
        self._allow = tuple(allow or ())
        self._ask = tuple(ask or ())
        self._deny = tuple(deny or ())
        self._default = default

    @property
    def default(self) -> str:
        return self._default

    @property
    def allow(self) -> tuple[Rule, ...]:
        return self._allow

    def decide(self, tool: str, arguments: str = "") -> Decision:
        for rule in self._deny:
            if rule.matches(tool, arguments):
                return Decision(ACTION_DENY, rule, f"命中拒绝规则 {rule.describe()}")
        for rule in self._ask:
            if rule.matches(tool, arguments):
                return Decision(ACTION_ASK, rule, f"命中确认规则 {rule.describe()}")
        for rule in self._allow:
            if rule.matches(tool, arguments):
                return Decision(ACTION_ALLOW, rule, f"命中放行规则 {rule.describe()}")
        return Decision(self._default, None, "没有命中任何规则")


# ------------------------------------------------------------------ 解析/格式化


def parse_rule(text: str) -> Rule:
    """解析一条规则。**格式不对就报错**，不猜。

    猜（比如"看不懂就当成整体工具名"）会让一条写错的 deny 变成一条**更宽**的
    放行规则——那是安全相关配置里最不能容忍的一种失败。
    """
    raw = (text or "").strip()
    if not raw:
        raise InvalidRequestError("规则不能为空")
    match = _RULE.match(raw)
    if not match:
        raise InvalidRequestError(
            f"规则格式不对：{text}（应当形如 Bash(git status:*) 或 mcp__server__tool）"
        )
    tool = match.group("tool")
    specifier = match.group("spec")
    if specifier is not None:
        specifier = specifier.strip()
        if len(specifier) > MAX_SPECIFIER_CHARS:
            raise InvalidRequestError(f"规则的参数部分太长（上限 {MAX_SPECIFIER_CHARS} 字）")
        if specifier == "":
            # `Bash()` 这种写法歧义（是"整体"还是"空参数"？）——拒绝，让人写清楚
            raise InvalidRequestError(
                f"规则 {text} 的括号里是空的。要表示「这个工具整体」，请去掉括号"
            )
    return Rule(tool=tool, specifier=specifier)


def _strip_comment(line: str) -> str:
    """剥掉行尾注释，**只在括号之外**。

    ``Bash(git status:*)  # 常看 # 注释`` 里的 ``#`` 在闭括号之后，
    这时它不可能是规则的一部分；而 ``Bash(echo #x)`` 里的 ``#`` 在括号内，
    它可能是参数的一部分——**剥错了会把规则改意思**，所以按括号深度判断。
    没有括号的裸工具名同理：``mcp__x__t # 备注`` 的 ``#`` 之后是注释。
    """
    depth = 0
    for index, char in enumerate(line):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "#" and depth == 0 and index > 0 and line[index - 1].isspace():
            return line[:index].rstrip()
    return line


def parse_rules(text: str) -> list[Rule]:
    """把多行文本解析成规则。空行与 ``#`` 注释行跳过（清单是给人编辑的）。

    重复的规则**保留**而不是去重：去重会让"我明明加了两条、界面只显示一条"
    变成一个新的疑问，而这个规模下多一条的成本是零。
    """
    rules: list[Rule] = []
    for line in (text or "").splitlines():
        stripped = _strip_comment(line).strip()
        if not stripped or stripped.startswith("#"):
            continue
        rules.append(parse_rule(stripped))
    return rules


def format_rule(rule: Rule) -> str:
    """规则 → 文本（与 :func:`parse_rule` 往返一致）。"""
    if rule.specifier is None:
        return rule.tool
    return f"{rule.tool}({rule.specifier})"


def suggest_rule(tool: str, arguments: str) -> Rule:
    """为一次调用**建议**一条放行规则（界面上「以后都允许」用它）。

    建议的是**这个词前缀**而不是完整命令：``git status --short`` 建议成
    ``Bash(git status:*)``。理由是用户点"以后都允许"时想的是"这类命令"，
    而完整命令每次参数都不同，记住它等于没记住。
    只对**单段**参数做前缀（``git commit -m x`` 建议 ``Bash(git commit:*)``）——
    把整行都当模式会让规则过窄。
    """
    if not arguments:
        return Rule(tool=tool, specifier=None)
    head = arguments.split()[0] if arguments.split() else arguments
    return Rule(tool=tool, specifier=f"{head}:*")


def build_rule_set(
    *,
    allow_text: str = "",
    ask_text: str = "",
    deny_text: str = "",
    default: str = ACTION_ASK,
    source: str = "",
) -> RuleSet:
    """从三张清单（多行文本）建一个 :class:`RuleSet`。

    ``source`` 只用于**报错时指明是哪张清单写错了**：三张清单都在设置页里，
    不指明的话用户得自己一行行翻（"第 3 行格式不对"这种报错才有用）。

    **一张清单解析失败不该让整个判定崩溃**：那会让"某条规则写错"
    升级成"所有执行都用不了"。所以坏行是**跳过 + 记日志**，
    而不是把异常抛给调用方——拒绝对应的那条规则本来也没被写对，
    它按"没命中"处理正好落回默认档（ask）。
    """
    def parse(text: str, name: str) -> list[Rule]:
        """**逐行解析**：一行写错只丢那一行。

        第一版是整张清单一起解析、出错就丢掉整张——那意味着一个手滑的字符
        会让用户其余的规则**全部失效**，而界面上只表现为"规则不再生效"
        （连日志都要去翻才知道）。逐行之后，坏的落回默认档（ask，更严），
        好的照常生效。
        """
        rules: list[Rule] = []
        for line in (text or "").splitlines():
            stripped = _strip_comment(line).strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                rules.append(parse_rule(stripped))
            except InvalidRequestError as exc:
                logger.warning("设置里 %s 清单的这一行用不了（已跳过）：%s", name, exc)
        return rules

    return RuleSet(
        allow=parse(allow_text, f"{source}放行" if source else "放行"),
        ask=parse(ask_text, f"{source}确认" if source else "确认"),
        deny=parse(deny_text, f"{source}拒绝" if source else "拒绝"),
        default=default,
    )


def tool_arguments(argv: list[str]) -> str:
    """把 argv 拼成规则匹配用的参数串（**就是用户看到的那一行命令**）。"""
    return " ".join(part for part in argv if part)


def rules_from_runtime(runtime: Any, *, source: str = "") -> RuleSet:
    """按用户配的三张清单建规则集。

    **三个调用点共用这一处**（沙箱执行、MCP 端点、对话里的工具闸）：三处各写一遍的话，
    迟早出现"某处读到的是另一个 key"或者"某处默认档不一样"，
    而那种分叉的表现是"这个入口拦得住、那个拦不住"——正是策略层最不能有的一种不一致。

    ``runtime`` 是鸭子类型的运行期配置（只要求 ``get(key)``），
    这样本模块不必依赖 ``RuntimeConfigService``。
    """
    return build_rule_set(
        allow_text=runtime.get("sandbox.rules_allow"),
        ask_text=runtime.get("sandbox.rules_ask"),
        deny_text=runtime.get("sandbox.rules_deny"),
        default=ACTION_ASK,
        source=source,
    )


def append_allow_rule(runtime: Any, rule: Rule) -> bool:
    """把一条放行规则追加进清单（界面上「以后都允许」用它）；返回**是否真的写了**。

    重复的行**不重复追加**：界面上那句话是"以后都允许"，第二次点还是同一句意思，
    而清单里多一行一模一样的规则，用户只会以为界面坏了。
    """
    current = (runtime.get("sandbox.rules_allow") or "").rstrip()
    line = rule.describe()
    if line in {item.strip() for item in current.splitlines()}:
        return False
    # 显式拼接，不在源码里写多行字符串：那段字符串被改坏过一次
    # （转义换行落进了真换行，语法直接错）
    runtime.set({"sandbox.rules_allow": "\n".join(part for part in (current, line) if part)})
    return True
