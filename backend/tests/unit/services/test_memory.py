"""长期记忆服务（v0.14；v0.46 起是**进程内的本地实现**，不再是 ReMe 的门面）。

镜像同构：``app/services/memory.py`` → 本文件。

这里测三类事，都是"错了会静默误导模型或丢掉用户的事实"的那类：

1. **关着的时候必须报错**，不能返回空——返回空会让模型以为"没有相关记忆"，
   然后基于错误前提继续推理；
2. **记住要合并去重**，否则同一件事被记很多遍，而 MEMORY.md 每轮都要注入上下文，
   越记越长等于越记越贵；**自动沉淀也一样要去重**（同一件事不要反复写）；
3. **召回要有出处、判据要能判定"命中 vs 不命中"**：命中给文件 + 行号区间，
   噪声查询就得返回空——而不是"随便给几条看着像的"。

用假的 runtime 而不是真配置服务：这几条都是记忆自身的逻辑，
不该依赖数据库（也就能在没有 PG 时跑起来）。捕获那条链路注入一个假的"问模型"，
不连任何真实模型（``ask`` 参数就是为它留的）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import InvalidRequestError
from app.services import memory as memory_service
from app.services.archive import SECTION_ENTRY_LIMITS, SOURCE_IMPLICIT
from app.services.llm import LLMConfig
from app.services.memory import (
    AGENTS_FILE,
    CAPTURE_SIGNALS,
    CORE_MEMORY_FILE,
    PROFILE_FILE,
    SOUL_FILE,
    MemoryHit,
    MemoryService,
    _parse_lines,
    matched_signal,
)


class _FakeRuntime:
    """只实现 MemoryService 用到的那几个读接口。"""

    def __init__(self, values: dict[str, str], llm: LLMConfig | None = None) -> None:
        self._values = values
        self._llm = llm

    def get(self, key: str) -> str:
        return self._values.get(key, "")

    def get_bool(self, key: str, *, default: bool = False) -> bool:
        raw = self._values.get(key)
        if raw is None or not raw.strip():
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def get_int(self, key: str) -> int:
        """与真实实现同一口径：解析不了返回 0，由调用方回落到默认值。

        替身的形状**必须和真的一样**，否则测的是替身的行为、不是产品的——
        这条在别处踩过（假的 `Msg` 少一个字段，于是"测试绿、真调用崩"）。
        """
        try:
            return int(self.get(key))
        except ValueError:
            return 0

    def llm(self) -> LLMConfig:
        """没绑定对话模型的那个快照（``is_configured`` 为假）。

        隐式捕获那条路要用它；给一个"没配模型"的替身，才能测到那条明确报错的路径
        （真实现里这个快照来自注册表，见 ``RuntimeConfigService.llm``）。
        要测"真的走到模型通道"时（比如思考开关），构造时传一份配置好的进来。
        """
        return self._llm or LLMConfig(base_url="", api_key="", model_id="")

    def llm_for(self, model_pk: str | None) -> LLMConfig:
        """按指定模型取快照（``memory.capture_model`` 那条路）。替身里两者等价。"""
        return self.llm()


class _FakeChat:
    """假的"问一次模型"：把准备好的回答按顺序发出去，并记下收到的提示词。

    隐式捕获这条链路要测的是"拿到模型的输出之后我们做什么"（解析、写档案、回执），
    不是"模型会不会说人话"——所以这里不连任何真实模型。
    """

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.prompts: list[list[object]] = []

    def __call__(self, messages: list[object]) -> str:
        self.prompts.append(messages)
        return self._replies.pop(0) if self._replies else ""


def _service(
    tmp_path: Path, *, ask=None, llm: LLMConfig | None = None, **values: str
) -> MemoryService:
    base = {"memory.enabled": "true", "memory.workspace": "memory"}
    base.update(values)
    return MemoryService(_FakeRuntime(base, llm), tmp_path, ask=ask)  # type: ignore[arg-type]


def _write(workspace: Path, path: str, content: str) -> Path:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    # 按字节写：与产品同一条纪律（Windows 上 write_text 会翻成 CRLF）
    target.write_bytes(content.encode("utf-8"))
    return target


# --------------------------------------------------------------------- 关着时


def test_disabled_recall_raises_instead_of_returning_empty(tmp_path: Path) -> None:
    """关着时必须报错。返回空会让模型以为"没有相关记忆"，然后基于错误前提继续。

    注意**开着时**返回空是另一回事（那是"真没有"，见召回那一组）——
    这两件事现在能分得清，正是因为本地检索不依赖任何外部东西：
    关着 = 我们故意不搜，开着 = 真搜过了。
    """
    service = _service(tmp_path, **{"memory.enabled": "false"})

    with pytest.raises(InvalidRequestError, match="未启用"):
        service.recall("随便问问")


def test_remember_works_even_when_the_switch_is_off(tmp_path: Path) -> None:
    """``remember`` 不看那道闸（§7.3）：它写的是档案，而**档案编辑不看开关**
    ——"关了也能改自己的东西"这条纪律保留。

    对照：``recall`` 在关着时仍然明确报错（它代表"档案进不进这一轮的上下文"）。
    """
    service = _service(tmp_path, **{"memory.enabled": "false"})

    result = service.remember("用户偏好先给结论")
    assert result.action == "added"
    with pytest.raises(InvalidRequestError, match="未启用"):
        service.recall("偏好")


def test_archive_block_is_empty_when_disabled_instead_of_raising(tmp_path: Path) -> None:
    """注入是"有就带上"：没启用（或档案还空着）时返回空串，不让对话失败。"""
    service = _service(tmp_path, **{"memory.enabled": "false"})
    service.remember("用户偏好先给结论")

    assert service.archive_block() == ""
    assert service.archive_text(), "档案本身照旧可读可写——那道闸只管注入与 recall"


# --------------------------------------------------------------------- 记住
#
# 写入走 `archive.ArchiveService`（分区、预算、顶替、变更流），本层只做
# "门面"那部分：开关、归区、把回执原样交出去。这一组钉的是**门面这一侧**的行为。


def test_remember_creates_the_archive_with_four_fixed_sections(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.remember("用户偏好先给结论")

    assert result.action == "added"
    assert result.section == "长期偏好与风格", "归区按内容走（词表与迁移共用一处）"
    body = (tmp_path / "memory" / PROFILE_FILE).read_text(encoding="utf-8")
    for title in ("身份与称呼", "长期偏好与风格", "进行中的项目", "工具与环境"):
        assert f"## {title}" in body
    assert "- 用户偏好先给结论" in body


def test_remember_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("用户偏好先给结论")

    again = service.remember("用户偏好先给结论")

    assert again.action == "existing"
    assert "已经有了" in again.receipt
    body = (tmp_path / "memory" / PROFILE_FILE).read_text(encoding="utf-8")
    assert body.count("用户偏好先给结论") == 1


def test_remember_honours_an_explicit_section(tmp_path: Path) -> None:
    """模型看得见整份档案，所以它可以指定分区；**分区名不认识时按内容归区**
    而不是拒掉这一笔（分区名不该成为一次写入失败的原因）。"""
    service = _service(tmp_path)

    tool = service.remember("用户的内网有一台 L20", section="工具与环境")
    assert tool.section == "工具与环境"

    fallback = service.remember("用户叫小又", section="身份")  # 是旧叫法，不是分区名
    assert fallback.section == "身份与称呼"


def test_remember_replaces_in_one_call(tmp_path: Path) -> None:
    """`replaces` 让"更正"**一次调用**完成（§4.3）：旧值进变更流、档案里是新值。

    这条对整个设计很关键：没有它就退化成"先忘掉、再记住"两次，而中间那一刻
    档案里是**没有这条**的——用户如果刚好在那时问一句，答案是错的。
    """
    service = _service(tmp_path)
    service.remember("用户要求回答简短", section="长期偏好与风格")

    result = service.remember(
        "用户要求回答先给结论、再列依据",
        section="长期偏好与风格",
        replaces="用户要求回答简短",
    )

    assert result.action == "replaced"
    assert result.replaced == "用户要求回答简短"
    body = service.archive_text()
    assert "用户要求回答先给结论" in body
    assert "用户要求回答简短" not in body, "被顶替的那条不该同时留着"
    # 旧值逐字留在变更流里（可还原）
    assert "用户要求回答简短" in (tmp_path / "memory" / "changes.md").read_text(encoding="utf-8")


def test_remember_appends_after_existing_entries(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("第一条", section="长期偏好与风格")

    service.remember("第二条", section="长期偏好与风格")

    assert len(service.archive_entries()) == 2
    body = service.archive_text()
    assert body.index("- 第一条") < body.index("- 第二条")


def test_remember_rejects_content_that_is_too_long(tmp_path: Path) -> None:
    """超长**由服务层以回执拒绝**（不是抛错）：回执里要给两条出路。

    它与协议层那道 500 字的粗护栏不是一回事——那个是"别白读一遍"，这个是设计本身
    （§3.3：档案每轮进上下文，一条必须是一句话）。
    """
    result = _service(tmp_path).remember("字" * 121)

    assert result.action == "rejected"
    assert "最多 120 字" in result.receipt
    assert "AGENTS.md" in result.receipt, "要给出路"


def test_remember_rejects_sensitive_content_without_leaving_a_trace(tmp_path: Path) -> None:
    """敏感信息**连变更流都不进**：留痕本身就是泄漏（§4.2 的否决项）。"""
    result = _service(tmp_path).remember("用户的 api key 是 sk-abcdefghijklmno")

    assert result.action == "rejected"
    changes = tmp_path / "memory" / "changes.md"
    assert not changes.exists() or "sk-abc" not in changes.read_text(encoding="utf-8")


def test_remember_rejects_blank_content(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequestError, match="缺少参数"):
        _service(tmp_path).remember("   ")


def test_forget_removes_the_entry_and_keeps_a_restorable_trace(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("用户偏好先给结论", section="长期偏好与风格")

    result = service.forget("用户偏好先给结论")

    assert result.action == "forgotten"
    assert "忘掉了" in result.receipt
    assert service.archive_entries() == ()
    assert "用户偏好先给结论" in (
        tmp_path / "memory" / "changes.md"
    ).read_text(encoding="utf-8")


def test_forget_accepts_a_topic_and_refuses_to_guess(tmp_path: Path) -> None:
    """``forget`` 的入参是**话题**，所以先按原样找、再按唯一子串兜一次；
    对得上不止一条时**不猜**，把候选列回来。"""
    service = _service(tmp_path)
    service.remember("用户要求先给结论", section="长期偏好与风格")
    service.remember("项目代号叫 kylab", section="进行中的项目")

    unique = service.forget("先给结论")
    assert unique.action == "forgotten"
    assert unique.text == "用户要求先给结论"

    missing = service.forget("合唱团的排练安排")
    assert missing.action == "rejected"
    assert missing.reason == "missing"


def test_forget_refuses_an_ambiguous_topic(tmp_path: Path) -> None:
    """一句话对得上两条时列候选——删错的代价与多问一句完全不对等。"""
    service = _service(tmp_path)
    service.remember("项目代号叫 kylab", section="进行中的项目")
    service.remember("项目目标是 11 月交付", section="进行中的项目")

    result = service.forget("项目")

    assert result.action == "rejected"
    assert result.reason == "ambiguous"
    assert "kylab" in result.receipt and "11 月交付" in result.receipt
    assert len(service.archive_entries()) == 2, "一条都不许删"


# --------------------------------------------------------------------- 召回
#
# **池子只有变更流**（§5.3）：档案本身每轮已经全量注入，再召回一次就是把同一段
# 内容进两次上下文。所以这一组钉两件相反的事：**改过的事要查得到（带行号）**、
# **没改过的事与档案条目一律查不到**。

_SEED_DIGEST = """---
summary: 锂价敏感性分析
tags: [锂价]
---

# 锂价敏感性

锂价下跌 10% 会让电池业务毛利下降约 0.8 个百分点。

## 关联

- 上游见 [[碳酸锂]]
"""

_SEED_DAILY = """# 2026-09-24

- 用户偏好先给结论再给理由
- 发布前必须先跑一遍后端门禁脚本
"""

_SEED_CHANGES = """- 2026-10-01 09:20 · 新增 · 长期偏好与风格 · 来源：显式
  - 新：用户要求回答先给结论，再列依据。
- 2026-10-02 14:05 · 顶替 · 进行中的项目 · 来源：显式
  - 旧：项目代号叫 kylab。
  - 新：项目代号叫 kylab2，11 月交付。
- 2026-10-03 08:00 · 忘掉 · 工具与环境 · 来源：界面
  - 旧：用户的内网有一台 L20。
"""


def _seed(service: MemoryService) -> Path:
    """铺一个像样的工作区：一份档案 + 一份变更流（外加 daily/digest 各一份）。

    daily 与 digest 那两份**故意留着**：它们曾经是召回池，现在**代码不再消费它们**
    （文件还在磁盘上，那是用户数据）。下面有用例钉"它们不再被 recall 返回"。
    """
    workspace = service.workspace
    _write(workspace, "digest/personal/锂价.md", _SEED_DIGEST)
    _write(workspace, "daily/2026-09-24.md", _SEED_DAILY)
    _write(
        workspace,
        PROFILE_FILE,
        "---\nupdated: 2026-10-03\n---\n\n# 用户档案\n\n## 身份与称呼\n\n"
        "- 用户叫小又，称呼「小又」即可。\n\n## 长期偏好与风格\n\n"
        "- 用户要求回答先给结论，再列依据。\n",
    )
    _write(workspace, "changes.md", _SEED_CHANGES)
    return workspace


def test_recall_finds_a_changed_entry_with_a_real_line_range(tmp_path: Path) -> None:
    """命中的证据是**文件 + 行号区间**：用户拿着它就能去看那次改动。

    行号必须是**文件里的真实行号**（这一条记录的头一行），不是"记录里的第几行"。
    """
    service = _service(tmp_path)
    _seed(service)

    hits = service.recall("项目代号叫什么")

    assert hits, "这条改动就在变更流里，必须能查到"
    top = hits[0]
    assert top.path == "changes.md"
    # 第二条记录：第一条占 2 行（头 + 「新：」），所以它从第 3 行开始、到第 5 行结束
    assert (top.start_line, top.end_line) == (3, 5)
    assert "kylab2" in top.text and "11 月交付" in top.text
    assert top.score > 0


def test_recall_refuses_a_noise_query(tmp_path: Path) -> None:
    """查不到就返回空——只有一个含义：**变更流里确实没有相关的话**。"""
    service = _service(tmp_path)
    _seed(service)

    assert service.recall("合唱团的排练时间安排") == []
    assert service.recall("今天天气怎么样") == []


def test_recall_pool_excludes_archive_entries_already_injected(tmp_path: Path) -> None:
    """§9.2 第 7 条：**已经注入的档案条目不再被召回返回**。

    档案每轮整份进上下文，再召回一遍等于同一段内容进两次——而且它会让模型
    以为"这是查出来的证据"，而不是"我本来就知道的设定"。
    """
    service = _service(tmp_path)
    _seed(service)

    assert service.recall("用户叫小又，称呼「小又」即可") == [], "档案里的条目不在召回池里"

    # 反向确认：变更流里那条**确实**能查到（池子不是空的，是换了个池子）
    assert service.recall("先给结论，再列依据")


def test_recall_pool_never_reads_the_daily_or_digest_files(tmp_path: Path) -> None:
    """daily / digest 那一层**不进召回池**（§5.3）——那一层的检索已经整条退场。

    它们仍在工作区里（旧文件不删，用户数据），但 recall 只看 ``changes.md``：
    这条与上一条一起把"池子换了"钉死——不是检索坏了，是换了池子。
    """
    service = _service(tmp_path)
    _seed(service)

    assert service.recall("锂价下跌对毛利的影响") == []
    assert service.recall("发布前必须先跑一遍后端门禁脚本") == []


def test_recall_never_returns_document_pool_content(tmp_path: Path) -> None:
    """两池红线（§9.2 第 8 条）：``recall`` 的池子是**这个工作区的变更流**，
    它不 import 检索服务、不碰 pgvector、也看不到任何文档片段。"""
    service = _service(tmp_path)
    _seed(service)

    assert service.recall("知识库原文里的那段话") == []
    assert all(hit.path == "changes.md" for hit in service.recall("项目代号"))


def test_recall_clamps_the_limit(tmp_path: Path) -> None:
    """上限生效，否则会把上下文塞爆（与检索工具同一口径）。"""
    service = _service(tmp_path)
    workspace = service.workspace
    _write(
        workspace,
        "changes.md",
        "".join(
            f"- 2026-10-01 09:0{index} · 新增 · 长期偏好与风格 · 来源：显式\n"
            f"  - 新：偏好记录 {index} 号\n"
            for index in range(30)
        ),
    )

    hits = service.recall("偏好记录", limit=9999)

    assert len(hits) == 20


def test_recall_is_empty_when_there_is_no_changelog(tmp_path: Path) -> None:
    """没有变更流时返回空——那是"还没有改过什么"，不是出错。"""
    service = _service(tmp_path)
    _write(service.workspace, PROFILE_FILE, "---\nupdated: 2026-10-03\n---\n\n- 项目代号叫 kylab\n")

    assert service.recall("项目代号") == []


def test_recall_raises_when_the_query_is_empty(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequestError, match="query"):
        _service(tmp_path).recall("   ")


# --------------------------------------------------- 查证结果与工作区的形状


def test_hit_carries_its_source_lines() -> None:
    """查证结果必须带出处（``path`` + 行号）：没有出处就没法溯源、也没法去改。"""
    hit = MemoryHit(
        text="项目代号叫 kylab2，11 月交付。",
        path="changes.md",
        start_line=3,
        end_line=5,
        score=1.5,
        coverage=1.0,
    )

    assert (hit.path, hit.start_line, hit.end_line) == ("changes.md", 3, 5)


def test_status_is_local_and_counts_the_files(tmp_path: Path) -> None:
    """状态是纯本地的数字：几份文件、上次更新时间。

    **没有任何"连没连上"**（v0.46 删了）：记忆在我们的进程里跑，没有第二个进程可连。
    """
    service = _service(tmp_path)
    _seed(service)

    status = service.status()

    assert status.enabled is True
    # 档案 + 变更流 + digest + daily = 4 份（`PROFILE.md` 与 `changes.md` 是期二
    # 新写进来的两份；`daily`/`digest` 那一层还在盘上，只是代码不再消费）
    assert status.file_count == 4
    assert status.core_file_exists is False, "MEMORY.md 退场：新部署不再有这份文件"
    assert status.last_changed_at, "有文件就该有'上次更新'时间"
    assert status.detail == ""


def test_status_says_why_it_is_off_when_disabled(tmp_path: Path) -> None:
    status = _service(tmp_path, **{"memory.enabled": "false"}).status()

    assert status.enabled is False
    assert "未启用" in status.detail


# --------------------------------------------------------------- 隐式捕获（§4.1 ②）
#
# 信号命中 → 一次判定调用 → 画像双问过了才写。这一组用假模型（``ask``）测我们这
# 一侧的全部逻辑：开关、词表、解析、写档案、回执、超限与敏感否决。

#: 判定说"值得记"时模型该给的那一行（分区 + 一句话）。
_CAPTURE_REPLY = "长期偏好与风格｜用户要求以后的回答都先给结论。"

#: 一句会命中信号词的话（"以后"）。
_SIGNAL_MESSAGE = "我以后都要先看结论"


def _capturing(tmp_path: Path, reply: str, **values: str) -> tuple[MemoryService, _FakeChat]:
    """开好开关、接上假模型的捕获服务（产品默认是关的，用例里显式打开）。"""
    chat = _FakeChat(reply)
    values.setdefault("memory.capture", "true")
    return _service(tmp_path, ask=chat, **values), chat


def test_implicit_capture_off_by_default(tmp_path: Path) -> None:
    """§9.2 第 9 条：``memory.capture=false`` 时捕获路径**一次模型都不调**。

    构造直接用**产品默认值**（不是自己写一份配置）：默认值哪天翻过来，这条跟着翻
    ——手抄一份的话，测的就是测试自己写的配置了。
    """
    from app.services.runtime_config import DEFAULTS

    assert DEFAULTS["memory.capture"] == "false", "隐式捕获必须默认关"

    chat = _FakeChat(_CAPTURE_REPLY)
    service = _service(tmp_path, ask=chat, **{"memory.capture": DEFAULTS["memory.capture"]})

    assert service.capture_enabled is False
    assert service.capture_implicit(_SIGNAL_MESSAGE) is None
    assert chat.prompts == [], "关着时连信号词都不看"
    assert service.archive().read().entries == ()


def test_no_signal_word_means_no_call(tmp_path: Path) -> None:
    """**不在无信号轮次调用**：词表没命中就连判定都不发起（§4.1 的机械前置筛）。"""
    service, chat = _capturing(tmp_path, _CAPTURE_REPLY)

    assert service.capture_implicit("帮我把这段改短一点") is None
    assert chat.prompts == [], "没信号的一轮一次都不该问模型"

    # 命中之后才问，而且**同一轮里只问一次**
    assert service.capture_implicit(_SIGNAL_MESSAGE) is not None
    assert len(chat.prompts) == 1


def test_capture_signal_table_is_a_flat_literal_lookup() -> None:
    """词表是一张**扁平的字面表**（可调、可读、可被真实使用推翻，§10 第 5 条）。"""
    assert matched_signal("我以后都要这样") == "以后"
    assert matched_signal("记住：别用 emoji") in {"记住", "别"}
    assert matched_signal("帮我看一下这个文件") == ""
    # 设计文档点名的那几个词都在（词表改动要能一眼对上文档）
    for word in ("记住", "以后", "下次都", "每次都", "别", "不要", "目标是", "必须是"):
        assert word in CAPTURE_SIGNALS


def test_parse_capture_reads_labels_bullets_and_drops_prose() -> None:
    """解析的宽容与严格各一半（模型随时会飘一点）。

    宽：项目符号、围栏、全角/半角竖线都认；分区名写错也认（归区有机械词表兜底）。
    严：**散文一律丢掉**——写进档案的东西每轮都在收税，而模型回一句
    「这一轮没有值得记的」正是要我们别写。
    """
    assert _parse_lines("长期偏好与风格｜用户要求先给结论。", limit=2) == [
        ("长期偏好与风格", "用户要求先给结论。")
    ]
    assert _parse_lines("```\n- 工具与环境|用户的内网有一台 L20。\n```", limit=2) == [
        ("工具与环境", "用户的内网有一台 L20。")
    ]
    # 分区名认不出来：正文照收，归区交给机械词表
    assert _parse_lines("我猜的分区｜用户喜欢简短回答。", limit=2) == [("", "用户喜欢简短回答。")]
    # 散文（既没有项目符号、也没有「｜」）：丢掉，不猜
    assert _parse_lines("这一轮没有值得记的。", limit=2) == []
    assert _parse_lines("好的，我整理如下：\n没有。", limit=2) == []
    # 上限：一次最多两条（隐式捕获那条路的"宁可漏不可滥"）
    five = "\n".join(f"长期偏好与风格｜第 {i} 条。" for i in range(5))
    assert len(_parse_lines(five, limit=2)) == 2


def test_implicit_capture_writes_into_the_archive_with_a_receipt(tmp_path: Path) -> None:
    """开着时真 mock 模型跑一句「我以后都要…」：**被捕获、进档案、有回执**。

    三条一起钉：分区按模型给的走、来源记成**隐式**、回执与显式那条路**同一份文案**
    （§4.4：模型从工具听到的、人在界面上看到的、这一句，必须是一句话）。
    """
    service, chat = _capturing(tmp_path, _CAPTURE_REPLY)

    outcome = service.capture_implicit(_SIGNAL_MESSAGE)

    assert outcome is not None
    assert outcome.signal == "以后"
    assert [item.action for item in outcome.results] == ["added"]
    assert outcome.receipt == "记下了：用户要求以后的回答都先给结论。"
    assert chat.prompts and "我以后都要先看结论" in str(chat.prompts[0])

    entries = service.archive().read().entries
    assert [(item.section, item.text) for item in entries] == [
        ("长期偏好与风格", "用户要求以后的回答都先给结论。")
    ]
    assert service.archive().changes()[0].source == SOURCE_IMPLICIT


def test_implicit_capture_takes_a_wrong_section_name_and_falls_back(tmp_path: Path) -> None:
    """分区名写错（模型偶尔会）**不该让一条真事实落不了盘**：按内容机械归区兜底。"""
    service, _chat = _capturing(tmp_path, "- 我猜到了｜用户喜欢简短回答。")

    outcome = service.capture_implicit(_SIGNAL_MESSAGE)

    assert outcome is not None
    assert [(item.section, item.text) for item in service.archive().read().entries] == [
        ("长期偏好与风格", "用户喜欢简短回答。")
    ]


def test_implicit_capture_does_not_consult_the_memory_switch(tmp_path: Path) -> None:
    """两个开关**各管一件事**：``memory.enabled`` 关着时隐式捕获照写。

    与 `remember` 同一条纪律（"关了也能改自己的东西"）：注入停不停，与自动写入
    能不能发生，是两件事——合成一个判据的后果是"关掉注入之后，用户显式打开的
    自动记也不明不白地停了"。
    """
    service, chat = _capturing(tmp_path, _CAPTURE_REPLY, **{"memory.enabled": "false"})

    outcome = service.capture_implicit(_SIGNAL_MESSAGE)

    assert outcome is not None and outcome.results
    assert len(chat.prompts) == 1
    assert service.archive().read().entries


def test_implicit_capture_writes_nothing_when_the_judgement_says_no(tmp_path: Path) -> None:
    """**画像双问皆否（或三条否决命中）时模型什么都不输出**：档案一个字不动。

    空结果要能与"没信号"分得开（那一种返回 ``None``）：前者是判定跑过了、
    后者是根本没跑——调用方靠这个决定要不要在过程面上提这件事。
    """
    service, chat = _capturing(tmp_path, "")

    outcome = service.capture_implicit("记住：今天先把这段改短")

    assert outcome is not None and outcome.results == ()
    assert outcome.receipt == ""
    assert len(chat.prompts) == 1, "判定确实跑过一次"
    assert service.archive().read().entries == ()
    assert service.archive().changes() == []


def test_implicit_capture_over_budget_is_rejected_with_a_way_out(tmp_path: Path) -> None:
    """超限被拒时回执**给出两条出路**（§4.4），而且档案**逐字节不变**（§9.2 第 3 条）。"""
    service, _chat = _capturing(tmp_path, _CAPTURE_REPLY)
    for index in range(SECTION_ENTRY_LIMITS["长期偏好与风格"]):
        service.remember(f"用户偏好第 {index} 条。", section="长期偏好与风格")
    before = service.archive_text()

    outcome = service.capture_implicit(_SIGNAL_MESSAGE)

    assert outcome is not None
    assert outcome.results[0].action == "rejected"
    assert "满了" in outcome.receipt and "删一条" in outcome.receipt
    assert service.archive_text() == before


def test_implicit_capture_never_writes_sensitive_content(tmp_path: Path) -> None:
    """敏感信息那条否决**绝不写**，而且变更流里一条痕都不留（留痕本身就是泄漏）。"""
    service, _chat = _capturing(tmp_path, "工具与环境｜用户的密钥是 sk-abcdef123456。")

    outcome = service.capture_implicit("记住：我的密钥是 sk-abcdef123456")

    assert outcome is not None
    assert outcome.results[0].action == "rejected"
    assert "不进档案" in outcome.receipt
    assert service.archive().read().entries == ()
    assert service.archive().changes() == []
    assert "sk-abcdef123456" not in service.archive_text()


def test_implicit_capture_reports_a_missing_model(tmp_path: Path) -> None:
    """没配模型时**明确报错**，不静默什么都不做（调用方把它吞成日志）。"""
    service = _service(tmp_path, **{"memory.capture": "true"})

    with pytest.raises(InvalidRequestError, match="没有可用的对话模型"):
        service.capture_implicit(_SIGNAL_MESSAGE)


def test_default_config_makes_zero_extra_model_calls_per_turn(tmp_path: Path) -> None:
    """§9.2 第 1 条：**默认配置下，一轮对话的模型调用次数与记忆无关**。

    注入、显式写入（``remember``/``forget``）、``recall`` 都不新增调用——
    它们要么是纯本地读写，要么本来就在当轮的工具循环里；而唯一会自动花钱的
    隐式捕获**默认关**（§9.2 第 9 条，见上面那条）。

    这一条**按产品默认值构造**（从 ``DEFAULTS`` 取 memory.* 那几个键），
    而不是自己发明一份配置：默认值一改，这条就会跟着改口径；要是手抄一份，
    测的就是测试自己写的配置了。

    顺带钉住 v0.56 的默认值：``memory.enabled`` 默认 **true**、
    ``memory.persona_files`` 只剩两份人设（档案不在那份清单里）。
    """
    from app.services.runtime_config import DEFAULTS

    assert DEFAULTS["memory.enabled"] == "true"
    assert DEFAULTS["memory.persona_files"] == "SOUL.md,AGENTS.md"
    assert DEFAULTS["memory.capture"] == "false"
    assert DEFAULTS["memory.capture_model"] == ""

    defaults = {key: value for key, value in DEFAULTS.items() if key.startswith("memory")}
    chat = _FakeChat()  # 每被调用一次就往 `prompts` 里记一笔
    service = MemoryService(_FakeRuntime(defaults), tmp_path, ask=chat)  # type: ignore[arg-type]

    # 一轮对话里与记忆有关的那几件事，一件不落
    service.seed_persona()
    service.archive_block()
    service.guidance()
    service.bootstrap_block()
    service.remember("用户要求先给结论", section="长期偏好与风格")
    service.recall("先给结论")
    service.archive_block()
    service.forget("先给结论")

    assert chat.prompts == [], "这几件事一件都不该问模型"

    # 自动写入只剩隐式这一条路，而它默认关：连信号词都不用看就返回
    assert service.capture_implicit("记住：我以后都要先给结论") is None
    assert chat.prompts == []


# ------------------------------------------------------------- 整理初稿（§8.3）
#
# 用户显式点一次才发生的一次模型整理：把迁移草稿改写成"主语是用户"的一句话并给
# 归区建议。**它只给建议**——落盘走 `remember` 那条正规的路（预算/顶替/变更流/回执
# 全是同一套），于是这一次模型调用不可能绕过 §3.3–§3.4 的任何一条。


def test_organize_draft_only_suggests_and_writes_nothing(tmp_path: Path) -> None:
    service = _service(tmp_path, ask=_FakeChat("长期偏好与风格｜用户要求先给结论。"))
    _write(service.workspace, "import-draft.md", "# 草稿\n\n- 先给结论\n- 说话别绕\n")

    items = service.organize_draft()

    assert [(item.section, item.text) for item in items] == [
        ("长期偏好与风格", "用户要求先给结论。")
    ]
    assert service.archive().read().entries == (), "预览阶段一个字都不写"
    assert service.archive().changes() == []


def test_organize_draft_says_so_when_the_model_gives_nothing(tmp_path: Path) -> None:
    """模型没给出可用条目时**如实报错**：这一次是花过钱的，静默返回空像"点了没反应"。"""
    service = _service(tmp_path, ask=_FakeChat("这一轮没有值得留下的。"))
    _write(service.workspace, "import-draft.md", "- 先给结论\n")

    with pytest.raises(InvalidRequestError, match="没有给出可用的条目"):
        service.organize_draft()


def test_organize_draft_needs_a_draft(tmp_path: Path) -> None:
    service = _service(tmp_path, ask=_FakeChat("长期偏好与风格｜用户要求先给结论。"))

    with pytest.raises(InvalidRequestError, match="没有可整理的条目"):
        service.organize_draft()


# --------------------------------------------------------------------- 注入


def test_archive_block_carries_every_entry_verbatim(tmp_path: Path) -> None:
    """§9.2 第 2 条：**档案每个条目都出现在注入块里**（逐条比对，不做抽样）。

    注入是"全量、不挑选、不摘要、不排序"（§5.1）——挑一条漏一条的表现是
    "我写进去了它却不知道"，而那是最难查的一类。
    """
    service = _service(tmp_path)
    entries = [
        service.remember("用户叫小又，称呼「小又」即可。", section="身份与称呼").text,
        service.remember("用户要求回答先给结论，再列依据。", section="长期偏好与风格").text,
        service.remember("项目目标是把知识库放进内网。", section="进行中的项目").text,
        service.remember("用户的内网有一台 L20。", section="工具与环境").text,
    ]

    block = service.archive_block()

    for item in entries:
        assert f"- {item}" in block, item
    # 四个分区按 §3.1 的固定顺序出现
    titles = ("身份与称呼", "长期偏好与风格", "进行中的项目", "工具与环境")
    positions = [block.index(f"## {title}") for title in titles]
    assert positions == sorted(positions)
    # 边界说明也在（§5.1）：少了它，模型会把档案当文献引用或当任务逐条念
    assert "不是文献依据" in block


def test_archive_block_is_empty_for_an_empty_archive(tmp_path: Path) -> None:
    """一条都没有时不注入空壳：四行标题加一句"这是你的档案"只占上下文。"""
    assert _service(tmp_path).archive_block() == ""


def test_archive_block_declares_truncation_instead_of_truncating_silently(
    tmp_path: Path,
) -> None:
    """§5.2：超限**必须在提示词里说出来**，而且只给前 N 条。

    正常路径碰不到它（写入侧 4000 字就拒了）——它兜的是"用户拿外部编辑器把档案
    改超了"这一态。**静默截断不可接受**：用户会以为助手看到了整份档案。
    """
    service = _service(tmp_path)
    body = "\n".join(f"- 第 {index} 条记录" + "字" * 100 for index in range(80))
    _write(
        service.workspace,
        PROFILE_FILE,
        f"---\nupdated: 2026-10-03\n---\n\n## 身份与称呼\n\n{body}\n",
    )

    block = service.archive_block()

    assert "超出上限" in block
    assert "请到记忆页整理" in block
    # 声明里报的条数要与实际给出的条数一致（不然它是一句假话）
    kept = block.count("- 第 ")
    assert f"前 {kept} 条" in block


def test_archive_block_does_not_depend_on_the_persona_list(tmp_path: Path) -> None:
    """**档案的注入不挂在 ``memory.persona_files`` 上**（§7.2）。

    挂在那一行上的后果很具体：用户从人设清单里删掉一个文件名，档案就静默停止注入
    ——而那条清单管的是"人设的取舍与顺序"。
    """
    service = _service(tmp_path, **{"memory.persona_files": "AGENTS.md"})
    service.remember("用户叫小又", section="身份与称呼")

    assert "用户叫小又" in service.archive_block()
    assert PROFILE_FILE not in service.persona_order()


def test_archive_block_is_empty_when_disabled_but_the_archive_stays_writable(
    tmp_path: Path,
) -> None:
    """关掉开关就**不注入**（§7.3）——而档案本身照旧可读可写（口径要分清）。"""
    service = _service(tmp_path, **{"memory.enabled": "false"})
    service.remember("用户叫小又", section="身份与称呼")

    assert service.archive_block() == ""
    assert "用户叫小又" in service.archive_text()

    service_on = _service(tmp_path, **{"memory.enabled": "true"})
    assert "用户叫小又" in service_on.archive_block()


# ------------------------------------------------- 铺模板并且不被记忆挤掉


def test_the_archive_is_written_as_the_four_section_skeleton(tmp_path: Path) -> None:
    """第一条写进来时，档案要有**四个固定分区的骨架**（§3.1）。

    为什么这条重要：用户拿编辑器打开这份文件时，看到的是四个标题而不是一份
    需要猜结构的散文；模型看到的顺序与用户看到的顺序因此是同一个。
    """
    service = _service(tmp_path)

    service.remember("对方偏好简短的答复", section="长期偏好与风格")

    body = (tmp_path / "memory" / PROFILE_FILE).read_text(encoding="utf-8")
    for title in ("身份与称呼", "长期偏好与风格", "进行中的项目", "工具与环境"):
        assert f"## {title}" in body
    assert "- 对方偏好简短的答复" in body
    # frontmatter 只放 `updated`（§3.5 第 1 条）
    assert body.startswith("---\nupdated: ")
    assert "summary:" not in body


def test_remember_does_not_rewrite_line_endings(tmp_path: Path) -> None:
    """写档案要**按字节写**：`write_text` 在 Windows 上会把换行改成 CRLF。

    后果不只是"文件变脏"：这份文件每次都在被读、被比对（顶替判据、注入），
    换行在多轮之间来回翻会让逐字节比较与哈希都不稳定——而"顶替"的判据正是
    逐字节的。
    """
    service = _service(tmp_path)
    service.remember("第一条", section="长期偏好与风格")

    raw = (tmp_path / "memory" / PROFILE_FILE).read_bytes()

    assert b"\r\n" not in raw
    changes = (tmp_path / "memory" / "changes.md").read_bytes()
    assert b"\r\n" not in changes


def test_capture_does_not_rewrite_line_endings(tmp_path: Path) -> None:
    """隐式捕获写档案，同一条纪律：**按字节写**。

    `write_text` 在 Windows 上会把换行改成 CRLF，而这份文件每次都在被读、被比对
    （顶替判据、注入、逐字节的还原），换行来回翻会让那些判据都不稳定。
    """
    service, _chat = _capturing(tmp_path, _CAPTURE_REPLY)

    service.capture_implicit(_SIGNAL_MESSAGE)

    assert b"\r\n" not in (tmp_path / "memory" / PROFILE_FILE).read_bytes()
    assert b"\r\n" not in (tmp_path / "memory" / "changes.md").read_bytes()


def test_untouched_legacy_templates_are_upgraded(tmp_path: Path) -> None:
    """**还在用旧模板的实例要能看到新模板**，改过的文件一个字也不动。

    v0.21 把三份模板换成了 QwenPaw 那套有内容的写法，而 `seed_persona` 的原则是
    "已存在的一律不动"——照字面执行的话，已经在跑的部署永远看不到新模板，
    除非用户自己去删文件（而他并不知道该删）。v0.56 的 `PROFILE.md` 又是一次
    换形状（散文 → 四区骨架），同一个问题再来一遍。

    判据是**逐字节相同**：那才是"我们写下去之后没人动过"。所以第三条断言
    （只改了一个标题的文件）与第二条（真正的旧模板）必须表现不同——
    这是"宁可漏升级、不可误覆盖"的落点。
    """
    from app.services.memory import _LEGACY_TEMPLATES

    workspace = tmp_path / "memory"
    workspace.mkdir(parents=True)
    (workspace / SOUL_FILE).write_bytes(_LEGACY_TEMPLATES[SOUL_FILE][0].encode("utf-8"))
    (workspace / AGENTS_FILE).write_bytes(
        _LEGACY_TEMPLATES[AGENTS_FILE][0].replace("## 工作方式", "## 我自己的工作方式").encode()
    )
    # 散文体的 PROFILE.md（v0.21–v0.55 的模板）也要被换成四区骨架
    (workspace / PROFILE_FILE).write_bytes(_LEGACY_TEMPLATES[PROFILE_FILE][1].encode("utf-8"))
    service = _service(tmp_path)

    service.seed_persona()

    # 旧模板 → 换成新的（有内容的那份）
    assert "真心帮忙" in (workspace / SOUL_FILE).read_text(encoding="utf-8")
    # 动过一个字的 → 原样不动
    assert "我自己的工作方式" in (workspace / AGENTS_FILE).read_text(encoding="utf-8")
    # 散文体资料 → 换成档案骨架（用户一个字都没动过）
    assert "## 身份与称呼" in (workspace / PROFILE_FILE).read_text(encoding="utf-8")
    # 幂等：第二次没有可升级的
    assert service.seed_persona() == []


# ------------------------------------------------- 铺模板（v0.56：三份，不含 MEMORY.md）
#
# 容器里"长期记忆不生效"里属于**文件那一半**的验收是：新实例起来后
# 「记忆」页就可写、重启后内容还在。所以模板要一次性铺全、幂等、
# 且**不看 memory.enabled**——那开关管的是注入与 recall，不是这几个文件。


def test_seed_persona_lays_down_the_three_files(tmp_path: Path) -> None:
    """三份都铺：SOUL / AGENTS / **PROFILE.md（= 档案）**。

    ``MEMORY.md`` **不再铺**（§7.2 退场：它不再注入、不再写入、也不再被播种）——
    新部署的「记忆」页上看的是这份四区档案，而不是一份"已知事实"的旧文件。
    """
    service = _service(tmp_path)

    created = service.seed_persona()

    assert sorted(created) == sorted([SOUL_FILE, PROFILE_FILE, AGENTS_FILE])
    assert not (tmp_path / "memory" / CORE_MEMORY_FILE).exists()
    # 幂等：第二次一个都不新建
    assert service.seed_persona() == []
    body = (tmp_path / "memory" / PROFILE_FILE).read_text(encoding="utf-8")
    for title in ("身份与称呼", "长期偏好与风格", "进行中的项目", "工具与环境"):
        assert f"## {title}" in body


def test_the_archive_is_seeded_even_when_the_switch_is_off(tmp_path: Path) -> None:
    """**关着也铺**：容器里"记忆页可写"不该依赖用户先去设置页把开关打开
    （浏览与编辑那几个文件本来就不走那道闸，见 ``services/memory.py`` 的说明）。"""
    service = _service(tmp_path, **{"memory.enabled": "false"})

    created = service.seed_persona()

    assert PROFILE_FILE in created
    assert (tmp_path / "memory" / PROFILE_FILE).exists()


def test_remember_writes_into_the_seeded_archive(tmp_path: Path) -> None:
    """先铺骨架、再记住：第一条落进对应分区，四个标题一个不少。

    （旧版本这条钉的是"``_write_entries`` 的兜底与模板逐字一致"；期二
    ``MEMORY.md`` 与那套重写逻辑一起退场，剩下的性质是"播种的骨架与之后
    写入的档案是同一个形状"——由 ``archive_files.render_archive`` 一个渲染器保证。）
    """
    service = _service(tmp_path)
    service.seed_persona()

    service.remember("设备名是 nas", section="工具与环境")

    body = (tmp_path / "memory" / PROFILE_FILE).read_text(encoding="utf-8")
    assert "- 设备名是 nas" in body
    assert "## 工具与环境" in body and "## 身份与称呼" in body
    # 写完之后骨架不再是"没人动过"（首次引导据此自己消失）
    assert service.profile_is_untouched() is False


# ------------------------------------------------------------------ 记忆指导


def test_guidance_is_empty_when_the_switch_is_off(tmp_path: Path) -> None:
    """**关着时不给指导**：那时 `recall` 会明确报错，把"什么时候该去查"
    讲给模型听，只会换来每轮一次无效调用加一句错误。

    这与知识库那一侧是同一个形状（见 `agent_tools._KB_TOOLS` 的说明：给了又拒，
    白花两个来回）——那边已经踩过一次，记忆这一侧别再踩。
    """
    off = _service(tmp_path, **{"memory.enabled": "false"})
    on = _service(tmp_path)

    assert off.guidance() == ""
    assert on.guidance() != ""


def test_guidance_says_the_archive_is_already_injected(tmp_path: Path) -> None:
    """指导里必须有一句"**档案已经全量注入了，不必再去检索它**"（§5.3）。

    旧文案教的是"问偏好时先 `recall`"——那句话让模型白花一次调用，还会把
    同一段内容读两遍；而它现在也**查不到**档案（池子只剩变更流）。
    """
    text = _service(tmp_path).guidance()

    assert "全量" in text
    assert "变更流" in text
    assert "recall" in text and "remember" in text and "forget" in text
    # 字数与条数从 archive 的常量取，不写死第二份
    assert "120" in text and "60" in text and "4000" in text


def test_guidance_points_at_replaces_instead_of_delete_then_write(tmp_path: Path) -> None:
    """更正要**一次调用**（`replaces`），指导里必须这么说。

    不写的话，"不是 A，是 B"会退化成两次调用：先忘掉、再记住——中间那一刻
    档案里是没有这条的（用户刚好在那时问一句，答案就是错的）。
    """
    text = _service(tmp_path).guidance()

    assert "replaces" in text
    assert "不要先删再记" in text


# ------------------------------------------------------------------ 首次引导


def test_bootstrap_shows_while_the_archive_is_still_the_skeleton(tmp_path: Path) -> None:
    """档案还是空骨架时给引导，**写进第一条之后它自己就没了**。

    这是"感觉不到人设"的正面解法：新装的档案是空的，而**没有任何机制会让它被填上**
    ——用户不会主动去改一份档案文件（他甚至不知道有这回事），Agent 也不会问。
    QwenPaw 用一份"用完就删"的 BOOTSTRAP.md 解决；我们用**没有这个问题的等价信号**
    （"档案还没被写过"），于是不需要"用过就删"的簿记。
    """
    service = _service(tmp_path)
    service.seed_persona()

    assert service.bootstrap_block() != ""

    service.remember("用户叫小又", section="身份与称呼")

    assert service.bootstrap_block() == ""


def test_bootstrap_tells_the_agent_how_to_write_it_down(tmp_path: Path) -> None:
    """引导里必须写清"答案往哪儿放"——不然模型问完就忘了。

    期二把落点从 `write_memory`（整份覆盖，已退场）换成了 **`remember` 一条一条写**
    （§7.4），而这条用例跟着换口径：点名 `remember` 与三个分区名，
    并**明确不许写 `write_memory`**——老提示词里那个名字已经不管用了。
    """
    service = _service(tmp_path)
    service.seed_persona()

    text = service.bootstrap_block()

    assert "remember" in text
    assert "身份与称呼" in text and "进行中的项目" in text and "长期偏好与风格" in text
    assert "write_memory" not in text
    assert "PROFILE.md" in text and "SOUL.md" in text


def test_bootstrap_does_not_depend_on_the_memory_switch(tmp_path: Path) -> None:
    """关着长期记忆也要引导：档案的编辑本来就不受那道闸管
    （挂在开关上的话，关着记忆的实例永远不做引导）。"""
    service = _service(tmp_path, **{"memory.enabled": "false"})
    service.seed_persona()

    assert service.bootstrap_block() != ""


def test_bootstrap_is_silent_without_a_profile_file(tmp_path: Path) -> None:
    """文件不存在时**不说话**：那多半是还没铺模板，这时判"没填过"等于把引导
    挂在一个不存在的对象上。"""
    assert _service(tmp_path).bootstrap_block() == ""


# ------------------------------------ 人设文件：注入哪几份、按什么顺序（v0.51）
#
# 照 QwenPaw 的 ``system_prompt_files``：那几份文件每轮整份进 system prompt，
# 所以"哪几份、什么顺序"是用户的设定，不该由代码钉死。


def test_persona_order_defaults_to_the_documented_order(tmp_path: Path) -> None:
    """没配就是那份默认表：**人格 → 规程**（§7.2 起只有这两份）。

    ``PROFILE.md``（= 档案）走独立贡献者，``MEMORY.md`` 已退场——
    它们都不在这份清单里，用户也就不可能"从清单里删掉一个名字，档案静默停止注入"。
    """
    service = _service(tmp_path)

    assert service.persona_order() == (SOUL_FILE, AGENTS_FILE)


def test_persona_order_follows_the_setting(tmp_path: Path) -> None:
    """用户能决定哪两份、按什么顺序进提示词；``persona_texts`` 跟着这个顺序读。"""
    service = _service(tmp_path, **{"memory.persona_files": "AGENTS.md, SOUL.md"})
    _write(service.workspace, SOUL_FILE, "我的人格")
    _write(service.workspace, AGENTS_FILE, "操作规程")

    assert service.persona_order() == (AGENTS_FILE, SOUL_FILE)
    assert [name for name, _text in service.persona_texts()] == [AGENTS_FILE, SOUL_FILE]


def test_persona_order_takes_a_full_width_comma_too(tmp_path: Path) -> None:
    """中文输入法下逗号是全角的——**那不是用户的错**，别让他配了没反应。"""
    service = _service(tmp_path, **{"memory.persona_files": "SOUL.md，AGENTS.md"})

    assert service.persona_order() == (SOUL_FILE, AGENTS_FILE)


def test_persona_order_drops_names_it_does_not_know(tmp_path: Path) -> None:
    """只认那几份核心文件，其余丢掉：**让任意路径进 system prompt 等于绕过分界**
    （哪些是"设定"）。想去掉重复的名字也在这里收掉。"""
    service = _service(
        tmp_path, **{"memory.persona_files": "SOUL.md, daily/x.md, SOUL.md, 笔记.md"}
    )

    assert service.persona_order() == (SOUL_FILE,)


def test_persona_order_drops_profile_and_memory(tmp_path: Path) -> None:
    """**``PROFILE.md`` 与 ``MEMORY.md`` 从清单里被丢掉**（§7.2、v0.56）。

    老配置里这一行八成还写着四个名字（默认值改之前就是那样）：
    - 档案挂上去的后果是**用户删一个名字、档案就静默停止注入**；
    - ``MEMORY.md`` 已经退场，再注入一遍就是把同一件事说两次。
    两者都是"名字写了但不生效"，所以**记一条日志**——静默忽略会让那一行看起来生效了。
    """
    service = _service(
        tmp_path, **{"memory.persona_files": "SOUL.md,PROFILE.md,AGENTS.md,MEMORY.md"}
    )

    assert service.persona_order() == (SOUL_FILE, AGENTS_FILE)


def test_persona_order_falls_back_when_nothing_is_recognised(tmp_path: Path) -> None:
    """全不认识 → 回到默认顺序：**"配错了"不该变成"一份都不注入"**。

    一份都不注入等于让 agent 突然失了人格——而用户只是打错了一个文件名。
    """
    service = _service(tmp_path, **{"memory.persona_files": "不存在的文件.md"})

    assert service.persona_order() == (SOUL_FILE, AGENTS_FILE)


def test_the_archive_block_warns_against_placeholder_words(tmp_path: Path) -> None:
    """D26 的另一半：**注入的那一段**要拦住"照着占位词去追问"。

    D26（2026-09-28 走查）：那份 `PROFILE.md` 里三行写着「待确认」，于是**每一轮**
    注入之后模型都把它当成"还没做完的事"，见面就问"怎么称呼你"——那天 17 条新会话
    里 14 条都出现了这种追问。

    规则在期二搬进了**档案块**：病灶那几行字现在住在档案里，规则就该跟着它走
    （写在人设总起句里会让那一段去讲一份它已经不管的文件）。模板本身不再带提示语
    ——它是四区骨架，用户要填的是自己的话，不是我们的说明。
    """
    service = _service(tmp_path)
    _write(
        service.workspace,
        PROFILE_FILE,
        "---\nupdated: 2026-10-03\n---\n\n## 身份与称呼\n\n- **怎么称呼他：** 待确认\n",
    )

    block = service.archive_block()

    assert "没填的字段就当没填" in block
    assert "追问" in block
    # 点名那几个占位词：不点名的话，模型未必把「待补」也当成同一类
    assert "待确认" in block and "待补" in block
    # 骨架里只有四个标题与 frontmatter，没有我们的提示语混进去
    assert "待确认" not in memory_service._PROFILE_TEMPLATE
