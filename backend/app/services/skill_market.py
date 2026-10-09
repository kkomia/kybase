"""技能市场（v0.16，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.1）。

技能是"会进模型上下文、并且能影响它怎么行动"的文本，所以**市场是这一层风险最高的入口**：
它把"从一个别处拿来的东西"直接放进提示词。QwenPaw 为此有 Skill Scanner 与
whitelist 机制。这里做三件必须做的事，其余的（签名、评分、举报）等有真市场再说：

1. **安装前扫描**（复用 ``services.skills`` 的扫描）：命中注入特征就**拒绝安装**
   （不是"装上去但标一下"——安装是主动引入，与扫描磁盘上已有的技能是两回事）；
2. **解压防护**：zip 里的路径要防"zip slip"（``../../x`` 写到仓库外）与
   符号链接（链到别处去），并限制条目数与总解压大小（zip 炸弹）；
3. **可追溯**：记下每个技能是从哪个源装的（写进 frontmatter 之外的一个小清单文件），
   这样"这玩意儿哪来的"永远答得出来，卸载也能只删该删的。

**源的形式**（都不需要新依赖）：

- 本地目录：``<catalog>/<技能名>/SKILL.md``——开发期最常用；
- 本地/远端 zip：``file://…/x.zip``、``https://…/x.zip``——分发形态；
- 索引（``catalog.json``）：
  ``{"skills": [{"name": "...", "description": "...", "source": "..."}]}``，
  本地路径或 http(s) URL 都行。

**没有真市场服务时它照样可用**：指向一个本地目录或一个 zip 就能装。
"市场"是这个服务的一种用法，不是它的前提。
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import logging
import shutil
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError, UpstreamError
from app.core.text_hygiene import recombine_surrogates, text_problem
from app.services.memory_files import parse_frontmatter
from app.services.skills import SKILL_FILE, SkillService, _scan, normalize_name

__all__ = ["CatalogEntry", "SkillMarketService"]

logger = logging.getLogger(__name__)

#: 索引文件的名字（放在源目录里）。空市场就是没有这个文件。
CATALOG_FILE = "catalog.json"

#: 装过哪些技能（写进数据目录）。**一行一个来源**，用来回答"这玩意儿哪来的"，
#: 也让卸载只删该删的（仓库自带的技能不在里面，所以删不掉）。
INSTALLED_FILE = "installed.json"

#: 单次下载的上限。市场拿来的东西**先假定它是坏的**：一个 500MB 的 zip
#: 会在解压前就把磁盘吃完。
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

#: 解压后的上限（zip 炸弹：一个几 KB 的 zip 能解出几个 GB）。
MAX_UNPACKED_BYTES = 32 * 1024 * 1024

#: 条目数上限（同上，防"十万个空文件"这类）。
MAX_ENTRIES = 500

#: 单个技能目录里允许的文件名。**白名单**，分三类，只有分类是给界面看的：
#: 落盘时三类都收，拒绝的是列表之外的东西（``.exe``、``.dll``、``.so``……）。
#:
#: **v0.27 起允许代码文件**（在此之前只收文本）。改的原因是真实的技能包里
#: ``scripts/`` 是常态：Anthropic 官方的 docx/pdf 技能就带着 Python 脚本，
#: 而"只收 Markdown"的规则等于把最有用的一批技能挡在门外（调研 §1）。
#: 换来的是三条更实在的防护：**装之前必须把文件清单摊给用户看**（含哪些是可执行代码）、
#: 落盘前扫描（命中注入特征当场拒绝）、**绝不自动执行**——脚本会不会被跑，
#: 由沙箱与工具策略决定，与"它在技能目录里"无关。
#:
#: **v0.1.1 起扩展名只是快路径，判"是不是二进制"看内容**（``is_allowed``）：
#: 白名单命中就收，命中不了的去读文件开头（``looks_like_text``），含 NUL 或不是
#: UTF-8 才拒。只看扩展名会漏掉一整类**真·文本**文件——OOXML 的
#: ``templates/minimal_xlsx/_rels/.rels`` 就是纯 XML，却因为后缀不在表里，
#: 让整个技能包被判成"带了二进制"而拒收（用户实测报的就是这条）。
TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".toml",
        ".ini",
        ".xml",
        ".html",
        ".css",
        # OOXML 的关系表：`_rels/.rels`、`xl/_rels/workbook.xml.rels` 都是纯 XML 文本，
        # 而 `.rels` 这种"整名就是扩展名"的点文件以前会被判成不明类型（见 suffix_of），
        # 于是带 Office 模板的技能包一律装不上（用户报的就是这条）。
        # 内容判定本来也会放过它，列进来是为了省一次读盘，也为了让 ``kind_of``
        # 把它归为文本而不是"可能会被执行的代码"。
        ".rels",
    }
)
CODE_SUFFIXES = frozenset(
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
ASSET_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf"}
)
#: 白名单 = **快路径**：命中它就不必读内容，直接收。资源类（图片、字体）本来
#: 就是二进制，内容判定会把它们全判成二进制，所以也必须在这里——"看内容"是
#: 给不认识的后缀兜底的，不是拿来否定已知类型的。
ALLOWED_SUFFIXES = TEXT_SUFFIXES | CODE_SUFFIXES | ASSET_SUFFIXES

#: 旧名字（迁移前的地方可能还在 import 它）。新代码用 ``ALLOWED_SUFFIXES``——
#: 它现在只是"快路径"的白名单，最终判据在 ``is_allowed``（看内容）。
_ALLOWED_SUFFIXES = ALLOWED_SUFFIXES

#: 判"是不是文本"时只看开头这么多字节。
#:
#: 二进制判据（NUL 字节、非法 UTF-8 序列）在文件头就会出现：PE/ELF 的魔数、
#: zip 的局部文件头、图片的 IHDR 都在前几十字节里；反过来，没有哪种文本格式是
#: "开头几 KB 干净、之后才蹦出 NUL"的。截断取样还避免为判一个文件把整份读进内存。
SNIFF_BYTES = 8 * 1024

#: 明确的可执行/二进制扩展名：**不看内容，直接拒**。
#:
#: 只看内容不够——两字节的 ``MZ``（PE 文件头）是合法 UTF-8、也不含 NUL，
#: 光按内容判会把一个 ``.exe`` 当成文本放行（用例 ``payload.exe = b"MZ"`` 钉的就是这条）。
#: 而这类文件在技能包里没有正当用途，所以用扩展名硬拒，与内容判定叠加。
BINARY_SUFFIXES = frozenset(
    {
        # 可执行文件、动态库、安装包
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".com",
        ".msi",
        ".scr",
        ".sys",
        # 目标码与静态库
        ".bin",
        ".o",
        ".obj",
        ".a",
        ".lib",
        # 其它运行时产物（字节码、容器、原生扩展）
        ".class",
        ".jar",
        ".pyc",
        ".wasm",
        ".node",
    }
)


def suffix_of(path: str) -> str:
    """取一个路径的后缀（小写、带点），**点文件取整名**。

    `Path(".rels").suffix` 是**空串**——Python 把开头的点当成"隐藏文件"，
    而不是扩展名。技能包里这种文件是真实存在的：OOXML 的 `_rels/.rels`
    就是纯文本 XML，不认它就会把好好的技能包判成"带了二进制"而整包拒收
    （v0.1.0 部署体验里报的那条）。
    """
    name = Path(path).name
    suffix = Path(name).suffix
    if suffix:
        return suffix.casefold()
    return name.casefold() if name.startswith(".") else ""


def looks_like_text(blob: bytes) -> bool:
    """内容是不是文本：**含 NUL、或开头无法按 UTF-8 解码的，算二进制**。

    判据与 ``app.parsers.probe._printable_ratio`` 的第一层一致（能 UTF-8 解码
    且不含 NUL 就是文本），但不做它的"可打印比例"兜底：技能包要回答的是
    "这堆字节能不能当文本落盘、进模型上下文"，宁可少收也不放二进制进来。
    """
    prefix = blob[:SNIFF_BYTES]
    if b"\x00" in prefix:
        return False
    try:
        # 增量解码器：取样窗口可能把一个多字节字符劈成两半，它会把没读完的那截
        # 留在缓冲里，而不是当成非法序列（用严格 decode 会把好端端的 UTF-8 判成二进制）
        codecs.getincrementaldecoder("utf-8")().decode(prefix, final=False)
    except UnicodeDecodeError:
        return False
    return True


def is_allowed(path: str, head: bytes) -> bool:
    """这个文件收不收。**三条安装路径（上传 / 本地目录 / zip）共用这一处**：
    分开写迟早会漂移，而漂移的那一处正是"哪条路能塞进二进制"。

    ``head`` 只需是文件开头的一段（调用方给 ``SNIFF_BYTES`` 即可）。
    """
    suffix = suffix_of(path)
    if suffix in BINARY_SUFFIXES:
        # 硬拒：``MZ`` 这种文件头是合法 UTF-8，只看内容会漏
        return False
    if suffix in ALLOWED_SUFFIXES:
        return True
    return looks_like_text(head)


def kind_of(path: str) -> str:
    """文件 → ``doc`` / ``code`` / ``asset``。**不认识的按 code 处理**：
    装之前那一屏里，"这东西可能会被执行"多提醒一次，比少提醒一次便宜。
    """
    suffix = suffix_of(path)
    if suffix in TEXT_SUFFIXES:
        return "doc"
    if suffix in ASSET_SUFFIXES:
        return "asset"
    return "code"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """索引里的一条。``source`` 是它的取用地址（相对目录或 URL）。"""

    name: str
    description: str = ""
    source: str = ""
    installed: bool = False


class SkillMarketService:
    """技能的安装 / 卸载 / 索引。

    **只写 ``data/skills/``**：仓库自带的 ``skills/`` 是随代码发布的，
    市场动不了它（``uninstall`` 也删不掉它）。这条边界很重要——
    否则一次误操作就能改掉"我们审过的那个版本"。
    """

    def __init__(self, data_dir: Path, skills: SkillService) -> None:
        self._data_dir = data_dir
        self._skills = skills

    @property
    def install_root(self) -> Path:
        return self._data_dir / "skills"

    @property
    def installed_index(self) -> Path:
        return self._data_dir / INSTALLED_FILE

    # ------------------------------------------------------------------ 索引

    def catalog(self, source: str) -> list[CatalogEntry]:
        """读一个源的索引。

        ``source`` 可以是目录（找里面的 ``catalog.json``），也可以直接是
        那个 json 文件/URL。**没有索引文件不算错**——那表示"这个源没有可浏览的清单"，
        此时用户仍然可以按名字直接装（``install(name, source=<目录>``）。
        """
        try:
            raw, origin = self._fetch(source)
        except NotFoundError:
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidRequestError(f"源的索引不是合法 JSON（{origin}）：{exc}") from exc

        items = data.get("skills") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise InvalidRequestError(f"索引格式不对（{origin}）：应当是 {{'skills': [...]}}")

        installed = {record.slug for record in self._skills.list() if record.source == "user"}
        out: list[CatalogEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            out.append(
                CatalogEntry(
                    name=name,
                    description=str(item.get("description") or ""),
                    source=str(item.get("source") or name),
                    installed=normalize_name(name) in installed,
                )
            )
        return out

    def installed(self) -> dict[str, str]:
        """已装清单：``技能名 → 来源``。

        兼容两代格式：v0.27 起每条是一个**记录**（来源、仓库、commit SHA、逐文件 hash），
        而在此之前只是一行来源字符串。旧记录照样读得出来——它唯一的作用是
        "知道这玩意儿哪来的"，为此让用户的既有安装失效不值得。
        """
        return {name: str(record.get("origin") or "") for name, record in self._records().items()}

    def installed_records(self) -> dict[str, dict[str, Any]]:
        """已装清单（详细版）：界面上要显示"来自哪个仓库的哪个版本"。"""
        return self._records()

    # ------------------------------------------------------------------ 安装

    def install_files(
        self,
        name: str,
        files: Mapping[str, bytes],
        *,
        origin: str,
        lock: Mapping[str, Any] | None = None,
    ) -> str:
        """装一份**已经取到手的文件集**（GitHub 源走这条路），返回落到的目录。

        与 ``install`` 的分工：那个负责"从源里找出来"，这个负责"写下去"。
        两份写入路径共用同一套防护（白名单、大小、扫描、失败清干净），
        只是取文件的动作在外面做完了——**这样"浏览 → 看清单 → 下载 → 落盘"
        四步里，中间那两步（用户确认）才有地方插进来**。

        ``lock`` 是记进清单的版本信息（仓库、SHA、来源页面），装完之后
        "这份技能是从哪个 commit 来的"永远答得出来。
        """
        clean = (name or "").strip()
        if not clean:
            raise InvalidRequestError("缺少参数：name")
        target = self.install_root / _safe_dirname(clean)
        if target.exists():
            raise ConflictError(f"已经装过「{clean}」了。要更新请先卸载，或换个名字装成两份。")
        if SKILL_FILE not in files:
            raise InvalidRequestError(f"这不是一个技能：里面没有 {SKILL_FILE}")
        # **装这一侧也要挡一次"过不了提示词的字符"**（D16 P0，2026-09-29）：
        # 读那一侧（`services/skills.py` 的扫描与读取）已经有"合并成对代理项 + 非法就丢弃"，
        # 但装的时候就该让调用方知道——不然它会以一个"装上了却读不出来"的样子留在库里，
        # 用户看到的是"装成功了但技能不出现"（比直接报错更难查）。判据与读侧**同一套**
        # （`app/core/text_hygiene.py`）：**先合并**成对代理项（`"\ud83e\udd16"` 那种转义
        # 写坏是能救的，救完就放行），合并后仍非法的（真孤立代理项 / 控制字符 / 名字带换行）
        # 才拒装。
        problem = _prompt_text_problem(files[SKILL_FILE])
        if problem:
            raise InvalidRequestError(
                f"这个技能里有进不了提示词的字符：{problem}"
                "（技能正文会被拼进模型请求，一个不可编码的字符会让整句对话失败）"
            )

        try:
            total = 0
            for relative, blob in files.items():
                safe = _safe_relative(relative)
                if safe is None:
                    raise InvalidRequestError(f"文件路径越界，已拒绝：{relative}")
                # 取到手的字节就在眼前，判定直接看内容（扩展名只当快路径）
                if not is_allowed(safe, blob):
                    raise InvalidRequestError(
                        f"技能里不收这类文件（{safe}）：可以是文本、脚本与资源，但不带二进制"
                    )
                total += len(blob)
                if total > MAX_UNPACKED_BYTES:
                    raise InvalidRequestError(f"技能内容超过 {MAX_UNPACKED_BYTES} 字节")
                destination = target / safe
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(blob)

            # 与另外两条安装路径同一个口径：装之前扫一遍，命中当场拒绝。
            flags = _scan(self._read_skill_text(target))
            if flags:
                raise InvalidRequestError(
                    f"「{clean}」没有通过安全检查，已拒绝安装：{'；'.join(flags)}"
                )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

        self._remember(
            clean,
            origin,
            extra={**(dict(lock or {})), "files": _digests(target)},
        )
        logger.info("技能已安装：%s ← %s", clean, origin)
        return str(target)

    def install(self, name: str, *, source: str, catalog: str = "") -> str:
        """装一个技能，返回它落到的目录（绝对路径）。

        ``source`` 三形态都认，**按顺序判断**：先看是不是 URL，再看是不是 zip，
        最后当目录处理。判断顺序不能反：Windows 上 ``C:/x.zip`` 既像盘符路径
        又像 URL，先判 URL 会把它当 http 去请求。

        读出来的东西一律交给 ``install_files`` 落盘——**写入只有一处实现**
        （白名单、扫描、失败清干净），三条安装路径（目录 / zip / 上传）共用它。
        """
        clean = (name or "").strip()
        if not clean:
            raise InvalidRequestError("缺少参数：name")
        payload, origin = self._resolve_source(name, source=source, catalog=catalog)
        if payload is None:
            raise NotFoundError(f"源里没有这个技能：{clean}（来自 {origin}）")
        raw_name, blob = payload
        # **按类型分派**，不能按"有没有 is_dir"猜：zip 形态的载荷是 ``bytes``，
        # 而 bytes 没有 is_dir（写这段时就是这么炸的）。目录走拷贝、字节走解压。
        files = self._read_dir(blob) if isinstance(blob, Path) else self._read_zip(blob)
        return self.install_files(clean, files, origin=f"{origin}#{raw_name}")

    def install_archive(self, blob: bytes, *, origin: str) -> str:
        """从**上传上来的一个 zip** 装技能（v0.28），返回落到的目录。

        与 ``install_uploads`` 同一套：解出来的文件集交给 ``install_files``，
        名字同样先看 ``SKILL.md`` 的 ``name``、再看 zip 里那一层顶层目录。
        **返回技能名**（不是目录）。
        """
        files = _strip_common_root(self._read_zip(blob))
        if not files:
            raise InvalidRequestError("压缩包里没有可用的文件")
        name = _skill_name(files) or _common_root(list(files))
        if not name:
            raise InvalidRequestError(
                "认不出这个技能叫什么：请在压缩包里放一个带 name 的 SKILL.md，"
                "或者让所有文件都在同一个顶层目录下（例如 my-skill/…）"
            )
        self.install_files(name, files, origin=origin)
        # 返回**技能名**（与 install() 返回目录不同）：调用方接下来多半要去
        # 技能注册表里把它读回来给界面，名字才是它要的东西
        return name

    def install_uploads(self, uploads: Sequence[tuple[str, bytes]], *, origin: str) -> str:
        """从**界面上传上来的一批文件**装一个技能（v0.28），**返回技能名**。

        ``uploads`` 是 ``[(相对路径, 内容)]``：选文件夹时浏览器给的就是这个形状
        （每个文件带自己的 ``webkitRelativePath``），压缩包由调用方先解开。

        技能名取 ``SKILL.md`` frontmatter 里的 ``name``（规范里它就等于目录名），
        取不到再退到"这批文件的顶层目录名"。两者都没有就报错——
        与其装成一个叫 ``skill`` 的东西，不如让用户把目录结构弄对。
        """
        files = _strip_common_root(
            {_safe_relative(path) or "": blob for path, blob in uploads}
        )
        if not files:
            raise InvalidRequestError("没有收到可用的文件")
        name = _skill_name(files) or _common_root(list(files))
        if not name:
            raise InvalidRequestError(
                "认不出这个技能叫什么：请在压缩包/文件夹里放一个带 name 的 SKILL.md，"
                "或者让所有文件都在同一个顶层目录下（例如 my-skill/…）"
            )
        self.install_files(name, files, origin=origin)
        # 返回名字（与 install() 返回目录不同，见 install_archive 的说明）
        return name

    def uninstall(self, name: str) -> None:
        """卸载。**只能卸市场装的**（仓库自带的不在清单里，所以删不掉）。"""
        clean = (name or "").strip()
        index = self._records()
        if clean not in index:
            raise NotFoundError(
                f"「{clean}」不是从市场装的，不能在这里卸载。"
                "随代码发布的技能要改就去改仓库里的 skills/"
            )
        target = self.install_root / _safe_dirname(clean)
        shutil.rmtree(target, ignore_errors=True)
        index.pop(clean, None)
        self._write_index(index)
        logger.info("技能已卸载：%s", clean)

    # ------------------------------------------------------------------ 内部

    def _resolve_source(
        self, name: str, *, source: str, catalog: str
    ) -> tuple[tuple[str, Path | bytes] | None, str]:
        """把 ``source`` 解析成 ``(技能名, 目录 Path 或 zip 的 bytes)``。

        三种形态：zip（本地或 http）、目录、以及"目录里再找一层同名目录"。
        最后那种是**发布形态**：一个源目录里并列着若干技能目录。
        """
        text = (source or "").strip()
        if not text:
            raise InvalidRequestError("缺少参数：source（技能从哪儿来）")

        if _is_url(text):
            blob = self._download(text)
            return (name, blob), text

        path = Path(text)
        if path.is_file() and path.suffix.lower() == ".zip":
            return (name, path.read_bytes()), str(path)
        if path.is_dir():
            # 目录直接就是技能（有 SKILL.md），或者是"若干技能并列"的源目录
            if (path / SKILL_FILE).is_file():
                return (path.name, path), str(path)
            for candidate in (path / name, path / _safe_dirname(name)):
                if (candidate / SKILL_FILE).is_file():
                    return (candidate.name, candidate), str(path)
            return None, str(path)

        # 目录形态的索引：`catalog` 指着一个源目录，`source` 只是它的条目名
        if catalog:
            base = Path(catalog)
            for candidate in (base / text, base / _safe_dirname(text)):
                if (candidate / SKILL_FILE).is_file():
                    return (candidate.name, candidate), str(base)
            if base.is_file() and base.suffix.lower() == ".zip":
                return (text, base.read_bytes()), str(base)
        raise NotFoundError(f"找不到这个技能的来源：{text}")

    def _fetch(self, source: str) -> tuple[bytes, str]:
        """取索引内容。找不到就 ``NotFoundError``（调用方把它当"空清单"）。"""
        text = (source or "").strip()
        if not text:
            return b'{"skills": []}', "(空源)"
        if _is_url(text):
            return self._download(text), text
        path = Path(text)
        if path.is_dir():
            path = path / CATALOG_FILE
        if not path.is_file():
            raise NotFoundError(f"源里没有索引：{text}")
        return path.read_bytes(), str(path)

    def _download(self, url: str) -> bytes:
        """下载并**限额**。``file://`` 也走这里，所以本地源与远端源是同一条路。"""
        try:
            with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - 用户自己配的源
                # 先看 Content-Length：等它传完再拒等于已经把磁盘写满了
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise InvalidRequestError(
                        f"源太大了（{declared} 字节，上限 {MAX_DOWNLOAD_BYTES}）"
                    )
                blob = response.read(MAX_DOWNLOAD_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UpstreamError(f"取不到源的地址 {url}：{exc}") from exc
        if len(blob) > MAX_DOWNLOAD_BYTES:
            raise InvalidRequestError(f"源超过 {MAX_DOWNLOAD_BYTES} 字节，已中止")
        return blob

    def _read_dir(self, source: Path) -> dict[str, bytes]:
        """读一个本地技能目录（相对路径 → 内容）。收不收与另外两条路同一个判据
        （``is_allowed``）：本地源也不该塞二进制进来（``.exe`` / ``.dll`` 会被
        跳过并记一条日志），而纯文本的 ``_rels/.rels`` 不该被当成二进制。"""
        out: dict[str, bytes] = {}
        total = 0
        for item in sorted(source.rglob("*")):
            if item.is_dir() or item.is_symlink():
                # **符号链接不读**：它能把技能目录链到仓库外，而那正是
                # 我们要防的那类"看起来在目录里、其实在别处"
                if item.is_symlink():
                    logger.warning("跳过符号链接：%s", item)
                continue
            # 先只读开头一段再决定：为一个注定要拒的文件把整份读进内存不值得
            with item.open("rb") as handle:
                head = handle.read(SNIFF_BYTES)
            if not is_allowed(item.name, head):
                logger.warning("技能里不收这类文件，跳过：%s", item)
                continue
            total += item.stat().st_size
            if total > MAX_UNPACKED_BYTES:
                raise InvalidRequestError(f"技能内容超过 {MAX_UNPACKED_BYTES} 字节")
            out[item.relative_to(source).as_posix()] = item.read_bytes()
        return out

    def _read_zip(self, blob: bytes) -> dict[str, bytes]:
        """解开一个 zip（相对路径 → 内容）。**三道防护**：路径穿越、符号链接、大小与条目数。"""
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as exc:
            raise InvalidRequestError(f"这不是一个合法的 zip：{exc}") from exc

        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise InvalidRequestError(f"zip 里条目太多（{len(entries)}，上限 {MAX_ENTRIES}）")

        out: dict[str, bytes] = {}
        total = 0
        for info in entries:
            relative = _safe_relative(info.filename)
            if relative is None:
                # zip slip：`../../x` 或绝对路径。**整包拒绝**而不是跳过那一条——
                # 一个想往仓库外写的包，里面剩下的东西不值得再信。
                raise InvalidRequestError(f"zip 里有越界路径，已拒绝整个包：{info.filename}")
            if info.is_dir():
                continue
            if _is_symlink(info):
                raise InvalidRequestError(f"zip 里有符号链接，已拒绝：{info.filename}")
            # 与上传、本地目录同一个判据；同样先只读开头一段（8KB，不受
            # 解压后大小影响），免得为判一个 zip 炸弹先把整份解出来
            with archive.open(info) as handle:
                head = handle.read(SNIFF_BYTES)
            if not is_allowed(relative, head):
                raise InvalidRequestError(
                    f"zip 里有不收的文件类型（{info.filename}）："
                    "技能可以是文本、脚本与资源，但不该带二进制（见模块头）"
                )
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise InvalidRequestError(
                    f"解压后超过 {MAX_UNPACKED_BYTES} 字节（zip 炸弹？），已中止"
                )
            out[relative] = archive.read(info)
        return out

    def _read_skill_text(self, target: Path) -> str:
        """把技能目录里的**文本**拼起来做扫描（二进制跳过：读了也只是乱码）。

        **不止扫 SKILL.md**：references/ 里的内容同样会进模型上下文
        （模型按需读它们），``scripts/`` 里的代码同样会被模型读进去当例子，
        所以注入特征藏在那一层同样危险。
        """
        parts: list[str] = []
        for item in sorted(target.rglob("*")):
            if not item.is_file():
                continue
            if suffix_of(item.name) in ASSET_SUFFIXES:
                # 图片字体是已知的二进制，不必读一遍只为判它不是文本
                # （大图读进内存再扔掉，纯属浪费）
                continue
            try:
                blob = item.read_bytes()
            except OSError:
                continue
            # 与"收不收"同一个判据：是文本才读来扫。**不能只按扩展名筛**——
            # `.rels` 这类点文件的后缀是空的（见 suffix_of），而它恰恰是
            # 会被模型读进上下文的文本；同理，无后缀的脚本也要扫。
            if not looks_like_text(blob):
                continue
            # errors=replace 兜底：取样只看开头，文件尾部仍可能有坏字节
            parts.append(blob.decode("utf-8", errors="replace"))
        return "\n".join(parts)

    def _records(self) -> dict[str, dict[str, Any]]:
        """已装清单（内部形状）：``技能名 → {origin, repo, sha, …, files}``。

        两代格式都读：v1 是 ``{"技能名": "来源字符串"}``，v2 是
        ``{"技能名": {"origin": …, "repo": …}}``。读的时候统一成 v2，
        写的时候一律写 v2（见 ``_write_index``）。
        """
        try:
            data = json.loads(self.installed_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for name, record in data.items():
            if isinstance(record, dict):
                out[str(name)] = dict(record)
            else:
                out[str(name)] = {"origin": str(record)}
        return out

    def _remember(self, name: str, origin: str, *, extra: dict[str, Any] | None = None) -> None:
        index = self._records()
        index[name] = {"origin": origin, "installed_at": _now(), **(extra or {})}
        self._write_index(index)

    def _write_index(self, index: dict[str, Any]) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.installed_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )


def _common_root(paths: Sequence[str]) -> str:
    """这批文件的共同顶层目录名（没有共同顶层就空串）。

    选文件夹上传时浏览器给的是 ``my-skill/…``；zip 也常常套一层同名目录。
    两种情况都要**把那一层去掉**：技能目录里再套一层 ``my-skill/``，
    扫出来的技能名与目录结构就都不对了。
    """
    tops = {path.split("/", 1)[0] for path in paths if path}
    if len(tops) != 1:
        return ""
    only = tops.pop()
    # 只有一层（所有文件都在根下）时它不是"顶层目录"，是文件名
    return only if any("/" in path for path in paths) else ""


def _strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """去掉共同的那一层顶层目录（见 ``_common_root``）。"""
    root = _common_root(list(files))
    if not root:
        return files
    prefix = f"{root}/"
    return {path[len(prefix) :]: blob for path, blob in files.items() if path.startswith(prefix)}


def _prompt_text_problem(raw: bytes) -> str:
    """这份 `SKILL.md` 里有没有**过不了提示词**的字符；有就说清为什么（空串 = 没问题）。

    判据与读侧（`services/skills.py` 的扫描/读取）**同一套**，来自 `app/core/text_hygiene.py`：
    **先合并**成对代理项（上游常把 emoji 写成字面转义 `"\\ud83e\\udd16"`，那是能救的 ✓），
    合并后仍非法的才算（真孤立代理项 / 控制字符 / 技能名里的换行）。
    """
    text = raw.decode("utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    for key in ("name", "description", "when_to_use", "summary"):
        value = recombine_surrogates(str(meta.get(key) or ""))
        problem = text_problem(value, single_line=(key == "name"))
        if problem:
            return f"{key}：{problem}"
    return text_problem(recombine_surrogates(body))


def _skill_name(files: Mapping[str, bytes]) -> str:
    """``SKILL.md`` frontmatter 里的 ``name``（规范里它等于目录名）。读不到就空串。"""
    raw = files.get(SKILL_FILE)
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace")
    meta, _body = parse_frontmatter(text)
    return str(meta.get("name") or "").strip()


def _digests(root: Path) -> dict[str, str]:
    """落盘之后的文件 → 内容 hash（前 12 位）。

    调研 §4.6 要的"锁文件"就是它：commit SHA 锁的是**上游那一版**，
    而这份 hash 锁的是**我们磁盘上这一份**——两者都要记，
    因为"上游的 commit 没变、本地文件被改过"是另一件要答得出来的事。
    """
    out: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        try:
            out[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()[
                :12
            ]
        except OSError:
            continue
    return out


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://", "file://"))


def _safe_dirname(name: str) -> str:
    """技能名 → 目录名。**掐掉首尾的点**：``..`` 在路径里不是名字、是导航。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name).strip("._")
    return cleaned or "skill"


def _safe_relative(raw: str) -> str | None:
    """zip 里的条目名 → 安全的相对路径；越界或非法返回 ``None``。

    三种要拦的：绝对路径（``/x``、``C:/x``）、``..`` 段、以及 Windows 上的反斜杠
    路径（``..\\x`` 在 POSIX 上看着人畜无害，解到 Windows 上就穿越了）。
    """
    text = (raw or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """zip 里的符号链接：高 4 位是 Unix 权限，``0o120000`` 就是链接。"""
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000
