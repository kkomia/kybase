"""斜杠命令：内置表、自定义 md、优先级与参数替换（P1-2，开发计划 §12.225）。

镜像同构：``app/services/commands.py`` → 本文件。

这一层的错法都偏安静，逐条钉住：

1. **文件名即命令名**（ZCode）：不合法的名字要**带着原因留在列表里**，
   而不是静默消失——"我放的文件为什么不生效"是最难查的一类问题；
2. **优先级内置 > 用户 > 仓库，first match wins**：被遮蔽的要能看出**被谁**遮蔽；
3. **参数两条占位符 + 那条兜底追加**（ZCode）：``$ARGUMENTS`` / ``$1..$N``，
   一个占位符都没写却带了参数时自动追加——它省掉的是"命令没收到参数"的困惑；
4. **``/`` 开头的输入永不静默降级**（DSH）：认不出的也当成命令（由协议层回一句
   "没有这个命令"），不返回 ``None``。
"""

from __future__ import annotations

from pathlib import Path

from app.services.commands import (
    BUILTIN_COMMANDS,
    COMMAND_MAX_DEPTH,
    COMMAND_NAME_RE,
    SKILL_SUMMARY_CHARS,
    CommandService,
    ModeWatch,
    TurnControl,
    builtin_names,
    modes_text,
    parse,
    render,
    split_args,
)


def _service(tmp_path: Path, *, repo: Path | None = None) -> CommandService:
    """一个把两条发现源都指到临时目录的服务（**不去读仓库里真实的那两个目录**）。"""
    return CommandService(tmp_path / "data", builtin_dir=repo or (tmp_path / "repo"))


def _write(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------- 解析


def test_only_a_leading_slash_makes_it_a_command() -> None:
    """判定只有一条：第一个词以 ``/`` 开头（大小写不敏感，照 ZCode 的归一化）。"""
    assert parse("帮我看下 /tmp 目录") is None
    assert parse("") is None
    assert parse("/mode plan") == parse("/Mode plan")
    assert parse("/mode plan").name == "mode"  # type: ignore[union-attr]
    assert parse("/mode plan").args == "plan"  # type: ignore[union-attr]


def test_a_lone_slash_asks_for_help() -> None:
    """只有一个 ``/``：按 ``/help`` 处理（与各家终端里的补全一致）。"""
    parsed = parse("/")
    assert parsed is not None and parsed.name == "help"


def test_an_unknown_slash_line_is_still_a_command() -> None:
    """**``/`` 行永不静默降级**（DSH）：认不出的也返回命令，由协议层答"没有这个命令"。

    返回 ``None`` 的话它会变成一次普通提问——用户以为自己在用命令，模型却在猜
    他想说什么，而"那句话到底被谁读了"他根本看不出来。
    """
    parsed = parse("/znou")
    assert parsed is not None and parsed.name == "znou"


# --------------------------------------------------------------------- 内置表


def test_the_builtin_table_covers_the_eight_commands_of_this_round() -> None:
    """内置十二条：上一轮的八条 + 调研 §3 第 1-4 条的 ``/rewind`` ``/context``
    ``/status`` ``/skills``。

    每条都要有 ``summary``（菜单那一行）与 ``usage``（``/help <命令>`` 的用法行）
    ——ZCode 那张表就是这四个字段，缺一个界面上就会少一句话。
    """
    assert builtin_names() == (
        "help",
        "compact",
        "new",
        "stop",
        "mode",
        "model",
        "plan",
        "skill",
        "rewind",
        "context",
        "status",
        "skills",
    )
    for record in BUILTIN_COMMANDS:
        assert record.summary, record.name
        assert record.usage.startswith(f"/{record.name}"), record.name
        assert record.source == "builtin"


def test_only_skill_is_not_short_circuited_among_builtins() -> None:
    """十二条里只有 ``/skill`` 是**表级**的改写类（它重写这一轮的提示，照 ZCode）。

    这一条直接决定界面往哪条路发：短路类不建回答气泡，改写类按普通一轮处理。

    ``/plan`` 的名字在这份名单里是**保守口径**：它不带描述时就是 ``/mode plan``
    （不碰模型），带上描述时那次的结果自带 ``prompt``、由协议层按改写类接着跑
    ——真正分流的判据是 ``api/v1/chat.py`` 的 ``_CommandResult.short_circuit``，
    那个只能拿到结果之后才说得准（见 ``CommandDef.short_circuit`` 的说明）。

    新落的那四条（``/rewind`` ``/context`` ``/status`` ``/skills``）全是短路类：
    它们回答的是"现在怎么样、撤掉什么"，没有一条需要模型。
    """
    short = {record.name for record in BUILTIN_COMMANDS if record.short_circuit}
    assert short == {
        "help",
        "compact",
        "new",
        "stop",
        "mode",
        "model",
        "plan",
        "rewind",
        "context",
        "status",
        "skills",
    }


def test_modes_text_lists_the_two_modes() -> None:
    """``/mode`` 不带参数时列的**两档**与 ``services/modes`` 同一份（不另抄清单）。

    2026-09-27：模式轴从四档收敛成两档（目标 / 计划），"能碰多少"那一半拆去了权限轴。
    """
    text = modes_text()
    for name in ("goal", "plan"):
        assert f"（{name}）" in text


# --------------------------------------------------------------------- 参数替换


def test_arguments_and_positional_placeholders(tmp_path: Path) -> None:
    """``$ARGUMENTS`` 给全部原样，``$1``…``$N`` 按空白切（ZCode 的两种占位符）。"""
    service = _service(tmp_path)
    body = "---\ndescription: 评审\n---\n先看 $1，再看 $2。\n用户说：$ARGUMENTS"
    record = _load_one(service, "review.md", body)
    assert render(record, "甲 乙") == "先看 甲，再看 乙。\n用户说：甲 乙"


def test_a_missing_positional_becomes_an_empty_string(tmp_path: Path) -> None:
    """给了 ``$2`` 而没有第二个参数：替成空串，**不报错**（照 ZCode 的宽松口径）。"""
    service = _service(tmp_path)
    record = _load_one(service, "two.md", "只写 $2 一个占位符")
    assert render(record, "只有一个") == "只写  一个占位符"


def test_arguments_are_appended_when_the_body_has_no_placeholder(tmp_path: Path) -> None:
    """**有参数但正文没有占位符时自动追加**（ZCode 那条兜底，第 3 条）。

    没有它，用户会得到"命令好像没收到我的参数"——那是命令这个功能上最常见的困惑。
    """
    service = _service(tmp_path)
    record = _load_one(service, "sum.md", "把这段总结成三句话。")
    assert render(record, "第一段 第二段") == "把这段总结成三句话。\n\n用户参数：第一段 第二段"
    # 没有参数就不追加（不留一个空的"用户参数："）
    assert render(record, "") == "把这段总结成三句话。"


def test_split_args_is_whitespace_only() -> None:
    """不做引号解析（ZCode 就没有这一层）：要带空格的整段用 ``$ARGUMENTS``。"""
    assert split_args('  甲   "乙 丙"  ') == ["甲", '"乙', '丙"']


def test_named_arguments_map_by_position(tmp_path: Path) -> None:
    """``arguments: [topic, tone]`` → ``$topic`` 是第一个、``$tone`` 是第二个（照 Claude）。

    它同时给菜单那一行补了用法（没写 ``argument-hint`` 时用声明的名字拼）：
    不拼的话 ``/story`` 与 ``/story 题材 语气`` 在菜单里长得一模一样。
    """
    service = _service(tmp_path)
    body = "---\ndescription: 写一段\narguments: [topic, tone]\n---\n用 $tone 的语气聊 $topic"
    record = _load_one(service, "story.md", body)
    assert record.arguments == ("topic", "tone")
    assert record.usage == "/story <topic> <tone>"
    assert render(record, "天气 幽默") == "用 幽默 的语气聊 天气"
    # `/help story` 会说清哪个名字取第几个参数（这是这套语法唯一会猜错的地方）
    assert any("$topic（第 1 个）" in line for line in record.details)


def test_a_named_placeholder_that_is_not_declared_is_left_alone(tmp_path: Path) -> None:
    """**没声明的 ``$name`` 一律原样留着**，而且要算作"没有占位符"（参数照样追加）。

    不限声明就替的话，正文里正常的美元符号会被吃掉（``$PATH``、``$100`` 一类的写法
    在提示词里很常见）——那是"命令好好地写着，正文却少了一块"。
    """
    service = _service(tmp_path)
    record = _load_one(service, "free.md", "保留 $PATH 与 $topic，不解释")
    assert render(record, "参数") == "保留 $PATH 与 $topic，不解释\n\n用户参数：参数"


def test_zero_based_and_one_based_indexes_coexist(tmp_path: Path) -> None:
    """``$0``（0 起，新）与 ``$1``（1 起，老口径）**并存**，都指第一个参数。

    ``$1`` 不能改成"第二个"：那样所有已经写好的命令会当场坏掉。而
    ``$ARGUMENTS[N]`` 是 Claude 的 0 起写法，排在 ``$ARGUMENTS`` 前面替——
    顺序反了的话 ``$ARGUMENTS[1]`` 会变成"全部参数 + [1]"。
    """
    service = _service(tmp_path)
    body = "零起：$ARGUMENTS[0]/$ARGUMENTS[2]；老口径：$1/$2；$0 也是第一个"
    record = _load_one(service, "mix.md", body)
    assert render(record, "甲 乙 丙") == "零起：甲/丙；老口径：甲/乙；甲 也是第一个"
    # 越界替成空串（与 $N 同一条宽松口径），不报错
    assert render(record, "只有一个").split("；")[0] == "零起：只有一个/"


def test_the_zero_placeholder_does_not_eat_ordinary_numbers(tmp_path: Path) -> None:
    """``$100`` 这种"美元符号 + 数字"照老口径当第 100 个参数（替成空串）——**行为不变**。

    写进用例是为了钉住"这一轮没顺手改掉老三样"：真改掉的话，所有
    ``$1``-系命令的语义会一起变，而那是无声的。
    """
    service = _service(tmp_path)
    record = _load_one(service, "price.md", "价格 $100，用户说：$ARGUMENTS")
    assert render(record, "太贵") == "价格 ，用户说：太贵"


# --------------------------------------------------------------------- 命名空间


def test_a_nested_directory_becomes_a_colon_namespaced_command(tmp_path: Path) -> None:
    """``commands/git/commit.md`` → ``/git:commit``（Claude 与 ZCode 都用 ``:`` 分）。

    目录不止一层也认（``a/b/c.md`` → ``a:b:c``），而命令名仍然是"一个词"：
    解析器只切到第一个空格，``/git:commit 一句话`` 的参数就是那句话。
    """
    service = _service(tmp_path)
    _write(service.user_dir / "git", "commit", "请提交：$ARGUMENTS")
    _write(service.user_dir / "a" / "b", "deep", "深一层")

    record = service.find("git:commit")
    assert record is not None
    assert record.source == "user", "带命名空间的自定义命令仍然是'用户放的'那一档"
    assert render(record, "修一下登录") == "请提交：修一下登录"
    assert service.find("a:b:deep") is not None
    # 顶层那条老写法一个字不变
    _write(service.user_dir, "top", "顶层")
    assert service.find("top") is not None


def test_namespace_dirs_keep_the_same_skipping_rules(tmp_path: Path) -> None:
    """``.``/``_`` 开头的**目录**与文件一样跳过（``_drafts/`` 里放的是草稿）。

    约定要是只对文件成立，``_drafts/x.md`` 就会变成一条命令——而用户放那个下划线
    正是为了"别让它成为命令"。
    """
    service = _service(tmp_path)
    _write(service.user_dir / "_drafts", "later", "草稿")
    _write(service.user_dir / ".hidden", "secret", "隐藏")
    _write(service.user_dir, "_template", "片段")
    assert [item.name for item in service.list() if item.source != "builtin"] == []


def test_a_bad_nested_name_is_dropped_with_a_reason(tmp_path: Path) -> None:
    """名字不合法的判断**对拼出来的命令名成立**：``git/Bad_Name.md`` → 丢弃并记原因。

    判原样的名字（不先转小写）是刻意的：``Bash_Tool.md`` 不能因为"反正会归一"就生效。
    """
    service = _service(tmp_path)
    _write(service.user_dir / "git", "Bad_Name", "正文")
    record = service.any_named("git:bad_name")
    assert record is not None and record.error
    assert not record.usable


def test_the_recursion_stops_at_the_depth_cap(tmp_path: Path) -> None:
    """深度封顶（``COMMAND_MAX_DEPTH``）：再深一层就不扫了。

    护栏而不是功能：命令目录是人手放的目录树，封顶挡的是"符号链接指回上级"
    那类布局把一次请求变成无限遍历。
    """
    service = _service(tmp_path)
    deep = service.user_dir
    for index in range(COMMAND_MAX_DEPTH + 2):
        deep = deep / f"d{index}"
    _write(deep, "too_deep", "太深了")
    assert all(item.source == "builtin" for item in service.list())


# --------------------------------------------------------------------- 技能即命令


class _FakeSkills:
    """一个最小的技能注册表（只用得到 ``list()`` 的四个字段）。"""

    def __init__(self, *records: tuple[str, str, str, bool]) -> None:
        self._records = records

    def list(self):  # type: ignore[no-untyped-def]
        return [
            _SkillRecord(name=name, description=description, source=source, discarded=discarded)
            for name, description, source, discarded in self._records
        ]


class _SkillRecord:
    def __init__(self, *, name: str, description: str, source: str, discarded: bool) -> None:
        self.name = name
        self.description = description
        self.source = source
        self.path = f"/skills/{name}/SKILL.md"
        self.discarded = discarded
        self.flagged = ("缺 description",) if discarded else ()


def _skill_service(tmp_path: Path, skills: _FakeSkills, summaries=None):  # type: ignore[no-untyped-def]
    return CommandService(
        tmp_path / "data",
        builtin_dir=tmp_path / "repo",
        skills=skills,
        skill_summaries=summaries,
    )


def test_a_skill_becomes_a_command_that_yields_to_builtin_and_custom(tmp_path: Path) -> None:
    """每个技能注册一条 ``/<技能名> [任务]``，**排在最后让位**（first match wins）。

    四条要一起成立：技能是**改写类**（``short_circuit`` 假——正文要注入这一轮）；
    与内置/自定义重名时让位、但**留在列表里带 ``shadowed_by``**（看得见才查得出）；
    ``source`` 取技能自己的来源（随代码发布 → ``builtin``，装进来的 → ``user``）；
    而 ``group`` **一律是 ``skill``**（菜单里单独一档，不混进那三档里）。
    """
    skills = _FakeSkills(
        ("kylab-web", "查网页", "builtin", False),
        ("mode", "顶掉内置 mode 的技能", "user", False),
    )
    service = _skill_service(tmp_path, skills)
    _write(service.user_dir, "mode", "用户也放了一个 mode")

    web = service.find("kylab-web")
    assert web is not None
    assert web.skill == "kylab-web" and web.source == "builtin"
    assert web.usage == "/kylab-web [任务]"
    assert web.short_circuit is False, "技能是改写类：正文要注入这一轮"
    assert web.path.endswith("SKILL.md"), "排错要能找到它的 SKILL.md"

    # 与内置重名：生效的是内置那条，技能那条带着 shadowed_by 留在列表里
    assert service.find("mode") is not None and service.find("mode").is_builtin  # type: ignore[union-attr]
    shadowed = [item for item in service.skill_commands() if item.name == "mode"]
    assert shadowed and shadowed[0].shadowed_by == "mode"
    assert shadowed[0].usable is False
    # 用户那份自定义的也在（它是**命令**，与技能不是一回事）
    custom = [item for item in service.list() if item.name == "mode" and item.source == "user"]
    assert custom and custom[0].shadowed_by == "mode"


def test_only_skill_commands_are_in_the_skill_group(tmp_path: Path) -> None:
    """``group`` 是**菜单分组**：技能那批一律 ``skill``，其余取发现源。

    两者**必须分开**：``source`` 决定优先级与 ``is_builtin``（仓库自带的技能仍算
    ``builtin``），而菜单要的是"技能是单独一类"——混进内置那档时二十多条命令会把
    ``/rewind`` ``/status`` ``/skills`` 挤出首屏（调研报告《对话命令-调研 v0.1》§3）。
    """
    skills = _FakeSkills(("kylab-web", "查网页", "builtin", False))
    service = _skill_service(tmp_path, skills)
    _write(service.user_dir, "deploy", "发版")
    _write(service.builtin_dir, "release", "发布")

    groups = {item.name: item.group for item in service.list()}
    assert groups["help"] == "builtin", "内置命令照旧"
    assert groups["deploy"] == "user"
    assert groups["release"] == "repo"
    assert groups["kylab-web"] == "skill"
    # 技能自己的发现源没变（它决定优先级，与分组是两件事）
    assert service.find("kylab-web").source == "builtin"  # type: ignore[union-attr]


def test_a_skill_whose_name_cannot_be_a_command_is_listed_with_a_reason(tmp_path: Path) -> None:
    """技能名不能直接当命令的（中文名、带空格）**照样登记**，带着一句出路。

    静默少一条只会让人以为技能没装上；而这句解释里必须点名 ``/skill``——
    技能还有那条通用入口，用户要的是"改用哪条"，不是"不行"。
    """
    skills = _FakeSkills(("我的技能", "中文名", "user", False))
    service = _skill_service(tmp_path, skills)
    record = service.skill_commands()[0]
    assert record.name == "我的技能" and record.skill == "我的技能"
    assert record.error and "/skill" in record.error
    assert service.find("我的技能") is None, "敲不出来的那条调不到"


def test_a_discarded_skill_is_registered_with_the_reason(tmp_path: Path) -> None:
    """被丢弃的技能也有一条（带原因）：能力页看得见的东西，``/skills`` 也该看得见。"""
    service = _skill_service(tmp_path, _FakeSkills(("broken", "坏技能", "user", True)))
    record = service.skill_commands()[0]
    assert record.error == "这个技能没通过校验：缺 description"
    assert not record.usable


def test_skill_commands_prefer_the_chinese_blurb(tmp_path: Path) -> None:
    """有中文简介就用它（与能力页同一份数据）；没有就退回技能的 ``description``。"""
    skills = _FakeSkills(("kylab-web", "Look it up on the web", "builtin", False))
    service = _skill_service(
        tmp_path, skills, summaries=lambda: {"kylab-web": "查网页并读页面"}
    )
    assert service.skill_commands()[0].summary == "查网页并读页面"
    without = _skill_service(tmp_path, _FakeSkills(("other", "Do a thing", "user", False)))
    assert without.skill_commands()[0].summary == "Do a thing"


def test_a_skill_summary_is_one_short_sentence(tmp_path: Path) -> None:
    """摘要**只取第一句、且截短**（约 40 个字）：菜单那一行要的是人话。

    技能的 ``description`` 是给模型看的触发文本，实测是 ``Look something up on the
    live web and read the page — "查一下", "搜一下"…`` 一整段；整段塞进菜单会被前端
    再 ``truncate`` 一次，读起来只剩一句半。截断只在后端做（两层截断更难读）。
    """
    long_english = (
        "Look something up on the live web and read the page — \"查一下\", \"搜一下\", "
        "or anything whose answer changes over time. Do NOT answer from your own knowledge."
    )
    service = _skill_service(
        tmp_path, _FakeSkills(("kylab-web", long_english, "builtin", False))
    )
    summary = service.skill_commands()[0].summary
    assert len(summary) <= SKILL_SUMMARY_CHARS, "截到短长度（一个省略号算在内）"
    assert summary.endswith("…") and "查一下" not in summary, "第一句之后的部分整个丢掉"
    assert "Do NOT answer" not in summary

    # 短描述原样留着，不加省略号（"截"不该把好的一行也剪一刀）
    short = _skill_service(tmp_path, _FakeSkills(("tiny", "Do a thing", "user", False)))
    assert short.skill_commands()[0].summary == "Do a thing"

    # 中文简介也照同一条上限收（有人写得很长时兜住）
    chinese = "这是一个很长的中文简介，" * 4
    clipped = _skill_service(
        tmp_path, _FakeSkills(("cn", "x", "user", False)), summaries=lambda: {"cn": chinese}
    ).skill_commands()[0].summary
    assert len(clipped) <= SKILL_SUMMARY_CHARS and clipped.endswith("…")


def test_no_skills_wired_means_no_skill_commands(tmp_path: Path) -> None:
    """没接技能注册表就没有技能那批（单测与不接技能的部署照常工作）。

    命令表**不因此报错**：技能是增强不是依赖（与"技能目录不存在只警告"同一条）。
    """
    service = _service(tmp_path)
    assert service.skill_commands() == []
    assert [item.name for item in service.catalog()][:3] == ["compact", "context", "help"]


# --------------------------------------------------------------------- 发现与优先级


def test_a_md_file_in_the_user_dir_becomes_a_command(tmp_path: Path) -> None:
    """**放进来一个 md 文件就是一条命令**（文件名即命令名，零注册、零重启）。"""
    service = _service(tmp_path)
    body = "---\ndescription: 发版\nargument-hint: <版本号>\n---\n按 $1 发版"
    _write(service.user_dir, "release", body)
    record = service.find("release")
    assert record is not None
    assert record.summary == "发版"
    assert record.usage == "/release <版本号>"
    assert record.source == "user"
    assert record.argument_hint == "<版本号>"
    assert record.body == "按 $1 发版"


def test_a_bad_file_name_is_dropped_with_a_reason_instead_of_vanishing(tmp_path: Path) -> None:
    """名字不合法：**丢弃并记原因**，而且那条仍然在列表里（照 ZCode 的正则）。

    静默消失等于把"为什么我放的文件不生效"变成一个查不出的问题。
    """
    service = _service(tmp_path)
    _write(service.user_dir, "Bash_Tool", "正文")
    record = service.any_named("bash_tool")
    assert record is not None, "被丢弃的那条也要出现在列表里"
    assert record.error, "必须带上原因"
    assert not record.usable
    # 名字不合法就调不到
    assert service.find("bash_tool") is None


def test_the_name_pattern_is_zcodes() -> None:
    """命令名的正则是**逐字照抄 ZCode** 的那一条。"""
    assert COMMAND_NAME_RE.pattern == r"^[a-z0-9][a-z0-9_:-]{0,63}$"
    assert COMMAND_NAME_RE.match("review:code")
    assert not COMMAND_NAME_RE.match("_private")
    assert not COMMAND_NAME_RE.match("A")


def test_builtin_beats_user_and_user_beats_repo(tmp_path: Path) -> None:
    """优先级：**内置 > 用户 > 仓库**，重名 first match wins（照 ZCode 的去重顺序）。

    被遮蔽的那条**留在列表里并记下"被谁遮蔽"**——与插件列表同一套做法。
    """
    service = _service(tmp_path)
    _write(service.user_dir, "mode", "用户放的 mode")  # 与内置同名 → 被内置遮蔽
    _write(service.user_dir, "deploy", "用户放的 deploy")
    _write(service.builtin_dir, "deploy", "仓库自带的 deploy")  # 被用户那份遮蔽
    _write(service.builtin_dir, "onboard", "仓库自带的 onboard")

    resolved = service.find("mode")
    assert resolved is not None and resolved.is_builtin, "同名时内置的那条生效"

    # 列表里三条都要在：生效的那条 + 两条被遮蔽的（各带 shadowed_by）
    listed = {item.name: item for item in service.list() if item.name in {"mode", "deploy"}}
    assert listed["mode"].shadowed_by == "mode"
    assert listed["mode"].source == "user"
    assert listed["deploy"].shadowed_by == "deploy"
    assert listed["deploy"].source == "repo"
    assert service.find("deploy") is not None
    assert service.find("deploy").body == "用户放的 deploy"  # type: ignore[union-attr]
    assert service.find("onboard") is not None


def test_the_catalog_only_has_usable_commands(tmp_path: Path) -> None:
    """菜单与 ``/help`` 吃的是 ``catalog()``：被遮蔽的与坏掉的都不该出现在菜单里。"""
    service = _service(tmp_path)
    _write(service.user_dir, "help", "想顶掉内置的 help")
    _write(service.user_dir, "Bad_Name", "名字不合法")
    names = [item.name for item in service.catalog()]
    assert "bad_name" not in names
    assert names.count("help") == 1
    assert service.catalog()[0].is_builtin


def test_read_failures_do_not_raise(tmp_path: Path) -> None:
    """读不出来的文件也不抛错（一条命令写坏了不该让整个列表 500）。"""
    service = _service(tmp_path)
    service.user_dir.mkdir(parents=True, exist_ok=True)
    # 非 UTF-8 内容：UnicodeDecodeError 是 ValueError 的子类，**不是 OSError**
    (service.user_dir / "broken.md").write_bytes(b"\xff\xfe\x00\x00bad")
    record = service.any_named("broken")
    assert record is not None and record.error


# --------------------------------------------------------------------- 模式观测


def test_mode_watch_reports_the_previous_mode_once() -> None:
    """档换过就报一次上一档；同一档连着几轮不重复报（P1-1 遗留 #6）。"""
    watch = ModeWatch(lambda _conversation_id: "build")
    assert watch.observe("c1", "build") is None, "第一次遇到：基线就是 build，没换"
    assert watch.observe("c1", "yolo") == "build"
    assert watch.observe("c1", "yolo") is None, "同一档不重复报"
    assert watch.observe("c1", "plan") == "yolo"


def test_mode_watch_without_a_baseline_stays_silent() -> None:
    """日志里查不到上一档时**不报**：宁可不记，也不写一个猜的 previousMode。"""
    watch = ModeWatch(lambda _conversation_id: None)
    assert watch.observe("c1", "yolo") is None
    assert watch.observe("c1", "plan") == "yolo"


def test_mode_watch_note_syncs_without_comparing() -> None:
    """``note`` 只同步、不比较：``/mode`` 那条路自己已经记过事件了。"""
    watch = ModeWatch()
    watch.observe("c1", "build")
    watch.note("c1", "yolo")
    assert watch.observe("c1", "yolo") is None
    # 没有会话时什么都不做（脚本、无状态调用）
    assert watch.observe("", "plan") is None


# --------------------------------------------------------------------- 停止登记


def test_turn_control_reports_whether_a_turn_was_running() -> None:
    """``/stop`` 要能回答"当时确实有一轮在跑吗"——没有就是没有，不假装（P1-2）。"""
    turns = TurnControl()
    assert turns.request_stop("c1") is False, "没有一轮在跑时不该说'已停止'"
    turns.begin("c1")
    assert turns.running("c1")
    assert turns.request_stop("c1") is True
    assert turns.stop_requested("c1"), "标记要一直可读（流那边会问不止一次）"
    turns.end("c1")
    assert not turns.running("c1")
    assert not turns.stop_requested("c1"), "收工之后标记要清掉"


def test_turn_control_clears_a_stale_stop_on_the_next_turn() -> None:
    """上一轮留下的停止标记不许影响下一轮（``begin`` 时清掉）。"""
    turns = TurnControl()
    turns.begin("c1")
    turns.request_stop("c1")
    turns.end("c1")
    turns.begin("c1")
    assert not turns.stop_requested("c1")


def _load_one(service: CommandService, file_name: str, body: str):  # type: ignore[no-untyped-def]
    """往用户目录写一个 md 并取回它（参数替换那几条用例的共用夹具）。"""
    _write(service.user_dir, file_name.removesuffix(".md"), body)
    record = service.find(file_name.removesuffix(".md"))
    assert record is not None
    return record
