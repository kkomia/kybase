"""部署清单的静态契约（v0.1.1 / §12.224 第 9、12 条）。

这台开发机上**没有 docker**（计划里的风险 R9），镜像构建在这儿跑不了。能在这儿
钉住的是清单之间的**对应关系**——它们错了，构建要么当场崩（COPY 源不存在），
要么以"镜像里少一批文件"的方式静默失败，而后者在运行时只表现为
"技能列表是空的"，极难归因到构建上下文。

1. 两份 compose 的 ``backend.build`` 都指到"包含 ``backend/`` 的那一层"
   （仓库根 / NAS 上的 ``src/``），dockerfile 都是 ``backend/Dockerfile``——
   镜像里要装仓库自带的 ``skills/``，而 COPY 只能从上下文里取文件
   （见 ``backend/Dockerfile`` 的说明）；
2. 仓库根那份 ``.dockerignore`` 在（上下文一变成本仓库根，不挡它就会把
   ``.venv`` / ``node_modules`` 整个打包发给守护进程）；
3. Dockerfile 把 skills 拷到 ``/app/skills``、用 ENV 钉死 ``KYLAB_SKILLS_DIR``，
   且**拷在 chmod 之前**（NAS 的 ``/vol1`` 把文件报成 000，落在 chmod 之后
   就没有人再给它补可读位了——§12.223 第 4 条）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

#: 本文件在 ``backend/tests/unit/`` 下，往上三层是仓库根。
ROOT = Path(__file__).resolve().parents[3]

#: 每份 compose 里 backend 服务的构建上下文（相对 compose 文件所在目录）。
#: **写死是刻意的**：改它等于同时改 Dockerfile 里 COPY 的路径前缀，
#: 这种"两处必须一起改"的关系不该只靠人记。
BACKEND_CONTEXT = {
    "deploy/docker-compose.yml": "..",
    "deploy/nas/docker-compose.yml": "../src",
}


def _compose(relative: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{relative} 不是一份 YAML 映射"
    return loaded


@pytest.mark.parametrize("relative", sorted(BACKEND_CONTEXT))
def test_compose_files_are_valid_yaml_with_the_expected_backend_build(relative: str) -> None:
    """两份 compose 都是合法 YAML，且 backend 的构建入口指向**上下文根里的
    ``backend/Dockerfile``**（而不是把上下文缩到 ``backend/``——那样 skills/ 进不来）。"""
    build = _compose(relative)["services"]["backend"]["build"]

    assert build["context"] == BACKEND_CONTEXT[relative]
    assert build["dockerfile"] == "backend/Dockerfile"


def test_the_repo_root_context_actually_contains_what_the_dockerfile_copies() -> None:
    """上下文 = 仓库根那一份：Dockerfile 里四条"从上下文取文件"的 COPY，源都在。

    NAS 那份的 ``../src`` 只在服务器上存在（git archive 解开的源码树），
    在这儿无法验证——所以这条只查仓库根这一份，NAS 的靠构建时当场失败。
    """
    build = _compose("deploy/docker-compose.yml")["services"]["backend"]["build"]
    context = (ROOT / "deploy" / build["context"]).resolve()

    assert context == ROOT
    for relative in ("backend/pyproject.toml", "backend/uv.lock", "backend/README.md"):
        assert (context / relative).is_file(), relative
    assert (context / "backend" / "app").is_dir()
    assert (context / "skills").is_dir()
    assert (context / ".dockerignore").is_file()


def test_dockerfile_puts_the_bundled_skills_in_the_image_readably() -> None:
    """skills 的落点是 ``/app/skills``，用 ENV 显式覆盖，且**在 chmod 之前**。

    顺序不是风格问题：COPY 会把源文件的 mode 原样带进镜像，而这台 NAS 的
    ``/vol1`` 把文件报成 000——拷在 ``chmod -R a+rX /app`` 之后，那批文件在
    非 root 运行时就再也读不了了。
    """
    text = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    copy_line = "COPY --chown=kylab:kylab skills ./skills"
    assert copy_line in text
    assert "ENV KYLAB_SKILLS_DIR=/app/skills" in text
    # 找指令本身（"RUN chmod ..."）：上面那段说明里也引用了同一条命令，
    # 按裸字符串找会命中注释、把顺序断言变成永真
    assert text.index(copy_line) < text.index("RUN chmod -R a+rX /app")
