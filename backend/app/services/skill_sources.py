"""技能源：从线上仓库里**浏览**技能（v0.27，调研见《技能仓库与技能市场调研-v0.1》）。

与 ``services/skill_market.py`` 的分工：那边管"装到磁盘上"（写入、扫描、锁、卸载），
这边只管"从哪儿看、有哪些、拉下来"。**两边不互相引用**——组合发生在 API 层
（先 ``inspect`` 拿到清单与 SHA，用户确认后 ``download``，最后交给市场 ``install_files``）。

## 三件从调研里抄来的事

1. **扫描 ``**/SKILL.md``，不扫"市场清单"**。技能格式已经收敛（`SKILL.md` + frontmatter），
   但**没有任何厂商提供 registry 协议**，而且各家的目录约定都不一样（Anthropic 在
   ``skills/``、Microsoft 在 ``.github/skills/``、Google 是多级嵌套）。
   所以"一个源"就定义为"一个 GitHub 仓库"，浏览 = 递归拉一次文件树、过滤出所有
   ``SKILL.md``。**这带来一个免费的好处：任意合规仓库天然可用**（用户粘一个
   ``owner/repo`` 就能看），不必等谁去做适配。
2. **只解析 frontmatter 的 name / description**：那是模型判断"何时该用"的唯一依据，
   也是列表要显示的东西。正文与附件**等用户点了某个技能再拉**（渐进式加载的天然对应）。
3. **一次树请求 + N 次 raw 请求**。GitHub API 未认证只有 60 次/小时（实测被限过流），
   所以：树与仓库信息**带 TTL 落盘缓存**、只走 API；`SKILL.md` 正文走
   ``raw.githubusercontent.com``（**没有那个限流**）。

## 两条必须守住的边界

- **前端永远不直接打 GitHub**（60/h 一分钟就能打爆）。所有出站都在这里，带缓存。
- **装的时候按 40 位 commit SHA 取**，不按分支：分支会在你点"安装"和真正下载之间变。
  ``inspect`` 给出的 SHA 就是用户看到的那个版本，``download`` 用的是同一个。

## 未做的（写在这里，免得看起来像忘了）

``npx skills`` / ``giget``（要 Node，且行为不由我们控制）、skills.sh 的榜单接口
（**强制认证**，拿不到）、SkillsMP 全文检索（口径误导，见调研 §3.4）、
签名与评分（没有真市场，做了也没人验）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from app.core.exceptions import InvalidRequestError, NotFoundError, UpstreamError
from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.services.memory_files import parse_frontmatter
from app.services.skill_blurb import Translator
from app.services.skills import SKILL_FILE

__all__ = [
    "BUILTIN_SOURCES",
    "MarketSkill",
    "SkillBundle",
    "SkillFile",
    "SkillSource",
    "SkillSourceService",
]

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
"""GitHub REST。**只有这里会被限流**（未认证 60 次/小时/IP），所以调用点要少。"""

RAW_BASE = "https://raw.githubusercontent.com"
"""GitHub 自己的单文件地址。无 API 限流，但**在部分网络下会被重置**（见 ``_FILE_ROUTES``）。"""

_API_VERSION = "2022-11-28"
"""GitHub API 的版本头。写死是有意的：跟版本走会让行为随上游变。"""

CDN_BASE = "https://cdn.jsdelivr.net/gh"
"""jsDelivr 的 GitHub 镜像。**同样是按 ref 取单文件**，走 CDN、没有 API 限流。

调研 §4.2 把它列为备选，代价是"仓库超过 50MB 直接失败"——所以它是**先试的那条**，
失败就退到 ``raw``：两条都试，比只信一条稳（实测本机到 raw 的连接七到六十秒才断，
而 jsDelivr 八个文件并发 0.9 秒全成）。
"""

#: 浏览结果的缓存时长。调研建议"小时级"：技能仓库不是分钟级变化的东西，
#: 而每次浏览都要打 GitHub 的 API 配额。
CACHE_TTL_SECONDS = 6 * 3600

#: 单个源码文件的下载上限。技能是文本与少量资源，2MB 足够；
#: 上限的作用是挡住"某个仓库里塞了一个大文件"。
MAX_FILE_BYTES = 2 * 1024 * 1024

#: 一个技能全部文件加起来的上限（与市场那边解压后的上限同一个量级）。
MAX_BUNDLE_BYTES = 32 * 1024 * 1024

#: 一个技能最多多少个文件。
MAX_BUNDLE_FILES = 200

#: 并行取 frontmatter 的线程数。**只为省浏览时的墙钟**：19 个技能串行取
#: 就是 19 个 RTT，而它们互不依赖。
_PARALLEL = 8

#: 出站请求的重试次数（含首次）。见 ``_get`` 的说明：这个网络下连接会被重置。
_RETRIES = 2

#: 最多跟几次跳转。CDN 一般只跳一次，两次是留余量。
_REDIRECTS = 2

#: 认得的跳转状态码。
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: 认得的三个主机（``_FILE_ROUTES`` 里那几条）。留在这里是给"这条 URL 是不是我们的"
#: 那类判断用的（跨主机跳转**不跟**，见 ``_get``）。
_ALLOWED_HOSTS = frozenset({"cdn.jsdelivr.net", "raw.githubusercontent.com", "api.github.com"})

#: 取单个文件的几条路，**按顺序试，第一条成了就不试后面的**（第二条是
#: "这个地址用哪套请求头"）。
#:
#: 实测（2026-09-19，本机）三条路各自的样子：
#:
#: - ``raw``：多数请求被重置或七八十秒超时（重试也可能再撞上）；
#: - jsDelivr：走 CDN 很快，**但它对没缓存的文件会 301 回 raw**（实测 4 个文件里
#:   3 个如此）——所以"CDN 优先"在这张网里常常等于又走一遍那条不通的路；
#: - **GitHub 的 contents 接口**（``Accept: application/vnd.github.raw`` 直接回字节）：
#:   实测 8 个文件并发 0.8 秒全成，是这里唯一稳定的那条。
#:
#: 代价写清楚：contents 接口**走 API 配额**（匿名 60 次/小时/IP），一次安装有几个
#: 文件就花几次。所以它排在 CDN 后面（能省则省），而撞上配额时的报错会告诉用户
#: 配 ``KYLAB_GITHUB_TOKEN``（提到 5000 次/小时）。
_FILE_ROUTES: tuple[tuple[str, str], ...] = (
    (f"{CDN_BASE}/{{repo}}@{{sha}}/{{path}}", "cdn"),
    (f"{GITHUB_API}/repos/{{repo}}/contents/{{path}}?ref={{sha}}", "api-raw"),
    (f"{RAW_BASE}/{{repo}}/{{sha}}/{{path}}", "raw"),
)

#: 文件分类。**给界面用的**，不是给磁盘用的：装之前要把清单摊开给用户看，
#: 而"哪些是会执行的代码、哪些是纯文本、哪些是资源"是他在那一步唯一能判断的东西。
_DOC_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml", ".ini", ".xml", ".html", ".css"}
)
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".sql",
    }
)
_ASSET_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf"}
)


@dataclass(frozen=True, slots=True)
class SkillSource:
    """一个可以浏览的源（就是"一个 GitHub 仓库"）。

    ``ref`` 留空表示用仓库的默认分支——**不写死 main**：不少仓库还在用 master，
    而这个字段存在的意义是"想钉某个分支/标签时能钉"。
    """

    id: str
    name: str
    repo: str
    ref: str = ""
    subpath: str = ""
    builtin: bool = True
    enabled: bool = True
    why: str = ""
    """为什么内置它（界面上给用户看的一句话）。空 = 用户自己加的源。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "repo": self.repo,
            "ref": self.ref,
            "subpath": self.subpath,
            "builtin": self.builtin,
            "enabled": self.enabled,
            "why": self.why,
        }


#: 内置源清单，抄自《技能仓库与技能市场调研-v0.1》§5。
#:
#: **默认启用的是前 5 个**（调研的"默认启用"档）：Anthropic 的官方示例、
#: Vercel 的 `find-skills`、全生态 star 最高的 `obra/superpowers`、
#: Google 官方、以及国产第一的 MiniMax。其余列出来但默认关着——
#: 一屏二十个源对"我想找个技能"没有帮助，而用户可以自己打开。
#:
#: ``repo`` 是 ``owner/repo``，与界面上的显示名分开：名字会改，仓库地址不会。
BUILTIN_SOURCES: tuple[SkillSource, ...] = (
    SkillSource(
        id="anthropic",
        name="Anthropic 官方技能",
        repo="anthropics/skills",
        why="格式的参考实现，含 skill-creator 与文档四件套",
    ),
    SkillSource(
        id="vercel",
        name="Vercel 官方技能",
        repo="vercel-labs/agent-skills",
        why="含 find-skills（全站安装量第一）",
    ),
    SkillSource(
        id="superpowers",
        name="Superpowers",
        repo="obra/superpowers",
        why="社区 star 最高，方法论型技能（TDD、系统化调试）",
    ),
    SkillSource(
        id="google",
        name="Google 官方技能",
        repo="google/skills",
        why="云 / 广告 / 分析方向，多级目录",
    ),
    SkillSource(
        id="minimax",
        name="MiniMax 官方技能",
        repo="MiniMax-AI/skills",
        why="国产官方第一，前端与多媒体",
    ),
    SkillSource(
        id="github", name="GitHub Copilot 集合", repo="github/awesome-copilot", enabled=False
    ),
    SkillSource(id="microsoft", name="Microsoft 官方技能", repo="microsoft/skills", enabled=False),
    SkillSource(
        id="cloudflare", name="Cloudflare 官方技能", repo="cloudflare/skills", enabled=False
    ),
    SkillSource(
        id="anthropic-plugins",
        name="Anthropic 插件目录",
        repo="anthropics/claude-plugins-official",
        enabled=False,
    ),
    SkillSource(id="glm", name="智谱 GLM 技能", repo="zai-org/GLM-skills", enabled=False),
    SkillSource(
        id="modelscope", name="ModelScope 技能", repo="modelscope/modelscope-skills", enabled=False
    ),
    SkillSource(
        id="tech-leads",
        name="tech-leads-club 注册表",
        repo="tech-leads-club/agent-skills",
        enabled=False,
    ),
)


@dataclass(frozen=True, slots=True)
class SkillFile:
    """技能目录里的一个文件。``kind`` 是给界面看的：code 要显眼地标出来。"""

    path: str
    size: int
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class MarketSkill:
    """浏览结果里的一条（还没装）。"""

    name: str
    description: str
    path: str
    """技能目录在仓库里的相对路径（``skills/pdf``）。安装时按它取文件。"""

    source_id: str
    repo: str
    installed: bool = False
    summary: str = ""
    """**中文简介**（v0.28）。空 = 没翻成或不需要翻，界面退回 ``description``。

    技能生态里绝大多数描述是英文，而这一页是给中文用户看的（见
    ``services/skill_blurb.py``）。**只影响界面**：模型的触发文本仍是原描述。
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "summary": self.summary,
            "path": self.path,
            "source_id": self.source_id,
            "repo": self.repo,
            "installed": self.installed,
        }


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """**装之前**摊给用户看的那一份：文件清单 + 版本 + 体积。

    调研 §4.6 的安全要求就是它：落盘前完整展示文件清单（含大小与是否可执行），
    对 ``scripts/`` 明确告警。所以这里给的是"清单 + 一个 40 位 SHA"，
    而不是一个"点一下就装"的按钮。
    """

    source_id: str
    repo: str
    sha: str
    path: str
    name: str
    description: str
    ref: str = ""
    license: str = ""
    files: tuple[SkillFile, ...] = ()
    total_bytes: int = 0
    truncated: bool = False
    summary: str = ""
    """中文简介（v0.28）。跟着安装一起记进清单，装完在能力页上还能看到。"""
    """仓库太大、GitHub 的文件树被截断（``truncated``）——**如实标出来**：
    这种情况下清单可能不全，用户该知道。"""

    @property
    def code_files(self) -> tuple[SkillFile, ...]:
        return tuple(item for item in self.files if item.kind == "code")

    @property
    def code_count(self) -> int:
        """有几个文件是**可执行的代码**。装之前那一屏要显眼地报这个数。"""
        return len(self.code_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "repo": self.repo,
            "sha": self.sha,
            "path": self.path,
            "name": self.name,
            "description": self.description,
            "ref": self.ref,
            "license": self.license,
            "files": [item.to_dict() for item in self.files],
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "code_count": len(self.code_files),
        }


class SkillSourceService:
    """内置源 + 自定义源 + 从 GitHub 浏览它们。

    **缓存落盘**（``data/skill_cache/<源 id>.json``）而不是只放内存：GitHub 的
    未认证配额是 60 次/小时/IP，进程重启就把配额清零的做法等于没有缓存。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        token: str = "",
        client: httpx.Client | None = None,
        ttl: int = CACHE_TTL_SECONDS,
        translator: Translator | None = None,
    ) -> None:
        self._data_dir = data_dir
        #: 可选的 GitHub token（``KYLAB_GITHUB_TOKEN``）。给了就从 60 次/小时
        #: 变成 5000 次/小时——**只读公开仓库时它是可选的**，所以默认空着。
        self._token = (token or "").strip()
        self._client = client
        self._ttl = max(60, ttl)
        #: 英文描述 → 中文简介（v0.28，见 ``services/skill_blurb.py``）。
        #: 不给就不翻，界面显示原描述——**中文化是增强，不是依赖**。
        self._translator = translator

    def use_translator(self, translator: Translator | None) -> None:
        """接上"英文描述 → 中文简介"的实现（v0.28）。

        组合根在 ``ChatService`` 建好之后调它（那时才拿得到模型）。**可以不给**：
        不给就照原样显示英文描述，市场功能一件不少。
        """
        self._translator = translator

    # ------------------------------------------------------------------ 源

    @property
    def cache_dir(self) -> Path:
        return self._data_dir / "skill_cache"

    @property
    def sources_file(self) -> Path:
        return self._data_dir / "skill_sources.json"

    def list_sources(self) -> list[SkillSource]:
        """内置 + 用户加的。用户对内置源的启停**覆盖**在 ``skill_sources.json`` 里。"""
        overrides = self._overrides()
        out: list[SkillSource] = []
        for item in BUILTIN_SOURCES:
            enabled = overrides.get(item.id, {}).get("enabled")
            out.append(
                item
                if enabled is None
                else SkillSource(**{**item.to_dict(), "enabled": bool(enabled)})
            )
        for source_id, data in overrides.items():
            if data.get("builtin", False):
                continue
            repo = str(data.get("repo") or "").strip()
            if not repo:
                continue
            out.append(
                SkillSource(
                    id=source_id,
                    name=str(data.get("name") or repo),
                    repo=repo,
                    ref=str(data.get("ref") or ""),
                    subpath=str(data.get("subpath") or ""),
                    builtin=False,
                    enabled=bool(data.get("enabled", True)),
                )
            )
        return out

    def get_source(self, source_id: str) -> SkillSource:
        for item in self.list_sources():
            if item.id == source_id:
                return item
        raise NotFoundError(f"没有这个技能源：{source_id}")

    def add_source(self, text: str) -> SkillSource:
        """加一个自定义源。``text`` 可以是 ``owner/repo``、仓库 URL，
        或带子目录的 URL（``…/tree/main/skills``）——**扫描是递归的**，
        所以子目录只是"少看几眼"，不是必须的。
        """
        repo, ref, subpath = parse_repo_reference(text)
        for item in self.list_sources():
            if item.repo.casefold() == repo.casefold():
                raise InvalidRequestError(f"这个仓库已经在列表里了：{item.name}（{item.repo}）")
        source_id = _source_id(repo)
        overrides = self._overrides()
        overrides[source_id] = {
            "repo": repo,
            "ref": ref,
            "subpath": subpath,
            "name": repo,
            "builtin": False,
        }
        self._write_overrides(overrides)
        logger.info("技能源已添加：%s（%s）", repo, source_id)
        return self.get_source(source_id)

    def remove_source(self, source_id: str) -> None:
        """删一个自定义源。**内置源删不掉**（但可以停用）——它是我们审过的清单。"""
        overrides = self._overrides()
        data = overrides.get(source_id)
        if data is None or data.get("builtin", False):
            if any(item.id == source_id for item in BUILTIN_SOURCES):
                raise InvalidRequestError(
                    "内置源不能删除，只能停用（关掉之后它就不出现在浏览列表里了）"
                )
            raise NotFoundError(f"没有这个自定义源：{source_id}")
        overrides.pop(source_id, None)
        self._write_overrides(overrides)
        self._cache_path(source_id).unlink(missing_ok=True)

    def set_enabled(self, source_id: str, enabled: bool) -> SkillSource:
        source = self.get_source(source_id)
        overrides = self._overrides()
        record = overrides.get(source_id) or {
            "repo": source.repo,
            "ref": source.ref,
            "subpath": source.subpath,
            "name": source.name,
            "builtin": source.builtin,
        }
        record["enabled"] = bool(enabled)
        overrides[source_id] = record
        self._write_overrides(overrides)
        return self.get_source(source_id)

    # ------------------------------------------------------------------ 浏览

    def browse(
        self, source_id: str, *, refresh: bool = False, installed: Sequence[str] = ()
    ) -> tuple[SkillSource, list[MarketSkill]]:
        """列出一个源里**全部**技能（只解析 name / description）。

        ``installed`` 是"本地已经有的技能名"（规范化后的），用来在列表上打"已装"标——
        界面据此把安装按钮换成别的样子，而不是让用户点进去才发现装过了。
        """
        source = self.get_source(source_id)
        cached = None if refresh else self._read_cache(source_id)
        if cached is None:
            cached = self._fetch_source(source)
            self._write_cache(source_id, cached)
        self._translate(source_id, cached)
        known = {name.casefold() for name in installed}
        items = [
            MarketSkill(
                name=entry["name"],
                description=entry.get("description", ""),
                path=entry["path"],
                source_id=source_id,
                repo=source.repo,
                installed=entry["name"].casefold() in known,
                summary=entry.get("summary", ""),
            )
            for entry in cached.get("skills", [])
        ]
        return source, items

    def _translate(self, source_id: str, cached: dict[str, Any]) -> None:
        """把还没翻过的英文描述翻成中文，**结果写回缓存**（v0.28）。

        写在缓存里而不是每次现翻：翻译要花一次模型调用，
        而缓存的有效期（6 小时）正好是"这份清单还有效"的期限。
        **翻失败就什么都不做**——界面退回英文原描述，市场照常能用。
        """
        if self._translator is None:
            return
        todo = [
            (str(entry.get("name") or ""), str(entry.get("description") or ""))
            for entry in cached.get("skills", [])
            if entry.get("description") and not entry.get("summary")
        ]
        if not todo:
            return
        try:
            got = self._translator(todo)
        except Exception:  # 翻译是增强：它坏了不该让"逛市场"这件事失败
            logger.warning("技能简介翻译失败（%s）", source_id, exc_info=True)
            return
        if not got:
            return
        for entry in cached.get("skills", []):
            summary = got.get(str(entry.get("name") or ""))
            if summary:
                entry["summary"] = summary
        self._write_cache(source_id, cached)
        logger.info("技能简介已生成：%s（%d 条）", source_id, len(got))

    def inspect(self, source_id: str, path: str) -> SkillBundle:
        """某个技能的**文件清单**（装之前给用户看的那一步）。

        用的是浏览时缓存下来的那份树（同一个 SHA），所以这一步通常不再打网络——
        前提是刚浏览过。缓存过期时会重新拉一次（并因此换一个 SHA，这没问题：
        用户看到的 SHA 与装下来的 SHA 始终是**同一次**读取的结果）。
        """
        source, cached = self._ensure(source_id)
        entry = self._entry(cached, path)
        files = tuple(
            SkillFile(path=item[0], size=int(item[1]), kind=_kind_of(item[0]))
            for item in entry.get("files", [])
        )
        return SkillBundle(
            source_id=source.id,
            repo=source.repo,
            sha=str(cached.get("sha") or ""),
            path=entry["path"],
            name=entry["name"],
            description=entry.get("description", ""),
            ref=str(cached.get("ref") or ""),
            license=str(cached.get("license") or ""),
            files=files,
            total_bytes=sum(item.size for item in files),
            truncated=bool(cached.get("truncated")),
            summary=str(entry.get("summary") or ""),
        )

    def download(self, bundle: SkillBundle) -> dict[str, bytes]:
        """按 bundle 里那个 SHA 取回全部文件，键是**相对技能目录**的路径。

        **一个都不能少**：少一个文件就是半成品（``references/x.md`` 缺了，
        技能正文里的引用会指向不存在的东西），所以任何一个文件取不到都整体失败，
        让调用方去决定"要不要退而求其次"——这里不替它决定。
        """
        if not bundle.sha:
            raise UpstreamError("这次浏览没有拿到 commit SHA，不能安装（见 browse 的说明）")
        names = [item.path for item in bundle.files]
        if not names:
            raise NotFoundError(f"「{bundle.name}」的清单是空的，没有可下载的文件")
        if len(names) > MAX_BUNDLE_FILES:
            raise InvalidRequestError(
                f"这个技能有 {len(names)} 个文件（上限 {MAX_BUNDLE_FILES}），不像一个技能包"
            )
        if bundle.total_bytes > MAX_BUNDLE_BYTES:
            raise InvalidRequestError(
                f"这个技能有 {bundle.total_bytes // 1024} KB（上限 {MAX_BUNDLE_BYTES // 1024} KB）"
            )

        # **并行取**：真实的技能包有几十个文件（pptx 那个 56 个），串行就是 56 个 RTT。
        # 实测（jsDelivr）串行 39s，并发 8 之后是个位数。取回来的顺序由 `names` 决定，
        # 与线程完成的先后无关。
        with ThreadPoolExecutor(max_workers=min(_PARALLEL, len(names))) as pool:
            blobs = list(
                pool.map(
                    lambda relative: self._raw(
                        bundle.repo, bundle.sha, f"{bundle.path}/{relative}"
                    ),
                    names,
                )
            )
        out = dict(zip(names, blobs, strict=True))
        total = sum(len(blob) for blob in out.values())
        if total > MAX_BUNDLE_BYTES:
            raise InvalidRequestError(f"下载的内容超过 {MAX_BUNDLE_BYTES // 1024} KB，已中止")
        return out

    def cached_sha(self, source_id: str) -> str:
        """缓存里那份树的 commit SHA（界面上"这是哪个版本"）。没缓存则空串。"""
        cached = self._read_cache(source_id)
        return str((cached or {}).get("sha") or "")

    # ------------------------------------------------------------------ GitHub

    def _ensure(self, source_id: str) -> tuple[SkillSource, dict[str, Any]]:
        source = self.get_source(source_id)
        cached = self._read_cache(source_id)
        if cached is None:
            cached = self._fetch_source(source)
            self._write_cache(source_id, cached)
        return source, cached

    def _fetch_source(self, source: SkillSource) -> dict[str, Any]:
        """打一次仓库信息 + **一次 commit** + 一次递归文件树 + N 次 raw 取 frontmatter。

        那次 commit 请求是必须的，而且**踩过坑**：文件树接口回的是 **tree 对象的
        SHA**，而 ``raw.githubusercontent.com`` 只认 commit-ish（分支、标签、commit SHA）
        ——拿 tree SHA 去取文件一律 404。所以这里显式把 ``ref`` 解析成 commit，
        再拿这个 commit 去取树：这样"用户看到的清单"与"装下来的文件"
        也确实是同一个 commit（见 ``inspect`` / ``download``）。
        """
        info = self._api_json(f"/repos/{source.repo}")
        default_branch = str(info.get("default_branch") or "main")
        ref = source.ref or default_branch
        head = self._api_json(f"/repos/{source.repo}/commits/{ref}")
        sha = str(head.get("sha") or "")
        if not sha:
            raise UpstreamError(f"没能解析出 {source.repo} 的 commit（{ref}）")
        tree = self._api_json(f"/repos/{source.repo}/git/trees/{sha}?recursive=1")
        entries = [item for item in (tree.get("tree") or []) if item.get("type") == "blob"]
        truncated = bool(tree.get("truncated"))

        skills: list[dict[str, Any]] = []
        for item in entries:
            path = str(item.get("path") or "")
            if not path.endswith(f"/{SKILL_FILE}") and path != SKILL_FILE:
                continue
            directory = path[: -len(f"/{SKILL_FILE}")] if "/" in path else ""
            if source.subpath and not (
                directory == source.subpath or directory.startswith(f"{source.subpath}/")
            ):
                continue
            files = _files_under(entries, directory)
            skills.append({"path": directory, "files": files, "size": 0})
        skills.sort(key=lambda item: item["path"])

        # frontmatter 并行取：它们互不依赖，而串行就是 N 个 RTT
        with ThreadPoolExecutor(max_workers=min(_PARALLEL, max(1, len(skills)))) as pool:
            metas = list(pool.map(lambda item: self._frontmatter(source.repo, sha, item), skills))
        for entry, meta in zip(skills, metas, strict=True):
            entry["name"] = meta.get("name") or Path(entry["path"]).name or source.repo
            entry["description"] = meta.get("description", "")
            entry["license"] = meta.get("license", "")

        return {
            "fetched_at": _now(),
            "repo": source.repo,
            "ref": ref,
            # **commit SHA**（不是 tree SHA）：装的时候要拿它去 raw 取文件，
            # 而那里只认 commit-ish（见 _fetch_source 的说明）
            "sha": sha,
            "tree_sha": str(tree.get("sha") or ""),
            "default_branch": default_branch,
            "stars": int(info.get("stargazers_count") or 0),
            "license": _license_name(info),
            "truncated": truncated,
            "skills": skills,
        }

    def _frontmatter(self, repo: str, sha: str, entry: dict[str, Any]) -> dict[str, str]:
        """取一个技能的 ``SKILL.md`` 并解析 frontmatter。

        **失败不抛**：一个技能读不到（网络抖一下、文件是非 UTF-8）不该让整次浏览失败——
        退回目录名 + 空描述，界面上那一行照样在，用户只是看不到它的说明。
        """
        path = f"{entry['path']}/{SKILL_FILE}" if entry["path"] else SKILL_FILE
        try:
            blob = self._raw(repo, sha, path)
        except (UpstreamError, InvalidRequestError, NotFoundError) as exc:
            # **一个技能读不到不该让整次浏览失败**：退回目录名 + 空描述，
            # 那一行照样在，用户只是看不到它的说明（也看得见"没有说明"）
            logger.info("取不到技能说明：%s（%s）", path, exc)
            return {}
        text = blob.decode("utf-8", errors="replace")
        meta, _body = parse_frontmatter(text)
        return {
            "name": str(meta.get("name") or "").strip(),
            "description": " ".join(str(meta.get("description") or "").split()),
            "license": str(meta.get("license") or "").strip(),
        }

    def _api_json(self, path: str) -> dict[str, Any]:
        url = f"{GITHUB_API}{path}"
        response = self._get(url, timeout=30, headers=self._headers())
        if response.status_code == 404:
            raise NotFoundError(f"这个仓库或分支不存在（也可能是私有仓库）：{path}")
        if response.status_code in (401, 403):
            # 限流与"没有权限"在这里同一个码。把剩余配额念出来，用户才知道是等一会儿
            # 还是去配 token（见 _headers 的说明）。
            remaining = response.headers.get("X-RateLimit-Remaining", "?")
            hint = (
                "GitHub 的匿名配额（60 次/小时）用完了，等一会儿再试，"
                "或设 KYLAB_GITHUB_TOKEN 提到 5000 次/小时"
                if remaining == "0"
                else "GitHub 拒绝了这个请求（可能是私有仓库，或需要 token）"
            )
            raise UpstreamError(f"{hint}（剩余配额：{remaining}）")
        if response.status_code >= 400:
            raise UpstreamError(f"GitHub 返回 {response.status_code}：{response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError(f"GitHub 返回的不是 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise UpstreamError("GitHub 返回的不是一个对象")
        return data

    def _raw(self, repo: str, sha: str, path: str) -> bytes:
        """按 SHA 取一个文件。**不用分支名**：分支会在两次请求之间变。

        取法见 ``_FILE_ROUTES``：几条路按顺序试，任一条成功就用它。
        """
        failure = ""
        for template, kind in _FILE_ROUTES:
            url = template.format(repo=repo, sha=sha, path=_quote_path(path))
            response = self._get(url, timeout=30, headers=self._headers(kind))
            if response.status_code == 200:
                blob = response.content
                if len(blob) > MAX_FILE_BYTES:
                    raise InvalidRequestError(
                        f"文件太大（{path}，{len(blob) // 1024} KB，"
                        f"上限 {MAX_FILE_BYTES // 1024} KB）"
                    )
                return blob
            if response.status_code in (401, 403) and _quota_left(response) == "0":
                # 配额用完就别再往下试了：下一条会更慢而不是更可能成功
                raise UpstreamError(
                    "GitHub 的匿名配额（60 次/小时）用完了——安装要逐个文件取，"
                    "花的次数比浏览多。等一会儿再试，或设 KYLAB_GITHUB_TOKEN"
                    "（提到 5000 次/小时）。"
                )
            # 404 可能是"真没有"，也可能是 jsDelivr 拒收这个大仓库、或者它 301 回 raw
            # ——所以接着试下一条，而不是当场判定"文件不存在"
            failure = f"HTTP {response.status_code}"
            logger.info("这条取法没成（%s）：%s", failure, url)
        raise NotFoundError(f"取不到仓库里的这个文件：{path}（{failure}）")

    def _get(self, url: str, *, timeout: float, headers: dict[str, str]) -> httpx.Response:
        """出站 GET，**带一次重试**。

        为什么需要它（不是洁癖）：实测本机到 ``raw.githubusercontent.com`` 的连接
        会被对端时不时重置（``RemoteProtocolError: Server disconnected``），
        一次浏览 20 个 SKILL.md 里会撞上两三个——而被重置的那几个会让对应技能
        **显示成"没有说明"**，看起来像仓库写得不全。重试一次之后整份清单是齐的。

        **只重试传输层**（连不上、被重置、读超时）：4xx/5xx 是对方给的答复，
        重试不会变好，而那些错误要原样往上走（限流要告诉用户去配 token）。

        **自己跟跳转，但只跟到同一个主机**（v0.27 补）。共享的 httpx 客户端
        **刻意不跟随重定向**（见 ``app/core/http.py``：联网抓取每一跳都要重新校验地址），
        于是 CDN 偶尔回的 301 会被我们当成失败。这里补上"同一个主机内跟一下"，
        因为那是最常见的一种（CDN 内部换节点），而且不用论证跳转目标安不安全。

        **跨主机的跳转不跟**，直接把它当这条取法失败：实测 jsDelivr 对没缓存的文件
        会 301 回 ``raw.githubusercontent.com``，而那张网里 raw 恰恰不通
        ——跟过去等于又卡一次。让它失败、由 ``_raw`` 换下一条路（contents 接口），
        比"跟着跳到一个可能更差的主机"可控得多。
        """
        current = url
        origin = (urlparse(url).hostname or "").casefold()
        for _ in range(_REDIRECTS + 1):
            last: Exception | None = None
            for attempt in range(_RETRIES):
                try:
                    response = self._http().get(current, headers=headers, timeout=timeout)
                    break
                except httpx.TransportError as exc:
                    last = exc
                    logger.info("出站请求失败（第 %d 次）：%s（%s）", attempt + 1, current, exc)
            else:
                raise UpstreamError(f"连不上 {current}：{last}")
            if response.status_code not in _REDIRECT_CODES:
                return response
            target = urljoin(current, response.headers.get("location") or "")
            if target == current:
                # 没有 Location（或它指回自己）：这不是"再试一次"，是这条取法不成了
                logger.info("这条取法回了跳转却没给去处，换下一条：%s", current)
                return response
            if (urlparse(target).hostname or "").casefold() != origin:
                logger.info("这条取法被跳到别的主机，换下一条：%s → %s", current, target)
                return response
            logger.info("同一个主机内跟一次跳转：%s → %s", current, target)
            current = target
        raise UpstreamError(f"跳转次数过多，已中止：{url}")

    def _headers(self, kind: str = "api") -> dict[str, str]:
        """请求头。**token 是可选的**：只看公开仓库时不需要，而要求用户先配一个
        token 才能浏览是最没必要的门槛。配了就用——它把配额从 60 提到 5000。

        ``kind`` 决定 Accept：``api`` 要 JSON，``api-raw`` 要字节，
        ``cdn``/``raw`` 是纯文件地址（多一个 Accept 反而会让某些 CDN 返回别的东西）。
        """
        if kind == "cdn":
            headers: dict[str, str] = {}
        elif kind == "raw":
            headers = {}
        elif kind == "api-raw":
            headers = {"Accept": "application/vnd.github.raw", "X-GitHub-Api-Version": _API_VERSION}
        else:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _http(self) -> httpx.Client:
        return self._client or shared_client()

    # ------------------------------------------------------------------ 缓存

    def _cache_path(self, source_id: str) -> Path:
        return self.cache_dir / f"{_slug(source_id)}.json"

    def _read_cache(self, source_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._cache_path(source_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        age = time.time() - float(data.get("fetched_ts") or 0)
        if age > self._ttl:
            return None
        return data

    def _write_cache(self, source_id: str, data: dict[str, Any]) -> None:
        payload = {**data, "fetched_ts": time.time()}
        path = self._cache_path(source_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # 缓存写不进去**不该让浏览失败**：它只是一次加速，用户要的是那份清单
            logger.warning("技能源缓存写不进去：%s", path, exc_info=True)

    def _overrides(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.sources_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_overrides(self, data: dict[str, dict[str, Any]]) -> None:
        self.sources_file.parent.mkdir(parents=True, exist_ok=True)
        self.sources_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    @staticmethod
    def _entry(cached: dict[str, Any], path: str) -> dict[str, Any]:
        wanted = (path or "").strip().strip("/")
        for entry in cached.get("skills", []):
            if entry.get("path") == wanted:
                return entry
        raise NotFoundError(f"这个源里没有这个技能：{path}")


# ---------------------------------------------------------------------- 工具


def parse_repo_reference(text: str) -> tuple[str, str, str]:
    """把用户粘进来的东西解析成 ``(owner/repo, ref, subpath)``。

    认这几种写法（都是用户手上真实会有的形态）：

    - ``anthropics/skills``
    - ``https://github.com/anthropics/skills``
    - ``https://github.com/anthropics/skills/tree/main/skills``（带分支与子目录）
    - 末尾带 ``.git`` 或 ``#ref``

    **不认"三段简写"**（``owner/repo/skills/pdf``）：调研里这一条本身是未确认项，
    而它和"带子目录的完整 URL"表达的是同一件事——少一种写法比认错一个仓库好。
    """
    raw = (text or "").strip()
    if not raw:
        raise InvalidRequestError("请填仓库地址，例如 anthropics/skills")
    ref = ""
    if "#" in raw:
        raw, ref = raw.split("#", 1)

    if "://" in raw:
        without_scheme = raw.split("://", 1)[1]
        host, _, rest = without_scheme.partition("/")
        if "github.com" not in host.casefold():
            raise InvalidRequestError("目前只认 github.com 的仓库")
        parts = [item for item in rest.split("/") if item]
    else:
        parts = [item for item in raw.split("/") if item]
        # 粘 URL 时忘了带协议（``github.com/owner/repo``）是常事：
        # 第一段看着像主机名（带点）就把它当主机名去掉，而不是当成 owner
        if parts and "." in parts[0] and "/" not in parts[0]:
            if "github.com" not in parts[0].casefold():
                raise InvalidRequestError("目前只认 github.com 的仓库")
            parts = parts[1:]

    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][: -len(".git")]
    if len(parts) < 2:
        raise InvalidRequestError(f"看不清这是哪个仓库：{text}（要 owner/repo 两段）")
    repo = f"{parts[0]}/{parts[1]}"

    subpath = ""
    rest_parts = parts[2:]
    if rest_parts:
        if rest_parts[0] in ("tree", "blob") and len(rest_parts) >= 2:
            ref = ref or rest_parts[1]
            subpath = "/".join(rest_parts[2:])
        else:
            # 不是 tree/xxx 就当成子目录（`github.com/o/r/skills/pdf` 这种）
            subpath = "/".join(rest_parts)
    return repo, ref, subpath.strip("/")


def _files_under(entries: list[dict[str, Any]], directory: str) -> list[list[Any]]:
    """某个技能目录下的全部文件：``[[相对路径, 字节数], ...]``，按路径排序。

    **路径只有相对的那一段**：装的时候目标目录就是技能根，
    留着 ``skills/pdf/`` 前缀会让解出来的目录多一层。
    """
    prefix = f"{directory}/" if directory else ""
    out: list[list[Any]] = []
    for item in entries:
        path = str(item.get("path") or "")
        if prefix and not path.startswith(prefix):
            continue
        if not prefix and "/" in path:
            # 仓库根就是一个技能：只取根下的文件，不递归进子目录
            continue
        relative = path[len(prefix) :]
        if not relative:
            continue
        out.append([relative, int(item.get("size") or 0)])
    out.sort(key=lambda pair: pair[0])
    return out


def _kind_of(path: str) -> str:
    """这一条是**什么性质**（浏览清单上给用户看的提示）。

    ``.rels`` 这类**点文件**要特别当心：``Path('.rels').suffix`` 是空串，
    于是它落进"不认识 → 当代码看"那一支，界面上把 OOXML 的关系表标成
    "可能会被执行的代码"。那两个口径（这里与 ``skill_market.suffix_of``）
    必须一致，否则同一份文件在两个页面上的说法不一样。
    """
    suffix = _suffix_of(path)
    if suffix in _CODE_SUFFIXES:
        return "code"
    if suffix in _ASSET_SUFFIXES:
        return "asset"
    if suffix in _DOC_SUFFIXES or suffix in _RELS_SUFFIXES:
        return "doc"
    # 不认识的（.jsonl、无后缀的脚本……）**当代码看**：宁可多提醒一次
    return "code"


def _suffix_of(path: str) -> str:
    """取扩展名；**点文件取整个名字**（``.rels`` 的 suffix 是空串，那是踩过的坑）。"""
    name = path.strip().rsplit("/", 1)[-1]
    if name.startswith(".") and name.count(".") == 1:
        return name.casefold()
    return Path(name).suffix.casefold()


#: OOXML 的关系表：纯 XML 文本，但点文件的"后缀"是它整个名字
#: （见 ``_suffix_of``）——不单列一份的话会被当代码（与 ``skill_market`` 的口径打架）。
_RELS_SUFFIXES = frozenset({".rels"})


def _quota_left(response: httpx.Response) -> str:
    """这个响应里声明的剩余配额（``?`` = 对方没说）。见 ``_raw`` 里的早退。"""
    return response.headers.get("X-RateLimit-Remaining", "?")


def _license_name(info: dict[str, Any]) -> str:
    license_info = info.get("license")
    if isinstance(license_info, dict):
        return str(license_info.get("spdx_id") or license_info.get("name") or "")
    return ""


def _quote_path(path: str) -> str:
    """URL 里只转义真正需要转义的字符：``/`` 必须留着（它是路径分隔符）。"""
    from urllib.parse import quote

    return quote(path, safe="/")


def _source_id(repo: str) -> str:
    """仓库 → 源 id。``owner/repo`` → ``owner-repo``（稳定、可读、能当文件名）。"""
    return _slug(repo.replace("/", "-"))


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip()).strip("-.")
    return cleaned.casefold() or "source"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")
