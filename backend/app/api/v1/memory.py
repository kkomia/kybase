"""记忆端点（v0.14 三期；v0.56 起是**一份档案**，设计见 ``docs/设计/记忆档案-设计-v0.1.md``）。

四件事：看状态、用档案（查变更 / 记住 / 忘掉 / 还原 / 组改名）、跑迁移、读文件（名单与原文）。

**记忆与知识库是两个池子**（设计文档 §2.1），这里的一切都只碰记忆那一侧：
没有任何一个端点会去读文档、片段或向量。``GET /memory`` 列的是工作区里的
Markdown 文件，不是知识库文档。

**文件级写入端点与图谱端点已经删掉**（档案制 §6.3、§7.4）：

- ``PUT`` / ``DELETE /memory/files/{path}``：留在这里就是一个**绕过预算与变更流的
  后门**——整份覆盖能一次撑爆预算，也能删掉一条而不留痕；档案的写入只有
  ``POST /memory/remember``（外加界面上按条目编辑：删一条走 ``POST /memory/forget``，
  还原走 ``POST /memory/restore``）；
- ``GET /memory/graph``：图谱退场（wikilink 那一层随 ``daily/``/``digest/`` 一起清理）。

只读的 ``GET /memory/files/{path}`` **保留**：档案卡右下角那个「原文」要展示磁盘上
那份 Markdown，迁移草稿也要能看。

**鉴权档位与 MCP 那份保持一致**（读用 ``ReadDep``、写用 ``WriteDep``）：
MCP 上 ``recall`` / ``remember`` / ``forget`` 对 API Key 是开放的（外部 agent 得能用记忆），
REST 这边如果收紧成管理员专属，同一串 API Key 从两个入口进来就会得到两种答案
——那正是 ``api/auth.py`` 里说的"两处各写一份就不会有测试同时看到"的不一致。

**浏览与编辑走本地目录**，因此 ``memory.enabled`` 关着也能用（理由见
``services/memory_files.py`` 的模块头）。只有**注入与 recall** 看那个开关。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_read, require_write
from app.api.v1.schemas import (
    MemoryArchiveOut,
    MemoryBudgetOut,
    MemoryChangeOut,
    MemoryChangesOut,
    MemoryDraftOrganizeOut,
    MemoryDraftOut,
    MemoryDraftSuggestionOut,
    MemoryEntryOut,
    MemoryFileDetailOut,
    MemoryFileOut,
    MemoryForgetIn,
    MemoryGroupOut,
    MemoryGroupRenameIn,
    MemoryHitOut,
    MemoryMigrationOut,
    MemoryOverviewOut,
    MemoryRecallIn,
    MemoryRecallOut,
    MemoryRememberIn,
    MemoryRememberOut,
    MemoryRestoreIn,
    MemorySectionOut,
    MemoryStatusOut,
)
from app.core.caller import Caller
from app.core.services import Services, get_services
from app.services import archive_files as af
from app.services import tools as tools_service
from app.services.archive import WriteResult
from app.services.memory import INJECTED_FILES

router = APIRouter(prefix="/memory", tags=["memory"])

#: 查证结果里那条提醒，**与工具那份是同一句**（共用同一个常量，不重写一遍）。
#: 写两份的话，模型从 MCP 听到的和人在界面上看到的就是两种说法。
RECALL_NOTE = tools_service.RECALL_NOTE


def _scope(caller: Caller) -> str | None:
    """**这个调用者的记忆属于谁**——"一个账号一个 Agent"在记忆层的落点。

    普通成员 → 自己的账号（各自一份 ``data/memory/<user_id>/``）；
    管理员会话与 API Key 通道 → ``None``（共享桶，即 ``data/memory/`` 本身）。

    与知识库 / 会话 / 笔记的归属口径一致（见 ``api/auth.py``）：
    管理员用网页会话要能看到全部，所以不能折成"管理员=自己的账号"。
    """
    if caller.user is not None and not caller.is_admin:
        return caller.user.id
    return None


def _file_out(record) -> MemoryFileOut:  # type: ignore[no-untyped-def]
    """记录 → 响应。不 import 存储层类型（工程规范 §3.3 L1），
    形状由 services 返回的对象保证（与 wiki.py 的 ``_page_out`` 同一套写法）。"""
    return MemoryFileOut(
        path=record.path,
        name=record.name,
        title=record.title,
        kind=record.kind,
        summary=record.summary,
        tags=list(record.tags),
        size_bytes=record.size_bytes,
        modified_at=record.modified_at,
        # "会被注入"是**那几份设定文件**的专属性质（每轮进 system prompt，见 §5.1），
        # 名字清单由记忆层给（`INJECTED_FILES`）：v0.56 起 `MEMORY.md` **不在里面**
        # ——它已经退场（§7.2），界面上它显示成"旧记忆（只读）"。
        injected=record.path in INJECTED_FILES,
    )


def _status_out(record) -> MemoryStatusOut:  # type: ignore[no-untyped-def]
    """状态 → 响应。**全是服务层数出来的本地数字**（几份文件、上次改动），
    界面只该显示数字：这些数的口径（哪些目录算记忆、什么算一份）只有服务层知道。
    """
    return MemoryStatusOut(
        enabled=record.enabled,
        workspace=record.workspace,
        core_file_exists=record.core_file_exists,
        file_count=record.file_count,
        last_changed_at=record.last_changed_at,
        detail=record.detail,
    )


@router.get("", response_model=MemoryOverviewOut, summary="记忆状态与文件列表")
def get_memory(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> MemoryOverviewOut:
    """一次给全页面首屏要的东西（状态 + 文件列表）。

    **状态是纯本地的**（数一遍工作区）：没有第二个进程、没有探测，
    所以"打开记忆页"不会变成一次网络等待。
    """
    scope = _scope(caller)
    files = services.memory.files(scope)
    status_out = _status_out(services.memory.status(scope))
    return MemoryOverviewOut(
        status=status_out,
        files=[_file_out(item) for item in files],
        # 服务层的扫描上限（见 memory_files.MAX_LISTED_FILES）
        truncated=len(files) >= services.memory.scan_limit,
    )


@router.get("/files/{path:path}", response_model=MemoryFileDetailOut, summary="读一个记忆文件")
def read_memory_file(
    path: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> MemoryFileDetailOut:
    """读原文（含 frontmatter）。

    路径里的 ``path:path`` 让 ``digest/wiki/xxx.md`` 这种带斜杠的路径能当**一个**
    路径参数传进来，前端不必把斜杠编码成 ``%2F``（有些反代会先解开再匹配，反而更脆）。
    """
    detail = services.memory.file_text(path, _scope(caller))
    base = _file_out(services.memory.describe(detail.path, _scope(caller)))
    return MemoryFileDetailOut(
        **base.model_dump(),
        content=detail.content,
        meta=detail.meta,
        truncated=detail.truncated,
    )


@router.post("/recall", response_model=MemoryRecallOut, summary="在档案的变更流里查证")
def recall_memory(
    payload: MemoryRecallIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> MemoryRecallOut:
    """与知识库检索**两条路、永不合并**（设计文档 §2.1）——连索引都不共用。

    **池子只有 ``changes.md``**（档案制 §5.3）：用户档案每轮已经全量注入，
    再召回一次就是把同一段内容进两次上下文。所以这里回答的是"这条以前是什么、
    什么时候改的"，``path`` 恒为 ``changes.md``，排序是纯字面判据（无分词、无索引）。

    **没启用时明确报错**，不返回空结果（§2.3）——返回空会让模型（和用户）
    以为"没有相关记忆"，然后基于错误前提继续。启用着而真的没有相关记录时，
    返回空列表才是诚实的答案（那时检索确实跑过了）。
    """
    hits = services.memory.recall(
        payload.query, limit=payload.limit, user_id=_scope(caller)
    )
    return MemoryRecallOut(
        query=payload.query,
        hits=[
            MemoryHitOut(
                text=item.text,
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                score=item.score,
                coverage=item.coverage,
                source=item.source,
            )
            for item in hits
        ],
        note=RECALL_NOTE,
    )


@router.post("/remember", response_model=MemoryRememberOut, summary="记一条（新增或顶替）")
def remember(
    payload: MemoryRememberIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryRememberOut:
    """写进 ``PROFILE.md``（档案）的一个分区，**不经过任何外部东西**。

    三件事与工具那一侧**同源**：

    - **不看 ``memory.enabled``**：档案关着时照样可写（写进去下一轮就注入）；
    - **``replaces`` 让"更正"一次完成**（§4.3）：填要顶替的那条原文；
    - **返回体带 ``action`` 与 ``receipt``**（§4.4）：``action`` 是
      ``added`` / ``replaced`` / ``existing`` / ``rejected``，``receipt`` 是
      给人看的那一句——**界面与模型用的是同一句**，谁也不该自己另编。

    ``existing`` 与 ``rejected`` 都**不是错误**（一个是"本来就有"，一个是"越线了、
    回执里给了两条出路"），所以它们照样 200：调用方按 ``action`` 分派。
    """
    result = services.memory.remember(
        payload.content,
        section=payload.section,
        replaces=payload.replaces or None,
        user_id=_scope(caller),
    )
    return _write_out(result, services, caller)


@router.get("/archive", response_model=MemoryArchiveOut, summary="档案卡（分区、条目、读数）")
def get_memory_archive(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> MemoryArchiveOut:
    """档案卡首屏要的一切：四个分区（含未知分区）的条目与读数、草稿计数、迁移入口显隐。

    **未知分区照常返回**（§3.5 第 4 条）：用户拿外部编辑器加的 ``## 某区`` 也算进来，
    只是 ``known=false``，界面标"分区不认识"。

    条目的来源小字取自变更流（最近一条把它写进来的记录）：`显式/隐式` 来自会话、
    `界面` 来自这一页、`迁移` 来自旧记忆折叠；对不上记录的就是空（外部编辑器直接改的）。
    """
    scope = _scope(caller)
    archive = services.memory.archive(scope)
    parsed = archive.read()
    budget = archive.budget(parsed)
    origin: dict[str, tuple[str, str, int]] = {}
    for index, record in enumerate(archive.changes()):
        if (
            record.action in (af.ACTION_ADDED, af.ACTION_REPLACED, af.ACTION_RESTORED)
            and record.new
        ):
            origin[af.normalize(record.new)] = (record.source, record.at, index)

    sections: list[MemorySectionOut] = []
    for item in budget.sections:
        items: list[MemoryEntryOut] = []
        for entry in parsed.entries:
            if entry.section != item.name:
                continue
            source, at, index = origin.get(af.normalize(entry.text), ("", "", -1))
            items.append(
                MemoryEntryOut(
                    text=entry.text,
                    group=entry.group,
                    source=source,
                    change_at=at,
                    change_index=index,
                )
            )
        sections.append(
            MemorySectionOut(
                name=item.name,
                known=item.known,
                entries=item.entries,
                chars=item.chars,
                limit=item.limit,
                suggested_chars=item.suggested_chars,
                group_limit=item.group_limit,
                groups=[
                    MemoryGroupOut(name=name, entries=count)
                    for name, count in item.group_entries
                ],
                items=items,
            )
        )

    workspace = services.memory.workspace_for(scope)
    draft_entries = services.memory.draft_entries(scope)
    return MemoryArchiveOut(
        path=af.ARCHIVE_FILENAME,
        updated=parsed.updated,
        budget=MemoryBudgetOut(
            entries=budget.entries,
            chars=budget.chars,
            entry_limit=budget.entry_limit,
            char_limit=budget.char_limit,
        ),
        sections=sections,
        draft=MemoryDraftOut(
            exists=(workspace / af.IMPORT_DRAFT_FILENAME).exists(),
            path=af.IMPORT_DRAFT_FILENAME,
            entries=draft_entries,
        ),
        migration_available=services.memory.migration_available(scope),
    )


@router.get("/changes", response_model=MemoryChangesOut, summary="变更流（倒序）")
def get_memory_changes(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> MemoryChangesOut:
    """变更流时间线，**最新的在前**（§6.2）。

    ``index`` 是它在这份文件里的位置（**文件顺序，最旧为 0**）——档案卡上那条来源小字
    靠它指回来，所以这里的排序与 ``index`` 是两件事：显示倒序，索引按文件顺序。
    """
    records = services.memory.archive(_scope(caller)).changes()
    changes = [
        MemoryChangeOut(
            index=index,
            at=record.at,
            action=record.action,
            section=record.section,
            source=record.source,
            old=record.old,
            new=record.new,
            restorable=record.action in (af.ACTION_REPLACED, af.ACTION_FORGOTTEN)
            and bool(record.old),
        )
        for index, record in reversed(list(enumerate(records)))
    ]
    return MemoryChangesOut(changes=changes)


@router.post("/forget", response_model=MemoryRememberOut, summary="忘掉一条")
def forget_memory(
    payload: MemoryForgetIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryRememberOut:
    """从档案删除 + 变更流留痕 + 可还原（§9.2 第 6 条）。**不看 ``memory.enabled``**。

    对得上不止一条时**不猜**：``action`` 是 ``rejected``，回执列出候选让它说清。
    """
    result = services.memory.forget(payload.topic, user_id=_scope(caller))
    return _write_out(result, services, caller)


@router.post("/restore", response_model=MemoryRememberOut, summary="还原一条旧值")
def restore_memory(
    payload: MemoryRestoreIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryRememberOut:
    """把一条旧值写回档案，新值作为一次新的顶替进流（§3.4、§6.2）。

    ``text`` 是**要还原的那条旧值原文**（变更流里 ``旧：`` 后面那一行）。
    """
    result = services.memory.restore(payload.text, user_id=_scope(caller))
    return _write_out(result, services, caller)


@router.post("/group", response_model=MemoryRememberOut, summary="项目组改名")
def rename_memory_group(
    payload: MemoryGroupRenameIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryRememberOut:
    """项目段的组标题改名（§3.1 第 2 条：改名 = 一次顶替，旧名进变更流）。

    改的是这一组全部条目的分组名，正文不动；变更流只留一条记录。
    """
    result = services.memory.rename_group(
        payload.section, payload.old, payload.new, user_id=_scope(caller)
    )
    return _write_out(result, services, caller)


@router.post("/migrate", response_model=MemoryMigrationOut, summary="折叠旧记忆（零模型调用）")
def migrate_memory(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryMigrationOut:
    """跑一遍机械折叠迁移（§8）：把旧文件折成档案初稿，返回迁移报告。

    **只读旧文件、只写新文件**（§8.4）：``MEMORY.md`` / ``digest/**`` / ``daily/**``
    一字不动；可重跑、幂等（源文件指纹没变且档案已存在时 ``skipped=true``，净改动为零）。
    **零模型调用**——模型整理初稿那一步不在这里。
    """
    report = services.memory.migrate(_scope(caller))
    return MemoryMigrationOut(
        added=report.added,
        replaced=report.replaced,
        existing=report.existing,
        dropped_sensitive=report.dropped_sensitive,
        downgraded=report.downgraded,
        trimmed=report.trimmed,
        skipped=report.skipped,
        archive_changed=report.archive_changed,
        draft_entries=report.draft_entries,
        per_source=list(report.per_source),
    )


@router.post(
    "/draft/organize",
    response_model=MemoryDraftOrganizeOut,
    summary="整理迁移草稿（一次模型调用，只给建议）",
)
def organize_memory_draft(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> MemoryDraftOrganizeOut:
    """跑一次模型，把 ``import-draft.md`` 里的旧条目改写成画像条目（§8.3）。

    **用户显式点一次才发生**（"会花钱的默认关"），而它**一个字都不写**：
    返回的是预览建议，用户确认之后前端逐条打 ``POST /memory/remember``——
    于是这一次模型调用不可能绕过预算、顶替判据与变更流（§3.3–§3.4），
    每一条的回执也仍然是从那一处文案来的。

    **失败与"没整理出东西"都如实报错**（映射成可读的错误信封）：
    这一次是花过钱的，静默返回空列表会让用户以为"点了没反应"。
    """
    items = services.memory.organize_draft(_scope(caller))
    return MemoryDraftOrganizeOut(
        items=[MemoryDraftSuggestionOut(text=item.text, section=item.section) for item in items],
        note="这些还只是建议，确认之后才会写进档案。",
    )


def _write_out(result: WriteResult, services: Services, caller: Caller) -> MemoryRememberOut:
    """一次写入的结果 → 响应（``remember`` / ``forget`` / ``restore`` / 改名共用）。

    ``entries`` 是写完之后档案里一共几条（界面读数），**写后现读**：被拒时档案没变，
    这个数照旧是当前值，不给它就没法在这一条上继续显示预算。
    """
    return MemoryRememberOut(
        action=result.action,
        receipt=result.receipt,
        text=result.text,
        section=result.section,
        replaced=result.replaced,
        entries=len(services.memory.archive_entries(_scope(caller))),
    )
