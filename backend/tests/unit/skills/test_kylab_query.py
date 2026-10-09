"""官方 Skill 的查询脚本（T4.8 后半）。

镜像同构：``skills/kylab-knowledge-base/scripts/kylab_query.py`` → 本文件。

**为什么脚本也要测**：它是这个 Skill 面向"不会说 MCP 的 Agent"的那条路径，
而脚本的错误**全部表现为一段奇怪的终端输出**——没有 HTTP 状态码可看、
没有界面可查。所以把它的三条契约钉住：

1. **空结果不是错误**（退出码 0），但要说清意味着什么、下一步看哪里；
2. **401 有独立的退出码**——它是调用方唯一能自己修的那类失败，
   混进"没结果"里就白费了；
3. **输出的是原文片段而不是回答**——那是 kylab 的价值所在，
   一个把片段总结掉的脚本等于把这个 Skill 做没了。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "skills"
    / "kylab-knowledge-base"
    / "scripts"
    / "kylab_query.py"
)


def _load():  # type: ignore[no-untyped-def]
    """按文件路径加载脚本。

    它不是 backend 包的一部分（是交付给别人装的 Skill），所以不能被 import，
    只能用 spec 加载。
    """
    spec = importlib.util.spec_from_file_location("kylab_query", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():  # type: ignore[no-untyped-def]
    return _load()


HITS = {
    "hits": [
        {
            "document_id": "doc_1",
            "document_name": "眼轴共识.pdf",
            "text": "眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一。",
            "score": 0.8412,
            "page": 3,
            "heading_path": "二、测量方法",
        },
        {
            "document_id": "doc_2",
            "document_name": "近视防控指南.md",
            "text": "持续监测眼轴有助于判断近视进展情况。",
            "score": 0.71,
            "page": None,
            "heading_path": None,
        },
    ],
    "filtered_out": 2,
}


# --------------------------------------------------------------------- 输出


def test_prints_passages_not_a_summary(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """**必须打印原文片段。**

    把它总结成一段回答就等于把这个 Skill 做没了——用户要的正是可核对的原文。
    """
    script._print_hits(HITS, query="眼轴怎么监测", as_json=False)

    out = capsys.readouterr().out
    assert "眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一。" in out
    assert "持续监测眼轴有助于判断近视进展情况。" in out


def test_includes_traceable_location(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """出处必须能追回去：文档名是底线，页码与标题路径有就带上。"""
    script._print_hits(HITS, query="眼轴", as_json=False)

    out = capsys.readouterr().out
    assert "眼轴共识.pdf" in out
    assert "第 3 页" in out
    assert "二、测量方法" in out


def test_reports_filtered_out(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """被过滤掉的条数要说出来，否则用户会以为检索只找到这么点。"""
    script._print_hits(HITS, query="眼轴", as_json=False)

    assert "2 段被过滤" in capsys.readouterr().out


def test_empty_result_explains_what_it_means(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """**空结果不是错误**，但光说"没找到"没有用——
    要说清它意味着什么、以及下一步该看哪里（多半是文档还没处理完）。
    """
    script._print_hits({"hits": [], "filtered_out": 0}, query="不存在的词", as_json=False)

    out = capsys.readouterr().out
    assert "没有找到" in out
    assert "indexed" in out, "要提示未处理完的文档搜不到"


def test_no_knowledge_base_says_so(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """一个库都没有时，要说"还没有知识库"，而不是"没找到相关内容"——
    后者会把用户引到"换个词再搜"，而问题在于库里根本没东西。"""
    script._print_hits(
        {"hits": [], "filtered_out": 0, "_no_knowledge_base": True},
        query="随便",
        as_json=False,
    )

    assert "还没有任何知识库" in capsys.readouterr().out


def test_long_passage_is_truncated(script, capsys) -> None:  # type: ignore[no-untyped-def]
    """终端不是阅读器：片段过长要截断，否则一次检索刷几百行。"""
    result = {"hits": [{**HITS["hits"][0], "text": "字" * 2000}], "filtered_out": 0}

    script._print_hits(result, query="x", as_json=False)

    out = capsys.readouterr().out
    assert "…" in out
    assert len(out) < 1000


def test_json_mode_is_machine_readable(script, capsys) -> None:  # type: ignore[no-untyped-def]
    script._print_hits(HITS, query="眼轴", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"][0]["document_name"] == "眼轴共识.pdf"


# --------------------------------------------------------------------- 组合


def test_search_without_kb_ids_queries_everything(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """不传 --kb 时查全部：Agent 往往不知道有哪些库，
    逼它先列一遍是多余的一步。
    """
    calls: list[tuple[str, dict | None]] = []

    def fake_request(base_url, path, *, token, method="GET", body=None):  # type: ignore[no-untyped-def]
        calls.append((path, body))
        if path == "/knowledge-bases":
            return {"items": [{"id": "kb_1"}, {"id": "kb_2"}]}
        return HITS

    monkeypatch.setattr(script, "_request", fake_request)
    script.search("http://x", None, query="眼轴", kb_ids=[], top_k=3)

    assert calls[0][0] == "/knowledge-bases"
    assert calls[1][0] == "/search"
    assert calls[1][1] == {"query": "眼轴", "top_k": 3, "kb_ids": ["kb_1", "kb_2"]}


def test_search_passes_explicit_kb_ids(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """传了 --kb 就不该再去列全部库——那是一次多余的请求。"""
    calls: list[str] = []

    def fake_request(base_url, path, *, token, method="GET", body=None):  # type: ignore[no-untyped-def]
        calls.append(path)
        return HITS

    monkeypatch.setattr(script, "_request", fake_request)
    script.search("http://x", None, query="眼轴", kb_ids=["kb_9"], top_k=3)

    assert calls == ["/search"], "传了 kb 还去列库 = 多余请求"


def test_search_short_circuits_when_no_knowledge_base(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """一个库都没有时不必发 /search——它必然返回空，而且空白更让人困惑。"""
    calls: list[str] = []

    def fake_request(base_url, path, *, token, method="GET", body=None):  # type: ignore[no-untyped-def]
        calls.append(path)
        return {"items": []}

    monkeypatch.setattr(script, "_request", fake_request)
    result = script.search("http://x", None, query="眼轴", kb_ids=[], top_k=3)

    assert calls == ["/knowledge-bases"]
    assert result["_no_knowledge_base"] is True


# --------------------------------------------------------------------- 退出码


def test_list_returns_zero(script, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        script, "_request", lambda *a, **k: {"items": [{"id": "kb_1", "name": "产品手册"}]}
    )

    assert script.main(["--list"]) == 0
    assert "kb_1" in capsys.readouterr().out


def test_empty_list_says_so(script, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(script, "_request", lambda *a, **k: {"items": []})

    assert script.main(["--list"]) == 0
    assert "还没有知识库" in capsys.readouterr().out


def test_missing_query_is_a_usage_error(script) -> None:  # type: ignore[no-untyped-def]
    """不给 --query 也不给 --list 时 argparse 会 SystemExit(2)，
    比"默默查了空字符串"清楚得多。"""
    with pytest.raises(SystemExit) as excinfo:
        script.main([])
    assert excinfo.value.code == 2


def test_successful_query_returns_zero(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(script, "_request", lambda *a, **k: HITS)

    assert script.main(["--query", "眼轴", "--kb", "kb_1"]) == 0


def test_auth_failure_has_its_own_exit_code(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**401 要有独立退出码。**

    它是调用方唯一能自己修的那类失败（去填令牌），
    混进"没结果"里等于把唯一的线索丢了。
    """
    import urllib.error

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(script.urllib.request, "urlopen", boom)

    with pytest.raises(SystemExit) as excinfo:
        script.main(["--query", "眼轴", "--kb", "kb_1"])
    assert excinfo.value.code == 2


def test_transport_failure_exits_one(script, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """连不上后端是环境问题（退出码 1），与"要令牌"（2）分开。"""
    import urllib.error

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(script.urllib.request, "urlopen", boom)

    with pytest.raises(SystemExit) as excinfo:
        script.main(["--query", "眼轴", "--kb", "kb_1"])
    assert excinfo.value.code == 1


# --------------------------------------------------------------------- 交付形态


def test_skill_files_exist() -> None:
    """Skill 的三个产物都要在：说明书、脚本、以及给人看的 README。

    少一个都会让"装到 Agent 后能直接用"这条验收不成立。
    """
    root = SCRIPT.parents[1]
    assert (root / "SKILL.md").is_file()
    assert (root / "README.md").is_file()
    assert SCRIPT.is_file()


def test_skill_frontmatter_is_parseable() -> None:
    """``SKILL.md`` 的 YAML frontmatter 是**平台识别 Skill 的唯一依据**——
    写坏了这个 Skill 就装了也不生效，而且不会有任何报错。
    """
    text = (SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "缺少 frontmatter 起始标记"
    _, frontmatter, _body = text.split("---\n", 2)
    fields = {}
    for line in frontmatter.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    assert fields.get("name") == "kylab-knowledge-base"
    description = fields.get("description", "")
    # 描述是**给模型看的触发条件**：说清"什么时候该用"比说清"是什么"更重要，
    # 否则模型不知道该在什么场合唤起它
    assert len(description) > 80
    assert "知识库" in description


#: 说明书**刻意不提**的对外工具（与 ``_NOT_DOCUMENTED_REASON`` 一一对应）。
#:
#: ``ingest_artifact`` 要的是一份**已经导出的会话产物**，而外部 MCP 通道
#: **没有会话上下文**——它在这个服务上只可能回"产物不存在"。说明书不提它是有意的：
#: 把一条必然失败的路写进操作手册，等于教模型去踩。工具本身留着（剥离方案 §7.5
#: 的 10 个之一），失败时给的是诚实错误（``NotFoundError`` → MCP 层的 JSON ``error``）。
_NOT_DOCUMENTED: frozenset[str] = frozenset({"ingest_artifact"})

_NOT_DOCUMENTED_REASON = (
    "``ingest_artifact`` 需要会话产物，外部 MCP 通道没有会话：说明书提它只会教模型去踩"
)


def test_every_mcp_tool_is_mentioned_by_some_skill() -> None:
    """**对外 MCP 面上的每个工具都要被某一份说明书点到**，漏掉的那个等于不存在。

    为什么不是"这一份要点到全部"：技能可能不止一份（kylab 那边 Office 产出那一组
    另有 `kylab-office-export`）。把导出工具塞进"查知识库"那份说明书里，
    两边都会变糊；而"没有任何一份说明书提到它"仍然是真问题——
    模型在工具表里能看到名字，却不知道什么时候该用、有什么边界。

    **比的是 ``MCP_TOOL_NAMES`` 而不是 ``TOOL_NAMES``**（v0.1.0 收窄）：这个仓里
    只有知识库那一份技能，让它去覆盖 Agent 侧那 11 个工具既做不到、也不该做——
    那些工具根本不在对外面上。唯一与它同名的例外见 :data:`_NOT_DOCUMENTED`。
    """
    from app.mcp_server.server import MCP_TOOL_NAMES

    root = SCRIPT.parents[1].parent  # skills/
    manuals = list(root.glob("*/SKILL.md"))
    assert manuals, "一份技能都没扫到，这条用例就没意义了"
    text = "\n".join(path.read_text(encoding="utf-8") for path in manuals)

    expected = [name for name in MCP_TOOL_NAMES if name not in _NOT_DOCUMENTED]
    missing = [name for name in expected if name not in text]
    assert not missing, f"没有任何技能说明书提到这些工具：{missing}"
    assert set(MCP_TOOL_NAMES) >= _NOT_DOCUMENTED, (
        f"例外表里的名字已经不在对外面上了，该删掉：{sorted(_NOT_DOCUMENTED)}（"
        f"{_NOT_DOCUMENTED_REASON}）"
    )
    for name in _NOT_DOCUMENTED:
        assert name not in text, (
            f"{name} 已写进说明书，那就该从例外表里移走：{_NOT_DOCUMENTED_REASON}"
        )


def test_skill_does_not_claim_there_is_no_authentication() -> None:
    """**回归用例**：说明书曾写着"HTTP 传输自身没有鉴权"。

    v0.12 起每个工具调用都要带凭据（会话令牌或 API Key），那句话就成了反的——
    而误导性的文档比"没有鉴权"更危险：读到的人会以为不必配 Key，
    照着配完发现工具全部报错，却不知道该改哪里。
    """
    text = (SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "has no authentication of its own" not in text
    # 反过来要明确告诉读者怎么带凭据
    assert "KYLAB_MCP_KEY" in text
    assert "Authorization: Bearer" in text
