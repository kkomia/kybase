"""技能端点（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.1）。

技能是磁盘上的 ``SKILL.md``（仓库自带 ``skills/`` + 数据目录 ``data/skills/``）。
这一组端点做两件事：**列出来给人看**、**读正文给模型用**。

**技能是部署级的能力，不按账号隔离**（与工作区不同）：它是一段流程文本，
放在哪个账号名下都不改变它的内容。真正的账号隔离在于
"谁能看到哪些工作区、哪些知识库"——那两样才是数据。

被安全扫描拦下的技能**照样列出来**，只是标着原因（``flagged``）且不进模型目录：
静默藏掉会让用户以为技能没装上，而"为什么它不生效"就成了一个查不出的问题。

**被丢弃的技能（P0-3）同样列出来**，`discarded=true` + ``flagged`` 里那条理由：
校验（缺 ``name``/``description``、描述超长）是在加载阶段做的，坏技能不进目录也读不出
正文，但它写在磁盘上、也确实"装了"——能力页要说得出这件事，否则用户只会看到
"我明明放进去了，怎么没有"。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.api.auth import require_admin, require_read
from app.api.v1.schemas import (
    MarketSkillOut,
    SkillBrowseIn,
    SkillBrowseOut,
    SkillBundleOut,
    SkillCategoryOut,
    SkillDetailOut,
    SkillEnabledIn,
    SkillInspectIn,
    SkillInstalledOut,
    SkillInstallIn,
    SkillListOut,
    SkillMarketEntryOut,
    SkillMarketIn,
    SkillMarketOut,
    SkillOut,
    SkillSourceIn,
    SkillSourceInstallIn,
    SkillSourceListOut,
    SkillSourceOut,
    SkillSourcePatchIn,
)
from app.core.caller import Caller
from app.core.exceptions import InvalidRequestError
from app.core.services import Services, get_services
from app.services.skill_categories import CATEGORIES
from app.services.skill_market import MAX_UNPACKED_BYTES
from app.services.skills import DISABLED_REASON, featured_by_category

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(record, summary: str = "", *, featured: bool = False) -> SkillOut:  # type: ignore[no-untyped-def]
    """技能记录 → 界面形状。``summary`` 是**中文简介**（v0.28，v0.53 补上内置那批）。

    两个来源，先看清单再退回技能自己：

    1. **市场装的技能**：安装时记进清单的那一行（``data/installed.json``）。技能的
       ``SKILL.md`` 我们一个字都不改（见 ``services/skill_blurb.py``），所以简介
       只能存在清单里，由调用方查出来传进来；
    2. **仓库自带的技能**（内置那 5 个）：没有安装那一步，简介写在 ``SKILL.md``
       的 frontmatter 里（``SkillRecord.summary``）——**同一件东西，同一处消费**。

    **两边都有时以清单那份为准**（市场那份是为中文界面存的、更短更贴；frontmatter
    那份常是英文），没有才退回技能自己那份。这个顺序必须与命令菜单那边
    （``core/services.py`` 的 ``_skill_summaries``）一致——反了就会出现
    "能力页一句、菜单里另一句"。

    ``category`` 直接来自记录（扫描时算好的，见 ``SkillRecord.category``）；
    ``featured`` 是**列表级的判断**（"这一类默认摆哪几条"），只有列表端点算得出来，
    所以由调用方传进来——别在这里自己算：那是全库排序，详情端点不该为它付代价。
    """
    return SkillOut(
        name=record.name,
        description=record.description,
        summary=summary or record.summary,
        source=record.source,
        path=record.path,
        directory=record.directory,
        used_by_prompt=record.used_by_prompt,
        # **用户的开关**（D23）与"实际进没进"是两件事：安全扫描/依赖/丢弃都会让
        # `used_by_prompt` 为 false，那时开关仍该画成"开着"（关它是没用的）。
        # 判据就是服务写进去的那条理由常量——不是另写一句字面量，免得两处漂。
        enabled=DISABLED_REASON not in record.flagged,
        flagged=list(record.flagged),
        category=record.category,
        featured=featured,
        discarded=record.discarded,
    )


def _summaries(services) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """已装技能的中文简介（``技能名 → 简介``）。读不出来就当没有。"""
    try:
        records = services.skill_market.installed_records()
    except Exception:  # 清单坏了不该让技能列表打不开——它只是界面上多一行
        return {}
    return {
        name: str(record.get("summary") or "")
        for name, record in records.items()
        if record.get("summary")
    }


def _installed_names(services) -> list[str]:  # type: ignore[no-untyped-def]
    """"装进来的"那批技能名（市场 / 上传安装清单）。读不出来就当空表。

    只用来给精选排序**多一档判据**（装过一次的比随库躺着的更说明有人要它），
    所以它坏了不该让列表打不开——与 `_summaries` 同一处置。
    """
    try:
        return list(services.skill_market.installed())
    except Exception:  # 清单坏了只是少一档判据
        return []


@router.get("", response_model=SkillListOut, summary="技能列表")
def list_skills(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SkillListOut:
    """**每次都重新扫磁盘**：用户可能刚往 ``data/skills/`` 丢了一个技能，
    而那正是"技能比插件轻"的地方——不该要求他重启或点"重新加载"。

    （"每次重新扫"现在由 `SkillService` 的目录签名 + 单条记录缓存兜着：
    目录没变、文件没改时只花两次 stat；见 `SCAN_TTL_SECONDS`。）

    **分页是后加的（2026-09-29 性能事故）**：库涨到 6,000+ 条之后，一次返回全部
    既是 18 秒的另一半原因，也没人真的会一屏看 6,000 行。**默认行为一位不变**
    （不传 `limit` 就是全部，向后兼容），前端以后再逐步采用。
    `total` 与 `usable` 报的是**全库**的数，不是这一页的（否则界面上"共几条"会随着
    翻页变来变去）。

    **分类与精选（v0.61）**同样是**全库**的口径，不是这一页的：每条的 `category`
    来自记录，`categories` 给"界面默认摆什么"（每类 2 条，判据机械可复现）。
    翻到第 3 页时那两条精选标记**仍然标在它们自己身上**——精选是技能的一个属性，
    不该随分页变。这两样都**不进提示词目录**（那条预算仍是 60 条 / 2 万字符）。"""
    items = services.skills.list()
    summaries = _summaries(services)
    featured = featured_by_category(items, installed=_installed_names(services))
    marks = {item.slug for group in featured.values() for item in group}
    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    window = items if limit is None else items[offset : offset + limit]
    return SkillListOut(
        items=[
            _out(item, summaries.get(item.name, ""), featured=item.slug in marks)
            for item in window
        ],
        usable=sum(1 for item in items if item.used_by_prompt),
        categories=[
            SkillCategoryOut(
                slug=group.slug,
                label=group.label,
                total=counts.get(group.slug, 0),
                featured=[item.name for item in featured.get(group.slug, ())],
            )
            for group in CATEGORIES
        ],
    )


@router.put("/{name}/enabled", response_model=SkillOut, summary="开/关一条技能")
def set_skill_enabled(
    name: str,
    payload: SkillEnabledIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillOut:
    """单条技能的启停（D23，2026-09-28 走查）。

    **关掉 = 不进提示词**：文件不动、市场那边的安装记录也不动，只是"这一轮不给模型看"。
    权限与装/卸插件同一档（``require_admin``）：技能会改变模型的行为，这是一件
    **部署级**的事，与"改我自己的偏好"不是一类。
    """
    record = services.skills.set_enabled(name, payload.enabled)
    return _out(record, _summaries(services).get(record.name, ""))


@router.get("/{name}", response_model=SkillDetailOut, summary="技能详情（含正文）")
def get_skill(
    name: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> SkillDetailOut:
    """连正文一起给：界面上要能读它（这也是用户核对"这个技能到底教了模型什么"的地方）。

    **被丢弃的技能这里也读得出来**（``allow_discarded=True``）：模型那条路
    （``read_skill`` 工具）读不到，但人要看的就是"它到底写了什么、为什么被丢掉"
    ——详情页是排错的地方，藏起来等于让人只能去翻磁盘。

    ``translate_names=False``：这一份是给人看的，**照上游原文给**
    （模型那一份会把 Claude Code 的工具名换掉，见 ``services/skill_tools.py``）——
    用户要能对着上游核对"这个技能到底教了什么"。
    """
    record, body = services.skills.read(name, allow_discarded=True, translate_names=False)
    return SkillDetailOut(
        **_out(record, _summaries(services).get(record.name, "")).model_dump(), body=body
    )


# --------------------------------------------------------------------- 市场
#
# 市场是这一层风险最高的入口：它把"从别处拿来的文本"直接放进提示词。
# 所以安装**要管理员**，而且**装之前先扫描**（命中注入特征当场拒绝并删掉半成品）。
# 扫描/解压的防护在 services/skill_market.py，这里只做协议层。


@router.post("/market", response_model=SkillMarketOut, summary="浏览一个源的技能索引")
def browse_market(
    payload: SkillMarketIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillMarketOut:
    """`source` 可以是目录、``catalog.json``，或它们的 http(s)/file URL。

    **没有索引文件不算错**：那表示这个源没有可浏览的清单，用户仍可按名字直接装。
    """
    entries = services.skill_market.catalog(payload.source)
    return SkillMarketOut(
        source=payload.source,
        items=[
            SkillMarketEntryOut(
                name=item.name,
                description=item.description,
                source=item.source,
                installed=item.installed,
            )
            for item in entries
        ],
    )


@router.get("/market/installed", response_model=SkillInstalledOut, summary="已从市场装的技能")
def installed_skills(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> SkillInstalledOut:
    """``技能名 → 来源``。**回答"这玩意儿哪来的"**，也是卸载时唯一允许删的集合。"""
    items = services.skill_market.installed()
    return SkillInstalledOut(items=items, total=len(items))


@router.post("/market/install", response_model=SkillOut, summary="安装一个技能")
def install_skill(
    payload: SkillInstallIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillOut:
    """装到 ``data/skills/<名字>/``。

    **装之前先扫描**：命中注入特征就拒绝安装并清掉已经落地的目录——
    与"扫描磁盘上已有的技能"不同，安装是**主动引入**，所以处置是拒绝而不是标注。
    """
    # 装完**回读注册表**（而不是自己拼响应）：名称与描述以磁盘上那份为准，
    # 这个接口要能验证"装完之后它真的被扫进来了"。
    services.skill_market.install(payload.name, source=payload.source, catalog=payload.catalog)
    record = services.skills.get(payload.name)
    return _out(record)


@router.delete(
    "/market/installed/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="卸载一个技能",
)
def uninstall_skill(
    name: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> None:
    """**只卸市场装的**：仓库自带的技能不在清单里，所以删不掉——
    否则一次误操作就能改掉"我们审过的那个版本"。"""
    services.skill_market.uninstall(name)


# --------------------------------------------------------------------- 线上源
#
# 与上面那组的分工：那组是"从一个地址装"，这组是"从一个仓库浏览着挑"。
# 出站全部走服务端（GitHub 匿名配额 60 次/小时，前端直连一分钟就能打爆），
# 装之前**先把文件清单给出去**（`/inspect`），用户确认了才 `/install-source`。
# 这里也**只给管理员**：装技能 = 往提示词里加东西，与 MCP 那一侧同一档。


@router.get("/market/sources", response_model=SkillSourceListOut, summary="技能源列表")
def list_sources(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> SkillSourceListOut:
    """内置源 + 用户自己加的。**读得到的都能看**：清单本身不是敏感信息，
    而管理员要在这里决定启用哪些（见下一个端点）。"""
    return SkillSourceListOut(
        items=[SkillSourceOut(**item.to_dict()) for item in services.skill_sources.list_sources()]
    )


@router.post("/market/sources", response_model=SkillSourceOut, summary="添加一个技能源")
def add_source(
    payload: SkillSourceIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillSourceOut:
    """填 ``owner/repo``，或 GitHub 上那个仓库（含子目录）的 URL。

    **任意合规仓库都能加**：我们扫的是 ``**/SKILL.md``，不是谁家的市场清单——
    而市场清单这件事各家都不一样，也压根没有统一协议（调研 §0.1）。
    """
    source = services.skill_sources.add_source(payload.repo)
    return SkillSourceOut(**source.to_dict())


@router.patch(
    "/market/sources/{source_id}", response_model=SkillSourceOut, summary="启用 / 停用一个源"
)
def patch_source(
    source_id: str,
    payload: SkillSourcePatchIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillSourceOut:
    source = services.skill_sources.set_enabled(source_id, payload.enabled)
    return SkillSourceOut(**source.to_dict())


@router.delete(
    "/market/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一个自定义源",
)
def delete_source(
    source_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> None:
    """**内置源删不掉，只能停用**：它是一份我们审过的清单。"""
    services.skill_sources.remove_source(source_id)


@router.post("/market/browse", response_model=SkillBrowseOut, summary="浏览一个源里的技能")
def browse_source(
    payload: SkillBrowseIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> SkillBrowseOut:
    """递归扫这个仓库里的 ``**/SKILL.md``，只取 name 与 description。

    **正文与附件不在这里取**：那是"点了某个技能之后"的事——渐进式加载的天然对应，
    也省得一次浏览就把几十个技能的正文全拉下来（调研 §1）。
    """
    installed = [item.name for item in services.skills.list()]
    source, items = services.skill_sources.browse(
        payload.source_id, refresh=payload.refresh, installed=installed
    )
    return SkillBrowseOut(
        source=SkillSourceOut(**source.to_dict()),
        items=[MarketSkillOut(**item.to_dict()) for item in items],
        cached=not payload.refresh,
    )


@router.post("/market/inspect", response_model=SkillBundleOut, summary="看一个技能的文件清单")
def inspect_skill(
    payload: SkillInspectIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillBundleOut:
    """**装之前的那一步**：文件清单（含大小与"是不是可执行的代码"）、体积、commit SHA。

    调研 §4.6 把这一步列为"必须做"：技能目录里的 ``scripts/`` 是会被 agent
    执行的代码，而用户在看到清单之前没有别的地方能判断"我到底在装什么"。
    """
    bundle = services.skill_sources.inspect(payload.source_id, payload.path)
    return SkillBundleOut(**bundle.to_dict())


@router.post("/market/upload", response_model=SkillOut, summary="上传一个技能（文件夹或压缩包）")
async def upload_skill(
    files: Annotated[list[UploadFile], File(description="技能的文件；压缩包就传一个 .zip")],
    paths: Annotated[
        str,
        Form(description="与 files 一一对应的相对路径（JSON 数组）。传 .zip 时忽略"),
    ] = "[]",
    services: Services = Depends(get_services),  # type: ignore[assignment]
    caller: Caller = Depends(require_admin),  # type: ignore[assignment]
) -> SkillOut:
    """**从本机添加技能**：选一个文件夹，或选一个 ``.zip``。

    两件事与市场那条路完全一致（写盘、扫描、拒绝覆盖），因为落到最后都是同一个
    ``install_files``：**只有一处写入实现**。

    文件夹那条路用的是浏览器给的相对路径（``webkitRelativePath``）——
    它与用户磁盘上的目录结构一致，所以顶层的 ``my-skill/`` 会被去掉，
    技能名取 ``SKILL.md`` 里的 ``name``。
    """
    if not files:
        raise InvalidRequestError("没有选中任何文件")
    origin = f"upload:{files[0].filename or 'skill'}"
    if len(files) == 1 and (files[0].filename or "").casefold().endswith(".zip"):
        blob = await files[0].read()
        if len(blob) > MAX_UNPACKED_BYTES:
            raise InvalidRequestError(
                f"压缩包太大（{len(blob) // 1024} KB，上限 {MAX_UNPACKED_BYTES // 1024} KB）"
            )
        name = services.skill_market.install_archive(blob, origin=origin)
    else:
        wanted = _upload_paths(paths, len(files))
        uploads = [
            (wanted[index] or (item.filename or f"file{index}"), await item.read())
            for index, item in enumerate(files)
        ]
        name = services.skill_market.install_uploads(uploads, origin=origin)
    return _out(services.skills.get(name), _summaries(services).get(name, ""))


def _upload_paths(raw: str, count: int) -> list[str]:
    """解析与文件一一对应的相对路径数组（前端传 JSON）。

    解析不出来就**返回空串数组**（退回用 ``filename``）：路径只是用来还原目录结构的，
    而这份表单是浏览器自己生成的——为它 500 掉一次上传不值得。
    """
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return [""] * count
    if not isinstance(parsed, list):
        return [""] * count
    out = [str(item) for item in parsed if isinstance(item, str)]
    return (out + [""] * count)[:count]


@router.post("/market/install-source", response_model=SkillOut, summary="从线上源安装一个技能")
def install_from_source(
    payload: SkillSourceInstallIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_admin)],
) -> SkillOut:
    """按 ``inspect`` 给出的那个 commit SHA 取文件并安装。

    **两步之间用同一个 SHA**：分支会在"用户点安装"与"我们下载"之间变，
    而用户确认的是他看到的那个版本。装完记进清单（来源、SHA、逐文件 hash），
    于是"这份技能是哪个版本、有没有被改过"永远答得出来。
    """
    bundle = services.skill_sources.inspect(payload.source_id, payload.path)
    files = services.skill_sources.download(bundle)
    services.skill_market.install_files(
        bundle.name,
        files,
        origin=f"github:{bundle.repo}@{bundle.sha}#{bundle.path}",
        lock={
            "repo": bundle.repo,
            "sha": bundle.sha,
            "path": bundle.path,
            "source_id": bundle.source_id,
            # 中文简介跟着一起记：装完之后能力页上那一行还得是中文
            "summary": bundle.summary,
        },
    )
    record = services.skills.get(bundle.name)
    return _out(record, bundle.summary)
