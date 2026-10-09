"""Agent 的工具集：给模型看的规格 + 执行器（P0）。

与 `app/services/tools.py` 的关系是**同一份实现、两个门**：对外 MCP 客户端调
`call_tool`，对内由 `services/tool_loop.py` 调这里的 runner。所以"知识库降级成一个工具"
几乎是免费的——`search` 早就是那 13 个工具之一，只是以前的对话循环没走它。

这个模块里只有三类是内部门特有的：

1. **技能工具**（`list_skills` / `read_skill`）：它们是"读自己身上的说明书"，
   对外部 MCP 客户端没有意义（那个客户端自己就是 agent）。
2. **检索要收在会话选定的库范围内**（见 `build_runner`）：知识库不再是回答的框架，
   但"这一轮允许查哪些库"仍然要说清楚——否则关掉开关之后，模型一句
   `search` 就能把库全查了，那个开关就成了摆设。
3. **外部 MCP 服务暴露的工具**（v0.20）：用户在能力页接进来的服务，它们的工具
   一并进这张表（名字带 `mcp__<服务>__<工具>` 前缀），所以模型能像用内置工具
   一样用它们。**准入策略在调用那一刻判**，见 ``_call_mcp``。

工具表里三段的顺序是**内置 → 技能 → 外部**：模型对"先看到什么"有轻微偏好，
前面两段是我们自己担保的，外部的东西排最后。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.caller import WRITE, Caller
from app.core.exceptions import InvalidRequestError, KylabError
from app.core.logging import sanitize_log_value
from app.services import isolation as isolation_service
from app.services import tool_meta
from app.services.agent_exec import run_command
from app.services.agent_files import (
    DEFAULT_READ_LINES,
    MAX_LIST_ENTRIES,
    MAX_READ_LINES,
    MAX_SEARCH_HITS,
    list_files,
    looks_binary_bytes,
    looks_binary_name,
    read_bytes,
    read_file,
    resolve_roots,
    search_files,
)
from app.services.chat import SourceRef
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_DENY,
    rules_from_runtime,
    suggest_rule,
)
from app.services.llm import ToolSpec
from app.services.mcp_client import normalized_server_name, split_qualified
from app.services.schedules import timezone_name
from app.services.skills import recombine_surrogates, text_problem
from app.services.subagent import parse_tool_content as parse_subagent_content
from app.services.tool_loop import ToolOutcome, ToolRunner
from app.services.tools import ARTIFACT_KEY, MAX_UPLOAD_BYTES, call_tool, tool_definitions

__all__ = ["build_runner", "tool_specs"]

logger = logging.getLogger(__name__)

#: 单个技能附件最多读多少字符。技能里的脚本可能很大，读进来就是上下文成本。
MAX_ATTACHMENT_CHARS = 20000

#: 一条外部工具结果最多回给模型多少字符（在 `tool_loop.MAX_RESULT_CHARS` 之外再收一道）。
#: 外层那个上限是给所有工具的；外部服务的返回不受我们约束，实测有的服务会把
#: 整个网页塞回来。
MAX_MCP_RESULT_CHARS = 8000

#: `list_skills` 一页列几条（§12.341 ⑤）。近两百个技能全量输出一次是 12,024 字（被截断），
#: 而这一条是"看状态"，不是把目录搬进上下文——所以默认一页 40 条（约 40 行）。
SKILLS_PAGE_SIZE = 40
#: 单次最多列几条：调用方要更多也不给（否则分页形同虚设，那个 12,024 字又回来了）。
SKILLS_PAGE_MAX = 60

_SKILL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_skills",
        "description": (
            "列出这台机器上的技能（SOP 文档）**安装与可用状态**。"
            "**可用技能的名字、描述与文件位置每一轮已经在你的系统提示词里**（"
            "「可用技能」那一段），所以平时不必调它；"
            "只有要确认「某个技能为什么不可用」（被安全扫描拦下、依赖没满足、或被丢弃）时才看。"
            f"**一次最多列 {SKILLS_PAGE_SIZE} 条**（列表长了会挤出上下文）："
            "还有更多就用 `offset` 翻页（返回里会告诉你下一页的 offset）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer",
                    "description": (
                        f"从第几个开始列（默认 0）：翻页时用它，每页 {SKILLS_PAGE_SIZE} 条。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"这一页列几条（默认 {SKILLS_PAGE_SIZE}，最多 {SKILLS_PAGE_MAX}）。"
                    ),
                },
            },
        },
    },
    {
        "name": "spawn_subagent",
        "description": (
            "派一个子 Agent 去做一件**自包含**的事，把它的结论拿回来。"
            "它看不到我们这段对话，所以任务描述要写全（问什么、依据什么）。"
            "适合「需要啃一批资料才能得到一句话结论」的活；"
            "**它没有派生能力、有轮次与时限**，简单的事自己做更快。"
            "它交回的是**结论 + 六项结构化结果**（关键发现 / 证据或引用 / 已做的决策 / "
            "更改的文件 / 风险与置信度 / 建议的下一步）：引用它的结论前先看**置信度与风险**，"
            "下一步可以照它给的建议走；某一项是空的，就是**它这一项没交回来**，"
            "不要当成「它没有这件事」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要它回答的问题，自包含、具体",
                }
            },
            "required": ["task"],
        },
    },
    {
        "name": "read_skill",
        "description": (
            "读一个技能的正文（它的流程与注意事项）——**正文不在提示词里，只能这样取**。"
            "技能名用系统提示词「可用技能」那一段里的名字（每行开头的那个）。"
            "带 `file` 参数时读该技能的附属文件（脚本、模板、参考）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名，来自系统提示词的技能目录"},
                "file": {
                    "type": "string",
                    "description": "附属文件的相对路径；省略则读 SKILL.md 正文",
                },
            },
            "required": ["name"],
        },
    },
)

#: **这台机器上的能力**（v0.33）：文件、执行、表格副本。
#:
#: 与 ``_SKILL_TOOLS`` 同一档：**只有对话这条门有**。它们的共同点是
#: "依赖这一轮的上下文"——工作区（会话挂的那个目录）、沙箱（这条会话的试错目录）、
#: 以及会话上选定的库范围。外部 MCP 客户端这三样都给不出，
#: 给它一个凭据不明的根或一份没有范围的表，比不给更糟。
_LOCAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_files",
        "description": (
            "列出一个目录里有**什么**（工作区或沙箱）。"
            "**不知道文件名时先列一遍**——search_files 要先知道搜什么。"
            "默认跳过 node_modules / .git / __pycache__ 这类依赖与缓存目录。"
            "给的路径是相对路径，可以直接回传给 read_file。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录的相对路径；留空 = 根目录"},
                "where": {
                    "type": "string",
                    "enum": ["workspace", "sandbox"],
                    "description": (
                        "看哪一侧：workspace = 对方的真实项目目录（默认），"
                        "sandbox = 你自己跑命令时的试错目录"
                    ),
                },
                "pattern": {"type": "string", "description": "按名字过滤，如 *.py；留空 = 全列"},
            },
        },
    },
    {
        "name": "read_file",
        "description": (
            "读一个文本文件的内容（按行分页，默认前 400 行）。"
            "**想看某个文件里到底写了什么时用它**；文件很长时会告诉你总行数，"
            "接着读用 offset。压缩包 / 图片 / Office 读不出文本，出路按这个顺序："
            "**① 压缩包先用 `run_command` 解开**（`unzip -o 包 -d 临时目录`）"
            "**再读解压出来的文件**；"
            "② 想先了解它是什么，用 `run_command` 跑 `file` / `ls -l` 看元信息；"
            "**③ 只有在用户明确要求把这份材料收进知识库时**才用 `ingest_file` —— "
            "别把知识库当「读不了时的迂回手段」，那是替用户做决定。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件的相对路径"},
                "where": {
                    "type": "string",
                    "enum": ["workspace", "sandbox"],
                    "description": "同 list_files；留空 = 有工作区就用工作区",
                },
                "offset": {"type": "integer", "description": "从第几行开始（从 1 计），默认 1"},
                "limit": {"type": "integer", "description": "读多少行，默认 400，上限 2000"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "在工作区里**按内容搜**，回「哪个文件、第几行、那一行是什么」。"
            "**想知道某个函数在哪定义、某个配置在哪写的、哪个文件提到过某个词时用它**"
            "——比逐个文件读省得多。支持正则。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "要找的内容，支持正则"},
                "path": {"type": "string", "description": "在哪个子目录里搜；留空 = 整个根"},
                "where": {
                    "type": "string",
                    "enum": ["workspace", "sandbox"],
                    "description": "同 list_files",
                },
                "ignore_case": {"type": "boolean", "description": "忽略大小写，默认 true"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "**在沙箱里执行一条命令**，把输出拿回来。"
            "命令行的工作目录是沙箱目录（绝对路径见工具结果），工作区会被挂载成读写。"
            "适合：跑一段脚本算数、处理刚生成的文件、看依赖装没装、批量改名。"
            "**默认断网**（要联网得显式说明理由并让用户放行）。"
            "三道闸都在：不是管理员、策略没放行、机器上没有内核级隔离，"
            "这三种情况都会**明确拒绝并告诉你怎么放开**——被拒时不要重试同一条命令。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要跑的命令（会被按 shell 词法拆成参数，但**不走 shell**）",
                },
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "也可以直接给参数数组（更精确，推荐）；与 command 二选一",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        f"超时秒数，默认 {int(isolation_service.DEFAULT_TIMEOUT_SECONDS)}"
                    ),
                },
                "allow_network": {
                    "type": "boolean",
                    "description": "是否允许联网（默认 false = 断网）",
                },
            },
        },
    },
    {
        "name": "list_tables",
        "description": (
            "列出**有结构化副本的表格文档**（入库的 CSV / Excel），带表名、列名与行数。"
            "**要回答统计类问题（一共多少、哪个月最高、按人汇总）时先调它**，"
            "再用 query_table 查——检索只给最相关的几行，答不了「一共」。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_table",
        "description": (
            "对表格副本跑一条**只读 SQL**（DuckDB 语法，只能 SELECT / WITH）。"
            "表名就是 list_tables 给的那个 document_id，列名就是 CSV 的表头。"
            "**聚合统计必须走它**：`SELECT sum(金额) FROM doc_xxx`。"
            "结果最多回几百行——要精确的数字请让 SQL 自己算（sum / count / group by）。"
            "它不能改数据、不能读写文件、也不能查这一轮范围之外的文档。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "一条 SELECT / WITH 查询"},
                "limit": {"type": "integer", "description": "最多回多少行，默认 100"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "**挂一条定时任务**：到点自动替对方跑这句话，结果落在一条会话里。"
            "只在对方明确说「以后每天/每周…帮我做这件事」时才调——"
            "**不要替他决定要不要定时**。时间按**服务器时区**解释（工具结果里会说明是哪个时区）；"
            "一次性的事用 once + 具体时间，周期性的事用 cron（5 字段：分 时 日 月 周）。"
            "建完请把「什么时候跑、跑什么、结果在哪看」告诉对方。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名（会用它当那条会话的标题）"},
                "prompt": {
                    "type": "string",
                    "description": (
                        "到点要问的那句话，写具体（如「把昨天的构建日志汇总成三条结论」）"
                    ),
                },
                "cron": {
                    "type": "string",
                    "description": (
                        "5 字段表达式：分 时 日 月 周。"
                        "例：`0 9 * * *` = 每天 9:00；`30 8 * * 1` = 每周一 8:30"
                    ),
                },
                "run_at": {
                    "type": "string",
                    "description": (
                        "一次性的时刻（ISO 8601，如 2026-09-21T09:00）。给了它就是一次性任务"
                    ),
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "到点查哪些库；留空 = 跟随这条对话的库范围",
                },
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "list_scheduled_tasks",
        "description": (
            "列出已经挂上的定时任务（下次什么时候跑、上次跑成没跑成）。"
            "**对方问「我之前让你定时做的事呢」时用它**，别凭记忆答。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ---- 会话文件区（v0.55，用户报的"上传的图片文件不是应该能用来交互吗"）----
    #
    # 这两个工具补的是一个**真实的洞**：`list_files` / `read_file` 只看工作区与沙箱
    # 两个根，而**没挂工作区的会话**把用户上传的文件放在对象存储里——模型原先
    # 既列不到、也拿不到 artifact_id，于是"上传一张图问它"完全无从谈起。
    {
        "name": "list_conversation_files",
        "description": (
            "列**这条会话的文件**：对方上传的文件、以及你之前产出的文件都在这儿（平铺一层）。"
            "**对方说「我传的那个文件」时先列一眼**，不要凭文件名猜内容。"
            "项目目录里的文件（用户自己的项目文件）不在这里——那种用 `list_files`。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_conversation_file",
        "description": (
            "读会话文件区里的一份**文本**文件（按行分页，key 用 list_conversation_files 给的）。"
            "图片 / PDF / Office 这类二进制读不出文本，出路按这个顺序："
            "**① 压缩包先用 `run_command` 解开**（`unzip -o 包 -d 临时目录`）"
            "**再读里面的文件**；"
            "② 想先了解它是什么，用 `run_command` 跑 `file` / `ls -l`；"
            "**③ 只有在用户明确要求时**才用 `ingest_file` 加进知识库，"
            "切块完成后再 `search` 检索它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    # D34：模型照抄整行（`f art_x（2.3 KB）`）是实测发生过的失败路径，
                    # 所以"取哪一段"要写在参数说明里，与列表表头那句一致
                    "description": "列表里 `key=` 后面那一串（原样照抄，别带文件名与大小）",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "从第几行开始读，默认 1",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LINES,
                    "description": f"最多读几行，默认 {DEFAULT_READ_LINES}",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "ingest_file",
        "description": (
            "把**会话里已经有的一份文件**加进知识库（对方上传的、或你自己产出的都算）。"
            "`path` 给 `list_conversation_files` 的 key，或工作区/沙箱里的相对路径"
            "（后者可用 `where` 指定哪个根）。入库是异步的：返回 document_id 后"
            "用 `get_document_status` 查进度，处理完就能被 `search` 检索到。"
            "**图片 / PDF / Office 这类读不出文本的文件，看内容就只有这一条路。**"
            "大文件也走这个（`upload_document` 要把内容写成 base64 放进参数，只适合很小的文本）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "knowledge_base_id": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": (
                        "文件区的 key（list_conversation_files），或工作区/沙箱里的相对路径"
                    ),
                },
                "where": {
                    "type": "string",
                    "enum": ["workspace", "sandbox"],
                    "description": "按本机路径找时用哪个根；留空 = 有工作区就用工作区",
                },
            },
            "required": ["knowledge_base_id", "path"],
        },
    },
)

#: 这些工具**同样属于知识库那一侧**：关掉知识库开关时它们一起消失。
#: 判据是"数据从哪来"——表格副本就是入库文档的产物，用户关掉知识库时
#: 不该还留一条按 SQL 读库里内容的近路（与 ``_KB_TOOLS`` 同一条纪律）。
_LOCAL_KB_TOOLS = frozenset({"list_tables", "query_table", "ingest_file"})

#: 文件三件事：一趟走 ``agent_files`` 的那三个函数（它们共用"两个根"的解析）。
_FILE_TOOLS = frozenset({"list_files", "read_file", "search_files"})

#: **会话文件区**的两个工具（v0.55）：``list_conversation_files`` / ``read_conversation_file``。
#:
#: 为什么不并进 ``_FILE_TOOLS``：那一组认的是"两个根"（工作区 / 沙箱），而文件区
#: 是**另一层**——没挂工作区的会话把文件放在对象存储里，那里既不是工作区也不是沙箱
#: （见 ``services/artifacts.py`` 的 ``ArtifactSpot``）。三种落点各走各的门。
_CONVERSATION_FILE_TOOLS = frozenset({"list_conversation_files", "read_conversation_file"})

#: 记忆**关着时不该出现在工具表里**的工具。
#:
#: - ``recall``：关着时它会明确报"未启用长期记忆"（关的正是注入与 recall 这一对），
#:   而"给了又拒"正是知识库那一侧已经修过的坑（见 ``_KB_TOOLS``——模型会先试一次、
#:   再拿一句错误，白花一个来回）。
#:
#: **其余三件都不看这个开关**：``remember`` / ``forget`` 写的是档案（"关了也能改自己
#: 的东西"，§7.3），``read_memory`` 读的也是它。旧口径把 ``read_memory`` 藏起来，
#: 是因为它**只在给 recall 的片段做展开**时有入口；现在它读的是整份档案，
#: 与 recall 没有依赖关系。
_MEMORY_SWITCH_TOOLS = frozenset({"recall"})

#: 记忆这一侧的内部工具（对外 MCP 面只有 ``recall`` / ``remember`` / ``forget``）。
#:
#: **为什么需要 ``read_memory``**：档案虽然每轮已经注入，但模型写进去之后要能
#: **自查它落在哪、写成什么样**（编辑是"顶替一条"，而顶替目标必须逐字准确）。
#: 它读的是**磁盘上那份原样的 Markdown**（含 frontmatter 与用户手写的内容），
#: 比注入块多一层"文件是什么样"的事实。
#:
#: **``write_memory`` 已退场**（§7.4）：整份覆盖与条目级预算/变更流不相容，
#: 它等于给预算与变更流开一个后门。工具表里不再有它，但分发上留了一个
#: **一律拒绝**的兼容壳（见 ``_write_memory``）——老客户端与老提示词里可能还写着
#: 这个名字，让它拿到一句"改用 remember"比抛一个未知工具更可解释。
_MEMORY_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "read_memory",
        "description": (
            "读**用户档案**（`PROFILE.md`）的原文，用来自查它现在写了什么、"
            "某一条的确切文字是什么（更正时 `replaces` 要逐字对得上）。"
            "它读的是磁盘上那份 Markdown 全文，含四个分区与 frontmatter。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)


def _memory_on(services: Any) -> bool:
    """这一轮记忆开着没有。

    ``services`` 不给（单测、纯规格检查）时按**开着**算：那种调用问的是"工具的规格
    长什么样"，不是"这一轮用户开了什么"。同样地，给了 services 但里面没有 memory
    这一项时也不隐藏——**判不了就不动**，隐藏工具比多给一个工具的后果更隐蔽。
    """
    if services is None:
        return True
    memory = getattr(services, "memory", None)
    if memory is None:
        return True
    return bool(memory.enabled)


#: **知识库这一侧**的工具（v0.27）。
#:
#: 用户把会话上的知识库开关关掉时（这一轮的 ``kb_ids`` 为空），这些工具
#: **一个都不出现在工具表里**。改之前只是"检索会被拒绝"——工具照给，
#: 于是模型每轮都先 `list_knowledge_bases` 看一眼、再 `search` 一次，
#: 拿到一句"这一轮没有可查的知识库"，两个来回就这么花掉了
#: （用户报的现象："没开知识库，但每轮都去知识库检索"）。
#:
#: 边界**只画在知识库上**：记忆（`recall` / `remember`）与笔记（`create_note` /
#: `list_notes`）不属于这一侧——用户点名说过"这里的知识库不包括 agent 记忆"。
#: 而"把笔记加入知识库"（`attach_note_to_kb`）与"把产物存进知识库"
#: （`ingest_artifact`）**算**这一侧：它们动的是知识库。
_KB_TOOLS = frozenset(
    {
        "search",
        "list_knowledge_bases",
        "create_knowledge_base",
        "list_documents",
        "get_document_status",
        "delete_document",
        "upload_document",
        "add_data_source",
        "attach_note_to_kb",
        "ingest_artifact",
    }
)


def tool_specs(
    services: Any = None, *, owner_id: str | None = None, kb_ids: Sequence[str] | None = None
) -> list[ToolSpec]:
    """**完整**工具表（核心 + 外围 + 发现通道）。

    ⚠️ 对话那条链路**不要直接用它**：完整表意味着外围工具也每轮常驻，等于没做暴露分层。
    对话走 :func:`build_tool_table`（核心常驻 + 外围靠 ``find_tools``）。
    保留这一个的原因：外部 MCP 客户端、定时任务、脚本与用例那条路上**没有发现通道**，
    给它们一张"看得见就能调"的完整表才不是把工具藏起来。
    """
    specs = _all_specs(services, owner_id=owner_id, kb_ids=kb_ids)
    skills, skills_listed = _skill_counts(services)
    specs.extend(
        _exposure_specs(
            _exposure_text(
                resident=len(specs) + len(_EXPOSURE_TOOLS),
                peripheral=0,
                skills=skills,
                skills_listed=skills_listed,
            )
        )
    )
    return specs


def _all_specs(
    services: Any = None, *, owner_id: str | None = None, kb_ids: Sequence[str] | None = None
) -> list[ToolSpec]:
    """不分暴露档的完整清单（除发现通道本身）。**装配点只有这一处**（见模块头六段顺序）。

    顺序：内置 → 技能 → 本机能力 → 记忆 → 外部 MCP。

    ``services`` 给不给决定后两段在不在：

    - 不给（单测、纯规格检查）→ 只有内置工具，行为与 P0 时一致；
    - 给了 → 再加上外部服务暴露的工具。**只列调用方自己有权限用的那些**
      （``owner_id`` 收口，与能力页看到的同一批），否则会出现
      "别人登记的 MCP 服务在我的对话里被调起来"。
      外部那一段走**缓存**（见 ``MCPClientService.cached_tools``），
      所以这句话不便宜但也不贵：它是每轮一次的内存查找，不是每轮一次握手。

    ``kb_ids`` 是**这一轮允许查的库**（会话上选的那些）。为空 = 用户关掉了
    知识库开关，那就**别把知识库那一侧的工具摆给它**（见 ``_KB_TOOLS``）——
    给了又拒，只会白花两个来回（模型先看一眼有哪些库，再检索一次被拒）。
    **不传**（None）按"没有知识库"处理：与 ``build_runner`` 同一口径——
    没有范围就是查不了，那么工具表里也不该有它。
    """
    scope = [str(item) for item in (kb_ids or []) if str(item).strip()]
    memory_on = _memory_on(services)
    specs = [
        ToolSpec(
            name=item["name"],
            description=str(item.get("description") or ""),
            # MCP 叫 inputSchema，OpenAI 叫 parameters——同一个东西，这里翻一次名
            parameters=_dialogue_parameters(item),
        )
        for item in tool_definitions()
        if (scope or item["name"] not in _KB_TOOLS)
        # 记忆关着时 `recall` 会明确报错，那就别摆给它（理由见 `_MEMORY_SWITCH_TOOLS`）
        and (memory_on or item["name"] not in _MEMORY_SWITCH_TOOLS)
    ]
    specs.extend(
        ToolSpec(
            name=item["name"],
            description=str(item["description"]),
            parameters=item["inputSchema"],
        )
        for item in _SKILL_TOOLS
    )
    # 这台机器上的能力（文件 / 执行 / 表格）**排在内置与技能之后、外部服务之前**：
    # 前两段是我们担保的，外部的东西排最后（见模块头）。
    specs.extend(
        ToolSpec(
            name=item["name"],
            description=str(item["description"]),
            parameters=item["inputSchema"],
        )
        for item in _LOCAL_TOOLS
        if scope or item["name"] not in _LOCAL_KB_TOOLS
    )
    # 记忆这一侧的内部工具：`read_memory`（读整份档案，供模型自查它写进去的是什么）。
    # 它**不跟着开关走**（开关管的是注入与 recall），所以这里那道过滤对它其实不起作用
    # ——留着这条判据是因为 `_MEMORY_SWITCH_TOOLS` 是**一处**定义（谁哪天把某件工具
    # 归进"关着就不摆"，这里自动跟上）。
    specs.extend(
        ToolSpec(
            name=item["name"],
            description=str(item["description"]),
            parameters=item["inputSchema"],
        )
        for item in _MEMORY_TOOLS
        if memory_on or item["name"] not in _MEMORY_SWITCH_TOOLS
    )
    if services is not None:
        specs.extend(_mcp_specs(services, owner_id))
    return specs


# ============================================================ 暴露：核心常驻 + 外围可发现
#
# 用户裁定（《Agent-暴露机制-对标与落点-v0.1》）：**核心工具常驻、外围技能与工具延迟发现**。
# 这一节就是那条裁定的实现，三件事：
#
# 1. **核心/外围的划分**只有一处定义：``tool_meta.PERIPHERAL_TOOLS`` + ``is_peripheral()``
#    （那里有判据与"永远核心"的硬约束）——这里不另起名单；
# 2. **发现通道**是一个常驻工具 ``find_tools(query)``：命中就回它的**完整 schema**
#    （最多 `MAX_DISCOVER_PER_CALL` 个），没命中就回**索引**（名字 + 一句话）；
# 3. **调用**走常驻的 ``use_tool(name, arguments)``。
#
# 为什么调用要绕一层网关、而不是"发现了就把它加进 tools 数组"：工具循环在构造时就把
# 工具表**拷了一份**（``tool_loop.py`` 的 ``self._tools = list(tools)``），
# 而那一行不在本轮的改动范围内。网关这条路把"发现 → 调用"整条链留在我这一侧：
# 执行器本来就是按工具名分发的（见 `build_runner.run`），网关只是把
# ``use_tool`` 的参数翻成一次**同样的内部分发**——审批、权限、产物落点、
# 来源账本全部走原路（见 `run` 里 ``use_tool`` 那一支的说明）。
# 升级路径（真·按需声明）记在设计文档里，需要的正好是那一行的配合。

#: 一次 ``find_tools`` 最多回几个工具的完整 schema。
#:
#: 取 4：一条 schema 平均 ~250 token，4 条 ≈ 1,000 token——比"外围全常驻"
#: 便宜一个数量级，又不至于让模型发现一次只够用一步（它多叫一次这个工具也不贵）。
MAX_DISCOVER_PER_CALL = 4

#: 索引模式（没命中/空查询）最多列几行名字。
MAX_INDEX_LINES = 40

_EXPOSURE_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "find_tools",
        "description": (
            "{exposure}\n"
            "上面这些是**这一轮的真实数量**（当场从工具注册表与技能目录算的）。"
            "要取外围工具的完整参数、或想看看还有什么，就调本工具："
            "用一句话说清你要干什么，它会回**匹配到的工具的完整参数 schema**，"
            "之后用 `use_tool` 调；不带查询（或查询为空）时它回一份**索引**（名字 + 一句话）。"
            "**动手做那件事之前先调它**——不要凭「这个环境没有这个能力」就放弃。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "你要做的事（例如「把结果导出成 Excel」「建一个知识库」「跑一段命令」）"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_DISCOVER_PER_CALL,
                    "description": f"最多回几个工具的 schema，默认 {MAX_DISCOVER_PER_CALL}",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "use_tool",
        "description": (
            "调用**你已经用 `find_tools` 找到过**的工具：`name` 是工具名、"
            "`arguments` 是它的参数对象（按 find_tools 给的 schema 填）。"
            "没发现过、或不存在的名字这里会拒绝，并把外围工具索引回给你——"
            "**先 find_tools、再 use_tool**。调用结果与直接调用完全一样"
            "（审批、落盘、出处都走原路）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名，例如 export_table"},
                "arguments": {
                    "type": "object",
                    "description": "那个工具的参数对象（照 find_tools 返回的 schema 填）",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
)


def _exposure_specs(exposure_text: str) -> list[ToolSpec]:
    """两个网关工具（它们**永远常驻**，见 ``tool_meta.CORE_ALWAYS``）。

    ``find_tools`` 的描述里带着**这一轮的真实暴露数量**（用户要求：核心注入必须
    始终告诉模型"当前可用技能 N 个 / 工具 M 个 / 另有 K 个可按需取"）。
    为什么放在工具描述里而不是系统提示词：那份提示词在别的 lane 手里，
    而工具表本来每轮就发——数一数就能算出来的事不该跨文件改。
    """
    return [
        ToolSpec(
            name=item["name"],
            description=str(item["description"]).format(exposure=exposure_text),
            parameters=item["inputSchema"],
        )
        for item in _EXPOSURE_TOOLS
    ]


def _skill_counts(services: Any) -> tuple[int, int]:
    """（可用技能数，已进目录的技能数）。**当场算**，不写死。

    与 ``SkillService.catalog()`` 同一套判据（``used_by_prompt`` + ``MAX_CATALOG``），
    所以"目录里列了几条"与系统提示词里那份目录**永远对得上**。
    技能服务是**无状态、每次重扫**的（`skills.py` 模块头有取舍说明），
    这里只调 ``list()`` 不渲染目录——多一次扫描（几十毫秒量级），换来的是
    "模型知道自己没看到多少"这一条硬要求。
    """
    skills = getattr(services, "skills", None) if services is not None else None
    if skills is None:
        return (0, 0)
    try:
        usable = [item for item in skills.list() if item.used_by_prompt]
    except Exception:  # 技能目录读不出来不该让整轮对话起不来
        logger.warning("技能计数失败，本轮暴露数量按 0 算", exc_info=True)
        return (0, 0)
    from app.services.skills import MAX_CATALOG

    return (len(usable), min(len(usable), MAX_CATALOG))


def _exposure_text(*, resident: int, peripheral: int, skills: int, skills_listed: int) -> str:
    """核心注入里的那一段"数量 + 怎么发现"（**每轮都发**，用户点名的硬要求）。

    两个数必须是**真的**：常驻/外围来自这一轮的工具表，技能来自技能目录的同一套判据。
    外围工具或技能增减时这段文字跟着变（用例钉住这一条）。
    """
    total_tools = resident + peripheral
    return (
        "【本环境的暴露情况】"
        f"当前工具共 **{total_tools}** 个：其中 **{resident}** 个已常驻在你的工具表里"
        "（记忆、技能、检索、联网、读文件与下面这两个入口），"
        f"另有 **{peripheral}** 个**按需检索**（导出、建库、跑命令、派子 Agent、"
        "定时任务、文件列举与搜索、外部 MCP 服务等）；"
        f"当前可用技能 **{skills}** 个，其中 **{skills_listed}** 个已列在系统提示词里，"
        f"另有 **{max(0, skills - skills_listed)}** 个可直接用 `list_skills` 查、"
        "`read_skill` 取正文。"
        "要外围工具：`find_tools`（查）→ `use_tool`（调）；要技能：`read_skill`。"
    )


def _bigrams(text: str) -> set[str]:
    """字符二元组集合（中英一视同仁，不引分词器）。"""
    line = " ".join((text or "").lower().split())
    if len(line) < 2:
        return {line} if line else set()
    return {line[index : index + 2] for index in range(len(line) - 1)}


def _match_score(query: str, spec: ToolSpec) -> int:
    """查询与一个工具的匹配分：二元组命中数 + 名字直接命中的加权。

    **刻意是可算的**：不引模型、不引向量——这个函数会被用例逐条钉住，
    而且它决定"外围工具能不能被找到"，判错了的后果是"工具存在但永远发现不了"。
    """
    line = (query or "").lower()
    if not line:
        return 0
    target = f"{spec.name} {spec.description}".lower()
    score = sum(1 for gram in _bigrams(line) if gram in target)
    if spec.name.lower() in line:
        score += 8
    return score


def _clip(text: str, limit: int) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit] + "…"


def _plan_mode_blocks(services: Any, name: str) -> bool:
    """计划档下要不要拦住这个内层工具（网关那一支专用，见 `use_tool` 分支）。

    ``plan`` 档的规矩是"没给计划之前写类工具一律被拦"（``tool_loop`` 用**外层调用**
    的元数据判）。网关在元数据里是只读的，所以内层这一判必须自己补——
    判据不另写一套：用 ``modes.is_write``（与那道门闸同一个函数）。
    """
    runtime = getattr(services, "runtime", None)
    if runtime is None:
        return False
    try:
        from app.services import modes

        if modes.coerce(runtime.get("chat.mode")) != modes.MODE_PLAN:
            return False
        return modes.is_write(tool_meta.meta_of(name))
    except Exception:  # 读不到档位时**不拦**（与"拦错了"相比，写成"放行了"更接近既有行为）
        logger.warning("网关判计划档失败，按放行处理：%s", name, exc_info=True)
        return False


class ToolTable:
    """一轮的暴露表：**核心常驻** + **外围（发现后可调）**。

    形状是一个对象而不是三个列表：`find_tools` / `use_tool` 的执行器要能拿到
    "这一轮有哪些外围工具、哪些已经被发现过"，而那两件事必须与交给模型的
    那张表是同一份事实（分两处维护迟早出现"表里有、发现不到"）。
    """

    def __init__(self, resident: list[ToolSpec], peripheral: list[ToolSpec]) -> None:
        #: 交给模型的那一份（**核心 + 网关**）。顺序即装配顺序。
        self._resident = resident
        self._peripheral = peripheral
        self._by_name = {spec.name: spec for spec in peripheral}
        #: 已经发现过的外围工具（只做记录与诊断——`use_tool` 允许调用任何在册工具，
        #: 但"它是不是被发现的"这件事在排查时要看得见）。
        self._discovered: set[str] = set()

    # ------------------------------------------------------------------ 读

    def resident(self) -> list[ToolSpec]:
        """核心那张表（**就是交给模型的那一份**，别再拷一遍）。"""
        return self._resident

    def peripheral(self) -> list[ToolSpec]:
        return list(self._peripheral)

    def peripheral_names(self) -> list[str]:
        return [spec.name for spec in self._peripheral]

    def discoveries(self) -> list[str]:
        return sorted(self._discovered)

    def knows(self, name: str) -> bool:
        """这个名字在**这一轮的表里**吗（外围或常驻都算）。"""
        return name in self._by_name or any(spec.name == name for spec in self._resident)

    def allows(self, name: str) -> bool:
        """``use_tool`` 放不放它：**必须先被发现过**（外围）或本来就是常驻的一个。

        为什么要求"发现过"：`use_tool` 的说明书就是这么写的，而这条限制让
        "发现通道"成为**唯一的路**——否则模型可以凭记忆瞎猜工具名（猜中了会绕过
        发现，猜错了拿到一个含糊的错误）。常驻那几个不需要发现（它们本来就在表里）。
        """
        if any(spec.name == name for spec in self._resident):
            return True
        return name in self._discovered

    def snapshot(self) -> dict[str, object]:
        """给日志 / 仪表 / 用例看的只读快照。"""
        return {
            "resident": [spec.name for spec in self._resident],
            "peripheral": [spec.name for spec in self._peripheral],
            "discovered": self.discoveries(),
        }

    # ------------------------------------------------------------------ 发现

    def discover(self, query: str, limit: int | None = None) -> list[ToolSpec]:
        """按查询挑出外围工具（**按分数降序、最多 limit 个**）。

        查询为空、或一条都没命中时回**空列表**——调用方据此改回索引模式
        （"没找到"和"没有这个能力"是两件事，见 `render_discovery`）。
        """
        cap = max(1, min(int(limit or MAX_DISCOVER_PER_CALL), MAX_DISCOVER_PER_CALL))
        scored = [
            (_match_score(query, spec), spec)
            for spec in self._peripheral
        ]
        hits = [(score, spec) for score, spec in scored if score > 0]
        # 分数相同时按名字排：**同一轮里同样的查询给同样的答案**（可复现）
        hits.sort(key=lambda item: (-item[0], item[1].name))
        found = [spec for _, spec in hits[:cap]]
        self._discovered.update(spec.name for spec in found)
        return found

    def render_discovery(self, query: str, found: Sequence[ToolSpec]) -> str:
        """`find_tools` 的结果文本（**JSON**：schema 要能被原样抄进 arguments）。"""
        if found:
            return json.dumps(
                {
                    "found": [spec.name for spec in found],
                    "note": (
                        "这些工具现在可以用了：用 `use_tool` 调，"
                        "`name` 填工具名、`arguments` 按下面的 parameters 填。"
                    ),
                    "tools": [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "parameters": spec.parameters,
                        }
                        for spec in found
                    ],
                },
                ensure_ascii=False,
                indent=1,
            )
        index = [
            {"name": spec.name, "description": _clip(spec.description, 80)}
            for spec in self._peripheral[:MAX_INDEX_LINES]
        ]
        more = max(0, len(self._peripheral) - len(index))
        return json.dumps(
            {
                "found": [],
                f"index（{len(self._peripheral)} 个外围工具，这里只列名字）": index,
                "剩余没列出的": more,
                "note": (
                    f"没匹配上「{_clip(query, 60)}」。上面是这边**按需暴露**的工具索引："
                    "如果你要做的事对应其中一个，用 `find_tools` 带上更具体的一句话"
                    f"（例如「用它导出 Excel」），每次最多取回 "
                    f"{MAX_DISCOVER_PER_CALL} 个的完整参数。"
                ),
            },
            ensure_ascii=False,
            indent=1,
        )

    def render_unknown(self, name: str) -> str:
        """`use_tool` 拿到一个不在册的名字时回的文本（**给出路，不只说不行**）。"""
        return json.dumps(
            {
                "error": f"没有这个工具：{name}",
                "hint": "先用 `find_tools` 查（它会把名字与完整参数给你），再 `use_tool` 调。",
                "index": [
                    {"name": spec.name} for spec in self._peripheral[:MAX_INDEX_LINES]
                ],
            },
            ensure_ascii=False,
        )


def build_tool_table(
    services: Any = None, *, owner_id: str | None = None, kb_ids: Sequence[str] | None = None
) -> ToolTable:
    """对话那条链路的工具表：**核心 + 网关常驻，外围可发现**。

    划分只看 ``tool_meta.is_peripheral``（一处定义）。``CORE_ALWAYS`` 那几个
    永远在常驻那一侧——哪怕有人把它们写进外围名单（那是硬约束，不是偏好）。
    """
    specs = _all_specs(services, owner_id=owner_id, kb_ids=kb_ids)
    resident = [spec for spec in specs if not tool_meta.is_peripheral(spec.name)]
    peripheral = [spec for spec in specs if tool_meta.is_peripheral(spec.name)]
    skills, skills_listed = _skill_counts(services)
    resident.extend(
        _exposure_specs(
            _exposure_text(
                # 常驻数**算上这两个网关**（模型看到的常驻工具就是这么多）
                resident=len(resident) + len(_EXPOSURE_TOOLS),
                peripheral=len(peripheral),
                skills=skills,
                skills_listed=skills_listed,
            )
        )
    )
    return ToolTable(resident, peripheral)


#: 在**对话这条门**上不收 ``knowledge_base_id`` 的工具（v0.26）。#:
#: 外部门（MCP）保持原契约：那条通道没有会话，产物唯一的落点就是知识库，
#: 而且"外部客户端点名叫了哪个库"这件事本身就是显式的。
#: 对话这条门不一样：产物先落盘，入库是另一个动作。**参数留在这里过不了日子**——
#: 它是可选的，模型就会在某些时候顺手填上；而它一旦被填上，
#: 它就会重新开始替用户挑库（这正是"把 docx 塞进「笔记」"的成因）。
_DIALOGUE_DROPS_KB = frozenset({"export_document", "export_table", "export_deck"})


def _dialogue_parameters(item: dict[str, Any]) -> dict[str, Any]:
    """把内置工具的 schema 调成**对话这条门**该有的样子。

    目前只有一件事：导出类工具不再向模型暴露 ``knowledge_base_id``。
    """
    schema = item.get("inputSchema") or {"type": "object", "properties": {}}
    if item["name"] not in _DIALOGUE_DROPS_KB:
        return schema
    properties = {
        key: value
        for key, value in (schema.get("properties") or {}).items()
        if key != "knowledge_base_id"
    }
    return {**schema, "properties": properties}


def _mcp_specs(services: Any, owner_id: str | None) -> list[ToolSpec]:
    """外部 MCP 服务的工具，翻成给模型的规格。

    **描述要带上服务名**：模型拿到的是一个平铺的工具表，`search_web` 这个名字
    本身说不清它来自哪个服务、能做到什么程度；而 `mcp__tavily__search_web`
    既说了归属，也让它在解释"我用了哪个工具"时说得出人话。

    清单取不到（服务没起、命令不存在）时**那个服务整体不出现**，其余照常——
    一个外部服务挂掉不该让整张能力表消失。
    """
    specs: list[ToolSpec] = []
    try:
        pairs = services.mcp.available_tools(user_id=owner_id)
    except Exception:
        # 连服务列表都读不出来（存储异常）时不该让整轮对话起不来：工具表少一段
        # 仍然能答，而抛出去会让"问一句话"直接失败。
        logger.warning("外部 MCP 工具清单读取失败，本轮不带外部工具", exc_info=True)
        return []
    for record, tool in pairs:
        origin = f"[外部工具 · 来自 MCP 服务「{record.name}」]"
        specs.append(
            ToolSpec(
                name=tool.qualified,
                description=f"{origin} {tool.description}".strip(),
                # 外部服务的 inputSchema 不受我们约束：它可能是空的、也可能带
                # 我们没见过的关键字。原样透传，但**必须是个对象**——
                # 有的服务给 null，那样模型侧会直接拒掉整张表。
                parameters=tool.schema or {"type": "object", "properties": {}},
            )
        )
    return specs


#: 子 Agent 那一步的结论行里，任务摘要留几个字。
#:
#: 那一行是**给用户看的**（"它派去干什么了"），不是给模型的：子任务本身就是一句自足的话
#: （见 `services/subagent.build_task_prompt` 的长度校验），开头那句最能说明它在查什么。
#: 留太长会把过程面板那一行撑爆、也不利于扫读，60 字够看出主题。
_SUMMARY_TASK_CHARS = 60


def build_runner(
    services: Any,
    caller: Caller,
    *,
    kb_ids: Sequence[str] | None = None,
    conversation_id: str | None = None,
    subagent: Callable[[str], tuple[str, list[Any]]] | None = None,
    seed_sources: Sequence[SourceRef] = (),
    exposure: Any = None,
) -> ToolRunner:
    """绑一个执行器。``kb_ids`` 是**这一轮允许查的库**（会话上选的那些）。

    ``conversation_id`` 决定**产物落在哪儿**（v0.26，见 ``services/artifacts.py``）：
    挂在工作的会话落进工作区目录，没挂的落进对象存储里按会话分的临时前缀。
    没有它（外部 MCP 客户端、一次性脚本）时导出类工具只剩"直接入库"那一条路。

    范围规则（三条，都与"关掉知识库开关就该真的查不到"一致）：

    - 有范围、模型没指定库 → 用这个范围（它通常压根不知道有哪些库，逼它先列一遍
      是多余的一步）；
    - 有范围、模型指定了库 → **取交集**；交集为空就报错，并把它能查的库告诉它；
    - 没范围（用户关掉了知识库开关）→ 检索直接拒绝，并说明"要查资料得让对方打开"。
      这一条是那个开关的意义所在：它不该只挡界面。

    **外部工具也走这里**：``mcp__<服务>__<工具>`` 交给 ``_call_mcp``，
    它在真正调用之前过一遍准入策略（见那个函数的说明）。

    **执行器持有这一轮的"来源账本"**（``book``）。它是每轮新建的，所以账本正好
    等于"这一轮用到的资料"。为什么要它：模型可以在一轮里查好几次，而每次检索
    都从 [1] 开始编号——不累计的话，第二次检索的编号与第一次撞车，
    界面上（只认最后一批）会把先查到的那些资料**整批丢掉**，
    于是答案里的 [1][2] 指向的东西与用户看到的对不上。
    累计并**重新编号**必须在渲染资料之前做（内容与界面必须是同一套号），
    所以它在这里而不是在工具循环里。

    **账本是共享可变状态，所以要加锁**（v0.27）：工具循环会把同一批里的几次调用
    并发跑（见 ``tool_loop._execute_batch``），而两个检索线程同时进 ``_absorb``
    会抢同一个编号——两边都读到"账本里有 3 条"，各自从 4 开始编，于是同一段资料
    拿到同一个号、或者一条编号谁也没占。去重、顺延编号、取快照这三件事
    因此都在同一把锁里做完。
    """
    scope = [str(item) for item in (kb_ids or []) if str(item).strip()]
    # 有些调用点（子 Agent 的测试、脚本）没有凭据主体，那就是**共享桶**
    # （``None``），与"管理员/API Key 通道"同一档——不是错误，不给它编一个身份。
    owner_id = caller.owner_id if caller is not None else None
    # ``seed_sources``：**续跑**时把上一轮已经拿到的出处接着带上（见 services/resume.py）。
    # 必须从这里进来，不能只写进提示词——提示词里告诉模型"[3] 是那份共识"，
    # 而账本里没有第 3 条，它引用出来的编号就会指向别的资料。
    # 编号从 1 重排：与提示词里给它的编号是同一套。
    book: list[SourceRef] = _renumber(list(seed_sources), offset=0)
    book_lock = threading.Lock()

    def _record(incoming: Sequence[SourceRef]) -> list[SourceRef]:
        """把一批新资料并进账本，返回**真正新增**的那些（编号已排好）。"""
        with book_lock:
            return _absorb(book, list(incoming))

    def _snapshot() -> list[SourceRef]:
        """当前账本的副本。**与 ``_record`` 同一把锁**：读的时候可能正有人在写。"""
        with book_lock:
            return list(book)

    #: 两个根（工作区 / 沙箱）**按需解析、一轮里只解析一次**：多数回合压根不碰文件，
    #: 而解析要查一次会话与工作区（两次查询）。用列表当格子是为了在闭包里赋值。
    _roots_cache: list[Any] = []

    def _roots():  # type: ignore[no-untyped-def]
        if not _roots_cache:
            _roots_cache.append(
                resolve_roots(services, conversation_id=conversation_id, caller=caller)
            )
        return _roots_cache[0]

    def run(name: str, args: dict[str, Any], *, approval: str | None = None) -> ToolOutcome:
        """``approval`` 只有 ``run_command`` 用得上（v0.41）：``ask`` 档下工具循环
        会先把这条命令**问成一条待确认**（执行器登记、循环发事件并等人回答），
        拿到决定之后带着它把这一条重跑一遍——见 ``tool_loop._resolve_approvals``。"""
        if name.startswith("mcp__"):
            return _call_mcp(services, name, args, owner_id=owner_id)
        if name == "find_tools":
            # 发现通道（核心常驻）：回匹配到的外围工具的**完整 schema**，没命中回索引。
            # 它**不依赖任何别的工具**（只读这一轮的表）——这一条是硬约束：
            # 发现工具要是自己也需要被发现，外围就永远不可达。
            if exposure is None:
                return ToolOutcome(content="这条链路没有工具暴露表，find_tools 不可用。")
            query = str(args.get("query") or "")
            found = exposure.discover(query, args.get("limit"))
            return ToolOutcome(content=exposure.render_discovery(query, found))
        if name == "use_tool":
            # 调用网关：把参数翻成一次**同样的内部分发**。三条不能省：
            # 1. **必须先发现过**（说明书这么写的，也让发现通道成为唯一的路）；
            # 2. 不许拿它包自己或 find_tools（自递归）；3. 审批照原路走——
            #    内层工具该问的还会问（`approval` 原样透传，见下面那行）。
            if exposure is None:
                return ToolOutcome(content="这条链路没有工具暴露表，use_tool 不可用。")
            target = str(args.get("name") or "").strip()
            inner = args.get("arguments") or {}
            if target in ("use_tool", "find_tools"):
                return ToolOutcome(content=f"{target} 是常驻入口，直接调它就行，不必包一层。")
            if not isinstance(inner, dict):
                return ToolOutcome(
                    content="arguments 必须是一个对象（照 find_tools 给的 schema 填）。"
                )
            if not exposure.allows(target):
                return ToolOutcome(content=exposure.render_unknown(target))
            # **plan 档的写类门闸要在这里补判一次**：网关自己在元数据里是只读的
            # （否则用户会被问两遍，见 tool_meta 的说明），而工具循环那道门闸只看
            # **外层调用**的元数据——不补这一判，写类工具就能绕道网关溜进计划档。
            if _plan_mode_blocks(services, target):
                return ToolOutcome(
                    text=(
                        f"计划档下先不给动手：`{target}` 是会改动东西的工具，"
                        "请先把计划给出来、等对方确认，再用 `use_tool` 调它。"
                    )
                )
            return run(target, inner, approval=approval)
        if name == "spawn_subagent":
            task = str(args.get("task") or "").strip()
            if not task:
                return ToolOutcome(content="缺少参数：task")
            if subagent is None:
                # 明确说"这一轮没有"，而不是假装派了：模型据此才该自己去查
                return ToolOutcome(content="子 Agent 在这一轮不可用，请自己查。")
            try:
                answer, sources = subagent(task)
            except Exception as exc:
                logger.info("子 Agent 失败：%s", exc)
                return ToolOutcome(content=f"子 Agent 没跑成：{exc}")
            refs = _record(list(sources))
            # 结论行要说清"派去干什么了"：只写"回报了结论"对用户等于没说 ——
            # 过程面板那一行与 `tool_loop._LABELS` 是同一条取舍：名字要说清"它替我做了什么"。
            # 换行折成空格，免得把那一行撑成多行。
            brief = " ".join(task.split())
            if len(brief) > _SUMMARY_TASK_CHARS:
                brief = f"{brief[:_SUMMARY_TASK_CHARS]}…"
            # 交回的东西是**结构化的**（D15）：把六项里用户最该先知道的两项提到那一行上
            # ——置信度（这段结论有多可靠）与建议下一步（接下来该干什么）。
            # **只有真解析出那一块才加**：拿不到结构就照旧只说"回报了结论"，
            # 不猜、也不给一行看起来像结构化结果的东西。
            prefix = "子 Agent 回报了结论"
            structured = parse_subagent_content(answer)
            if structured:
                extra: list[str] = []
                confidence = structured.get("confidence")
                if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
                    extra.append(f"置信度 {confidence:g}")
                risks = structured.get("risks")
                if isinstance(risks, list) and risks:
                    extra.append(f"风险 {len(risks)} 条")
                steps = structured.get("next_steps")
                if isinstance(steps, list) and steps:
                    extra.append(f"建议下一步 {len(steps)} 条")
                if extra:
                    prefix = f"{prefix}（{'，'.join(extra)}）"
            return ToolOutcome(
                content=answer or "（子 Agent 没有给出结论）",
                sources=_snapshot(),
                summary=f"{prefix}：{brief}",
                added=len(refs),
            )
        if name == "list_skills":
            return ToolOutcome(content=_render_skills(services, args))
        if name == "read_skill":
            return ToolOutcome(content=_read_skill(services, args))
        if name in _FILE_TOOLS:
            return _run_file_tool(name, _roots(), args)
        if name in _CONVERSATION_FILE_TOOLS:
            return _run_conversation_file_tool(name, services, conversation_id, args)
        if name == "ingest_file":
            return _ingest_file(services, caller, conversation_id, _roots(), args)
        if name == "read_memory":
            return _read_memory(services, caller, args)
        if name == "write_memory":
            return _write_memory(services, caller, args)
        if name == "run_command":
            outcome = run_command(
                services, caller, conversation_id=conversation_id, args=args, approval=approval
            )
            # **结构化地标出"这一轮到底执行了没有"**（D22，2026-09-28 走查）。
            #
            # 界面原先靠**匹配句式**认"被拦下"（「没有执行」「等待确认」「拒绝执行」…），
            # 措辞一改就瞎；而这一行是**默认展开**的，认错的代价不小（用户会以为它做了）。
            # 这里不改执行器、也不新增分支：从它**已经给出**的结构化事实推——
            # 在等确认 → ``awaiting``；连进程都没起 → ``blocked``（跑起来但失败的不算拦截）。
            if outcome.approval is not None:
                blocked_kind = "awaiting"
            elif not outcome.ran:
                blocked_kind = "blocked"
            else:
                blocked_kind = ""
            return ToolOutcome(
                content=outcome.text,
                summary=outcome.summary,
                approval=outcome.approval,
                outcome=blocked_kind,
            )
        if name == "list_tables":
            return _list_tables(services, scope)
        if name == "query_table":
            return _query_table(services, scope, args)
        if name == "schedule_task":
            return _schedule_task(services, caller, scope, args)
        if name == "list_scheduled_tasks":
            return _list_scheduled(services, caller)
        if name == "search":
            scoped = _scope_search(args, scope)
            if scoped is None:
                return ToolOutcome(content=_NO_KB_SCOPE)
            # **走 `retrieve_sources`，不走 MCP 那个 `search` 工具。** 两者的区别不是
            # 检索本身（都是同一套混合检索），而是**取回来的是哪一段文本**：
            # 那个工具回的是命中的**那一块**（chunk），这里回的是它所在的**整段小节**
            # 加上文档摘要、并按条数均摊字数预算（v17/v25 做的"小块检索、大块阅读"）。
            #
            # 这正是 P0 换框架时丢过的东西：模型拿到的是被切碎的块，读起来缺上下文，
            # 但它**看起来完全正常**——只是答得更浅。所以内部这条门要用这一份；
            # 外部门保持 chunk 级返回不变（那是已发布的 MCP 契约，外部客户端
            # 靠 chunk_id 做二次读取）。
            kb_ids = [str(item) for item in (scoped.get("knowledge_base_ids") or [])]
            services.kb.api_keys.check_access(caller, kb_ids=kb_ids)
            refs = _record(
                services.chat.retrieve_sources(
                    query=str(scoped.get("query") or ""),
                    kb_ids=kb_ids,
                    top_k=_int_or_none(scoped.get("top_k")),
                )
            )
            if refs:
                content = _render_sources(refs)
                summary = f"命中 {len(refs)} 段原文"
            else:
                # 两种情况要分开说：库里真没有，与"命中的前面都给过了"。
                # 都回 `_render_sources([])` 那句"没有命中任何片段"，模型会把后者
                # 读成前者，于是放弃换角度的尝试——而它其实只是重复查了同一处。
                content = (
                    "这一次没有新增片段：命中的内容前面已经给过（或这个库里没有相关的）。"
                    "请基于已有资料作答；还要查就换一个角度或关键词。"
                )
                summary = "没有新的片段"
            return ToolOutcome(
                content=content,
                # **累计列表**（协议约定：多轮检索多次发出，始终是累计的）：
                # 只发这一批的话，先查到的那些资料会在界面上消失
                sources=_snapshot(),
                summary=summary,
                added=len(refs),
            )
        payload = call_tool(services, name, args, caller=caller, conversation_id=conversation_id)
        # 产出物**先摘走、再渲染**：那个键是给界面用的，模型不该看到一份
        # 自己结果的副本（它会照着复述，白占上下文）。顺序不能反。
        artifacts: list[dict[str, object]] = []
        if isinstance(payload, dict) and ARTIFACT_KEY in payload:
            raw = payload.pop(ARTIFACT_KEY)
            if isinstance(raw, dict):
                artifacts = [dict(raw)]
        return ToolOutcome(
            content=_render(payload),
            summary=_summary(name, payload),
            artifacts=artifacts,
        )

    return run


# ------------------------------------------------------------------ 外部 MCP 工具


def _call_mcp(
    services: Any, qualified: str, args: dict[str, Any], *, owner_id: str | None
) -> ToolOutcome:
    """调一个外部 MCP 服务的工具，**调用前先过准入策略**。

    三档的处理方式都与"这是它自己想要的"这件事一致：

    - ``allow`` → 调；
    - ``deny`` → 不调，并把**为什么**说清楚（模型据此换路，而不是反复重试）；
    - ``ask`` → **也不调**，并请模型如实告诉用户怎么放开它。

    ``ask`` 为什么不是"停下来问用户"：对话的这条链路是一个**拉取式的生成器**
    （事件由上层一个个取走），而"问用户"要求在这一步**把已经发生的事件先送出去、
    再阻塞等人回答**。拉取式生成器做不到——阻塞发生在 yield 之前，
    界面根本收不到那个询问，两边一起等（实测过这个死锁的形状：
    事件攒在列表里，前端什么都没有）。要支持它得先换成推送式事件流，
    那是一次协议层改造，不是这里加两行代码。

    于是取舍是：**宁可"这轮用不了、并说清怎么打开"，也不要静默执行**。
    默认档就是 ``ask``，所以"接进来就悄悄以用户名义调外部服务"这件事不会发生。
    """
    split = split_qualified(qualified)
    if split is None:
        return ToolOutcome(content=f"认不出这个工具名：{qualified}")
    server_part, tool = split
    record = _find_server(services, server_part, owner_id)
    if record is None:
        return ToolOutcome(
            content=(
                f"没有找到启用的 MCP 服务「{server_part}」"
                "（可能没登记、被停用，或不属于当前账号），这个工具这轮用不了。"
            )
        )
    decision = services.mcp.decide(
        record, tool, rules=rules_from_runtime(services.runtime, source="外部工具")
    )
    if decision.action == ACTION_DENY:
        return ToolOutcome(
            content=(
                f"这个外部工具被拒绝调用（{decision.reason}）。请换一条路完成这件事；"
                "如果确实需要它，把这一点告诉对方（它在「能力 → MCP 服务」里可以改）。"
            ),
            summary="被策略拒绝",
        )
    if decision.action != ACTION_ALLOW:
        rule = suggest_rule(qualified, "").describe()
        return ToolOutcome(
            content=(
                f"调用 {qualified} 需要用户先确认，**这一轮没有执行**。"
                "请如实告诉对方：要用这个外部工具，得先把「能力 → MCP 服务」里"
                f"「{record.name}」的策略改成「允许」，"
                f"或者在设置的放行清单里加一行 `{rule}`。"
                "**不要假装调用过，也不要凭猜测编造它的返回。**"
            ),
            summary="需要先确认，本轮没执行",
        )
    try:
        text = services.mcp.call(record, tool, args, approved=True)
    except Exception as exc:
        # 与内置工具同一口径：失败**如实回到循环**，不包装成空结果
        logger.info("外部工具 %s 调用失败：%s", qualified, sanitize_log_value(exc))
        return ToolOutcome(content=f"调用外部工具失败：{exc}")
    clipped = text[:MAX_MCP_RESULT_CHARS]
    if len(text) > MAX_MCP_RESULT_CHARS:
        clipped += f"\n\n（结果过长已截断，以上是前 {MAX_MCP_RESULT_CHARS} 字）"
    return ToolOutcome(
        content=clipped or "（外部工具没有返回内容）",
        summary=f"{record.name} 的 {tool} 返回了结果",
    )


def _find_server(services: Any, server_part: str, owner_id: str | None) -> Any | None:
    """按限定名里那段服务名找回登记记录（**只认启用中的，且按账号收口**）。

    比的是**规范化之后**的名字（``tool_qualified_name`` 会把下划线折成横线）：
    直接拿原名比的话，用户给服务起名 ``my_tool`` 时对话里永远找不到它。
    """
    for record in services.mcp.list(user_id=owner_id):
        if record.enabled and normalized_server_name(record.name) == server_part:
            return record
    return None


# ------------------------------------------------------------------ 检索的范围与形状


def _scope_search(args: dict[str, Any], scope: list[str]) -> dict[str, Any] | None:
    """把检索参数收进范围；无可查返回 ``None``。"""
    if not scope:
        return None
    asked = [str(item) for item in (args.get("knowledge_base_ids") or []) if str(item)]
    if not asked:
        return {**args, "knowledge_base_ids": scope}
    allowed = [item for item in asked if item in scope]
    if not allowed:
        # 明确拒绝而不是"悄悄换成允许的库"：换了它就会以为查的是自己指定的那个，
        # 于是把别处的结论按在这个库上说
        return None
    return {**args, "knowledge_base_ids": allowed}


def _render_sources(refs: Sequence[SourceRef]) -> str:
    """把资料渲染成**带编号的文本块**。

    不直接 `json.dumps`：模型接下来要给这些片段编引用号，而 JSON 里的字段名
    会把"编号"这件事弄糊（它分不清哪个是该引的数字）。这里的编号就是
    `SourceRef.index`——界面上的 [1][2] 与它一一对应。

    没命中时**明说没命中**：回一个空串会让模型把"没有资料"读成"资料是空的"，
    然后照样往下写。
    """
    if not refs:
        return "没有命中任何片段。"
    lines: list[str] = []
    for ref in refs:
        where = ref.document_name
        if ref.heading_path:
            where += f" › {ref.heading_path}"
        if ref.page is not None:
            where += f"（第 {ref.page} 页）"
        lines.append(f"[{ref.index}] {where}\n{ref.preview}")
    return "\n\n".join(lines)


def _renumber(refs: Sequence[SourceRef], *, offset: int) -> list[SourceRef]:
    """把一批资料接着**已有的编号**往下排（``offset`` = 这轮已经给出去几段）。

    引用号是模型与用户之间唯一的对接方式：模型写 [2]，用户点 [2] 要看的就是
    **那段**原文。所以编号一旦重排，渲染给模型的文本与发给界面的出处必须是
    同一次重排的结果——这也是它只能发生在渲染之前的原因。
    """
    return [replace(ref, index=offset + position) for position, ref in enumerate(refs, start=1)]


def _absorb(book: list[SourceRef], incoming: Sequence[SourceRef]) -> list[SourceRef]:
    """把一批新资料并进这一轮的账本，返回**真正新增**的那些（已接着现有编号排好）。

    去重按 ``chunk_id``：一轮里可以查好几次，换了检索词但落点相同是常事。
    不去重的话同一段会占两个引用号——界面上两条一模一样的出处，
    而模型可能各引一次，读者会以为那是两份不同的资料。

    **先到的那条保留原编号，不因为"这次分数更高"替换**：编号在第一次检索时就已经
    渲染给模型了（``[n]`` 写在工具结果里），重排会让它先前写下的引用指向别处。
    代价是同一段保留的分数未必是最高那次——分数只用于排序与阈值过滤，
    不影响引用关系，所以这个取舍是划算的。

    返回值同时也是"这一步新增了几段"的口径（`ToolOutcome.added`）。
    """
    known = {item.chunk_id for item in book}
    fresh: list[SourceRef] = []
    for ref in incoming:
        if ref.chunk_id in known:
            continue
        known.add(ref.chunk_id)
        fresh.append(ref)
    refs = _renumber(fresh, offset=len(book))
    book.extend(refs)
    return refs


def _int_or_none(value: Any) -> int | None:
    """``top_k`` 可能是模型给的字符串（有些模型把数字包成 ``"6"``）或缺失。"""
    if isinstance(value, bool):  # bool 是 int 的子类，这里必须先排掉
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# ------------------------------------------------------------------ 文件 / 表格


def _run_file_tool(name: str, roots: Any, args: dict[str, Any]) -> ToolOutcome:
    """文件三件事的入口。**结果渲染成文本而不是 JSON**：文件内容与列目录的
    换行在 JSON 里会变成一屏 ``\\n``，而这段文本是给模型读的（与 ``_web_fetch`` 同理）。"""
    if name == "list_files":
        payload = list_files(
            roots,
            where=args.get("where"),
            path=str(args.get("path") or ""),
            pattern=str(args.get("pattern") or ""),
            limit=_int_or_none(args.get("limit")) or MAX_LIST_ENTRIES,
        )
        entries = payload["entries"] if isinstance(payload["entries"], list) else []
        lines = [
            f"{'d' if item['type'] == 'dir' else 'f'} {item['path']}"
            + (f"（{_size_text(item['size_bytes'])}）" if item["type"] == "file" else "")
            for item in entries
        ]
        head = f"{payload['where']}:{payload['path']} 共 {payload['total']} 项"
        return ToolOutcome(
            content=_join_blocks(
                head, chr(10).join(lines) or "（这个目录是空的）", str(payload["note"])
            ),
            summary=f"{payload['where']} 下 {payload['total']} 项",
        )
    if name == "read_file":
        payload = read_file(
            roots,
            where=args.get("where"),
            path=str(args.get("path") or ""),
            offset=_int_or_none(args.get("offset")) or 1,
            limit=_int_or_none(args.get("limit")) or DEFAULT_READ_LINES,
        )
        head = (
            f"【{payload['where']}:{payload['path']}】"
            f"第 {payload['offset']}–{payload['offset'] + payload['lines'] - 1} 行"
            f"（共 {payload['total_lines']} 行，{_size_text(payload['size_bytes'])}）"
        )
        return ToolOutcome(
            content=_join_blocks(
                head, str(payload["text"]) or "（这个文件是空的）", str(payload["note"])
            ),
            summary=f"读了 {payload['lines']} 行",
        )
    payload = search_files(
        roots,
        where=args.get("where"),
        path=str(args.get("path") or ""),
        pattern=str(args.get("pattern") or ""),
        ignore_case=args.get("ignore_case") is not False,
        limit=_int_or_none(args.get("limit")) or MAX_SEARCH_HITS,
    )
    hits = payload["hits"] if isinstance(payload["hits"], list) else []
    lines = [f"{item['path']}:{item['line']}: {item['text']}" for item in hits]
    head = (
        f"搜「{payload['pattern']}」：命中 {payload['total']} 处"
        f"（扫了 {payload['scanned_files']} 个文件）"
    )
    return ToolOutcome(
        content=_join_blocks(head, chr(10).join(lines) or "（没有命中）", str(payload["note"])),
        summary=f"命中 {payload['total']} 处" if hits else "没有命中",
    )


# ------------------------------------------------------------------ 会话文件区（v0.55）


def _run_conversation_file_tool(
    name: str, services: Any, conversation_id: str | None, args: dict[str, Any]
) -> ToolOutcome:
    """会话文件区那两个工具的入口（列 / 读）。

    **为什么必须存在**：用户报的"我们不是有 minio 吗，非工作区会话上传的图片文件
    不是应该能用来交互吗"——查下来发现存储那一半早就对了（没挂工作区的会话，
    上传确实落进对象存储），**缺的是"让 agent 用得上它"**：``list_files`` / ``read_file``
    只看工作区与沙箱两个根，而对象存储里的那份文件哪个根都不是，模型既列不到、
    也拿不到 ``artifact_id``，连 ``ingest_artifact`` 都用不上。

    两个动作都走 ``services.artifacts``（它自己按会话决定落点：工作区模式给相对路径、
    对象模式给产物 id），所以这里**同一个工具在两种会话下都成立**。
    """
    if not conversation_id:
        return ToolOutcome(
            content="这条链路没有会话，用不了文件区（要看本机文件用 list_files）。"
        )
    if name == "list_conversation_files":
        try:
            # 会话文件区是**平铺**的（记录驱动），没有 path 可言——见 artifacts.list_files
            listing = services.artifacts.list_files(conversation_id)
        except KylabError as exc:
            return ToolOutcome(content=f"列不了这条会话的文件：{exc}")
        # 每行**自解释**（D34，2026-09-28 走查）：原来的形状是 `f art_6a6f9058babd（2.3 KB）`
        # ——既没有文件名，也没说那串 id 是干什么的。于是模型把**整行**当 key 交给
        # read_conversation_file，实测必然读失败（`文件不存在：f art_6a6f9058babd（2.3 KB）`），
        # 白烧掉好几次调用。现在文件名与 `key=` 标签都写出来，表头再点一句"只取 key= 后面那串"。
        lines = [
            f"{'目录' if item.is_dir else '文件'} {item.name} — key={item.key}"
            + ("" if item.is_dir else f"（{_size_text(item.size_bytes)}）")
            for item in listing.entries
        ]
        note = "这份清单被截断过（只给了前一批）" if listing.truncated else ""
        return ToolOutcome(
            content=_join_blocks(
                f"{listing.label}：{len(listing.entries)} 项",
                "读某一份就把 `key=` 后面那一串原样交给 read_conversation_file（别带这一行其它字）",
                chr(10).join(lines) or "（文件区是空的）",
                note,
            ),
            summary=f"{listing.label} {len(listing.entries)} 项",
        )

    key = str(args.get("key") or "").strip()
    if not key:
        return ToolOutcome(content="缺少参数：key（用 list_conversation_files 拿）")
    try:
        content, filename = services.artifacts.read_file(conversation_id, key)
    except KylabError as exc:
        return ToolOutcome(content=f"读不了这份文件：{exc}")
    # **先按名字判一次**（D36）：PDF 这类文件头里没有 NUL、还能按 UTF-8 解出来，
    # 只看字节会把它当文本交给模型（实测读到 `1: %PDF-1.4 …` 那种原始字节）
    if looks_binary_name(filename):
        return _binary_file_outcome(filename, key)
    text = _text_or_none(content)
    if text is None:
        return _binary_file_outcome(filename, key)
    lines = text.splitlines()
    start = max(1, _int_or_none(args.get("offset")) or 1)
    limit = max(1, min(MAX_READ_LINES, _int_or_none(args.get("limit")) or DEFAULT_READ_LINES))
    window = lines[start - 1 : start - 1 + limit]
    body = chr(10).join(f"{start + index}: {line}" for index, line in enumerate(window))
    more = start + len(window) <= len(lines)
    return ToolOutcome(
        content=_join_blocks(
            f"【{filename}】第 {start}–{start + len(window) - 1} 行（共 {len(lines)} 行）",
            body or "（这一段是空的）",
            f"接着读用 offset={start + len(window)}" if more else "",
        ),
        summary=f"读了 {filename} 的 {len(window)} 行",
    )


def _text_or_none(content: bytes) -> str | None:
    """字节 → 文本；**二进制给 ``None``**（判据与 `agent_files` 那一侧**共用同一份**：
    魔数 / NUL 见 `looks_binary_bytes`，再加上"根本不是合法 UTF-8"）。

    注意"文件名"这条判据在调用点（`looks_binary_name`）——这里只拿得到字节，
    而 PDF 那种**没有 NUL** 的二进制只能靠名字先拦一道（D36）。
    """
    if looks_binary_bytes(content):
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _binary_file_outcome(filename: str, key: str) -> ToolOutcome:
    """二进制读不了——**如实说，并给出下一步**（按"代价最小、最不越权"排序）。

    顺序是刻意的（用户点名的 2026-09-29 走查）：

    1. **压缩包先解压再读**：`unzip` 就能读，绕去知识库是**缘木求鱼**（用户原话）；
    2. 再是"看元信息"（`file` / `ls -l`），用来判断它到底是什么；
    3. **最后**才是入库（`ingest_file`）——而且**只在用户明确要求时**：
       知识库是用户的资产，**模型不该替用户往里塞东西**（同一批走查的机制那一条）。

    以前这里把"加进知识库"写成**首选** ✗，于是读文件失败就变成一次入库动作。
    """
    return ToolOutcome(
        content=(
            f"{filename} 是二进制文件（图片 / PDF / Office 之类），读不出文本。"
            "下一步按这个顺序试："
            "① **是压缩包就用 `run_command` 解开再读**"
            "（例如 `unzip -o 包 -d /tmp/解压处`，然后 read_file 读解压出来的文件）；"
            "② 想先了解它是什么，用 `run_command` 跑 `file` / `ls -l`；"
            "③ **只有在用户明确要求把这份材料收进知识库时**，才用 `ingest_file`"
            f"（path 给同一个 key：{key}），处理完再用 `search` 检索。"
            "**不要替用户决定往知识库里塞东西。**"
        ),
        summary="二进制文件，读不出文本",
    )


def _ingest_file(
    services: Any,
    caller: Caller,
    conversation_id: str | None,
    roots: Any,
    args: dict[str, Any],
) -> ToolOutcome:
    """把**会话里已经有的文件**加进知识库（v0.55）。

    两条来源，按顺序找（顺序是刻意的：文件区是"用户给的东西"，文件面是"模型自己造的东西"）：

    1. **会话文件区**（``artifacts.read_file``）——用户上传的、以及产出的文件。
       工作区模式按相对路径落到磁盘，对象模式按产物 id 取对象存储里那份；
    2. **文件面**（``agent_files.read_bytes``）——工作区 / 沙箱里的相对路径，
       可以给 ``where`` 指定哪个根。

    入库那一半**与 ``upload_document`` 共用同一份实现**（``ingest.submit`` +
    ``documents.enqueue_ingest``）：两条路只是"字节从哪儿来"不同，落库之后一模一样。
    """
    kb_id = str(args.get("knowledge_base_id") or "").strip()
    path = str(args.get("path") or "").strip()
    if not kb_id or not path:
        return ToolOutcome(content="缺少参数：knowledge_base_id 与 path")
    # 写操作先过作用域：越界时指出是哪个库（模型据此能告诉对方"这把 Key 没那个库的写权限"）
    services.kb.api_keys.check_access(caller, need=WRITE, kb_ids=[kb_id])

    content: bytes | None = None
    filename = path.rsplit("/", 1)[-1] or "文件"
    if conversation_id:
        try:
            content, filename = services.artifacts.read_file(conversation_id, path)
        except KylabError:
            # 不在文件区里就往下走文件面——**不是错误**，两条来源本来就都常见
            content = None
    if content is None:
        try:
            content, filename = read_bytes(
                roots, where=args.get("where"), path=path, max_bytes=MAX_UPLOAD_BYTES
            )
        except KylabError as exc:
            return ToolOutcome(
                content=(
                    f"找不到这份文件：{path}（{exc}）。"
                    "文件区的 key 用 list_conversation_files 拿；本机的相对路径用 list_files 拿。"
                )
            )

    outcome = services.kb.ingest.submit(
        knowledge_base_id=kb_id,
        filename=filename,
        content=content,
        # 记上"是谁传的"（与 upload_document 同一口径）
        uploaded_by=caller.user.id if caller.user is not None else None,
    )
    if not outcome.is_duplicate:
        services.kb.documents.enqueue_ingest(outcome.document.id)
    return ToolOutcome(
        content=_join_blocks(
            f"{outcome.document.name} → {outcome.document.id}",
            "内容与库里已有文档相同，没有重复入库"
            if outcome.is_duplicate
            else "已入队处理，可以用 get_document_status 查进度；处理完就能被 search 检索到",
        ),
        summary="已放进知识库" if not outcome.is_duplicate else "库里已有同一份",
    )


def _read_memory(services: Any, caller: Caller, args: dict[str, Any]) -> ToolOutcome:
    """读**整份用户档案**（磁盘上那份 Markdown 的原文）。

    **它存在的理由**（§7.4）：档案虽然每轮已经注入，但模型写进去之后要能**自查**
    它落在哪、写成什么样——而"更正一条"（``replaces``）要求**逐字**给出目标，
    注入块里那几条是渲染过的（标题与条目行的形状可能与文件里不同）。
    这里给的是文件原文（含 frontmatter 与用户手写的部分），比注入块多一层
    "文件是什么样"的事实。

    结果渲染成文本而不是 JSON——这段文字是给模型读的，JSON 里换行会变成一屏
    ``\\n``（与 ``_run_file_tool`` 同一个理由）。
    """
    del args
    owner_id = caller.owner_id if caller is not None else None
    text = str(services.memory.archive_text(owner_id) or "").strip()
    if not text:
        return ToolOutcome(
            content="档案还是空的（一条都没写）。要用 `remember` 记第一条。",
            summary="档案还是空的",
        )
    return ToolOutcome(content=text, summary="读了整份档案")


def _write_memory(services: Any, caller: Caller, args: dict[str, Any]) -> ToolOutcome:
    """**已退场的兼容壳**：一律拒绝，并说清现在该用什么。

    ``write_memory``（整份改写 ``PROFILE.md``）在 v0.56 随档案制退场（§7.4）：
    整份覆盖与"条目级预算 + 变更流"不相容——它一次就能把预算撑爆、又能把变更流
    整个绕过（删掉一条不留痕）。档案的写入只有 `remember` / `forget` 两条路，
    外加界面上按条目编辑。

    工具表里已经不摆它（见 ``_MEMORY_TOOLS``），这一支留着只为"老客户端/老提示词
    仍然按名字调它"这一种情况：那时给一句"改用 remember"比抛"未知的工具"更可解释，
    也让模型知道**不要再试第二次**。
    """
    del services, caller, args
    return ToolOutcome(
        content=(
            "`write_memory` 已经不用了：档案是一句一条维护的，整份覆盖会把预算与"
            "变更流一起绕过去。现在这样写：\n"
            "- 记一条新的：`remember(content)`（可选 `section` 指定分区）；\n"
            "- 更正一条：`remember(content, replaces=旧的那句原文)`——一次调用即可；\n"
            "- 删掉一条：`forget(topic)`；\n"
            "- 想看档案现在写成什么样：`read_memory`。\n"
            "**`SOUL.md` 与 `AGENTS.md` 是对方自己的东西**：想改就把建议说给他听。"
        )
    )


def _list_tables(services: Any, scope: list[str]) -> ToolOutcome:
    """列有结构化副本的表格。范围与 ``search`` 同一套：**会话选定的那些库**。"""
    if not scope:
        return ToolOutcome(content=_NO_KB_SCOPE, summary="没有可查的知识库")
    items = services.kb.tabular.tables(kb_ids=scope)
    if not items:
        return ToolOutcome(
            content=(
                "这一轮能查的库里没有表格文档（只有 CSV / Excel 会有结构化副本）。"
                "PDF、Word 里的表格答不了统计问题——那是检索的活。"
            ),
            summary="没有表格可查",
        )
    lines = []
    for item in items:
        columns = "、".join(str(name) for name in item["columns"])
        lines.append(f"{item['document_id']}（{item['name']}，{item['rows']} 行）\n  列：{columns}")
    return ToolOutcome(
        content=_join_blocks(
            "有结构化副本的表格（SQL 里用这个 id 当表名）：",
            chr(10).join(lines),
            "统计类问题用 query_table 跑 SQL；只是想看几行原文用 read_document 那条路（检索）。",
        ),
        summary=f"{len(items)} 张表",
    )


def _query_table(services: Any, scope: list[str], args: dict[str, Any]) -> ToolOutcome:
    if not scope:
        return ToolOutcome(content=_NO_KB_SCOPE, summary="没有可查的知识库")
    sql = str(args.get("sql") or "")
    try:
        payload = services.kb.tabular.query_sql(
            sql=sql, kb_ids=scope, limit=_int_or_none(args.get("limit")) or 100
        )
    except Exception as exc:
        # 报错**原样回给模型**：里面的措辞是照着"它下一步该怎么做"写的
        # （表名不在范围内会告诉它能用哪些表），包装成"查询失败"就白写了
        logger.info("表格查询被拒或失败：%s", sanitize_log_value(exc))
        return ToolOutcome(content=str(exc), summary="查询没跑成")
    columns = [str(item) for item in payload["columns"]]
    rows: list = payload["rows"] if isinstance(payload["rows"], list) else []
    body = _table_text(columns, [[str(cell) for cell in row] for row in rows])
    return ToolOutcome(
        content=_join_blocks(
            f"SQL：{payload['sql']}",
            body,
            f"{len(rows)} 行" + ("（已达上限，可能还有更多）" if payload["truncated"] else ""),
            str(payload["note"]),
        ),
        summary=f"查到 {len(rows)} 行" if rows else "查询没有结果",
    )


def _table_text(columns: list[str], rows: list[list[str]]) -> str:
    """结果渲染成 Markdown 表。**不用 JSON**：模型对表格形状的读数比一层
    字段名包着的数组准（它要念的是"这一列是什么"）。单元格按 60 字截断——
    一格里塞一篇正文会让整张表没法看，而那种内容本来也不该进 SELECT *。"""
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(cell[:60].replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(item for item in blocks if item and item.strip())


# ------------------------------------------------------------------ 定时任务


def _schedule_task(
    services: Any, caller: Caller, scope: list[str], args: dict[str, Any]
) -> ToolOutcome:
    """挂一条定时任务（v0.33）。

    **库范围默认跟随这条对话**（而不是空）：模型在对话里被要求"以后每天帮我盯这件事"，
    它手里最合理的资料范围就是此刻这一轮的库——留给它一个空白字段，
    它要么编一个、要么把库全勾上，两种都不如"跟现在一样"。

    时间的解释权在服务层（cron 的解析只有一份），报错原文回给模型：
    那里的措辞是照着"怎么改对"写的。
    """
    sessions = getattr(services, "schedules", None)
    if sessions is None:  # pragma: no cover - 只在手工拼 Services 的测试里出现
        return ToolOutcome(content="这台服务没有启用定时任务。", summary="定时任务不可用")
    name = str(args.get("name") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    run_at_text = str(args.get("run_at") or "").strip()
    cron_text = str(args.get("cron") or "").strip()
    asked = [str(item) for item in (args.get("knowledge_base_ids") or []) if str(item)]
    # 先解析时间（**在 try 之外**）：格式不对是"参数给错了"，该作为工具错误抛出去，
    # 而不是被下面那段"建失败"的兜底吞成一句内容（那样模型看不出自己写错了格式）
    when = _parse_when(run_at_text) if run_at_text and not cron_text else None
    try:
        record = sessions.create(
            name=name,
            prompt=prompt,
            # 给了具体时间就是一次性的；两个都没给时按"每天"处理并让 cron 校验去报错
            kind="once" if when is not None else "cron",
            cron=cron_text,
            run_at=when,
            kb_ids=asked or scope,
            owner_id=caller.owner_id,
        )
    except Exception as exc:
        logger.info("定时任务建失败：%s", sanitize_log_value(exc))
        return ToolOutcome(content=str(exc), summary="定时任务没建成")
    return ToolOutcome(
        content=_join_blocks(
            f"已挂上定时任务「{record.name}」（{sessions.next_run_text(record)}，"
            f"按 {timezone_name()} 计算）。",
            "到点它会自己跑一遍，结果落在一条同名会话里——对方可以在「任务中心 → 定时任务」"
            "看到它，也可以点「立即跑一次」当场试验。",
        ),
        summary=f"已挂上定时任务：{sessions.next_run_text(record)}",
    )


def _list_scheduled(services: Any, caller: Caller) -> ToolOutcome:
    sessions = getattr(services, "schedules", None)
    if sessions is None:  # pragma: no cover
        return ToolOutcome(content="这台服务没有启用定时任务。", summary="定时任务不可用")
    records = sessions.list(owner_id=caller.owner_id)
    if not records:
        return ToolOutcome(content="还没有挂过定时任务。", summary="没有定时任务")
    lines = []
    for item in records:
        state = "启用" if item.enabled else "已停用"
        last = {
            "ok": "上次跑成了",
            "degraded": "上次没跑完（撞上步数或时间闸）",
            "failed": f"上次失败：{item.last_error[:80]}",
        }.get(item.last_status, "还没跑过")
        lines.append(
            f"- {item.name}（{state}，{sessions.next_run_text(item)}）：{item.prompt[:60]}"
            f"\n  下次：{_local_text(item.next_run_at)}；{last}"
        )
    return ToolOutcome(
        content="已挂的定时任务：\n" + "\n".join(lines), summary=f"{len(records)} 条定时任务"
    )


def _parse_when(text: str) -> datetime:
    """把 ``run_at`` 解析成时间。**不带时区的按服务器本地时间解释**——
    与界面上的 datetime-local 同一口径（用户填的是他看到的钟点）。"""
    cleaned = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise InvalidRequestError(
            f"run_at 不是合法的时间：{text!r}（用 ISO 8601，如 2026-09-21T09:00）"
        ) from exc


def _local_text(moment: datetime | None) -> str:
    if moment is None:
        return "不会再跑"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _size_text(value: object) -> str:
    if not isinstance(value, int):
        return "大小未知"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


#: 关掉知识库开关时那三个知识库工具统一用这句话（避免三处各写一份措辞）。
_NO_KB_SCOPE = (
    "这一轮没有可查的知识库（对方关掉了知识库，或本会话没选库）。"
    "需要资料的话，先把这个问题告知对方，不要凭常识补。"
)


# ------------------------------------------------------------------ 技能


def _render_skills(services: Any, args: dict[str, Any] | None = None) -> str:
    """列技能（**一页**）：这是"想看全部字段/状态"时才调的——目录每轮已经在提示词里了。

    为什么要分页（§12.341 ⑤，2026-09-29 用户点名的会话 `conv_a5f4628f405f`）：
    这台机器上有近两百个技能，全量输出一次是 **12,024 字**（结果被截断），
    而模型要的通常只是"有没有某个能力"或"某个为什么不可用"——一条一条铺满上下文，
    既挤掉别的资料，也让它读不完。所以：**默认一页 40 条**、`offset` 翻页、
    返回里直说"共几个、这是第几条到第几条、下一页从哪开始"。

    两种"不可用"要**分开说**（v0.43）：`discarded` 是没通过格式校验的
    （缺字段、描述超 1024——照 ZCode 的规则丢弃），`used_by_prompt=False`
    是别的缘故（例如同名被更高优先级的技能遮蔽）。混成一句"被拦下"，
    用户没法知道该改什么。
    """
    records = services.skills.list()
    if not records:
        return "这台机器上还没有安装技能。"
    options = args or {}
    offset = max(0, _as_int(options.get("offset"), 0))
    limit = min(max(1, _as_int(options.get("limit"), SKILLS_PAGE_SIZE)), SKILLS_PAGE_MAX)
    page = records[offset : offset + limit]
    if not page:
        return (
            f"技能一共 {len(records)} 个，offset={offset} 已经越界了。"
            "从头看就再调一次 `list_skills(offset=0)`。"
        )

    lines = []
    for record in page:
        if getattr(record, "discarded", False):
            reason = str(getattr(record, "flagged", "") or "没通过格式校验")
            usable = f"已丢弃（{reason}）"
        elif record.used_by_prompt:
            usable = "可用"
        elif getattr(record, "flagged", ()):
            # **有原因就照原因说**（2026-10-04）：`used_by_prompt=False` 至少有三种来路
            # ——被同名技能遮蔽、被用户**关掉**（D23 的 `chat.disabled_skills`）、
            # 以及将来别的"不进提示词"的理由。全写成"被同名技能遮蔽"会让模型
            # （和照它转述的用户）拿到一个**错的**原因：明明是自己关的，
            # 却被告知"有同名技能挡住了"——下一步就去找一个不存在的重名技能。
            # `SkillService.list()` 已经把这些理由写进 `flagged` 了，这里只是别丢掉它们。
            reasons = getattr(record, "flagged", ()) or ()
            if isinstance(reasons, str):  # 假记录（用例）可能给的是字符串
                reasons = (reasons,)
            usable = "、".join(str(item) for item in reasons)
        else:
            usable = "被同名技能遮蔽（不进提示词）"
        lines.append(f"- {record.name}（{usable}）：{record.description}")

    first, last = offset + 1, offset + len(page)
    head = f"技能 {first}–{last} / 共 {len(records)} 个："
    tail = ""
    if last < len(records):
        tail = (
            f"\n\n（后面还有 {len(records) - last} 个："
            f"再调一次 `list_skills(offset={last})` 看下一页；"
            "要某个技能的正文用 `read_skill`）"
        )
    return f"{head}\n" + "\n".join(lines) + tail


def _as_int(value: object, default: int) -> int:
    """把模型给的数当数看（它可能给字符串、也可能给 `true` 这类东西）。

    解析不了就用默认值：**翻页参数出错不该让这次调用失败**——最坏是回到第一页，
    模型的下一步是再调一次，而不是拿到一句"参数非法"再猜。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，得先挡掉
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _read_skill(services: Any, args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "缺少参数：name"
    record, body = services.skills.read(name)
    relative = str(args.get("file") or "").strip()
    if not relative:
        return f"【技能 {record.name}】\n{body}"
    path = _safe_attachment(record.directory, relative)
    if path is None:
        return "附属文件路径不合法（只能读技能目录里的文件）。"
    if not path.is_file():
        return f"技能里没有这个文件：{relative}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读不出来：{exc}"
    # 附属文件同样是**第三方内容**：先合并"转义写坏的代理对"，合并后仍非法的**拒绝读**
    # （`errors="replace"` 只挡解码错误，挡不住文件里真有的孤立代理项；
    # 而它一旦进提示词，httpx 编码就抛 UnicodeEncodeError —— **整句对话全废**，D16 P0）
    text = recombine_surrogates(text)
    problem = text_problem(text)
    if problem:
        return f"这个附属文件读不了：{problem}"
    return text[:MAX_ATTACHMENT_CHARS]


def _safe_attachment(directory: str, relative: str) -> Path | None:
    """把相对路径解析到技能目录**之内**。

    技能是可能从市场装来的第三方内容（见 services/skill_market.py 的防护），
    所以 `../..` 这类路径必须挡掉——否则一个技能就能让 agent 去读机器上任何文件。
    """
    root = Path(directory).resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _summary(name: str, payload: Any) -> str:
    """给过程面板一句人话。**只处理"结果里有条数"的那几个**，
    其余留给 `step_detail` 回退到结果开头——写死一堆猜的摘要不如不写。

    **裸列表也要认**（v0.22）：`list_knowledge_bases` 返回的就是一个 list，
    而第一版只处理 dict 里的 ``items``——于是那一行的回退路径把整个 JSON
    原样显示出来了（实测截图：面板里是
    ``[{"id": "kb_...", "name": "城市建成环境研究现状", "documents": 23…``）。
    "结果里有几条"这件事，无论外面包没包一层都一样要说得出来。

    ``items`` 只在这里算一次，**兜底那一条放在最后**：放在前面会把后面那些
    更具体的措辞（"回忆到 N 条"）挡掉——摘要写"共 2 条"没错，但不如说清是什么。
    """
    items = (
        payload
        if isinstance(payload, list)
        else (payload.get("items") if isinstance(payload, dict) else None)
    )
    if name == "list_knowledge_bases" and isinstance(items, list):
        return f"共 {len(items)} 个知识库"
    if name == "list_documents" and isinstance(payload, dict):
        total = payload.get("total")
        if isinstance(total, int):
            return f"共 {total} 篇文档"
    if name == "get_document_status" and isinstance(payload, dict):
        return f"当前阶段：{payload.get('stage') or '未知'}"
    if name == "recall" and isinstance(items, list):
        return f"查到 {len(items)} 条变更"
    if name == "list_notes" and isinstance(payload, dict):
        total = payload.get("total")
        if isinstance(total, int):
            return f"共 {total} 条笔记"
    if name in ("export_document", "export_table", "export_deck") and isinstance(payload, dict):
        # 这一条原先没人写，于是过程面板里**把整个 JSON 铺了出来**
        # （实测截图：`{"artifact_id": "art_89cb…", "name": "酒馆战棋S14上分攻略….pptx"…`）
        size = payload.get("size_bytes")
        size_text = f"（{int(size) // 1024} KB）" if isinstance(size, int) else ""
        return f"已生成「{payload.get('name') or '文件'}」{size_text}"
    if name == "ingest_artifact" and isinstance(payload, dict):
        return f"已存进知识库：{payload.get('name') or ''}".rstrip("：")
    if name == "remember" and isinstance(payload, dict):
        # 那三段 note 是**给模型看的操作说明**，不是给用户看的结论——
        # 原来它整段出现在过程面板里，用户读到的是"一条只记一句可复用的事实"。
        # 现在按 `action` 说清到底做了什么（§4.4 的四种）。
        return {
            "added": "已记进档案",
            "replaced": "已更正档案里的一条",
            "existing": "档案里已经有了",
            "rejected": "档案没收下这条",
        }.get(str(payload.get("action") or ""), "已写入用户档案")
    if name == "forget" and isinstance(payload, dict):
        if payload.get("action") == "forgotten":
            return "已从档案里删掉"
        return "档案里没有这一条"
    if name == "create_note" and isinstance(payload, dict):
        return f"已存为笔记「{payload.get('title') or ''}」"
    if name == "attach_note_to_kb" and isinstance(payload, dict):
        return "已把笔记加入知识库"
    if name == "create_knowledge_base" and isinstance(payload, dict):
        return f"已新建知识库「{payload.get('name') or ''}」"
    if name == "upload_document" and isinstance(payload, dict):
        suffix = "（库里已有同样的内容）" if payload.get("is_duplicate") else ""
        return f"已上传「{payload.get('name') or ''}」{suffix}"
    if name == "add_data_source" and isinstance(payload, dict):
        return f"已登记数据源「{payload.get('name') or ''}」"
    if name == "delete_document" and isinstance(payload, dict):
        return "已删除这份文档（7 天内可恢复）"
    if name == "search" and isinstance(payload, dict):
        hits = payload.get("hits")
        if isinstance(hits, list):
            return f"命中 {len(hits)} 段原文" if hits else "没有命中任何片段"
    if isinstance(items, list):
        return f"共 {len(items)} 条"
    return ""


def _render(payload: Any) -> str:
    """其余工具的结果：结构体走 JSON，字符串原样。"""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)
