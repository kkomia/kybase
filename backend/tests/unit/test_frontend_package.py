"""前端资源包的两个端点（壳的热更新那一侧）。

只挂这一个 router（**不建整套应用**）：这组端点不碰仓储，用不着 PG —— 与
`test_sidecar.py` 那族不同，这里不需要测试库。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import frontend


def _app() -> FastAPI:
    application = FastAPI()
    application.include_router(frontend.router, prefix="/api/v1")
    return application


def _make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "main.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_zip_is_deterministic_and_sorted(tmp_path: Path) -> None:
    """同样的内容打两次**逐字节一致** —— 壳的"版本"与"校验"都建立在这上面。"""
    dist = _make_dist(tmp_path)

    first = frontend.zip_dist(dist)
    second = frontend.zip_dist(dist)

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        # 排序 + 相对路径（正斜杠，与解压那一侧一致）
        assert archive.namelist() == ["assets/main.js", "index.html"]
        assert archive.read("index.html") == b"<html>app</html>"


def test_manifest_describes_the_very_package_it_serves(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """清单里的每个数都应当能在包里对上：size / sha256 / version=指纹前缀。"""
    dist = _make_dist(tmp_path)
    monkeypatch.setattr(frontend, "dist_dir", lambda: dist)
    client = TestClient(_app())

    manifest = client.get("/api/v1/app/frontend/manifest")
    package = client.get("/api/v1/app/frontend/package")

    assert manifest.status_code == 200
    body = manifest.json()
    assert package.status_code == 200
    assert package.headers["content-type"] == "application/zip"
    assert body["size"] == len(package.content)
    assert body["sha256"] == hashlib.sha256(package.content).hexdigest()
    assert body["version"] == body["sha256"][: frontend.VERSION_CHARS]
    assert body["package_url"].endswith("/app/frontend/package")
    assert body["min_shell_version"]
    assert body["released_at"].endswith("Z")


def test_missing_dist_says_how_to_build_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """没有产物时要说**怎么办**（跑 pnpm build / 指 KYLAB_FRONTEND_DIST），不是一句 404。"""
    monkeypatch.setattr(frontend, "dist_dir", lambda: None)
    client = TestClient(_app())

    response = client.get("/api/v1/app/frontend/manifest")

    assert response.status_code == 404
    assert "pnpm build" in response.json()["detail"]


def test_default_dist_dir_points_at_the_repo_frontend_dist() -> None:
    """默认路径的**结构**要钉住（2026-09-30 实测踩过：往上少数一层 → 静默 404）。

    判据不是"等于某个写死的绝对路径"（换个检出目录就假红），而是：
    那个 `dist` 的兄弟里**有本仓的 `backend/app`**。
    """
    default = frontend.default_dist_dir()

    assert default.name == "dist"
    assert default.parent.name == "frontend"
    assert (default.parent.parent / "backend" / "app").is_dir()
