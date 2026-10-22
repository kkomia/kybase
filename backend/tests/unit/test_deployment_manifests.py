"""部署清单的静态契约。

镜像构建不在这里执行；这里钉住清单之间的对应关系：

1. ``deploy/docker-compose.yml`` 的 ``backend.build`` 指向包含 ``backend/`` 的那一层
   （仓库根），dockerfile 是 ``backend/Dockerfile``——镜像里要装仓库自带的
   ``skills/``，而 COPY 只能从上下文里取文件；
2. 仓库根 ``.dockerignore`` 存在，避免把 ``.venv`` / ``node_modules`` 打进构建上下文；
3. Dockerfile 把 skills 拷到 ``/app/skills``，用 ENV 钉死 ``KYLAB_SKILLS_DIR``，
   并在 ``chmod -R a+rX /app`` 之前拷贝，保证非 root 运行时可读；
4. 完整栈包含 backend、mcp、frontend、postgres、minio 五个服务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.local

#: 本文件在 ``backend/tests/unit/`` 下，往上三层是仓库根。
ROOT = Path(__file__).resolve().parents[3]
COMPOSE = "deploy/docker-compose.yml"


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / COMPOSE).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{COMPOSE} 不是一份 YAML 映射"
    return loaded


def test_compose_has_the_expected_services() -> None:
    services = set(_compose()["services"])
    assert {"postgres", "minio", "backend", "mcp", "frontend"} <= services


def test_backend_build_context_covers_the_repository() -> None:
    build = _compose()["services"]["backend"]["build"]

    assert build["context"] == ".."
    assert build["dockerfile"] == "backend/Dockerfile"


def test_the_repo_root_context_actually_contains_what_the_dockerfile_copies() -> None:
    build = _compose()["services"]["backend"]["build"]
    context = (ROOT / "deploy" / build["context"]).resolve()

    assert context == ROOT
    for relative in ("backend/pyproject.toml", "backend/uv.lock", "backend/README.md"):
        assert (context / relative).is_file(), relative
    assert (context / "backend" / "app").is_dir()
    assert (context / "skills").is_dir()
    assert (context / ".dockerignore").is_file()


def test_dockerfile_puts_the_bundled_skills_in_the_image_readably() -> None:
    text = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    copy_line = "COPY --chown=kylab:kylab skills ./skills"
    assert copy_line in text
    assert "ENV KYLAB_SKILLS_DIR=/app/skills" in text
    # 找 RUN 指令本身：注释里也引用了同一条命令，裸字符串会命中注释。
    assert text.index(copy_line) < text.index("RUN chmod -R a+rX /app")


def test_mcp_uses_the_backend_image_and_does_not_run_a_worker() -> None:
    mcp = _compose()["services"]["mcp"]

    assert mcp["image"].startswith("kybase-backend:")
    assert mcp["environment"]["KYLAB_RUN_WORKER"] == "false"
    assert "8765" in str(mcp["ports"])
