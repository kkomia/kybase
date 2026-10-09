"""站点图标代理（D11-②，2026-09-29 用户："网页 logo 要显示站点**真实的** logo"）。

**为什么要经过我们的后端，而不是让浏览器去 `https://<域名>/favicon.ico`**：

1. **隐私**：浏览器直连等于在用户渲染这一行时，把他的 IP / UA / 访问时刻交给那家站点——
   而用户只是问了一句话。经过后端之后，向第三方发请求的是**服务器**，浏览器只跟我们自己的源说话；
2. **稳定与离线**：图标落盘缓存，之后一直从本地发。第三方站点慢 / 挂 / 改 robots，
   都不会让这一行变慢或闪；
3. **一次抓好、多处复用**：同一个站点在好几个步骤、好几条会话里出现，只抓一次。

**图标是怎么找到的**（三步，逐级放宽；每一步的每个候选地址都过 `check_public_url`）：

1. **先试固定两条路径**（`/apple-touch-icon.png`、`/favicon.ico`）× `{host, www.host}`——
   绝大多数站点在这；
2. 都落空就**读一眼首页 HTML**，按它自己声明的 `<link rel="icon" href="…">` 去抓
   （2026-10-01 真机复验：SPA 站点让固定路径变成瞎猜——`docs.mthreads.com` 的
   `/favicon.ico` 回 **200 但 `Content-Type: text/html`**（兜底页，按"只收位图"被拒），
   `mineru.atomgit.com` 的 `/favicon.ico` 直接 404，而两站首页里都写着真图标，
   后者还是相对路径 `./assets/images/favicon.png`）；
3. 还是取不到就**如实回"没有图标"**（`opendatalab.github.io` 的根页里就没有 link 声明）
   ——端点发 404，前端退回站点字牌或通用地球。

**边界（每条都有用例钉着）**：

- **任何形态合法的域名都代为抓取**（2026-10-01 用户："这个为啥抓不到真实的图标呢"）。
  改前只认 ``ALLOWED_DOMAINS`` 那张白名单、表外域名一律 422，于是真实结果里绝大多数站点
  （`opendatalab.github.io` 这种）只剩一枚字母圆——**白名单当初是防跳板，但每个候选地址
  本来就过下面那条校验，它只是多余的收紧**。收什么域名不是这条接口该管的事；
- **每个候选地址都过** ``web.check_public_url``（本仓唯一那处"是不是公网地址"的判断）——
  这条接口收的是用户数据里来的域名，绝不能让内网地址借它当跳板（SSRF）。
  白名单撤了之后，这一条就是"这个域名该不该抓"的**唯一**判断。**首页里发现的地址也一样**
  （那是第三方页面说了算的字符串，更不能例外）；
- 只接受**位图**（png / jpeg / gif / ico / webp，按魔数嗅探）：**SVG 明确拒收**——
  它和我们同源，被打开就是一个同源脚本执行面（会话令牌就在 localStorage 里）。
  `.svg` 因此不需要在"首页里声明的图标"那一层特判：嗅探这一关本来就过不去；
- 体积上限、缓存条数上限、正负缓存都有 TTL；取不到就**回"没有图标"**（端点发 404，
  前端退回站点字牌或通用地球），不抛给用户、不在页面上留空位。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import InvalidRequestError
from app.core.http import shared_client
from app.core.lazy_httpx import httpx
from app.services.web import check_public_url

__all__ = ["SiteIcon", "SiteIconService", "normalize_domain"]

logger = logging.getLogger(__name__)

#: 先试的固定路径。**apple-touch-icon 在前**：它是给"放到主屏"用的 180×180 实心图标，
#: 看起来才是 logo；`favicon.ico` 常见 16×16 或多尺寸打包，缩到 14px 时常常糊成一团。
#: 这两条**不是全部**：都落空时还会读首页 HTML 找 `<link rel="icon">`（见 `_discover_icon`）。
ICON_PATHS: tuple[str, ...] = ("/apple-touch-icon.png", "/favicon.ico")

#: 允许的位图种类 → 服务时用的 media type。**没有 svg**（理由见模块说明）。
_KINDS: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "ico": "image/x-icon",
    "webp": "image/webp",
}

#: 单个图标的字节上限。真实的 touch icon 在 10–80KB，256KB 已经宽裕。
MAX_ICON_BYTES = 256 * 1024

#: 首页 HTML 的字节上限。SPA 的兜底页常有几十 KB，512KB 足够宽松；
#: 超了就当"这页读不成"——图标是最不重要的东西，不值得为它把内存铺开。
MAX_PAGE_BYTES = 512 * 1024

#: 首页里最多认几个图标候选（同一个站点常有 32/180/svg 好几枚，按声明顺序取前几个）。
MAX_ICON_CANDIDATES = 4

#: `<link …>` 整条标签：`rel` 与 `href` 的先后不固定（两种写法真实站点都有），
#: 所以先抓标签、再从里面各抽各的属性。
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)

#: 属性值三种写法都得认：`rel="icon"` / `rel='icon'` / `rel=icon`。
_REL_RE = re.compile(r"""\brel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)

#: 目录里最多留多少个图标文件（正 + 负缓存一起算）。超了删最旧的。
CACHE_MAX_FILES = 200

#: 图标缓存多久算过期（秒）。图标是低频变化的东西，30 天够了；
#: 过期后**重新抓一次**，抓不到就继续用旧的（见 `icon`）。
CACHE_TTL_SECONDS = 30 * 24 * 3600

#: "这个站点抓不到"记多久（秒）。不记的话每次渲染都会去撞一个挂掉的站点。
MISS_TTL_SECONDS = 6 * 3600

#: 单个候选地址的墙钟上限（秒）。图标是最不重要的东西，不值得让页面等它。
FETCH_TIMEOUT_SECONDS = 6.0

#: 跟几跳。与 `services/web` 同一套纪律：**每一跳都重新校验地址**。
_MAX_REDIRECTS = 3

_USER_AGENT = "kylab/0.1 (+site icon)"

#: 前缀。放在 `<data_dir>/site-icons/` 下，与 skills / plugins / workspaces 同一层。
DIRNAME = "site-icons"


def normalize_domain(raw: str) -> str:
    """把入参收拾成可比对的域名：小写、去尾部点。**不做后缀匹配**（那是前端的事）。

    只接受"看着像域名"的字符串：大写字母、空格、斜杠、带协议的一律拒绝——
    这个参数会拼进 URL，宽松解析就是把外部字符串塞进请求里。
    """
    domain = (raw or "").strip().lower().rstrip(".")
    if not domain:
        raise InvalidRequestError("缺少参数：domain")
    if "/" in domain or ":" in domain or " " in domain:
        raise InvalidRequestError(f"domain 只能是域名：{raw}")
    if not all(part and part.replace("-", "").isalnum() for part in domain.split(".")):
        raise InvalidRequestError(f"domain 只能是域名：{raw}")
    return domain


@dataclass(frozen=True, slots=True)
class SiteIcon:
    """一枚可以直接发出去的图标。"""

    content: bytes
    media_type: str
    kind: str


class SiteIconService:
    """带磁盘缓存的站点图标抓取。

    **无状态**（除了根目录）：缓存全在磁盘上，所以每个请求 new 一个也不心疼，
    不必在组合根里占一个字段（见 `api/v1/site_icons.py` 的说明）。
    """

    def __init__(self, data_dir: Path, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> None:
        self._root = Path(data_dir) / DIRNAME
        self._timeout = timeout

    # ------------------------------------------------------------------ 读

    def icon(self, domain: str) -> SiteIcon | None:
        """取一枚图标；**畸形域名抛 422**，形态合法但取不到就回 ``None``。

        为什么把这两种分开：前者是"这个接口就不该被这样调"（写错了 / 在探），
        后者是"这个站点暂时没有图标"（正常的降级路径）。前端两种都退回站点字牌 / 通用地球。

        **缓存按入参域名本身存**：`en.wikipedia.org` 与 `zh.wikipedia.org` 各抓一份、各存一份。
        改前子域归到白名单里的根域，那要先有一张"谁是根域"的表——随白名单一起撤了。
        """
        host = normalize_domain(domain)
        cached = self._read_cached(host, ttl=CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        if self._miss_is_fresh(host):
            return None
        # 过期了也先留着旧的：抓失败时继续用手上这一份，比"突然没有 logo"好
        stale = self._read_cached(host, ttl=None)

        fetched = self._fetch(host)
        if fetched is None:
            self._note_miss(host)
            return stale
        self._store(host, fetched)
        return fetched

    def _read_cached(self, host: str, *, ttl: int | None) -> SiteIcon | None:
        path = self._find_cached(host)
        if path is None:
            return None
        if ttl is not None and time.time() - path.stat().st_mtime > ttl:
            return None
        kind = path.suffix.lstrip(".")
        try:
            return SiteIcon(content=path.read_bytes(), media_type=_KINDS[kind], kind=kind)
        except (OSError, KeyError):
            logger.warning("读图标缓存失败：%s", path, exc_info=True)
            return None

    def _find_cached(self, host: str) -> Path | None:
        for kind in _KINDS:
            candidate = self._root / f"{host}.{kind}"
            if candidate.is_file():
                return candidate
        return None

    def _miss_is_fresh(self, host: str) -> bool:
        marker = self._root / f"{host}.miss"
        if not marker.is_file():
            return False
        return time.time() - marker.stat().st_mtime <= MISS_TTL_SECONDS

    def _note_miss(self, host: str) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / f"{host}.miss").write_bytes(b"")
            # 抓到图标之后这个标记就没意义了：读到图标时顺手清掉，免得它一直躺在那里
            self._prune()
        except OSError:
            logger.warning("写图标缺失标记失败：%s", host, exc_info=True)

    def _store(self, host: str, icon: SiteIcon) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = self._root / f"{host}.{icon.kind}"
            # 先写临时文件再改名：另一个请求正好读到半截文件时不会拿到坏字节
            temp = target.with_suffix(f".{icon.kind}.part")
            temp.write_bytes(icon.content)
            temp.replace(target)
            (self._root / f"{host}.miss").unlink(missing_ok=True)
            # 换了种类（比如原来缓存的是 ico、这次拿到 png）时把旧的删掉，免得两份都在
            for kind in _KINDS:
                if kind != icon.kind:
                    (self._root / f"{host}.{kind}").unlink(missing_ok=True)
            self._prune()
        except OSError:
            logger.warning("写图标缓存失败：%s", host, exc_info=True)

    def _prune(self) -> None:
        """条数上限：超了删最旧的。**这是缓存不是档案**，删掉最多再抓一次。"""
        try:
            files = sorted(self._root.iterdir(), key=lambda item: item.stat().st_mtime)
        except OSError:
            return
        for item in files[: max(0, len(files) - CACHE_MAX_FILES)]:
            try:
                item.unlink()
            except OSError:
                logger.debug("清理图标缓存失败：%s", item, exc_info=True)

    # ------------------------------------------------------------------ 抓

    def _fetch(self, host: str) -> SiteIcon | None:
        """先试固定路径，全落空再**读首页 HTML**；第一个拿到合格位图的就用它。"""
        for path in ICON_PATHS:
            for candidate_host in (host, f"www.{host}"):
                icon = self._fetch_one(f"https://{candidate_host}{path}")
                if icon is not None:
                    return icon
        return self._discover_icon(host)

    def _discover_icon(self, host: str) -> SiteIcon | None:
        """**从首页 HTML 里找它自己声明的图标**（`<link rel="icon" href="…">`）。

        为什么必须有这一步（2026-10-01 真机复验）：固定两条路径对 SPA 站点是瞎的——
        `docs.mthreads.com` 的 `/favicon.ico` 回 200 但 `Content-Type: text/html`
        （SPA 兜底页，按"只收位图"被拒），`mineru.atomgit.com` 的 `/favicon.ico` 直接 404；
        而两站首页里都写了真图标，后者还是相对路径 `./assets/images/favicon.png`。

        分寸：首页只读 `https://{host}/`，**失败才退 `www.{host}/`**；读到了就用它
        （同一条 path 里的相对地址只对得上那一页），里面没有能用的图标就收手回 ``None``
        ——`opendatalab.github.io` 就属于这一档，前端如实退通用地球。
        """
        for page in (f"https://{host}/", f"https://www.{host}/"):
            found = self._read_page(page)
            if found is None:
                continue
            html, final_url = found
            for href in _icon_hrefs(html):
                try:
                    # **相对谁**取决于跟完重定向之后那一页的地址，不是我们请求的那个
                    candidate = str(httpx.URL(final_url).join(href))
                except (httpx.InvalidURL, ValueError):
                    # 页面是第三方说了算，什么古怪写法都可能出现：解不出来就跳过这一个
                    logger.info("首页里那个图标地址解析不了，跳过：%s", href)
                    continue
                icon = self._fetch_one(candidate)
                if icon is not None:
                    return icon
            return None
        return None

    def _read_page(self, url: str) -> tuple[str, str] | None:
        """取一页首页 HTML（正文 + 最终地址）；**任何失败都回 ``None``**。

        与 `_fetch_one` 同一副分寸：初始地址过 `check_public_url`，某一跳跳到内网也算
        "没拿到"（不是调用方写错了），页面不是 HTML / 超限 / 超时同样只是"这一步没成"。
        """
        try:
            target = check_public_url(url)
        except InvalidRequestError:
            logger.info("站点首页不是公网地址，跳过：%s", url)
            return None
        try:
            return self._get_text(target)
        except (httpx.HTTPError, OSError) as exc:
            logger.info("站点首页抓取失败（%s）：%s", url, exc)
            return None
        except InvalidRequestError:
            logger.info("站点首页的某一跳跳到了非公网地址，跳过：%s", url)
            return None

    def _fetch_one(self, url: str) -> SiteIcon | None:
        try:
            target = check_public_url(url)
        except InvalidRequestError:
            logger.info("站点图标候选地址不是公网地址，跳过：%s", url)
            return None
        try:
            raw = self._get_bytes(target)
        except (httpx.HTTPError, OSError) as exc:
            logger.info("站点图标抓取失败（%s）：%s", url, exc)
            return None
        except InvalidRequestError:
            # 初始地址是公网的，但**某一跳重定向到了内网**：`_get_bytes` 每一跳都重新校验，
            # 这种候选按"没拿到"处理（不是调用方写错了），继续试下一个候选
            logger.info("站点图标的某一跳跳到了非公网地址，跳过：%s", url)
            return None
        if raw is None:
            return None
        kind = _sniff(raw)
        if kind is None:
            logger.info("站点图标不是认识的位图，丢弃：%s", url)
            return None
        return SiteIcon(content=raw, media_type=_KINDS[kind], kind=kind)

    def _get_bytes(self, target: str) -> bytes | None:
        """取图标的字节；**只收位图**（`content-type` 不像图片就直接放弃，不必等它下完）。

        "超限与超时都回 ``None`` 而不是半截字节"的理由见 `_get_body`：
        一个被截断的 PNG 在界面上就是"图片坏了"，比干脆没有图标（前端退字牌 / 地球）难看。
        """
        body = self._get_body(target, accept="image/", limit=MAX_ICON_BYTES, what="站点图标")
        return None if body is None else body[0]

    def _get_text(self, target: str) -> tuple[str, str] | None:
        """取一页 HTML 的**正文与最终地址**（后者供相对图标地址解析，见 `_discover_icon`）。

        只收 `text/html`（或**没声明 content-type** 的——少数站点的首页就是不带）；
        按 UTF-8 解码并容忍坏字节：图标地址在 HTML 里是 ASCII，
        编码猜错最多只影响我们不去读的那段正文。
        """
        body = self._get_body(target, accept="text/html", limit=MAX_PAGE_BYTES, what="站点首页")
        if body is None:
            return None
        raw, final = body
        return raw.decode("utf-8", "replace"), final

    def _get_body(
        self, target: str, *, accept: str, limit: int, what: str
    ) -> tuple[bytes, str] | None:
        """取一份正文与最终地址，**手工跟重定向、每一跳重新校验**（与 `services/web` 同一套纪律）。

        图标与首页共用这一份纪律，只在"收什么类型、上限多少"上分开：
        位图 256KB / `image/*`，首页 512KB / `text/html`。**跟重定向、逐跳校验、
        同一 deadline、超限与超时都回 ``None`` 而不是半截**这几条对两者一模一样——
        分两份写迟早有一份漏掉某一跳的校验。

        **超限与超时都回 ``None`` 而不是半截字节**：半张图在界面上就是"图片坏了"，
        半截 HTML 则可能正好断在 `<link …>` 中间、抽出一个坏地址——
        两者都比干脆没有图标难查（服务端还会把它们缓存下来）。
        """
        deadline = time.monotonic() + self._timeout
        current = target
        for _ in range(_MAX_REDIRECTS + 1):
            with shared_client().stream(
                "GET", current, headers={"User-Agent": _USER_AGENT}, timeout=self._timeout
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location") or ""
                    if not location:
                        return None
                    current = check_public_url(str(httpx.URL(current).join(location)))
                    continue
                if response.status_code >= 400:
                    return None
                declared = (response.headers.get("content-type") or "").lower()
                if declared and not declared.startswith(accept):
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= limit:
                        logger.info("%s超过 %d 字节，放弃：%s", what, limit, current)
                        return None
                    if time.monotonic() > deadline:
                        logger.info("%s读取超时，放弃：%s", what, current)
                        return None
                return b"".join(chunks), current
        return None


def _icon_hrefs(html: str) -> list[str]:
    """首页 HTML 里声明的图标地址：保序去重，最多 ``MAX_ICON_CANDIDATES`` 个。

    认 `rel` 里含 `icon` 的那些（`icon` / `shortcut icon` / `apple-touch-icon` 都算，
    大小写不敏感）——`rel="stylesheet"` 之类不含 `icon`，自然落选。

    `data:` 明确跳过：那是内联图片、不是可抓的地址。**`.svg` 不特判**：魔数嗅探本来就
    拒收它（同源脚本执行面，见模块说明），这一层少一条规则就少一处会漂的重复。
    """
    found: list[str] = []
    for tag in _LINK_TAG_RE.findall(html):
        if "icon" not in _attr(_REL_RE, tag).lower():
            continue
        href = _attr(_HREF_RE, tag).strip()
        if not href or href.lower().startswith("data:") or href in found:
            continue
        found.append(href)
        if len(found) >= MAX_ICON_CANDIDATES:
            break
    return found


def _attr(pattern: re.Pattern[str], tag: str) -> str:
    """从一条标签里取属性值（双引号 / 单引号 / 裸值三种写法都认）；没有就回空串。"""
    match = pattern.search(tag)
    if match is None:
        return ""
    return next((group for group in match.groups() if group is not None), "")


def _sniff(raw: bytes) -> str | None:
    """按魔数认位图；认不出来就是 ``None``（**不信 content-type**：那是第三方说了算的）。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if raw.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None
