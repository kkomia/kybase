"""HTML 正文提取 → Markdown（M6 / T6.2）。

**目标**：把一个网页变成"能进知识库的干净正文"。难点全在"剥离噪声"——
导航栏、侧边栏、广告、评论区、页脚，这些进了库会污染检索：
用户问什么都能匹配到导航菜单里的"首页 关于 联系方式"。

**做法（不引第三方库）**：两遍扫描。

1. **第一遍**收集所有块级候选元素及其文本长度，**按"文本密度"打分**
   （文本长度 ÷ 标签数）。正文区通常文字多、标签少；导航栏正好相反。
   ```
   <div><a>首页</a><a>关于</a></div>      标签多、文字少 → 低分
   <article><p>很长的一段正文……</p></article>  文字多、标签少 → 高分
   ```
2. **第二遍**只把最高分那棵子树的文本转成 Markdown。

**为什么不引 readability/markdownify**：本项目已经有一整套解析器抽象
（``parsers/``），再加两个重依赖只为处理"网页正文"这一种格式，不划算。
这里的实现覆盖常见语义标签（``article`` / ``main`` / ``p`` / ``h1-6`` /
``ul`` / ``ol`` / ``pre`` / ``blockquote``），够用且**行为完全可测**。

**明确的取舍**：不处理 JS 渲染的页面（那需要无头浏览器），也不追求
100% 还原排版。抓不到正文时**退回整个 body 的文本**，而不是返回空——
空文档进了库比噪声更难排查（用户以为抓到了，实际什么都没）。

**为什么住共享底座**（2026-10-08 剥离阶段 0，**例外上移**）：这一件不只是解析器——
KB 侧的连接器与上传路径要用它（`services/connectors/`、`parsers/html_upload.py`），
**Agent 侧的阅读模式也要用它**（`services/web.py` 抓回网页后抽正文）。
`parsers/` 整体仍随 KB 走，所以旧位置 `app/parsers/html_format.py` 留了一个
再导出的壳（KB 侧调用点一行没动）；这里只实现「整页 HTML → 干净正文」这件
与域无关的事，没有业务语义，也没有第三方依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = ["extract_article", "html_to_markdown", "html_to_text"]

#: 这些标签的内容直接丢掉：要么不是给人读的正文，要么是明确的噪声。
_DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "svg",
        "canvas",
        "form",
        "button",
        "input",
        "select",
        "textarea",
        "nav",
        "header",
        "footer",
        "aside",
        "template",
    }
)

#: 语义上最可能是正文的容器。命中就大幅加权——
#: 作者的语义标注比我们的启发性规则可靠得多。
_CONTENT_HINTS = frozenset({"article", "main", "post", "content", "entry", "markdown-body"})

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "br",
        "li",
        "tr",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

_HEADINGS = {f"h{level}": level for level in range(1, 7)}


@dataclass
class _Node:
    """轻量 DOM：只保留结构，不留属性（属性对提取正文没用）。"""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)
    text: str = ""

    def text_len(self) -> int:
        """这棵子树的可见文本长度。"""
        total = len(self.text.strip())
        return total + sum(child.text_len() for child in self.children)

    def tag_count(self) -> int:
        return 1 + sum(child.tag_count() for child in self.children)


class _TreeBuilder(HTMLParser):
    """把 HTML 解析成一棵 ``_Node`` 树。"""

    #: 这些标签不闭合也要能正确弹出（HTML 里 `<p>` 常常不写 `</p>`）
    _VOID = frozenset({"br", "img", "hr", "meta", "link", "source", "col", "area", "base"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="root")
        self._stack = [self.root]
        self._dropping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            # 进入丢弃区：里面所有内容（含子标签的文本）都不要
            self._dropping += 1
            return
        if self._dropping:
            return
        if tag in self._VOID:
            self._stack[-1].children.append(_Node(tag=tag))
            return
        node = _Node(tag=tag, attrs={key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._dropping = max(0, self._dropping - 1)
            return
        if self._dropping:
            return
        # 从栈顶往下找最近的一个同名标签（容忍未闭合的中间标签）
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._dropping:
            return
        # 文本**一律**建成 #text 子节点，不往 ``node.text`` 上append。
        #
        # 早先的写法按"当前标签是不是块级"二选一，而栈顶永远是**最内层**元素
        # （``<pre><code>文本`` 的栈顶是 ``code``，不是 ``pre``），
        # 于是文本全落进了 ``code.text``——一个写入器根本不会去看的字段，
        # 结果代码块渲染成空（实测踩到）。统一成子节点之后，
        # "块级还是行内"这件事只由写入器判断，只有一个真相来源。
        self._stack[-1].children.append(_Node(tag="#text", text=data))


def _score(node: _Node) -> float:
    """正文候选打分。

    **不能只看"文本密度"**：密度最高的永远是单个 ``<p>``（一个标签、几十个字，
    密度 50+），而真正的正文是一棵子树（``<article>`` 里十几个标签，密度只有 4）。
    只按密度选，结果是"只抓到第一个段落"——实测就是这样。

    所以取 **长度 × 密度**：
    - 单个段落：``50 × 50 = 2500``
    - 整篇正文：``400 × 4 = 1600``，语义标签再 ×3 → ``4800``，胜出

    长度那一项保证"越完整越好"，密度那一项保证"别把导航栏也算进去"，
    语义提示则让作者的显式标注压过我们的启发性规则。
    """
    if node.tag == "#text":
        return 0.0
    length = node.text_len()
    if length == 0:
        return 0.0

    density = length / max(1, node.tag_count())
    score = float(length) * density

    name = node.tag.lower()
    if name in _CONTENT_HINTS:
        score *= 3.0
    if any(hint in node.attrs.get("class", "").lower() for hint in _CONTENT_HINTS):
        score *= 2.0
    if any(hint in node.attrs.get("id", "").lower() for hint in _CONTENT_HINTS):
        score *= 2.0
    if name in {"body", "html"}:
        # 整页只是兜底：它一定包含正文，但也一定包含导航与页脚，
        # 不该赢过真正的正文容器
        score *= 0.5
    return score


def _best(node: _Node, *, depth: int = 0) -> _Node:
    """自顶向下找得分最高的子树。"""
    # 深度上限防止畸形嵌套把递归拉爆
    if depth > 200:
        return node
    best = node
    best_score = _score(node)
    for child in node.children:
        candidate = _best(child, depth=depth + 1)
        candidate_score = _score(candidate)
        if candidate_score > best_score:
            best, best_score = candidate, candidate_score
    return best


class _MarkdownWriter:
    """把 ``_Node`` 子树写成 Markdown。

    **关键点：容器要往下走，只有"叶子内容"才取整段文本。**
    早先的实现在每个分支前面都调 ``_plain(node)`` 兜底，而 ``_plain`` 对任何节点
    都返回非空文本——于是容器在第一次判断时就"命中"了，标题、列表、代码块
    全被压成一段纯文本，Markdown 结构全丢（实测踩到）。
    所以这里的顺序是：**先认结构，认不出再递归，最后才兜底取文本**。
    """

    #: 这些标签只是容器，本身不产出内容，必须往下走
    _CONTAINERS = frozenset(
        {
            "root",
            "html",
            "body",
            "div",
            "section",
            "article",
            "main",
            "span",
            "ul",
            "ol",
            "table",
            "thead",
            "tbody",
            "tr",
            "dl",
            "dt",
            "dd",
            "figure",
            "figcaption",
            "header",
        }
    )

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._list_depth = 0

    def write(self, node: _Node) -> str:
        self._walk(node)
        # 相邻的列表项之间只留单个换行——它们是一组，不该被当成两个段落
        text = "\n\n".join(part for part in self._parts if part.strip())
        text = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # ------------------------------------------------------------------ 内部

    def _emit(self, chunk: str) -> None:
        chunk = chunk.strip("\n")
        if chunk:
            self._parts.append(chunk)

    def _walk(self, node: _Node) -> None:
        tag = node.tag

        if tag == "#text":
            text = _collapse(node.text)
            if text:
                self._emit(text)
            return

        if tag in _HEADINGS:
            content = _inline_text(node)
            if content:
                self._emit(f"{'#' * _HEADINGS[tag]} {content}")
            return

        if tag == "pre":
            # 代码块原样保留换行——压平会把代码毁掉
            content = _raw_text(node)
            if content:
                self._emit(f"```\n{content}\n```")
            return

        if tag == "blockquote":
            content = _inline_text(node)
            if content:
                self._emit("\n".join(f"> {line}" for line in content.split("\n")))
            return

        if tag == "li":
            content = _inline_text(node)
            if content:
                self._emit(f"{'  ' * max(0, self._list_depth - 1)}- {content}")
            return

        if tag in {"ul", "ol"}:
            self._list_depth += 1
            for child in node.children:
                self._walk(child)
            self._list_depth -= 1
            return

        if tag in {"td", "th"}:
            content = _inline_text(node)
            if content:
                self._emit(content)
            return

        if tag == "br":
            return

        if tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "figcaption"}:
            content = _inline_text(node)
            if content:
                self._emit(content)
            return

        if tag in self._CONTAINERS:
            # 容器：往下走。**先试子节点**，一个能产出内容的都没有才兜底取文本
            before = len(self._parts)
            for child in node.children:
                self._walk(child)
            if len(self._parts) == before:
                content = _inline_text(node)
                if content:
                    self._emit(content)
            return

        # 其它标签（strong/em/a/code…）：取文本
        content = _inline_text(node)
        if content:
            self._emit(content)


def _raw_text(node: _Node) -> str:
    """原样取文本，**保留换行**（代码块用）。"""
    parts: list[str] = []

    def visit(current: _Node) -> None:
        if current.tag == "#text":
            parts.append(current.text)
            return
        for child in current.children:
            visit(child)

    visit(node)
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _inline_text(node: _Node) -> str:
    """把一棵子树压成**单行**文本（标题、列表项、表格格用）。

    与 ``_plain`` 的区别：这里只按空格拼接，不保留块级换行——
    一个标题里不该出现换行。
    """
    pieces: list[str] = []

    def visit(current: _Node) -> None:
        if current.tag == "#text":
            pieces.append(current.text)
            return
        for child in current.children:
            visit(child)

    visit(node)
    return _collapse(" ".join(pieces))


def _plain(node: _Node) -> str:
    """整棵子树的可见文本（块级之间按空行分段）。"""
    pieces: list[str] = []

    def visit(current: _Node) -> None:
        if current.tag == "#text":
            text = _collapse(current.text)
            if text:
                pieces.append(text)
            return
        if current.tag in _BLOCK_TAGS and pieces:
            pieces.append("\n")
        for child in current.children:
            visit(child)
        if current.tag in _HEADINGS or current.tag in {"p", "pre", "blockquote"}:
            pieces.append("\n")

    visit(node)
    text = " ".join(pieces)
    return _collapse(text, keep_newlines=True)


def _collapse(text: str, *, keep_newlines: bool = False) -> str:
    """压平空白。

    HTML 源码里的换行与缩进是**给人看源码用的**，不是内容的换行——
    不压掉的话，抓回来的正文会到处是莫名其妙的断行。
    """
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    if keep_newlines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def html_to_markdown(html: str) -> str:
    """整页 HTML → Markdown（不做正文提取，纯转换）。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return _MarkdownWriter().write(builder.root)


def html_to_text(html: str) -> str:
    """整页 HTML → 纯文本。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return _collapse(_plain(builder.root), keep_newlines=True)


def extract_article(html: str) -> str:
    """**从整页里提取正文**并转 Markdown。

    这是 HTML 连接器真正用的入口：先按文本密度找正文容器，
    再把那棵子树转成 Markdown。
    """
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()

    body = _find_tag(builder.root, "body") or _find_tag(builder.root, "html") or builder.root
    article = _best(body)
    markdown = _MarkdownWriter().write(article)

    if markdown.strip():
        return markdown

    # 正文提取失败时**退回整页文本**而不是返回空：空文档进了库比噪声更难排查
    # ——用户以为抓到了，实际什么都没
    return html_to_text(html)


def _find_tag(node: _Node, tag: str) -> _Node | None:
    if node.tag == tag:
        return node
    for child in node.children:
        found = _find_tag(child, tag)
        if found is not None:
            return found
    return None
