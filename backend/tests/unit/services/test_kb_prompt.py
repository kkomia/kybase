"""库级提示词生成（v0.19）。

这一组的重点不是"调通了一次模型调用"，而是**用户点名的那四条**能不能被钉住：

- 喂进去的只有文档摘要（不送全文）；
- 提示词里**不许出现摘要里没有的具体事实**；
- 写了具体事实的句子必须标 `[来源: 文件名]`；
- 引用解析出来之后，**指向不存在文件的引用要被揪出来**（那是编造的直接证据）。

前三条落在 `_META_PROMPT` 的文本上，所以这里直接断言那段文本里有没有那几条约束
——它变了却没有相应调整，这条用例就该红。第四条是纯逻辑，用假数据钉。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services import kb_prompt as module
from app.services.kb_prompt import KBPromptService, _build_user_prompt, _clean, _draft
from app.services.llm import ChatMessage

# ------------------------------------------------------------------ 元提示词的约束


def test_meta_prompt_forbids_inventing_specific_facts() -> None:
    """**不捏造**：把最容易"顺手补一个"的几类事实点名列出来，并要求宁可少写。"""
    text = module._META_PROMPT

    assert "摘要里没有的具体事实，一个字都不许写" in text
    # 这几类是模型最容易凭"领域常识"补上的，必须点名
    for category in ("数字", "阈值", "年份", "机构名", "药物名", "标准或法规编号"):
        assert category in text, category
    # 也点名了"用自己的领域知识补充"这条
    assert "你自己的领域知识" in text


def test_meta_prompt_requires_citations_and_forbids_vagueness() -> None:
    """**可溯源 + 不许把不知道的说圆**：两条都要在提示词里，缺一条就守不住。"""
    text = module._META_PROMPT

    # 引用格式要写死（v0.41 起是**编号式**：与系统提示词第 5 条同一个口径，
    # 与检索结果里的 [n] 一一对应）
    assert "[1]" in text
    assert "不要把文件名" in text
    # 旧口径必须**不再出现**——它正是"回答里塞满文件名"的来源
    assert "[来源: 文件名]" not in text
    # 「通常」「一般来说」这类措辞是把不确定性伪装成结论，必须禁
    assert "「通常」" in text and "「一般来说」" in text
    # 五节结构：没有结构就没有"专业"可言
    for section in ("角色", "资料范围", "专业口径", "回答要求", "拒答规则"):
        assert section in text, section


def test_user_prompt_lists_names_and_summaries_only() -> None:
    """喂进去的是**文件名 + 摘要**，且带上数量与截断口径（让模型知道自己看到的是节选）。"""
    body = _build_user_prompt([("d1", "指南.pdf", "讲的是干眼的诊断与分级。")])

    assert "指南.pdf" in body
    assert "讲的是干眼的诊断与分级。" in body
    assert "1 篇文档" in body
    assert str(module.MAX_SUMMARY_CHARS) in body


# ------------------------------------------------------------------ 外壳清理


def test_clean_unwraps_a_code_fence() -> None:
    """模型习惯把整段包进代码块；不拆的话用户复制到设置里会带着三个反引号。"""
    assert _clean("```markdown\n你是助手。\n```") == "你是助手。"
    assert _clean("```\n你是助手。\n```") == "你是助手。"


def test_clean_keeps_content_that_merely_contains_a_fence() -> None:
    """只拆"整段就是一个代码块"那种。**不做启发式删除**——
    把正文里出现的反引号当包装切掉，是删掉用户要的东西（删错比留一句客套话严重）。"""
    raw = "你是助手。\n\n```\n示例\n```\n\n按原文回答。"
    assert _clean(raw) == raw


# ------------------------------------------------------------------ 引用解析（溯源）


def test_draft_keeps_the_summary_list_without_claiming_attribution() -> None:
    """草稿带上"依据了哪些摘要"（供界面展示），但**不再逐篇声称它被引用**。

    v0.41 改了引用口径：原来要求模型逐句写 `[来源: 文件名]`，那时还能核对
    "它说的那几篇在不在清单里"；改成编号式引用之后逐篇归属不可知——
    所以那个字段被删掉了，而不是留一个永远为假的勾。
    """
    prompt = "回答要求：句尾标 [1] [2]，不要把文件名写进正文。"
    draft = _draft(prompt, [("d1", "共识.pdf", "摘要一"), ("d2", "另一篇.pdf", "摘要二")])

    assert draft.prompt == prompt
    assert [item.name for item in draft.sources] == ["共识.pdf", "另一篇.pdf"]
    assert not hasattr(draft, "cited_documents")
    assert draft.filename_style_citations == []


def test_draft_flags_a_prompt_that_still_demands_filenames() -> None:
    """**可验证的那一半换了对象**：现在扫的是"它还在不在要求把文件名写进正文"。

    留着旧口径的提示词会和系统提示词第 5 条打架——正文里铺一串文件名，
    正是用户报的"回答一大半都是引用"。界面据此提示核对后再保存。
    """
    prompt = "回答要求：每处具体事实后以 [来源: 共识.pdf] 标注出处。"
    draft = _draft(prompt, [("d1", "共识.pdf", "摘要")])

    assert draft.filename_style_citations == ["共识.pdf"]


def test_draft_reads_both_colon_styles() -> None:
    """中英文冒号模型两种都会写，只认一种就会漏掉一半的旧口径残留。"""
    draft = _draft("甲 [来源：共识.pdf]，乙 [来源: 另一篇.pdf]。", [("d1", "共识.pdf", "摘要")])

    assert draft.filename_style_citations == ["共识.pdf", "另一篇.pdf"]


# ------------------------------------------------------------------ 服务层


class _FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[list[ChatMessage]] = []

    def ask_raw(self, messages: list[ChatMessage], *, model_pk: str | None = None) -> str:
        self.messages.append(list(messages))
        return self.reply


def _doc(document_id: str, name: str, summary: str):  # type: ignore[no-untyped-def]
    return type("Doc", (), {"id": document_id, "name": name, "summary": summary})()


class _Meta:
    def __init__(self, docs: list) -> None:  # type: ignore[type-arg]
        self._docs = docs

    def list_documents(self, kb_id: str, *, limit: int | None = None):  # type: ignore[no-untyped-def]
        return self._docs[: limit or len(self._docs)]


class _Stores:
    def __init__(self, docs: list) -> None:  # type: ignore[type-arg]
        self.meta = _Meta(docs)


def _service(docs: list, reply: str) -> tuple[KBPromptService, _FakeChat]:  # type: ignore[type-arg]
    chat = _FakeChat(reply)
    return KBPromptService(_Stores(docs), chat), chat  # type: ignore[arg-type]


def test_generate_skips_documents_without_a_summary() -> None:
    """没摘要的文档跳过而不是报错：新旧混着是常态，先有摘要的那批先生成一版。"""
    service, chat = _service(
        [_doc("d1", "有摘要.pdf", "讲了 A。"), _doc("d2", "没摘要.pdf", "")],
        "你是资料助手。资料范围是本库收录的文档。回答要求：按原文表述，标出出处。拒答规则：资料里没有的内容直接说没有。",
    )

    draft = service.generate("kb_1")

    assert [item.name for item in draft.sources] == ["有摘要.pdf"]
    sent = chat.messages[0][1].content
    assert "有摘要.pdf" in sent
    assert "没摘要.pdf" not in sent


def test_generate_explains_that_there_is_nothing_to_work_from() -> None:
    """一条摘要都没有时，要说清**用户该做什么**，而不是抛一句通用错误。"""
    service, _ = _service([_doc("d1", "没摘要.pdf", "")], "无所谓")

    with pytest.raises(InvalidRequestError) as exc:
        service.generate("kb_1")

    assert "文档摘要" in str(exc.value)
    assert "手写" in str(exc.value)


def test_generate_rejects_an_empty_model_reply() -> None:
    """模型只回一句"好的"时不许当成草稿——那种文本存进设置里等于把提示词清空了。"""
    service, _ = _service([_doc("d1", "有摘要.pdf", "讲了 A。")], "好的")

    with pytest.raises(InvalidRequestError):
        service.generate("kb_1")


def test_generate_truncates_long_summaries() -> None:
    """单篇摘要异常长时截断：防一篇把整轮预算吃光。"""
    service, chat = _service(
        [_doc("d1", "长文.pdf", "字" * (module.MAX_SUMMARY_CHARS + 500))],
        "你是资料助手。资料范围是本库收录的文档。回答要求：按原文表述，标出出处。拒答规则：资料里没有的内容直接说没有。",
    )

    service.generate("kb_1")

    sent = chat.messages[0][1].content
    assert "字" * module.MAX_SUMMARY_CHARS in sent
    assert "字" * (module.MAX_SUMMARY_CHARS + 1) not in sent

def test_format_placeholder_is_not_reported_as_a_leftover() -> None:
    """**实测抓到的假警报**：模型在说明标注格式时会照抄 `[来源: 文件名]` 这种占位写法，
    那是"在讲格式"，不是"要求把真文件名写进正文"——不该报警。
    假警报会让用户学会忽略这个提示，于是真出问题时也没人看。
    """
    prompt = "回答要求：每处具体事实后以 [来源: 文件名] 标注出处。"
    draft = _draft(prompt, [("d1", "共识.pdf", "摘要")])

    assert draft.filename_style_citations == []
