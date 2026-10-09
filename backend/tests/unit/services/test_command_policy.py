"""命令与工具准入规则（v0.17）。

镜像同构：``app/services/command_policy.py`` → 本文件。

这套东西是**照抄 Claude Code 的权限模型**（规则语法、三档动作、优先级），
所以测的重点也正是那三条被抄过来的规则——它们的失效方式都是**静默放行**：

1. **``:*`` 只在词边界生效**：``git status:*`` 匹配 ``git status --short``，
   **不匹配** ``git statuses``。手写 glob 写成 ``git status*`` 就会把后一个也放行，
   而那是一条完全不同的命令；
2. **deny 永远优先**：先判 allow 的话，一条更宽的 allow 会把用户的 deny 静默盖掉，
   而用户以为自己已经禁掉了；
3. **没匹配上按默认档**（ask），不是 allow——默认放行等于规则表形同虚设。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    Rule,
    RuleSet,
    build_rule_set,
    format_rule,
    parse_rule,
    parse_rules,
    suggest_rule,
    tool_arguments,
)

# --------------------------------------------------------------------- 解析


def test_parses_bare_tool_name() -> None:
    """不带括号 = 这个工具整体。"""
    rule = parse_rule("mcp__github__create_issue")

    assert rule.tool == "mcp__github__create_issue"
    assert rule.specifier is None
    assert rule.matches("mcp__github__create_issue", "任何参数")


def test_parses_tool_with_specifier() -> None:
    rule = parse_rule("Bash(git status:*)")

    assert rule.tool == "Bash"
    assert rule.specifier == "git status:*"
    assert rule.is_wildcard is True
    assert rule.prefix == "git status"


def test_round_trips_through_text() -> None:
    """``format`` 与 ``parse`` 必须互为逆运算：规则要在设置页里可读可写。"""
    for text in ("Bash(git status:*)", "Bash(ls -la)", "mcp__srv__tool"):
        assert format_rule(parse_rule(text)) == text


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "Bash()", "有 空格 的工具名", "Bash(((", "Bash(git status:*) 尾巴"],
)
def test_malformed_rule_is_rejected_not_guessed(bad: str) -> None:
    """**格式不对就报错，不猜**。

    猜（比如"看不懂就当整体工具名"）会把一条写错的 deny 变成一条**更宽**的放行规则
    ——那是安全配置里最不能容忍的一种失败。
    """
    with pytest.raises(InvalidRequestError):
        parse_rule(bad)


def test_parse_rules_skips_blank_and_comment_lines() -> None:
    """清单是给人编辑的：空行、整行注释、**行尾注释**都要能写。"""
    rules = parse_rules("# 我的规则\n\nBash(ls)\n  \nBash(git status:*)  # 常看的\n")

    assert [format_rule(rule) for rule in rules] == ["Bash(ls)", "Bash(git status:*)"]


def test_comment_inside_parentheses_is_not_stripped() -> None:
    """**括号里的 ``#`` 是参数的一部分**，剥掉就把规则改了意思。

    这是个真实的坑：``Bash(echo #x)`` 里的 ``#`` 与行尾注释长得一样，
    只有按括号深度判断才分得开。
    """
    rules = parse_rules("Bash(echo #x)  # 这是注释\n")

    assert [format_rule(rule) for rule in rules] == ["Bash(echo #x)"]


# --------------------------------------------------------------------- 匹配


def test_wildcard_matches_on_word_boundary_only() -> None:
    """**这条是手写 glob 最容易错的地方**：``git status:*`` 匹配
    ``git status --short``，不匹配 ``git statuses``——后者是完全不同的命令。"""
    rule = parse_rule("Bash(git status:*)")

    assert rule.matches("Bash", "git status") is True
    assert rule.matches("Bash", "git status --short") is True
    assert rule.matches("Bash", "git statuses") is False
    assert rule.matches("Bash", "git stats") is False


def test_exact_rule_requires_exact_arguments() -> None:
    rule = parse_rule("Bash(ls -la)")

    assert rule.matches("Bash", "ls -la") is True
    assert rule.matches("Bash", "ls -la /tmp") is False


def test_rule_only_applies_to_its_own_tool() -> None:
    """``Bash(ls)`` 不该管到 MCP 工具上去。"""
    rule = parse_rule("Bash(ls)")

    assert rule.matches("Bash", "ls") is True
    assert rule.matches("mcp__x__ls", "ls") is False


# --------------------------------------------------------------------- 判定


def test_defaults_to_ask_when_nothing_matches() -> None:
    """**没匹配上不是放行**：默认放行等于规则表形同虚设。"""
    decision = RuleSet().decide("Bash", "curl https://example.test")

    assert decision.action == ACTION_ASK
    assert decision.needs_approval is True
    assert decision.rule is None


def test_deny_beats_a_broader_allow() -> None:
    """**deny 永远优先**。少了这一条，用户"我加了一条 deny"会被一条更宽的 allow
    静默盖掉，而用户以为自己已经禁掉了。"""
    rules = RuleSet(
        allow=parse_rules("Bash(git push:*)"),
        deny=parse_rules("Bash(git push --force:*)"),
    )

    assert rules.decide("Bash", "git push origin main").action == ACTION_ALLOW
    assert rules.decide("Bash", "git push --force origin main").action == ACTION_DENY


def test_ask_beats_allow() -> None:
    rules = RuleSet(
        allow=parse_rules("Bash(git:*)"),
        ask=parse_rules("Bash(git push:*)"),
    )

    assert rules.decide("Bash", "git status").action == ACTION_ALLOW
    assert rules.decide("Bash", "git push").action == ACTION_ASK


def test_decision_explains_which_rule_fired() -> None:
    """带上命中的那条规则：用户加错一条 deny 时，界面能直接指出是哪一条。"""
    rules = RuleSet(deny=parse_rules("Bash(rm -rf:*)"))

    decision = rules.decide("Bash", "rm -rf /")

    assert decision.blocked is True
    assert decision.rule is not None
    assert "Bash(rm -rf:*)" in decision.reason


def test_custom_default_is_respected() -> None:
    """默认档只用来决定"没规则时怎么办"，不改变优先级。"""
    rules = RuleSet(deny=parse_rules("Bash(rm:*)"), default=ACTION_ALLOW)

    assert rules.decide("Bash", "ls").action == ACTION_ALLOW
    assert rules.decide("Bash", "rm x").action == ACTION_DENY


def test_unknown_default_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        RuleSet(default="whatever")


# --------------------------------------------------------------- 设置里的清单


def test_build_rule_set_from_settings_text() -> None:
    rules = build_rule_set(
        allow_text="Bash(git status:*)\nBash(ls)",
        deny_text="Bash(rm -rf:*)",
        default=ACTION_ASK,
    )

    assert rules.decide("Bash", "git status").action == ACTION_ALLOW
    assert rules.decide("Bash", "rm -rf /").action == ACTION_DENY
    assert rules.decide("Bash", "curl x").action == ACTION_ASK


def test_a_broken_rule_does_not_break_the_whole_list() -> None:
    """某条规则写错，**不该让所有执行都用不了**——坏行跳过、好行照常生效。

    这与 `parse_rule` 的"报错不猜"不矛盾：那一层的调用方（设置页）需要知道
    自己写错了；这一层在请求路径上，宁可少一条规则也不能整条链路瘫掉。
    （少一条规则的结果是"落回 ask"——更严，不是更松。）
    """
    rules = build_rule_set(allow_text="Bash(ls)\n这个((写坏了\nBash(pwd)")

    assert rules.decide("Bash", "ls").action == ACTION_ALLOW
    assert rules.decide("Bash", "pwd").action == ACTION_ALLOW
    # 坏的那条按"没命中"处理 → 落回默认档（ask），而不是被当成放行
    assert rules.decide("Bash", "这个((写坏了").action == ACTION_ASK


# --------------------------------------------------------------------- 建议


def test_suggests_a_word_prefix_not_the_full_command() -> None:
    """「以后都允许」要记的是**这类命令**，而完整命令每次参数都不同——
    记住它等于没记住。"""
    rule = suggest_rule("Bash", "git status --short")

    assert format_rule(rule) == "Bash(git:*)"


def test_suggests_whole_tool_when_there_are_no_arguments() -> None:
    """MCP 工具没有参数串，整条工具就是它的粒度。"""
    assert format_rule(suggest_rule("mcp__github__create_issue", "")) == "mcp__github__create_issue"


def test_suggested_rule_actually_matches_the_call_it_came_from() -> None:
    """**建议出来的规则必须真能匹配刚才那条命令**——否则用户点了"以后都允许"，
    下次同样的命令还会再问一次，而他会以为这个按钮坏了。"""
    for argv in (["git", "status", "--short"], ["npm", "run", "test"], ["ls"], ["pytest", "-q"]):
        arguments = tool_arguments(argv)
        rule = suggest_rule("Bash", arguments)
        assert rule.matches("Bash", arguments), f"{rule.describe()} 匹配不了 {arguments}"


def test_tool_arguments_joins_argv() -> None:
    assert tool_arguments(["git", "commit", "-m", "x"]) == "git commit -m x"
    # 空段丢掉：规则匹配的是"用户看到的那一行"，多一个空格会让精确规则对不上
    assert tool_arguments(["ls", "", "-la"]) == "ls -la"


# --------------------------------------------------------------------- 形状


def test_rules_are_immutable() -> None:
    """规则与判定都是 frozen 记录：判定结果会被传出去，
    能被就地改的判定结果是"审计"这件事的漏洞。"""
    rule = Rule(tool="Bash", specifier="ls")
    # frozen dataclass 抛的是 FrozenInstanceError（AttributeError 的子类）
    with pytest.raises(AttributeError):
        rule.tool = "别的"  # type: ignore[misc]
