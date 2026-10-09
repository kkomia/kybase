"""桌面壳要的那份**前端资源包**（v0.56）。

为什么要有它（2026-09-30）：壳的界面本应"永远是服务器上那一份"，可壳自己**不会下载** ——
`frontend-resources/` 一直是**手工拷**进去的（当晚为验证就手工装了 v1.0.1 / v1.0.2 两次）。
没有这组端点，"服务器发了新前端、客户端下次启动自动用上"这条产品承诺
（《Tauri-壳资源分离与前端热更新-实现规格》§1 的验收）就一直只写在文档里。

契约（对着规格 §4 落地；路径按本仓约定收在 ``/api/v1`` 下）：

- ``GET /api/v1/app/frontend/manifest`` → ``{version, package_url, sha256, size,
  min_shell_version, released_at}``；
- ``GET /api/v1/app/frontend/package`` → ``application/zip``（整份 ``dist`` 打成一个包）。

两处与规格样例**有意不同**，都是为了让"服务器不必手工 bump 任何号"：

1. ``version`` 取**内容指纹**（zip 的 sha256 前 12 位）而不是语义版本 —— 壳只拿它做
   "和本地那份一样吗"的比较，"变了就是变了"；
2. ``package_url`` 指向**本端点自己**（``/api/v1/app/frontend/package``），不要求 CDN：
   这份部署有后端就够（规格里那个 CDN 是可选部署形态，不是契约的一部分）。

**公开端点**（与 ``/health`` 同档，不鉴权）：壳在**登录之前**就要拿到界面（引导页 →
登录 → 才领钥匙），而且这份前端本来就是服务器对任何浏览器都发的那一份。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.config import API_VERSION, get_settings

router = APIRouter(prefix="/app/frontend", tags=["frontend"])

#: 下载时那个包叫什么（Content-Disposition 用；壳自己按版本目录落盘）
PACKAGE_FILENAME = "kylab-frontend.zip"

#: 这份前端要求的**最低壳版本**（规范 §4.1 的 `min_shell_version`）。
#: 现在是常量：壳与前端同仓同发；等真有"旧壳配新前端"的兼容矩阵，再挪进构建产物。
MIN_SHELL_VERSION = "0.1.0"

#: 版本指纹取 sha256 的前几位：壳只用它比较"变了没有"，12 位（48 bit）足够。
VERSION_CHARS = 12


def dist_dir() -> Path | None:
    """这份部署的前端产物目录；没有就 ``None``（端点 404 并说清怎么造）。

    默认是**仓库里的 ``frontend/dist``**（从本文件往上四层即仓库根：backend/app/api/v1 →
    backend/app → backend → 仓库）；部署形态用 ``KYLAB_FRONTEND_DIST`` 指到别处。
    """
    configured = get_settings().frontend_dist_dir
    if configured is not None:
        return configured if configured.is_dir() else None
    default = default_dist_dir()
    return default if default.is_dir() else None


def default_dist_dir() -> Path:
    """默认产物目录：**仓库根的 `frontend/dist`**。

    从本文件往上数**五层**：`v1 → api → app → backend → 仓库根`（少一层就会落到
    `backend/frontend/dist` 上、开发态静默 404 —— 用例把这条路径的结构钉住了）。
    """
    return Path(__file__).resolve().parents[4] / "frontend" / "dist"


def zip_dist(dist: Path) -> bytes:
    """把 ``dist`` 整份打成一个 zip。

    **确定性**（条目排序 + 固定时间戳）：同样的内容每次打出来**逐字节一致** ——
    壳的"版本"与"sha256 校验"都建立在这上面，不确定就等于每次启动都判成"有新版本"，
    白下载一遍。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(dist.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(dist).as_posix()
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def fingerprint(package: bytes) -> str:
    """包的 sha256（小写 hex）——manifest 的 `sha256` 与 `version` 都从它来。"""
    return hashlib.sha256(package).hexdigest()


class ManifestOut(BaseModel):
    """壳取包之前先问的那份清单（规范 §4.1）。"""

    version: str
    package_url: str
    sha256: str
    size: int
    min_shell_version: str
    released_at: str


@router.get("/manifest", response_model=ManifestOut, summary="前端资源包的版本清单")
def manifest() -> ManifestOut:
    """壳在启动/连接时问一句"你那边是什么版本"，好和本地那份比。"""
    dist = _require_dist()
    package = zip_dist(dist)
    digest = fingerprint(package)
    return ManifestOut(
        version=digest[:VERSION_CHARS],
        package_url=f"/api/{API_VERSION}/app/frontend/package",
        sha256=digest,
        size=len(package),
        min_shell_version=MIN_SHELL_VERSION,
        released_at=_released_at(dist),
    )


@router.get("/package", summary="前端资源包（整份 dist 的 zip）")
def package() -> Response:
    """整份 ``dist`` 的 zip。确定性打包 ⇒ 同一个版本的字节永远一样。"""
    body = zip_dist(_require_dist())
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{PACKAGE_FILENAME}"',
            "ETag": '"' + fingerprint(body) + '"',
            # 内容寻址（manifest 给了 sha256）：壳按版本缓存，这里别再让它猜
            "Cache-Control": "no-cache",
        },
    )


def _require_dist() -> Path:
    dist = dist_dir()
    if dist is None:
        raise HTTPException(status_code=404, detail=_missing_detail())
    return dist


def _missing_detail() -> str:
    return (
        "这台服务器上没有前端产物（frontend/dist 不存在）：先在前端目录跑 `pnpm build`，"
        "或用 KYLAB_FRONTEND_DIST 指到产物所在目录。"
    )


def _released_at(dist: Path) -> str:
    """取 dist 里**最新的那个 mtime** 当"发布时间"：没有版本号可依，就如实给文件真实时间。"""
    newest = max((path.stat().st_mtime for path in dist.rglob("*") if path.is_file()), default=0.0)
    return datetime.fromtimestamp(newest, tz=UTC).isoformat().replace("+00:00", "Z")
