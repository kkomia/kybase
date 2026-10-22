"""仓库结构性规范自动核查：分层纪律、测试位置、脚本编码、界面文案、版本号、CSS 分层。

对应分层规则3.3（分层纪律）、5.1（测试存放铁律）与 6（脚本约定）、
界面规则5.1（界面里不写解释性小字、不写实现细节），以及 CHANGELOG「附：版本号约定」。
这些约束靠人工 review 容易漏，故做成机械检查接入 CI：
``L1`` 协议层越界、``L2`` 业务层直连数据库/SQL、``L3`` 解析器互引、
``L4`` 解析器反向依赖业务层、``L5`` 业务层依赖协议层、``L6`` app 根下的游离模块、
``A1`` 异步端点里没有 await（假异步，会按住事件循环）、
``C1`` CSS 分层纪律（层序声明 + 层外规则）、
```` 测试位置、``S1`` .ps1 缺少 UTF-8 BOM、``U1`` 界面里的解释性小字、
``U2`` 界面文案里的实现细节、``V1`` 手写版本号不一致、``PARSE`` 语法错误。

用法：python scripts/check_layering.py [仓库根目录，默认当前目录]
退出码：0 = 通过；1 = 发现违规。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 规则定义
# 注意：层常量必须是**点分模块前缀**（app.api），不能写成目录形式（app/api）——
# 模块名是点分的，写成目录形式会导致规则永不命中（静默假阴性）。

# L1：协议适配层不得越过 services/ 直接碰存储、解析器、队列
PROTOCOL_LAYERS = ("app.api", "app.mcp_server")
PROTOCOL_FORBIDDEN = ("app.storage", "app.parsers", "app.workers")
PROTOCOL_MSG = "协议适配层只能转发 services/，禁止直接依赖存储/解析器/队列"

# L2：业务层不得直接使用数据库驱动，也不得依赖具体存储实现。
# **只允许 `app.storage.base`**（抽象契约）：具体实现（postgres_impl / s3_impl / local_impl / duckdb_impl）
# 只被组合根装配。写成"允许清单"而不是"禁止前缀"是有意的——原来的禁止清单漏了
# `app.storage.duckdb_impl`，也漏了 `from app.storage import postgres_impl`（目标是
# 包名 `app.storage`，任何前缀规则都不命中），等于留了口子。
SERVICE_LAYER = "app.services"
SERVICE_FORBIDDEN_MODULES = (
    "sqlite3",
    "sqlite_vec",
    "duckdb",
    "sqlalchemy",
    # v0.12 起存储转向 PostgreSQL：psycopg 与 sqlite3 同级，业务层同样不得直连
    "psycopg",
    "psycopg_pool",
)
SERVICE_STORAGE_ALLOWED = "app.storage.base"
SERVICE_MSG = "业务层禁止直连数据库，存储访问必须经 storage/base.py 的 Repository 接口"

# L3：解析器实现之间互不引用（base.py 的 ParseResult 与 probe.py 是共享契约）
#
# `tabular_format` / `html_format` 是**共享的格式转换器**而不是"某个解析器实现"：
# 它们不含任何解析器注册逻辑，可以被多个解析器与连接器复用。放进允许清单是刻意的
# ——把它们算作"实现"，只会逼着后来的人复制一份 HTML 剥标签的代码。
PARSER_LAYER = "app.parsers"
PARSER_SHARED = {
    "base",
    "probe",
    "tabular_format",
    "html_format",
    "text_decode",
    "__init__",
}

# L4：解析器（插件层）不得反向依赖业务层/协议层。
# 原规则只查 parser→parser，于是 `parsers/tabular.py` import `app.services.tabular`
# 这种更严重的反向依赖直接通过——依赖方向反了，插件就没法脱离业务层复用。
PARSER_FORBIDDEN = ("app.services", "app.api", "app.mcp_server", "app.workers")
PARSER_MSG = "解析器是插件层，不得反向依赖业务层（services）或协议层（api/mcp/workers）"

# L5：业务层不得依赖协议适配层。
#
# 起因：`app/agent_tools.py`（当时在 app 根）import 了 `app.mcp_server.tools`，
# 而协议层（api）又 import 这个根模块——方向成了"协议层 → 业务实现 → 另一个协议层"。
# 共用实现只能住在 services/ 里（两个门都往下依赖它），反过来就是循环的形状。
SERVICE_FORBIDDEN_LAYERS = ("app.api", "app.mcp_server")
SERVICE_LAYER_MSG = "业务层不得依赖协议适配层（api / mcp_server）；共用实现要放在 services/ 里"

# L6：`app/` 根下只允许入口模块与 __init__.py——每个模块都必须属于一个分层。
#
# 起因与 L5 同：`app/agent_tools.py` 住在 app 根，既不匹配 `app.api` / `app.services`，
# 也不匹配任何禁止前缀，于是 L1–L4 一条都不作用于它。**一个不被任何规则覆盖的文件，
# 等于分层纪律对它不存在**：它 import 谁都不会红——而那块代码恰好管着工具准入与会话
# 范围收口，是全项目最需要护栏的地方。规则靠"命名空间白名单"而不是"记得加清单"。
#
# `sidecar`（客户端那第二个入口）**已随 调用方代码拆除**（v0.1.0 精简），
# 白名单里不再留它——这个仓只有一个入口。
APP_ROOT_ALLOWED = {"__init__", "main"}
APP_ROOT_MSG = (
    "app/ 根下不得放游离模块（只允许 main.py 与 __init__.py）："
    "请归入 api/ services/ storage/ parsers/ workers/ core/ models/ pipeline/ 之一"
)

# A1：协议层的 `async def` 端点**必须真的 await 点什么**。
#
# 起因是一次实测：38 个端点里有 33 个是 `async def` 但内部一行 await 都没有，
# 它们调的是同步的 psycopg / httpx。这会把这些阻塞调用**全部按在事件循环线程上**，
# 于是"并发"完全不成立——实测不碰库的 /health 在并发 20 下，中位延迟从 4.7ms
# 涨到 140ms（整个循环在等别人的同步 IO）。
#
# 修法是把这类端点写成普通的 `def`：Starlette 会把同步端点丢进线程池（默认 40 线程），
# 阻塞不再卡住循环。这条规则就是防止后来者（或"顺手加个 async"）把它退回去。
ASYNC_API_LAYERS = ("app.api",)
ASYNC_MSG = (
    "协议层的异步端点里没有任何 await：它调的是同步 IO，会把事件循环按住。"
    "请改成普通的 def（Starlette 会丢进线程池），或真的用异步驱动。"
)

# ：测试代码绝不进入源码目录
SOURCE_ROOTS = ("backend/app", "frontend/src")
# 后缀要跟着前端走：P5 之后组件测试是 `.test.tsx` / `.spec.tsx`（Vue 时代只有 `.ts`）。
# 与 U1 那次是同一类漏：只认老后缀，规则对新写法视而不见。
TEST_FILE_RE = re.compile(r"^(test_.*\.py|.*_test\.py|.*\.(test|spec)\.tsx?)$")

# SQL 语句起始关键字：业务层源码中的这类字符串字面量视为直接写 SQL。
#
# **要求关键字后面还有内容**（``\s+\S``），不能只匹配一个孤零零的词：
# 单个词是 HTML 标签名或枚举值的可能性更大——实测 `"select"`（HTML 的
# <select> 下拉框）与 `"delete"`（任务类型）都被误报成"业务层写 SQL"。
# 真正的 SQL 一定带列名或表名（`select *`、`delete from ...`），
# 所以多要求一个词就能把误报挡掉，而不会放过真正的违规。
SQL_START_RE = re.compile(
    r"(?i)^\s*(select|insert|update|delete|create|drop|alter|pragma|attach"
    r"|replace\s+into)\s+\S"
)


# ---------------------------------------------------------------- U1：界面文案
#
# 界面规则5.1：**界面里不写"这一页/这一节是什么"的解释性小字**。
# 它是用户明确要求删干净的一类东西（原话"所有类似这种的全部删除，一个不留"），
# 而它删完还会长回来——`PageShell` 的 `description` prop 就是被写回来的口子，
# 所以那个 prop 连同一整套样式都删了。
#
# 为什么这条要靠机械检查：**Vue 对多余属性是宽容的**，给 `<PageShell description="…">`
# 传一个不存在的 prop 不会报错，它会变成落到根元素的 attr 安静地渲染出来；
# 而 `class="panel-desc"` 这种新写的类名更是谁也不会拦。靠 review 一定漏。
#
# 判据是**命名约定**而不是"这段文字像不像解释"——后者没法机械判。所以：
# 属性名精确匹配；类名按前缀族匹配（要写别的用途的名字，就别用 page-/panel-/section- 开头）。
#
# 2026-09-24 扩展（用户第二轮反馈，11 张截图）：解释性小字不止住在 `*-desc` 里，
# 它更常叫 `*-hint` / `*-note` / `*-caption` / `*-sub`——`.m-block-hint`（"向量化部分按
# 字符数估算"）、`.m-usage-note`（"其中约 18,477,797 token 是按字符数估算的…"）、
# `.m-add-hint`、`.m-figure-note` 都是这么长出来的。所以尾巴族加上这四个词。

FORBIDDEN_ATTRS = ("description=", ":description=")

#: 类名族：命中即报。`page-desc` / `panel-desc` / `section-desc` / `page-description` /
#: `panel-lead` … 都在这几族里。
#:
#: **用分词而不是正则**：类名本来就是按空白分开的，拆开看更准；而正则要写的 ``
#: 这类转义在这个仓库里被 heredoc 吃掉过一次（第一版的正则里剩了个退格符，
#: 规则从此永远不命中——所以这条检查的写法本身就是那次事故的产物）。
UI_COPY_FAMILIES = ("page", "panel", "section", "view", "tab")
UI_COPY_TAILS = ("desc", "description", "lead")

#: 第二批尾巴族（2026-09-24）：`hint` / `note` / `caption` / `sub`。
#: 判据是**最后一个连字符段**（`name.rsplit("-", 1)[-1]`），因为这一族的命名习惯是
#: `<域>-<对象>-<用途>`（`m-block-hint`、`kb-modal-note`、`m-row-sub`、`*-caption`），
#: 前缀是域而不是用途。这带来两个刻意的边界：
#: - **不按"整串里出现过这个词"判**：`notes-toc` / `note-group` / `bg-subtle` 不命中
#:   （尾段是 `toc` / `group` / `subtle`）——它们是布局与笔记域的命名，不是说明文字；
#: - **尾段带后缀也不算**（`m-summary-warn`）——那条规则留给"想写别的用途"的人：
#:   按用途命名（`*-error` / `*-count` / `*-empty`），不要用 `*-hint`。
#:
#: 例外按**类名**列进 `UI_COPY_ALLOWED`，逐条写理由。真正有用途的那几类
#: （表单字段的约束提示"至少 8 个字符"、计数"0 / 200"、空态、校验错误、
#: 禁用/受限原因、会自己变的实时数）不算解释性小字——它们说的是"这一格该怎么填"
#: 或"现在为什么不能用"，不是"这一页是什么"。
UI_COPY_TAIL_FAMILIES = ("hint", "note", "caption", "sub")

UI_COPY_CLASS = re.compile(
    r"""class="[^"]*(?:(?:page|panel|section|view|tab)-desc(?:ription)?"""
    r"""|(?:page|panel|section|view|tab)-lead)""",
    re.I,
)

#: 允许的例外：有正当用途、名字恰好落在上面那几族里的类。
#: **能空就空着**——留一个例外就要写清理由，不然它会长成一条通道。
#: 键是类名（原样，含前缀），值是"它为什么不是解释性小字"。按用途分组写，便于核对。
UI_COPY_ALLOWED: frozenset[str] = frozenset(
    {
        # 设计系统的辅助文字原语：承载**字段计数**（`0 / 200`）与**约束提示**
        # （"至少 8 个字符"、"上限 1024（块长的一半）"）。它说的是"这一格怎么填"。
        "text-hint",
        "text-note",
        # 空态：只在没有数据时出现，说的是"现在是什么、点哪儿开始"。
        "empty-hint",
        "kb-empty-hint",
        "kb-doc-empty-hint",
        "kb-dropzone-hint",
        "m-empty-hint",
        "m-empty-note",
        # 受限/受阻原因：解释**为什么这个动作现在做不了**（不是解释这一页是什么）。
        "kb-blocked-note",
        "kb-readonly-note",
        "m-picker-note",
        # 会自己变的实时数：排队条数、并发槽位、进度。
        "m-load-hint",
        "m-load-note",
        "m-summary-note",
        # 快捷键提示：键位映射本身就是内容（不是对它的解释）。
        "m-shortcut-hint",
        # 弹窗/抽屉底部那句"这一步会做什么"（动作代价与后果），与"名词解释"不同。
        "kb-modal-note",
        "kb-foot-note",
        "kb-preview-note",
        "kb-tabs-hint",
        "kb-chunk-hint",
        "kb-timeline-note",
        "kylab-note",
        "kylab-none-note",
        "kylab-sheet-note",
        # 登录页的状态行（"当前已登录：X" / 首次使用的下一步）。
        "m-login-hint",
        # 供应商预设的一句话（"这一家要走什么地址"），属于"这一格该填什么"。
        "m-preset-hint",
        # 工作区页脚那句"不会动你的文件"是**后果说明**（删之前必须知道的事）。
        "m-foot-note",
        # 「启用开关」那一类状态行的灰字：空态（"还没有任何人"）、
        # 禁用原因（"未选定前不能新建知识库"）、加载中（"正在加载…"）。
        "m-row-note",
        # 编辑态顶部那句"改这一组之前该知道的一件事"（groupTips.editHint）：管的是**动作**
        # （开思考更慢、密钥只回显掩码、清掉凭据走哪个入口），不是"这一页是什么"。
        "m-edit-hint",
        # 工具栏上的状态行：任务中心"已取消 23"、记忆页"只列出了前 N 个"——报的是数，
        # 不是对界面的解释（记忆页那条通读全文的说明不算，它由另一条泳道重做那一页时收）。
        "m-toolbar-note",
        # 市场弹窗"还没有启用的源——先添加一个仓库"：无源时的空态 + 下一步。
        "m-market-hint",
        # 候选模型清单的加载/失败/为空状态行（失败时指明"可直接手写模型 ID"这条出路）。
        "m-source-note",
        # 首页大数卡片下面那行注解：**只在异常时出现**（N 篇失败 / N 篇待索引 / 还没有文档）。
        "m-figure-note",
        # 笔记列表的错误行（"笔记加载失败"）与编辑器上传进度（"图片上传中…"）。
        "list-hint",
        "upload-hint",
        # 账号行里"显示名（登录名）"的后半截：那是**身份的第二半**（登录要用的是它），
        # 不是说明文字。
        "m-row-sub",
    }
)

UI_COPY_MSG = (
    "界面里的解释性小字（标题下面那句「这一页是什么」）："
    "删掉它——用户不需要在界面上被解释这个东西是什么；"
    "**确实有用途**（字段约束、计数、空态、校验错误、受限原因）时，"
    "给这个类换个按用途起的名字（`*-error` / `*-count` / `*-empty`），"
    "或把它列进 `UI_COPY_ALLOWED` 并写清理由"
)


# ---------------------------------------------------------------- C1：CSS 分层纪律
#
# **同一个病根已经犯过三次**，三次都不是"选择器写错了"，而是"那份文件压根没有 `@layer`"：
# `tokens.css` 的元素重置换掉全站按钮样式、`misc.css` 的 364 条 `.m-*`、`knowledge.css`
# 的 `.kb-*`（外加本批收掉的 4 份）。机制只有一条：**未分层的声明永远压过
# `@layer utilities`，与优先级无关**——层里的工具类再怎么"后写、更具体"都赢不了层外。
#
# 为什么必须机械检查：**这个 bug 测试抓不到**。`tokens.css` 那次全站按钮都坏了，
# 742 条前端用例照样全绿（样式错了不报错、jsdom 也不算计算样式）。只有人眼看得见，
# 而"这次看得见"正是前三次没兜住的原因。
#
# 两条判据（都只看**样式规则**，`@keyframes` / `@font-face` / `@import` / `@theme`
# 这类 at-rule 不算——它们不参与"谁压过谁"的竞争）：
# 1. 文件里出现 `@layer <名字> { … }` 块时，必须在文件里**先**写一句显式的层序声明
#    `@layer theme, base, components, utilities;`。层的先后由**名字首次出现的位置**决定，
#    不声明就由 CSS 的加载顺序决定：`misc.css` 排在 `tokens.css` 前面那次实测，
#    `components` 拿到了第 1 位，Tailwind 的 `base` 压在它上面，preflight 的
#    `* { margin: 0; padding: 0; border: 0 }` 把整份组件样式吃掉（页标题字号退回 15px）；
# 2. `@layer` 块**之外**不允许有普通样式规则（顶层 `@media` 里的也算层外）。
#    这一条就是三次病根的同一条机制。
#
# 例外只有两类，都要求写清理由：
# - 规则上方一句 `/* @unlayered: 原因 */`（标记与规则之间只能隔空行或注释）：
#   留给**真的只能靠层外身份**的规则——典型是第三方库把 `<style>` 注入到我们给的容器
#   内部（`preview.css` 的 docx-preview 三条、`chat.css` 的 highlight.js 一条）：
#   对手是层外的规则，进层等于没写；把取值挪到调用点也不行（utilities 同样在层里）。
# - `CSS_UNLAYERED_FILES` 里逐条写明理由的**基座文件**：`styles/**` 与入口 `index.css`
#   按设计就在层外（令牌 `:root`、全局 `:focus-visible`、高度链），收它们是另一批工作。
#   **默认空着**：放行一整份文件是很大的口子，每加一行都要在 PR 里说清为什么。

#: 层序的唯一口径（与 `tokens.css` 顶部那句一致）。顺序**由首次出现决定**，
#: 所以每个开层文件都要重复声明一句（内容相同、幂等）。
CSS_LAYER_ORDER = ("theme", "base", "components", "utilities")

#: 规则级例外标记：写在规则上方（中间只允许空行或注释）。
CSS_UNLAYERED_MARKER = "@unlayered:"

#: 文件级例外：**按设计就留在层外**的基座文件。键是相对仓库根的 POSIX 路径。
#: 每一条都必须写清"为什么它可以不收"——这张表不是"懒得改"的存档。
CSS_UNLAYERED_FILES: dict[str, str] = {
    "frontend/src/styles/tokens.css": (
        "基座：层序的唯一声明者本身；`:root` 令牌与全局 `:focus-visible`、遗留辅助类"
        "（`.text-meta` / `.panel` …）按既有记载有意留在层外（`src/styles/tokens.css` 1.1 写着"
        "它们就是靠层外身份压过 utilities），收它们属于另一批工作"
    ),
    "frontend/src/index.css": (
        "Tailwind 入口：`@theme inline` 必须写在顶层；`html, body, #root { height: 100% }`"
        "是高度链重置（本批不收它——那要连同主题文件一起动）"
    ),
    "frontend/src/styles/themes/light.css": "主题令牌：整份只有 `[data-theme='light'] { --* }`",
    "frontend/src/styles/themes/dark.css": "主题令牌：整份只有 `[data-theme='dark'] { --* }`",
}

CSS_LAYER_BLOCK_MSG = (
    "这份 CSS 里有 @layer 块，却没有在文件里先显式声明层序。"
    "请照 tokens.css 顶部那句补一行 `@layer theme, base, components, utilities;`——"
    "层的先后由名字**首次出现的位置**决定，不声明就由 CSS 的加载顺序决定"
    "（`components` 可能排到 `base` 前面，Tailwind 的 preflight 会吃掉整份组件样式）"
)

CSS_UNLAYERED_MSG = (
    "层外的样式规则：**未分层的声明永远压过 `@layer utilities`**（与优先级无关），"
    "这就是三次病根（tokens.css 的重置 / misc.css / knowledge.css）的同一条机制。"
    "请把规则收进 `@layer components`（元素重置收 `@layer base`），或把取值挪到调用点；"
    "**真的只能靠层外身份**时（第三方库把 `<style>` 注入到容器内部），"
    "在规则上方写一句 `/* @unlayered: 原因 */`"
)

#: 条件组 at-rule：它们**不产生新的层**，里面的规则仍然是层外/层内身份不变，
#: 所以要钻进去看。其余 at-rule（`@keyframes` / `@font-face` / `@theme` …）不参与级联竞争，跳过。
CSS_CONDITIONAL_RULES = ("media", "supports", "container", "scope", "document", "starting-style")


class Violation:
    """一条违规记录。"""

    def __init__(self, rule: str, path: Path, lineno: int, detail: str) -> None:
        self.rule = rule
        self.path = path
        self.lineno = lineno
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}  [{self.rule}] {self.detail}"


def module_name_of(path: Path, root: Path) -> str:
    """把文件路径转成点分模块名，如 ``backend/app/api/v1/health.py`` → ``app.api.v1.health``。"""
    rel = path.relative_to(root / "backend").with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def in_layer(module: str, layer: str) -> bool:
    """判断模块是否属于某层（按点分前缀整段匹配，避免 ``app.api_x`` 误命中 ``app.api``）。"""
    return module == layer or module.startswith(f"{layer}.")


def imported_modules(tree: ast.AST, current: str) -> list[tuple[int, str]]:
    """收集 import 的目标模块名（含 ``from .x import y`` 的相对导入解析）。"""
    found: list[tuple[int, str]] = []
    package = current.rsplit(".", 1)[0] if "." in current else current
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：按当前包深度还原
                base_parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(base_parts) if base_parts else package
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.append((node.lineno, target))
    return found


def string_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """收集字符串字面量（含 f-string 片段）。"""
    values: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    values.append((node.lineno, part.value))
    return values


def check_layer_rules(path: Path, root: Path, tree: ast.AST) -> list[Violation]:
    """L1/L2/L3：分层纪律。``path`` 为绝对路径，报告时转成相对路径。"""
    violations: list[Violation] = []
    display = path.relative_to(root)
    module = module_name_of(path, root)

    for lineno, target in imported_modules(tree, module):
        if any(in_layer(module, layer) for layer in PROTOCOL_LAYERS) and any(
            target == prefix or target.startswith(f"{prefix}.") for prefix in PROTOCOL_FORBIDDEN
        ):
            violations.append(Violation("L1", display, lineno, f"{PROTOCOL_MSG}（import {target}）"))

        if in_layer(module, SERVICE_LAYER):
            allowed_storage = target == SERVICE_STORAGE_ALLOWED or target.startswith(
                f"{SERVICE_STORAGE_ALLOWED}."
            )
            uses_storage = target == "app.storage" or target.startswith("app.storage.")
            if target.split(".")[0] in SERVICE_FORBIDDEN_MODULES or (
                uses_storage and not allowed_storage
            ):
                violations.append(Violation("L2", display, lineno, f"{SERVICE_MSG}（import {target}）"))
            if any(
                target == layer or target.startswith(f"{layer}.")
                for layer in SERVICE_FORBIDDEN_LAYERS
            ):
                violations.append(
                    Violation("L5", display, lineno, f"{SERVICE_LAYER_MSG}（import {target}）")
                )

        if in_layer(module, PARSER_LAYER):
            parts = target.split(".")
            if parts[0] == "app" and len(parts) >= 3 and parts[1] == "parsers":
                if parts[2] not in PARSER_SHARED:
                    violations.append(
                        Violation(
                            "L3",
                            display,
                            lineno,
                            f"解析器实现之间禁止互相引用，共享契约只能来自 base.py（import {target}）",
                        )
                    )
            if any(target == prefix or target.startswith(f"{prefix}.") for prefix in PARSER_FORBIDDEN):
                violations.append(Violation("L4", display, lineno, f"{PARSER_MSG}（import {target}）"))

    if in_layer(module, SERVICE_LAYER):
        for lineno, value in string_literals(tree):
            if SQL_START_RE.match(value):
                snippet = value.strip().splitlines()[0][:60]
                violations.append(
                    Violation("L2", display, lineno, f"业务层出现 SQL 字面量：{snippet!r}")
                )

    return violations


def check_async_endpoints(path: Path, root: Path, tree: ast.AST) -> list[Violation]:
    """A1：协议层的 ``async def`` 必须真的 await 东西（见 ``ASYNC_MSG``）。"""
    violations: list[Violation] = []
    module = module_name_of(path, root)
    if not any(in_layer(module, layer) for layer in ASYNC_API_LAYERS):
        return violations
    display = path.relative_to(root)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
            continue
        # 只查**路由处理函数**：带装饰器的那些（Depends 注入的依赖函数不在此列）
        if not node.decorator_list:
            continue
        has_await = any(
            isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor)) for n in ast.walk(node)
        )
        if not has_await:
            violations.append(Violation("A1", display, node.lineno, f"{node.name}：{ASYNC_MSG}"))
    return violations


def check_test_placement(path: Path, root: Path) -> list[Violation]:
    """：源码目录内不得出现测试文件。``path`` 为绝对路径。"""
    rel = path.relative_to(root)
    rel_posix = rel.as_posix()
    if not any(rel_posix.startswith(source) for source in SOURCE_ROOTS):
        return []
    if "__tests__" in path.parts or TEST_FILE_RE.match(path.name):
        return [
            Violation(
                "",
                rel,
                1,
                "测试文件不得放入源码目录，请按分层规则 5.1 放到 backend/tests 或 frontend/tests",
            )
        ]
    return []


def check_ui_copy(path: Path) -> list[Violation]:
    """U1：界面里不许出现"这一页是什么"的解释性小字（界面规则5.1）。

    两条判据都是**机械可判的**：

    1. 给 `PageShell` / `PageHeader` 传 `description`——那个 prop 已经删了，
       而 Vue 不为多余属性报错，它会安静地落在根元素上渲染出来（这正是它会被写回来的原因）；
    2. 类名落在 `page-` / `panel-` / `section-` / `view-` / `tab-` 的 `desc` / `lead` 族里，
       或**尾段**是 `hint` / `note` / `caption` / `sub`（2026-09-24 扩的那一批）。

    **注释不算**：注释里提到这些词，多半正是在解释"为什么删掉它"，那要留着。

    例外靠 `UI_COPY_ALLOWED` 显式列（按类名，逐条写理由）：留一个例外就得写清理由，
    不然它会长成一条通道。

    **这条规则只查类名，查不出"用合法类名写着解释性文字"**（`text-hint` 是设计系统原语，
    25 处合法用法里也能混进一句"这是个什么东西"）。那一半靠 U2 兜实现细节、
    靠评审兜纯说教——机械判据要诚实地承认自己判不了语义。
    """
    # 后缀里必须有 `.tsx`：P5 之后组件全是 `.tsx`，而这条规则盯的正是组件里的文案——
    # 只认 `.vue` / `.ts` 的话，规则会因为"什么都没扫到"而永远绿（Vue 时代的写法，
    # 与旧 `\b` 那次静默失效是同一类错误：**看不出异常，其实没在干活**）。
    if path.suffix not in (".ts", ".tsx"):
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    found: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("//", "/*", "*", "<!--")):
            continue
        if any(token in line for token in FORBIDDEN_ATTRS) and _targets_page_shell(
            line, text, lineno
        ):
            found.append(Violation("U1", path, lineno, UI_COPY_MSG))
            continue
        banned = [name for name in _class_names(line) if _is_ui_copy_class(name)]
        if banned and not all(name in UI_COPY_ALLOWED for name in banned):
            found.append(Violation("U1", path, lineno, f"{UI_COPY_MSG}：{'、'.join(banned)}"))
    return found


def _class_names(line: str) -> list[str]:
    """这一行里 `class="…"` / `className="…"` 写到的类名。

    两种写法都要认：React（P5 之后）是 `className=`，Vue 时代是 `class=`。
    只认其中一种，另一半的违规就永远查不出来。
    """
    names: list[str] = []
    for token in ('className="', 'class="'):
        for chunk in line.split(token)[1:]:
            names.extend(chunk.split('"')[0].split())
    return names


def _is_ui_copy_class(name: str) -> bool:
    """这个类名是不是"页面/小节说明"那一族。

    两条判据：族前缀 + `desc`/`description`/`lead` 打头；或**尾段**落在
    `hint`/`note`/`caption`/`sub` 里（`.m-block-hint` / `.kb-modal-note` / `.m-row-sub`）。
    用 `partition` / `rsplit` 而不是正则：这个文件里**不写正则转义**（第一版写的 `\\b`
    被 heredoc 变成了退格符，规则从此永远不命中——一次看不出任何异常的静默失效）。
    """
    head, _, tail = name.partition("-")
    if head in UI_COPY_FAMILIES and tail:
        if any(tail == item or tail.startswith(f"{item}-") for item in UI_COPY_TAILS):
            return True
    return name.rsplit("-", 1)[-1] in UI_COPY_TAIL_FAMILIES


def _targets_page_shell(line: str, text: str, lineno: int) -> bool:
    """这一行的 `description=` 是不是挂在 `PageShell` / `PageHeader` 上。

    属性可能被 prettier 换到下一行写，所以本行看不到标签名时**往回找最近的那个开标签**。
    """
    window = [line]
    lines = text.splitlines()
    for back in range(0, 4):
        if back:
            index = lineno - 1 - back
            if index >= 0:
                window.insert(0, lines[index])
        opened = _opened_tags(chr(10).join(window))
        if opened:
            return opened[-1] in ("PageShell", "PageHeader")
    return False


def _opened_tags(text: str) -> list[str]:
    """文本里所有开标签的名字（按出现顺序）。不用正则：见 `_is_ui_copy_class` 的说明。"""
    names: list[str] = []
    for chunk in text.split("<")[1:]:
        name = ""
        for char in chunk:
            if char.isalnum() or char in "_.-":
                name += char
            else:
                break
        if name and name[0].isalpha():
            names.append(name)
    return names


# ---------------------------------------------------------------- U2：实现细节不许进界面文案
#
# 起因是 2026-09-24 那轮用户反馈（11 张截图）：界面上真的出现过
# `连不上记忆服务 http://127.0.0.1:2333/health_check：[WinError 10061] 由于目标计算机积极拒绝`、
# `API Key … 走 /api/v1 的鉴权头（后端 app/api/auth.py 是权威）`、
# `在线 v0.1.1 · v1`、`里面要有一个带 name 的 SKILL.md（my-skill/SKILL.md 这种一层目录就好）`。
# 用户原话："你写代码的时候不要再 webui 上向我解释这是个什么东西，我是科班出身的，不需要你解释。"
#
# 这些文字有个共同形状：**它们是开发者的词汇，不是用户的数据**——路径、端口、接口前缀、
# 异常原文、版本号。正因为它们"看起来很像有用信息"（有时还真的是排查线索），
# review 时最容易被放过；而它们一旦渲染出来，就是把内部实现漏给了用户。所以列成黑名单机械查。
#
# 判据（只扫**会渲染出来的字符**）：
# 1. 字符串字面量（`'…'` / `"…"` / 模板串）与 JSX 文本节点；
# 2. **注释不算**——注释里写 `app/api/auth.py` 往往正是在解释"为什么删掉它"（同 U1）；
# 3. **import / export 的模块说明符不算**：`'@/app/routes'` 是模块路径，永远不会渲染；
#    把它算进来只会逼着每加一个别名 import 就来加一条例外，规则会被用废。
#
# 例外按"**文件 + 字面量**"列进 `U2_ALLOWED`，逐条写理由——**不能按规则放行**，
# 按规则放行等于把整条规则关掉。要放行的那一类只有一种：字符串本身就是**数据**
# （可复制的地址、模型 base_url、示例 URL），而不是在向用户解释什么。

#: 黑名单：``(分类名, 正则)``。分类名只用于报错文案（告诉人这是哪一类漏了）。
U2_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 代码/文件路径与文件名。`\w+\.py` 只要求"某段字母开头、以 .py 结尾"，
    # 因为屏上出现的写法五花八门（`app/api/auth.py`、`auth.py`、`services/chat.py`）。
    (
        "代码路径",
        re.compile(
            r"app/|[A-Za-z_][A-Za-z0-9_.-]*\.py|SKILL\.md|installed\.json"
            r"|data/|C:\\\\|\.vue|\.sql"
        ),
    ),
    # 本机 / 内部服务地址。**不是**禁"所有 URL"：可复制的地址是数据，
    # 那种要放行得走 U2_ALLOWED（见下）。这里禁的是"把内部跑着的东西说给用户听"。
    ("内部地址", re.compile(r"127\.0\.0\.1|localhost|http://|https://")),
    # 端口：`127.0.0.1:2333` 里那个四位端口，单独也能命中（避免只写地址不写主机时漏网）。
    ("端口", re.compile(r":[0-9]{4}")),
    # 内部接口面。
    ("接口路径", re.compile(r"/api/v1|/v1/")),
    # 异常与运行时语汇：异常类名、驱动名、实现手法（防抖/幂等/缓存）。
    # **"缓存"用户原话是"要允许"**，我把它留下了：它现在是全仓零例外的干净词，
    # 而"缓存"一旦上界面，说的就是实现（"用的是缓存，几小时内不会重复请求 GitHub"）；
    # 将来真有面向用户的性能说法（"下次打开更快"），按 U2_ALLOWED 加一条并写清
    # "这是给用户看的性能口径"即可——留了出口，所以不必提前松口。
    (
        "运行时语汇",
        re.compile(r"WinError|Traceback|httpx|psycopg|debounce|幂等|缓存"),
    ),
    # 版本串：屏幕上不该出现后端/前端版本号（`v0.1.1`）。它与 `V1` 不冲突：
    # V1 查的是**配置文件里手写的版本号必须一致**（pyproject / package.json / compose），
    # 这里查的是"版本号有没有漏到界面上"，两者的地盘不重叠。
    ("版本串", re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")),
)

#: 例外：(相对仓库根的 POSIX 路径, 字面量里必须出现的片段, 理由)。
#: 理由一律回答同一个问题：**这个字符串为什么是数据而不是解释**。
U2_ALLOWED: tuple[tuple[str, str, str], ...] = (
    (
        "frontend/src/api/client.ts",
        "/api/v1",
        "API 基址常量：拼请求用的路径，不渲染",
    ),
    (
        "frontend/src/features/chat/model/markdown.tsx",
        "https://",
        "正文自动识别裸域名时补的前缀，落在链接 href 上（用户可点），不是文案",
    ),
    (
        "frontend/src/features/chat/panel/webText.ts",
        "https://",
        "地址栏里手打的地址缺协议时补的那个前缀（`normalizeWebUrl`）：补出来的是要抓的地址本身，"
        "不渲染成文案——与上面 markdown.tsx 那条同一口径",
    ),
    (
        "frontend/src/features/notes/NoteEditor.tsx",
        "https://",
        "插入链接时补的协议前缀，写进正文 markdown，不是文案",
    ),
    (
        "frontend/src/features/misc/capabilities/CapabilitiesPage.tsx",
        "example.com",
        "同上：MCP 地址栏的示例格式，不是对实现的解释",
    ),
    (
        "frontend/src/features/misc/settings/ModelRegistryPanel.tsx",
        "api.deepseek.com",
        "供应商预设的 base_url：它就是这条数据本身（用户要照着改地址）",
    ),
    (
        "frontend/src/features/misc/settings/KnowledgeConnectionSection.tsx",
        "http://nas:8000/api/v1",
        "「知识库地址」输入框的占位符示例（ 阶段 6）：示范这一格该填什么格式，"
        "本身就是可复制的那条数据，不是对实现的解释——与上面两条示例地址同一口径",
    ),
    # ---- 以下三条是 kybase 管理台自己的（2026-10-09 搬迁后按本仓现实补的白名单）----
    # kylab 那份白名单是按 kylab 前端的页面群写的，对不上 kybase 的管理台：这几处
    # 在 kybase 里**其余页面一条都没有**，逐条看了都不是"把实现讲给用户听"。
    (
        "frontend/src/api/sidecar.ts",
        "http://127.0.0.1:8765",
        "旧版客户端默认地址常量（`DEFAULT_SIDECAR_BASE`，可被 `VITE_SIDECAR_URL` 覆盖）："
        "它只被 `sidecarStatus().reason` 用来说清「打的是哪个基址」，是可复制、可核对的那条"
        "数据本身，与上面那条示例地址同一口径——不是把实现说给用户听",
    ),
    (
        "frontend/src/features/knowledge/SourcePanel.tsx",
        "example.com",
        "「地址」输入框两个占位符（RSS / 网页各一个）：示范这一格该填什么格式，"
        "本身就是那条数据；与 `ModelRegistryPanel.tsx` 的 `api.deepseek.com` 同一口径",
    ),
)

#: JSX 文本节点里出现这些字符就**不当文字看**（见 `jsx_text_runs`）：TS 的表达式
#: （比较、三元、调用）会落在 `>`…`<` 之间，带一个 `(` 或 `=` 就排掉。
U2_TEXT_LIKE_FORBIDDEN = set("()=;&|!?[]*+\"'\\")

U2_MSG = (
    "界面文案里出现了实现细节（代码路径/本机地址/端口/接口路径/异常原文/版本号）："
    "把它删掉——用户不需要在界面上读实现。**真只是数据**（可复制的地址、模型 base_url、"
    "示例 URL）时，在 `U2_ALLOWED` 里按「文件 + 字面量」加一条并写清为什么它不是解释"
)


def _blank(out: list[str], text: str, start: int, stop: int) -> None:
    """把 `[start, stop)` 里的字符换成空格（换行保留）——长度不变，行号才对得上。"""
    for index in range(max(start, 0), min(stop, len(text))):
        if text[index] != "\n":
            out[index] = " "


def _string_end(text: str, start: int) -> int | None:
    """`start` 处是一个引号时，返回收尾引号的下标；未闭合（跨行）返回 None。

    单行串跨行说明那多半不是字符串（写了一半的代码、或我认错了引号），
    这时**不当字符串处理**比"吃掉半个文件"安全——静默漏报总好过静默误报一片。
    """
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        if char == "\n" and quote != "`":
            return None
        index += 1
    return None


def _regex_can_start(text: str, index: int) -> bool:
    """`index` 处的 `/` 是不是正则字面量的开头（而不是除号）。

    判据是**前一个非空白字符**：它要是能结束一个表达式（字母、数字、`)`、`]`、`}`、
    引号、`$`、`` ` ``），那 `/` 就是除号；否则按正则处理。这条判断是必需的——
    仓库里真有 `` .replace(/\\*\\*|__|`{1,3}/g, '') ``：正则里那个反引号会让
    "先找成对引号"的做法把后面半份文件都当成模板串。

    **例外**：前一个词是关键字（`return` / `typeof` / `case` …）时，`/` 是正则。
    2026-09-24 补：`return /[",\\n]/.test(flat) ? … : flat`（`chat/ui/AnswerText.tsx`）
    里的 `/[",\n]/` 就因为前一个字符是 `return` 的 `n` 被当成除号，正则里那个 `"`
    开启了一个"假字符串"，把后面一行的注释当成用户可见文案（U3 报了一条假命中）。
    关键字**必须整词匹配**：`myreturn /re/` 里的 `/` 仍是除号。
    """
    probe = index - 1
    while probe >= 0 and text[probe] in " \t\r\n":
        probe -= 1
    if probe < 0:
        return True
    char = text[probe]
    if char.isalnum():
        end = probe + 1
        start = end
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$"):
            start -= 1
        return text[start:end] in REGEX_AFTER_KEYWORDS
    return char not in "_$)]}'\"`"


#: 后面能跟正则字面量的关键字（整词）。
REGEX_AFTER_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "case",
        "do",
        "else",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "yield",
        "await",
    }
)


def _regex_end(text: str, start: int) -> int | None:
    """`start` 处的 `/` 若是一个正则字面量，返回收尾 `/` 的下标；否则 None。"""
    index = start + 1
    in_class = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return None
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            return index
        index += 1
    return None


def strip_ts_comments_and_strings(text: str) -> tuple[str, list[tuple[int, str]]]:
    """脱注释（换空格，长度与换行不变）+ 收集字符串字面量：``(内容起始下标, 内容)``。

    为什么不用正则：`//` 会出现在字符串里（`'https://x'`），`'` 会出现在正则里
    （见 `_regex_can_start`），`/*` 会出现在 JSX 文本里——互相遮蔽。所以按字符走一遍，
    顺带把"注释"和"字符串"分开：注释要丢掉，字符串要留下（U2 要查的正是后者）。
    """
    out = list(text)
    literals: list[tuple[int, str]] = []
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char == "/" and following == "/":
            stop = text.find("\n", index)
            stop = len(text) if stop < 0 else stop
            _blank(out, text, index, stop)
            index = stop
            continue
        if char == "/" and following == "*":
            stop = text.find("*/", index + 2)
            stop = len(text) - 2 if stop < 0 else stop
            _blank(out, text, index, min(stop + 2, len(text)))
            index = min(stop + 2, len(text))
            continue
        if char in "\"'`":
            end = _string_end(text, index)
            if end is None:
                index += 1
                continue
            literals.append((index + 1, text[index + 1 : end]))
            _blank(out, text, index, end + 1)
            index = end + 1
            continue
        if char == "/" and _regex_can_start(text, index):
            end = _regex_end(text, index)
            if end is not None:
                _blank(out, text, index, end + 1)
                index = end + 1
                continue
        index += 1
    return "".join(out), literals


def jsx_text_runs(masked: str) -> list[tuple[int, str]]:
    """脱注释/字符串后的文本里，`>` 与 `<` / `{` 之间那段**像文字**的内容。

    两处刻意的取舍：

    1. **允许跨行**（`<p>` 换行再写说明的写法才是常态——第一版只认同一行，
       于是"文件夹或 .zip 都行：… SKILL.md"这种整段漏了，反向验证时才发现）；
    2. **不含代码味字符**（`()=;&|!?[]*+` 与引号）才算文字：TS 里 `a > b` 这种比较
       也会落进 `>`…`<` 之间，那段带一个 `(` 或 `=` 就被排掉。代价是
       "只含英文单词的 JSX 文本"可能漏（本仓库的用户可见文案是中文，
       英文串几乎都在字符串字面量里，那条路照查）。
    """
    found: list[tuple[int, str]] = []
    for match in re.finditer(r">([^<>{}]*)", masked):
        run = match.group(1)
        if not run.strip():
            continue
        if U2_TEXT_LIKE_FORBIDDEN & set(run):
            continue
        lead = len(run) - len(run.lstrip())
        found.append((match.start(1) + lead, run.strip()))
    return found


def _is_module_specifier(text: str, start: int) -> bool:
    """这个字符串是不是 import/export 的模块说明符（`from '…'` / `import('…')`）。

    模块路径永远不会渲染，不该按文案查。判断只看字面量前面那一小段：
    去掉空白**与那个开引号**后以 `from` / `import(` / `import` / `require(` 收尾就算。
    （`start` 是引号后一格，所以先要剥掉引号——第一版漏了这一步，
    于是 `'@/app/routes'` 这四条别名 import 全被误报成"代码路径上了界面"。）
    """
    head = text[max(start - 40, 0) : start].rstrip()
    if head and head[-1] in "'\"`":
        head = head[:-1].rstrip()
    return head.endswith(("from", "import(", "import", "require("))


def _u2_allowed(relative: str, value: str) -> bool:
    """这个文件里的这个字面量在例外表里吗（例外表按「文件 + 片段」匹配）。"""
    return any(
        relative == path and fragment in value for path, fragment, _reason in U2_ALLOWED
    )


def check_impl_leak(path: Path, root: Path) -> list[Violation]:
    """U2：界面文案里不许出现实现细节（见 ``U2_MSG`` 与 ``U2_PATTERNS``）。

    扫三类地方：字符串字面量、模板串、JSX 文本节点；**注释、import 说明符与 `.d.ts` 不算**
    （理由见 U2 那一段的头注释）。`.d.ts` 是纯类型声明——它在编译后不存在，一行都渲染不出来，
    而 `schema.d.ts`（生成的接口清单）里全是 `/api/v1/...` 这类**类型字面量**，
    把它们算成文案只会让这条规则天天红着、然后被绕过。
    """
    if path.suffix not in (".ts", ".tsx") or path.name.endswith(".d.ts"):
        return []
    relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    masked, literals = strip_ts_comments_and_strings(text)
    found: list[Violation] = []
    for start, value in literals + jsx_text_runs(masked):
        if _is_module_specifier(text, start) or _u2_allowed(relative, value):
            continue
        for label, pattern in U2_PATTERNS:
            match = pattern.search(value)
            if match is None:
                continue
            lineno = text.count("\n", 0, start) + 1
            snippet = " ".join(value.strip().split())[:60]
            found.append(
                Violation("U2", path, lineno, f"{U2_MSG}——{label}：{match.group(0)!r}（{snippet!r}）")
            )
    return found


# ---------------------------------------------------------------- U3：解释性长句
#
# 用户第二轮反馈（2026-09-24，语气很重："到底有没有再认真检查。我就上去看了一分钟，
# 就发现了好多类似的地方"）。他圈的两句都在刚被删掉的「工作区」页上（原文见下），
# 共同点是**在解释"这个东西怎么运作 / 会有什么后果 / 为什么这样"**，
# 而不是让用户能做成一件事：原话"不需要你解释这是个什么东西"，他是科班工程师。
#
#   `不绑也行：对话里打 @ 就能把某个库并进检索范围，或用输入框的「知识库」临时勾
#    ——每一轮各算各的。`
#   `工作区不复制文件：它只是"指向"你指定的目录。所以删工作区不会动你的文件，
#    改根目录也只是换一个指向。`
#
# **U1 抓不住它们**：U1 按类名判（`*-hint` / `*-desc` 那几族），而这两句写在**合法类名**
# 的元素里（`m-foot-note` / `kb-modal-note`，都是"有用途"的类，进不了黑名单）；
# U2 也抓不住：它们用的都是中文白话，没有路径/端口/版本串。
# 所以 U3 换成按**句子的形状**判，两条同时成立才命中：
#
# 1. **中文 ≥ 12 字**（只数汉字：标点、数字、英文都不算）——短标签不是句子
#    （"每轮注入"、"最多 500 字"这类数据/约束不在此列）；
# 2. 含一个"解释机制"的连接词/句式（`U3_CONNECTORS`）。
#
# 例外按「文件 + 字面量片段」列进 `U3_ALLOWED`，逐条写理由。**允许的只有四类**，
# 它们都不是"解释这一页/这个东西"：
# ① 错误/失败文案（"连不上…请检查网络"——那是让人行动的）；
# ② 空态（"还没有用量记录。提问或上传文档之后这里会有数据。"）；
# ③ 表单字段约束（"至少 8 个字符"、"至少 8 个字符"、上限、格式）；
# ④ 危险动作的后果确认（"它里面的 1 个子文件夹会被一起删除"——动手前必须知道的）。
#
# 盲区（这一轮扫出来、但机械判据兜不住的，留给出下一版）：**没有连接词的解释句**
# 也真实存在，例如"知识库是最外层的容器，每个库对应一套 embedding 模型与一组切分参数"
# （定义句式）、"页面越多耗时越长"（越…越…）、"到点自动跑一句话，结果落在一条同名会话里"
# （描述动作链）。这一轮靠人眼逐屏扫掉；要机械化得先想清楚怎么不误伤数据文案。

#: 连接词/句式族。**按实据收，每条都能指着一条真实漏网**（用户给的清单也要过这一关）：
#:
#: - 用户圈的两句原文：`每一轮` / `各算各的` / `也行`（第一句）、`只是` / `所以` / `不会`（第二句）；
#: - 本轮全项目扫到的真实文案（删掉的那批）：`其实`（"一开始其实是…"式的改写）、`也是`…
#:   实读命中的有 `只是` / `不会` / `所以` / `用于` / `即可` / `这就是` / `那是` / `它会` /
#:   `每轮`（记忆页"核心四份每轮整份注入提示词…"、TracePanel 同一句话）/
#:   `决定`（"放在哪个目录决定它怎么生效"、"决定这个库的向量空间"）；
#: - 与上列同族的另一半（同一句式换一个连接词）：`因为`（`所以` 的因半）、`并不` /
#:   `意思是` / `指的是` / `用来` / `可以通过` / `就行` / `换句话说` / `说白了`；
#: - **还没漏到屏上、但同一句式的**：`保证` / `避免` / `防止` / `一旦` / `否则` /
#:   `注意：` / `提示：` / `说明：`。这四个字词在本仓库的**注释**里高频出现——
#:   注释里这么写没问题，搬到屏幕上就是解释。现行代码命中 0 条：留着是护栏，
#:   不是抓现行（先宽后窄的那一半，等它真报出假命中再往下收）。
U3_CONNECTORS: tuple[str, ...] = (
    "因为",
    "所以",
    "也就是说",
    "其实",
    "只是",
    "不会",
    "并不",
    "指的是",
    "意思是",
    "用来",
    "用于",
    "可以通过",
    "就行",
    "也行",
    "即可",
    "各算各的",
    "每轮",
    "每一轮",
    "这就是",
    "那是",
    "它会",
    "决定",
    "保证",
    "避免",
    "防止",
    "一旦",
    "否则",
    "换句话说",
    "说白了",
    "注意：",
    "提示：",
    "说明：",
)

#: 例外：(相对仓库根的 POSIX 路径, 字面量里必须出现的片段, 理由)。
#: 理由一律回答同一个问题：**它为什么属于上面那四类之一，而不是"解释这一页"**。
#: 这张表**默认空着**：加一条要写清理由，不然它会长成一条通道。
U3_ALLOWED: tuple[tuple[str, str, str], ...] = (
    # ④ 危险动作的后果确认：动手前必须让人知道"什么会被删、什么不会被删"。
    (
        "frontend/src/features/misc/capabilities/CapabilitiesPage.tsx",
        "只删登记信息",
        "④ 删除 MCP 插件登记前的后果确认：说清「只删登记、不动那个服务」，点下去之前必须知道",
    ),
    (
        "frontend/src/features/misc/capabilities/CapabilitiesPage.tsx",
        "只删本地这份",
        "④ 卸载技能前的后果确认：删哪些、GitHub 上那份还在、能从市场装回来",
    ),
    (
        "frontend/src/features/misc/tasks/SchedulePanel.tsx",
        "它已经跑出来的会话不会被删",
        "④ 删除定时任务前的后果确认：跑出来的会话留着",
    ),
    (
        "frontend/src/features/misc/tasks/TasksPage.tsx",
        "只是不再处理",
        "④ 撤销排队任务的后果确认：文档与已入库内容保留、正在执行的不受影响",
    ),
    (
        "frontend/src/features/notes/NotesView.tsx",
        "篇笔记会回到未归档，不会被删除",
        "④ 删除归档目录前的后果确认（笔记归另一个泳道，标准同一口径）",
    ),
    (
        "frontend/src/features/notes/NotesView.tsx",
        "删除后无法恢复",
        "④ 删除笔记前的后果确认 + 影响范围（生成的知识库文档不受影响）",
    ),
    (
        "frontend/src/features/layout/ConversationRowMenu.tsx",
        "删除后不可恢复",
        "④ 删除会话前的后果确认，并给出替代动作（归档，可取消）",
    ),
    (
        "frontend/src/features/layout/WorkspaceRowMenu.tsx",
        "里面的会话不会被删",
        "④ 删除项目前的后果确认：说清「会话不会被删、只是退回对话栏」，"
        "并给出替代动作（归档，可取消）",
    ),
    # ① 失败/停滞态：说清"现在怎么了、能做什么"。
    (
        "frontend/src/api/local.ts",
        "批没跑完（重跑同一来源即可续上）",
        "① 失败态：`planned`/`running` 的导入批次是**上次没跑完**（被杀/断电）留下的，"
        "这一句说的是「现在这样、可以怎么办」；顶栏那条状态条上只有这一处提到它",
    ),
    (
        "frontend/src/features/misc/settings/KnowledgeConnectionSection.tsx",
        "连不上，所以现在看不到任何库",
        "① 失败态（ 阶段 6）：「看得见的库」那一块在后端**没给出原因**时的兜底一句——"
        "空清单会被读成「一个库都没有」，而这句说的是「现在读不到、因为连不上」；"
        "有原因时显示的是后端那句原话，这一句只在原因为空时出现",
    ),
    (
        "frontend/src/features/misc/settings/KnowledgeConnectionSection.tsx",
        "哪几个库参与检索",
        "③ 表单字段约束（ 阶段 6）：这一节只管「连接」，**不设默认库集**——"
        "不含这一句，用户会在这里找一个「默认查哪些库」的开关"
        "（而它由对话输入框那个「知识库」开关决定，方式 4.1 明确要求写清，"
        "避免再发明一个概念）",
    ),
    # ② 空态/首态：现在是什么、点哪儿开始。
    # 数据：字符串本身就是内容（发出去的正文 / 系统提示词原文），不是对界面的解释。
    (
        "frontend/src/features/chat/runtime/ChatProvider.tsx",
        "哪些只是推测",
        "数据：预设提问正文——它就是用户要发出去的那句话（含「只是」两字纯属巧合）",
    ),
    (
        "frontend/src/api/chat.ts",
        "都只是文档内容的一部分",
        "数据：内置系统提示词正文（发模型看的），弹窗里只读展示，不是界面说明",
    ),
    # ---- 以下两条是 kybase 管理台自己的（2026-10-09 搬迁后按本仓现实补的白名单）----
    # kylab 那份白名单是按 kylab 前端的页面群写的，对不上 kybase 的管理台；这两处
    # 逐条看了都落在「允许的四类」里，不是"解释这一页/这个东西怎么运作"。
    (
        "frontend/src/features/knowledge/ProcessingTimeline.tsx",
        "这一步停住了",
        "① 失败/停滞态：文档处理卡住时的那一句——说清「现在这样（进程重启过）」"
        "与「能怎么办（它会自动重跑，或手动重新摄入）」。只说「卡住」等于把问题丢回给用户；"
        "而这段实现的另一半（心跳/续约怎么算）已经被删掉了，留的正是①那一类",
    ),
    (
        "frontend/src/features/misc/auth/LoginPage.tsx",
        "第一次使用：创建管理员账号即可进入。",
        "② 首态：还没建管理员账号时，这一句是「现在该点哪儿开始」的落点（登录表单上方唯一一句）。"
        "它被 `frontend/tests/misc-auth.test.tsx` 逐字断言着——那是一条与本泳道无关的既有用例，"
        "改这里等于顺手改它的断言",
    ),
)

#: 汉字计数只认 CJK 统一汉字：标点（，。「」——）、数字、英文都不算。
#: 用 12 这个阈值，是因为 10 字以下常常是标签/状态（"每轮注入"、"最多 500 字"），
#: 而解释一句机制很难短于 12 个汉字（用户圈的两句分别是 32 / 34 字）。
U3_MIN_CJK = 12

U3_MSG = (
    "界面里的解释性长句（在解释「这东西怎么运作 / 会有什么后果 / 为什么这样」）："
    "把它删掉——用户不需要在界面上被解释。确实有用途时只有四类：错误/失败文案、"
    "空态、表单字段约束、危险动作的后果确认；那四类要在 `U3_ALLOWED` 里按"
    "「文件 + 字面量」加一条并写清理由"
)


def cjk_length(value: str) -> int:
    """汉字个数（标点/数字/英文不算）——U3 的第一条判据。"""
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _u3_allowed(relative: str, value: str) -> bool:
    """这个文件里的这个字面量在例外表里吗（例外表按「文件 + 片段」匹配）。"""
    return any(
        relative == path and fragment in value for path, fragment, _reason in U3_ALLOWED
    )


def check_explanatory_sentence(path: Path, root: Path) -> list[Violation]:
    """U3：界面里不许出现"解释机制"的长句（见 ``U3_MSG`` 与 ``U3_CONNECTORS``）。

    与 U2 扫的是同一批"会渲染出来的字符"（字符串字面量 + JSX 文本；注释不算），
    判据换成"长度 + 连接词"两条同时成立——U1 的类名族与 U2 的词表都拦不住
    "用合法类名写着的一段解释"，那正是用户这次圈的漏网。
    """
    if path.suffix not in (".ts", ".tsx") or path.name.endswith(".d.ts"):
        return []
    relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    masked, literals = strip_ts_comments_and_strings(text)
    found: list[Violation] = []
    for start, value in literals + jsx_text_runs(masked):
        if _is_module_specifier(text, start) or _u3_allowed(relative, value):
            continue
        if cjk_length(value) < U3_MIN_CJK:
            continue
        word = next((item for item in U3_CONNECTORS if item in value), None)
        if word is None:
            continue
        lineno = text.count("\n", 0, start) + 1
        snippet = " ".join(value.strip().split())[:60]
        found.append(Violation("U3", path, lineno, f"{U3_MSG}——「{word}」：{snippet!r}"))
    return found


def check_ps1_bom(root: Path) -> list[Violation]:
    """S1：``scripts/*.ps1`` 必须带 UTF-8 BOM。

    Windows PowerShell 5.1 会把无 BOM 的 .ps1 当 GBK 解码，中文字符串直接变乱码、
    脚本以 ParserError 崩掉——门禁脚本自己就是受害者。编辑器/工具改写文件时极易丢掉 BOM，
    故用机械检查兜住，而不是靠人记得。

    **只查我们自己写的脚本**：`node_modules/` 里的 `.ps1` 是 npm 生成的启动垫片
    （`scripts/deck/node_modules/.bin/*.ps1`），既不入库也不由我们保存，作者改不了它，
    报它只会让"这条规则红了"变成噪音——与 `scripts/scan_emoji.py` 的 `SKIP_DIRS` 同一条处置。
    """
    violations: list[Violation] = []
    scripts_dir = root / "scripts"
    if not scripts_dir.exists():
        return violations
    generated = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    for path in sorted(scripts_dir.rglob("*.ps1")):
        if generated & set(path.parts):
            continue
        if not path.read_bytes().startswith(b"\xef\xbb\xbf"):
            violations.append(
                Violation(
                    "S1",
                    path.relative_to(root),
                    1,
                    "缺少 UTF-8 BOM，PowerShell 5.1 会按 GBK 解码导致中文乱码与解析失败；"
                    "请以 UTF-8 with BOM 重新保存",
                )
            )
    return violations


# ---------------------------------------------------------------- 版本号一致性（V1）
#
# 版本号是**多处手写副本**：pyproject、package.json、Settings.app_version，
# 加上 compose 的默认标签（backend / frontend 各一处）。MCP Server 原本也是手写副本，
# v0.2.0 起改为读 Settings。
#
# 这条约定一直写在 CHANGELOG 的「附：版本号约定」里，但**没有任何检查**——
# 于是 0.1.0 → 0.2.0 那次升级才发现实际有六处，而文档说的是三处
# （见开发记录12.171）。手写约定不加机械核查的失效方式，与 12.32 记的"文档绿着撒谎"
# 一模一样，故补这条 V1。

VERSION_MSG = "手写版本号必须一致（约定见 CHANGELOG「附：版本号约定」）"


def _quoted_after(text: str, marker: str) -> str | None:
    """``marker`` 之后第一对双引号里的内容；找不到返回 None。"""
    start = text.find(marker)
    if start < 0:
        return None
    rest = text[start + len(marker) :]
    end = rest.find('"')
    return rest[:end] if end >= 0 else None


def version_sources(root: Path) -> dict[str, set[str]]:
    """各处手写版本号 → 读到的一个或多个值。

    一份文件里出现多次就有多个值（compose 有两处默认标签），**两个都要对**——
    只取第一个的话，改一处漏一处照样绿。

    文件不存在就跳过：这个脚本要能在只检出部分目录时跑，否则会误报。
    用 ``errors="replace"`` 读：被存成别的编码的文件不该让整条规则崩掉。
    """

    def read(relative: str) -> str | None:
        path = root / relative
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    found: dict[str, set[str]] = {}

    for relative, marker in (
        ("backend/pyproject.toml", 'version = "'),
        ("frontend/package.json", '"version": "'),
        ("backend/app/core/config.py", 'app_version: str = "'),
    ):
        text = read(relative)
        if text is None:
            continue
        value = _quoted_after(text, marker)
        if value:
            found[relative] = {value}

    compose = read("deploy/docker-compose.yml")
    if compose is not None:
        marker = "${KYLAB_VERSION:-"
        values: set[str] = set()
        cursor = 0
        while True:
            index = compose.find(marker, cursor)
            if index < 0:
                break
            cursor = index + len(marker)
            end = compose.find("}", cursor)
            if end < 0:
                break
            values.add(compose[cursor:end])
        if values:
            found["deploy/docker-compose.yml"] = values

    return found


def check_css_layers(root: Path) -> list[Violation]:
    """C1：前端 CSS 的分层纪律（见 ``CSS_LAYER_BLOCK_MSG`` / ``CSS_UNLAYERED_MSG``）。

    规则是"扫描 + 分类"，不是完整 CSS 解析器——只认这个仓库会出现的形状：
    层序声明（`@layer a, b;`）、层块（`@layer components { … }`）、条件组
    （`@media { … }`）、以及普通样式规则。**大括号按配对计数**，注释先换成空格
    （长度不变，所以行号与字符位置都还对得上）。

    为什么要自己扫而不是用现成的解析器：这个脚本要能在**只有标准库**的环境里跑
    （CI 与本地门禁都直接 `python scripts/…`），引入解析器依赖不值当。
    """
    frontend_src = root / "frontend" / "src"
    if not frontend_src.exists():
        return []

    violations: list[Violation] = []
    for path in sorted(frontend_src.rglob("*.css")):
        text = path.read_text(encoding="utf-8", errors="replace")
        masked = _mask_css_comments(text)
        items = _css_items(masked, 0, len(masked))
        display = path.relative_to(root)
        posix = display.as_posix()

        statements = [item for item in items if item[0] == "statement"]
        has_layer_block = any(
            item[0] == "block" and _at_rule_name(item[3]) == "layer" for item in items
        )
        rules = _css_unlayered_rules(masked, items, inside_layer=False)

        # 判据一：开层文件必须先显式声明层序
        declared = any(
            _at_rule_name(prelude) == "layer"
            and _layer_statement_names(prelude) == CSS_LAYER_ORDER
            for _, _, _, prelude in statements
        )
        if (has_layer_block or rules) and not declared:
            if posix not in CSS_UNLAYERED_FILES:
                violations.append(
                    Violation(
                        "C1", display, 1, f"{CSS_LAYER_BLOCK_MSG}（期望：{'、'.join(CSS_LAYER_ORDER)}）"
                    )
                )

        # 判据二：层外的普通样式规则
        if posix in CSS_UNLAYERED_FILES:
            continue
        for offset, selector in rules:
            if _marked_unlayered(text, offset):
                continue
            lineno = text.count("\n", 0, offset) + 1
            violations.append(Violation("C1", display, lineno, f"{CSS_UNLAYERED_MSG}（{selector}）"))
    return violations


def _mask_css_comments(text: str) -> str:
    """把注释内容换成空格（换行保留）——注释里的 `{` `}` `@layer` 不能参与扫描。

    长度与换行位置都不变，于是**字符偏移与行号可以直接用在原文上**（找 `@unlayered:` 标记时要）。
    """
    out = list(text)
    cursor = 0
    while True:
        start = text.find("/*", cursor)
        if start < 0:
            break
        end = text.find("*/", start + 2)
        end = len(text) - 2 if end < 0 else end
        for index in range(start, min(end + 2, len(text))):
            if out[index] != "\n":
                out[index] = " "
        cursor = end + 2
    return "".join(out)


def _css_items(masked: str, start: int, stop: int) -> list[tuple[str, int, int, str]]:
    """把 `[start, stop)` 里的顶层条目拆开：``(类型, 起始偏移, 结束偏移, prelude)``。

    类型是 ``statement``（以 `;` 结束，如 `@import …` / `@layer a, b;`）或
    ``block``（以 `{ … }` 包裹，如 `.a { }` / `@media … { }`）。
    """
    items: list[tuple[str, int, int, str]] = []
    index = start
    while index < stop:
        char = masked[index]
        if char in " \t\r\n}":
            index += 1
            continue
        prelude_start = index
        while index < stop and masked[index] not in "{;":
            index += 1
        if index >= stop:
            break
        prelude = masked[prelude_start:index].strip()
        if masked[index] == ";":
            items.append(("statement", prelude_start, index + 1, prelude))
            index += 1
            continue
        depth = 0
        body_end = stop
        cursor = index
        while cursor < stop:
            if masked[cursor] == "{":
                depth += 1
            elif masked[cursor] == "}":
                depth -= 1
                if depth == 0:
                    body_end = cursor
                    break
            cursor += 1
        items.append(("block", prelude_start, body_end + 1, prelude))
        index = body_end + 1
    return items


def _at_rule_name(prelude: str) -> str:
    """prelude 若是 at-rule，返回它的名字（小写，不含 `@`）；否则返回空串。"""
    if not prelude.startswith("@"):
        return ""
    return prelude[1:].split()[0].lower() if len(prelude) > 1 else ""


def _layer_statement_names(prelude: str) -> tuple[str, ...]:
    """`@layer theme, base, components, utilities;` → 四个名字（去空白）。"""
    if _at_rule_name(prelude) != "layer":
        return ()
    names = prelude.split(maxsplit=1)
    if len(names) < 2:
        return ()
    return tuple(name.strip() for name in names[1].split(","))


def _css_unlayered_rules(
    masked: str, items: list[tuple[str, int, int, str]], inside_layer: bool
) -> list[tuple[int, str]]:
    """层外的普通样式规则：``(起始偏移, 选择器)``。

    层块里的规则直接跳过（它们已经进层）；条件组（`@media` …）不产生新层，要钻进去；
    其余 at-rule（关键帧、字体、`@theme`…）与级联无关，跳过。
    """
    found: list[tuple[int, str]] = []
    for kind, start, end, prelude in items:
        name = _at_rule_name(prelude)
        if kind == "statement":
            continue
        if name == "layer":
            continue
        if name in CSS_CONDITIONAL_RULES:
            inner = _css_items(masked, masked.index("{", start) + 1, end - 1)
            found.extend(_css_unlayered_rules(masked, inner, inside_layer))
            continue
        if name:
            continue
        if not inside_layer:
            found.append((start, " ".join(prelude.split())))
    return found


def _marked_unlayered(text: str, offset: int) -> bool:
    """规则上方（中间只隔空行或注释）有没有 `/* @unlayered: … */` 标记。

    两条约束，都是被实测逼出来的：

    1. **只认紧邻的那一串注释**：隔一条别的规则就不算——否则一句标记会顺着文件一路罩下去，
       那就成了"写一次、后面全都放行"的通道；
    2. **标记必须写在注释开头**：`/* 上面那条为什么用 @unlayered: 标记…… */` 这种
       "注释里提到标记本身"的写法**不放行**——第一版就是按"注释里出现过这几个字"判的，
       结果那段解释性注释把下面那条层外规则也罩住了（反向验证时脚本该红没红）。
    """
    cursor = offset
    while True:
        probe = cursor - 1
        while probe >= 0 and text[probe] in " \t\r\n":
            probe -= 1
        if probe < 0:
            return False
        if text[probe] != "/":  # 只可能是注释的收尾 `*/`；别的字符说明上面是内容
            return False
        comment_start = text.rfind("/*", 0, probe)
        if comment_start < 0:
            return False
        body = text[comment_start + 2 : probe - 1].lstrip(" \t\r\n*")
        if body.startswith(CSS_UNLAYERED_MARKER):
            return True
        cursor = comment_start


def check_app_root_modules(root: Path) -> list[Violation]:
    """L6：``app/`` 根下只允许 ``main.py`` 与 ``__init__.py``（见 ``APP_ROOT_MSG``）。

    与其它规则不同，这条查的是"文件在不在规则覆盖范围内"——它不解析 import，
    只看目录。所以它对新增文件立刻生效，不需要有人记得去补一份清单。
    """
    app_dir = root / "backend" / "app"
    if not app_dir.exists():
        return []
    return [
        Violation("L6", path.relative_to(root), 1, APP_ROOT_MSG)
        for path in sorted(app_dir.glob("*.py"))
        if path.stem not in APP_ROOT_ALLOWED
    ]


def check_version_consistency(root: Path) -> list[Violation]:
    """V1：各处手写版本号必须一致。基准取 `backend/pyproject.toml`。"""
    found = version_sources(root)
    if not found:
        return []

    reference = (
        "backend/pyproject.toml" if "backend/pyproject.toml" in found else sorted(found)[0]
    )
    own = found[reference]
    if len(own) != 1:
        # 同一份文件里自相矛盾（典型：compose 两处默认标签只改了一处）
        return [
            Violation(
                "V1",
                Path(reference),
                1,
                f"{VERSION_MSG}：同一份文件里出现多个版本号：{'、'.join(sorted(own))}",
            )
        ]
    expected = next(iter(own))

    picture = "；".join(
        f"{name}={'、'.join(sorted(values))}" for name, values in sorted(found.items())
    )
    return [
        Violation(
            "V1",
            Path(name),
            1,
            f"{VERSION_MSG}：期望 {expected}（取自 {reference}），此处是 "
            f"{'、'.join(sorted(values))}。全仓实读：{picture}",
        )
        for name, values in sorted(found.items())
        if values != {expected}
    ]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    backend_app = root / "backend" / "app"

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    violations: list[Violation] = []

    if backend_app.exists():
        for path in sorted(backend_app.rglob("*.py")):
            # 用 utf-8-sig 读：Windows 上被存成 UTF-8 with BOM 的源码是合法的 Python，
            # 用 utf-8 读会让 ast.parse 报 "invalid non-printable character U+FEFF"
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                violations.append(Violation("PARSE", path, exc.lineno or 1, f"语法错误：{exc.msg}"))
                continue
            violations.extend(check_layer_rules(path, root, tree))
            violations.extend(check_async_endpoints(path, root, tree))
            violations.extend(check_test_placement(path, root))

    frontend_src = root / "frontend" / "src"
    if frontend_src.exists():
        for path in sorted(frontend_src.rglob("*")):
            if path.is_file():
                violations.extend(check_test_placement(path, root))
                violations.extend(check_ui_copy(path))
                violations.extend(check_impl_leak(path, root))
                violations.extend(check_explanatory_sentence(path, root))

    violations.extend(check_ps1_bom(root))
    violations.extend(check_version_consistency(root))
    violations.extend(check_app_root_modules(root))
    violations.extend(check_css_layers(root))

    for violation in violations:
        print(violation)

    if violations:
        print(
            f"\n共发现 {len(violations)} 处违规，违反分层规则3.3 / 5.1、"
            f"脚本编码约定、界面文案条款（解释性小字 / 实现细节 / 解释性长句）、"
            f"CSS 分层纪律或 CHANGELOG「附：版本号约定」。"
        )
        return 1
    print("分层纪律、测试位置、脚本编码、界面文案（解释性小字 / 实现细节 / 解释性长句）、"
          "CSS 分层与版本号检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
