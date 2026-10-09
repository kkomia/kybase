"""技能源：从 GitHub 仓库浏览技能（v0.27）。

镜像同构：``app/services/skill_sources.py`` → 本文件。

这里**不打真网络**：用 ``httpx.MockTransport`` 造一个假 GitHub（仓库信息 + 文件树 +
raw 文件），要验的是四件事：

1. **一次树请求列出全仓技能**——各家的目录约定不同（``skills/``、``.github/skills/``、
   多级嵌套），所以扫描必须是"递归找 ``**/SKILL.md``"；
2. **缓存真的省下了配额**：第二次浏览不再打 GitHub（匿名 60 次/小时是真的会打爆）；
3. **装之前看得见清单**：文件列表带大小与"是不是代码"；
4. **按 SHA 取文件**，而且只认自己解析过的仓库地址（别的地方不碰）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError, UpstreamError
from app.services.skill_sources import (
    BUILTIN_SOURCES,
    SkillSourceService,
    parse_repo_reference,
)

SHA = "a" * 40


class _FakeGithub:
    """一个够用的假 GitHub：仓库信息、递归文件树、raw 单文件。"""

    def __init__(
        self,
        *,
        tree: list[dict[str, Any]],
        files: dict[str, str],
        repo: dict[str, Any] | None = None,
        status: dict[str, int] | None = None,
        headers: dict[str, dict[str, str]] | None = None,
        cdn_status: int = 200,
    ) -> None:
        self.tree = tree
        self.files = files
        self.repo = repo or {
            "default_branch": "main",
            "stargazers_count": 176799,
            "license": {"spdx_id": "Apache-2.0"},
        }
        self.status = status or {}
        self.headers = headers or {}
        #: jsDelivr 那条路的返回值。200 = 正常；403/404 = 那个仓库它不收
        #: （超过 50MB 时它确实会拒），用来验"会退到 raw"。
        self.cdn_status = cdn_status
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for needle, code in self.status.items():
            if needle in url:
                return httpx.Response(code, json={}, headers=self.headers.get(needle, {}))
        if url.startswith("https://api.github.com/repos/"):
            if "/git/trees/" in url:
                return httpx.Response(200, json={"sha": SHA, "tree": self.tree, "truncated": False})
            if "/commits/" in url:
                return httpx.Response(200, json={"sha": SHA})
            if "/contents/" in url:
                # contents 接口的"给我字节"那一档（Accept: application/vnd.github.raw）
                path = url.split("/contents/", 1)[1].split("?")[0]
                from urllib.parse import unquote

                body = self.files.get(unquote(path))
                if body is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, text=body, headers={"X-RateLimit-Remaining": "42"})
            return httpx.Response(200, json=self.repo)
        if url.startswith("https://cdn.jsdelivr.net/gh/"):
            if self.cdn_status != 200:
                # 真机上 jsDelivr 对没缓存的文件会 301 回 raw（实测 4 个里 3 个如此），
                # 这里照这个形状造：跨主机跳转**不该被跟随**
                return httpx.Response(
                    self.cdn_status,
                    headers={
                        "location": url.replace(
                            "https://cdn.jsdelivr.net/gh/", "https://raw.githubusercontent.com/"
                        ).replace("@", "/", 1)
                    },
                )
            _, _, rest = url.partition("/gh/")
            _, _, tail = rest.partition("@")
            path = tail.split("/", 1)[1] if "/" in tail else ""
            body = self.files.get(path)
            return (
                httpx.Response(200, text=body)
                if body is not None
                else httpx.Response(404, text="not found")
            )
        if url.startswith("https://raw.githubusercontent.com/"):
            tail = url.split(f"/{SHA}/", 1)[-1]
            body = self.files.get(tail)
            if body is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=body)
        return httpx.Response(404, text=f"没有这条路由：{url}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _tree_for(paths: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"path": path, "type": "blob", "size": len(text.encode())} for path, text in paths.items()
    ]


def _skill(name: str, description: str = "干某件事") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n"


def _service(tmp_path: Path, github: _FakeGithub, **kwargs: Any) -> SkillSourceService:
    return SkillSourceService(tmp_path / "data", client=github.client(), **kwargs)


# ------------------------------------------------------------------ 地址解析


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("anthropics/skills", ("anthropics/skills", "", "")),
        ("https://github.com/anthropics/skills", ("anthropics/skills", "", "")),
        ("https://github.com/anthropics/skills.git", ("anthropics/skills", "", "")),
        (
            "https://github.com/anthropics/skills/tree/main/skills",
            ("anthropics/skills", "main", "skills"),
        ),
        (
            "https://github.com/google/skills/tree/main/skills/cloud",
            ("google/skills", "main", "skills/cloud"),
        ),
        ("anthropics/skills#v1.2", ("anthropics/skills", "v1.2", "")),
        ("github.com/anthropics/skills", ("anthropics/skills", "", "")),
    ],
)
def test_repo_references_parse(text: str, expected: tuple[str, str, str]) -> None:
    """用户手上会有好几种写法：短名、仓库 URL、带分支与子目录的 URL。

    **不认"三段简写"**（``owner/repo/skills/pdf``）：调研里那条本身是未确认项，
    而它与"带子目录的完整 URL"表达的是同一件事——少一种写法比认错一个仓库好。
    """
    assert parse_repo_reference(text) == expected


@pytest.mark.parametrize("text", ["", "just-a-name", "https://gitlab.com/a/b", "owner/"])
def test_unusable_references_say_what_is_wrong(text: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_repo_reference(text)


# ------------------------------------------------------------------ 浏览


def test_browse_finds_every_skill_in_the_repo(tmp_path: Path) -> None:
    """**递归找 ``**/SKILL.md``**，而不是假定 ``skills/`` 是根目录。

    实测各家约定都不同（Google 是多级嵌套、Microsoft 在 ``.github/skills/``），
    这条用例的树里就混了三种位置。
    """
    files = {
        "skills/pdf/SKILL.md": _skill("pdf", "处理 PDF"),
        "skills/pptx/SKILL.md": _skill("pptx", "处理 PPT"),
        ".github/skills/azure/SKILL.md": _skill("azure", "Azure 相关"),
        "skills/cloud/bigquery/SKILL.md": _skill("bigquery", "查 BigQuery"),
        "README.md": "不是技能",
        "skills/pdf/reference.md": "参考",
    }
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    source, items = service.browse("owner-repo")

    assert source.repo == "owner/repo"
    assert [item.name for item in items] == ["azure", "bigquery", "pdf", "pptx"]
    assert items[2].description == "处理 PDF"
    assert items[2].path == "skills/pdf"


def test_browse_marks_what_is_already_installed(tmp_path: Path) -> None:
    """装过的在列表里要标出来——不然用户点进去才发现已经装过了。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo", installed=["PDF"])

    assert items[0].installed is True


def test_the_second_browse_does_not_touch_github(tmp_path: Path) -> None:
    """缓存落盘：第二次浏览一次出站都不发。

    GitHub 匿名配额是 60 次/小时/IP，而一次浏览要花 1 次仓库信息 + 1 次文件树 +
    N 次 raw——不缓存的话，逛两个仓库就把配额用掉一半。
    """
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    service.browse("owner-repo")
    first_round = len(github.calls)
    service.browse("owner-repo")

    assert len(github.calls) == first_round
    assert first_round >= 3  # 仓库信息 + 文件树 + SKILL.md


def test_refresh_ignores_the_cache(tmp_path: Path) -> None:
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    before = len(github.calls)

    service.browse("owner-repo", refresh=True)

    assert len(github.calls) > before


def test_the_cache_expires(tmp_path: Path) -> None:
    """TTL 到点就当没有缓存——否则"这个仓库后来加了技能"永远看不到。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github, ttl=60)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    path = service.cache_dir / "owner-repo.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fetched_ts"] = 0  # 假装是很久以前抓的
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = len(github.calls)

    service.browse("owner-repo")

    assert len(github.calls) > before


def test_a_repo_without_skills_is_empty_not_an_error(tmp_path: Path) -> None:
    """一个仓库里没有 SKILL.md：返回空清单。**不是错误**——
    "这个源里没有技能"与"这个源打不开"对用户是两件事。"""
    github = _FakeGithub(tree=_tree_for({"README.md": "hi"}), files={})
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo")

    assert items == []


def test_a_skill_without_frontmatter_falls_back_to_its_directory_name(tmp_path: Path) -> None:
    """某个 SKILL.md 读不出来（网络抖、编码坏）**不该让整次浏览失败**：
    退回目录名，那一行照样在，用户只是看不到说明。"""
    files = {
        "skills/pdf/SKILL.md": _skill("pdf", "正经技能"),
        "skills/broken/SKILL.md": "这里没有 frontmatter",
    }
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo")

    assert [item.name for item in items] == ["broken", "pdf"]
    assert items[0].description == ""
    assert items[1].description == "正经技能"


# ------------------------------------------------------------------ 清单与下载


def test_inspect_lists_files_with_kind_and_sha(tmp_path: Path) -> None:
    """装之前摊开的那一屏：文件、大小、是不是代码，以及这是哪个 commit。"""
    files = {
        "skills/pdf/SKILL.md": _skill("pdf"),
        "skills/pdf/scripts/fill.py": "print('x')",
        "skills/pdf/reference.md": "参考",
        "skills/pdf/logo.png": "PNG",
        "skills/pptx/SKILL.md": _skill("pptx"),
    }
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")

    bundle = service.inspect("owner-repo", "skills/pdf")

    assert bundle.sha == SHA
    assert bundle.name == "pdf"
    assert [item.path for item in bundle.files] == [
        "SKILL.md",
        "logo.png",
        "reference.md",
        "scripts/fill.py",
    ]
    kinds = {item.path: item.kind for item in bundle.files}
    assert kinds["scripts/fill.py"] == "code"
    assert kinds["SKILL.md"] == "doc"
    assert kinds["logo.png"] == "asset"
    assert bundle.total_bytes > 0
    assert bundle.code_count == 1
    # 别的技能的文件不能混进来
    assert all(not item.path.startswith("pptx") for item in bundle.files)


def test_inspect_of_an_unknown_path_is_404(tmp_path: Path) -> None:
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")

    with pytest.raises(NotFoundError):
        service.inspect("owner-repo", "skills/nope")


def test_download_fetches_by_sha_not_by_branch(tmp_path: Path) -> None:
    """**按 SHA 取**：分支会在"用户点安装"与"我们下载"之间变，
    而用户确认的是他看到的那个版本。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf"), "skills/pdf/notes.md": "笔记"}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/pdf")

    blobs = service.download(bundle)

    assert set(blobs) == {"SKILL.md", "notes.md"}
    assert b"pdf" in blobs["SKILL.md"]
    assert all(f"/{SHA}/" in url for url in github.calls if "raw.githubusercontent" in url)


def test_download_without_a_sha_refuses(tmp_path: Path) -> None:
    """没有 SHA 就不装：那意味着"装的是个会变的东西"，而用户没法复核。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/pdf")

    from dataclasses import replace

    with pytest.raises(UpstreamError):
        service.download(replace(bundle, sha=""))


# ------------------------------------------------------------------ 出站错误


def test_a_missing_repo_says_so(tmp_path: Path) -> None:
    github = _FakeGithub(tree=[], files={}, status={"api.github.com": 404})
    service = _service(tmp_path, github)
    service.add_source("owner/gone")

    with pytest.raises(NotFoundError):
        service.browse("owner-gone")


def test_exhausted_quota_tells_the_user_what_to_do(tmp_path: Path) -> None:
    """匿名配额用完**要说清怎么办**（等，或者配 token），而不是一句 HTTP 403。

    这一条是真会撞上的：调研当天在本机就撞到过 ``403 API rate limit exceeded``。
    """
    github = _FakeGithub(
        tree=[],
        files={},
        status={"api.github.com": 403},
        headers={"api.github.com": {"X-RateLimit-Remaining": "0"}},
    )
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    with pytest.raises(UpstreamError) as excinfo:
        service.browse("owner-repo")

    message = str(excinfo.value)
    assert "KYLAB_GITHUB_TOKEN" in message
    assert "60" in message


def test_a_file_over_the_cap_is_refused(tmp_path: Path) -> None:
    """单个文件的大小上限：挡住"仓库里塞了一个大文件"。"""
    files = {"skills/big/SKILL.md": _skill("big")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    # 树里报一个巨大的体积，下载前就该被拦住
    github.tree = [{"path": "skills/big/SKILL.md", "type": "blob", "size": 99_000_000}]
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/big")

    from dataclasses import replace

    from app.services.skill_sources import MAX_BUNDLE_BYTES, SkillFile

    huge = replace(
        bundle,
        files=(SkillFile(path="SKILL.md", size=1, kind="doc"),),
        total_bytes=MAX_BUNDLE_BYTES + 1,
    )
    with pytest.raises(InvalidRequestError):
        service.download(huge)


# ------------------------------------------------------------------ 源的增删改


def test_builtin_sources_are_listed_and_can_only_be_disabled() -> None:
    """内置源是一份我们审过的清单：**能停用，删不掉**。"""
    assert len(BUILTIN_SOURCES) >= 10
    assert sum(1 for item in BUILTIN_SOURCES if item.enabled) == 5


def test_adding_and_removing_a_custom_source(tmp_path: Path) -> None:
    github = _FakeGithub(tree=[], files={})
    service = _service(tmp_path, github)

    added = service.add_source("https://github.com/me/my-skills/tree/main/pack")

    assert added.repo == "me/my-skills"
    assert added.ref == "main"
    assert added.subpath == "pack"
    assert added.builtin is False
    assert any(item.id == added.id for item in service.list_sources())

    service.set_enabled(added.id, False)
    assert service.get_source(added.id).enabled is False

    service.remove_source(added.id)
    assert all(item.id != added.id for item in service.list_sources())


def test_builtin_sources_cannot_be_removed(tmp_path: Path) -> None:
    service = _service(tmp_path, _FakeGithub(tree=[], files={}))

    with pytest.raises(InvalidRequestError):
        service.remove_source("anthropic")

    service.set_enabled("anthropic", False)
    assert service.get_source("anthropic").enabled is False


def test_adding_the_same_repo_twice_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path, _FakeGithub(tree=[], files={}))
    service.add_source("me/one")

    with pytest.raises(InvalidRequestError):
        service.add_source("https://github.com/me/one")


def test_an_unknown_source_is_404(tmp_path: Path) -> None:
    service = _service(tmp_path, _FakeGithub(tree=[], files={}))

    with pytest.raises(NotFoundError):
        service.get_source("nope")


def test_files_come_from_the_cdn_first_then_the_api_then_raw(tmp_path: Path) -> None:
    """取一个文件有**三条路**，按顺序退：CDN → GitHub contents 接口 → raw。

    实测（2026-09-19，本机）三条路各自的样子：raw 多数请求被重置/超时；
    jsDelivr 快，**但它对没缓存的文件会 301 回 raw**（4 个文件里 3 个如此）；
    contents 接口（``Accept: application/vnd.github.raw``）8 个并发 0.8 秒全成，
    是这里唯一稳的那条——代价是花 API 配额，所以排在 CDN 后面。
    """
    files = {"skills/pdf/SKILL.md": _skill("pdf")}

    cdn = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path / "a", cdn)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/pdf")

    assert b"pdf" in service.download(bundle)["SKILL.md"]
    assert any("cdn.jsdelivr.net" in url for url in cdn.calls)
    assert not any("/contents/" in url for url in cdn.calls)

    # CDN 拒了（真机上它表现为 301 回 raw）→ 退到 contents 接口
    refused = _FakeGithub(tree=_tree_for(files), files=files, cdn_status=301)
    service2 = _service(tmp_path / "b", refused)
    service2.add_source("owner/repo")
    service2.browse("owner-repo")
    bundle2 = service2.inspect("owner-repo", "skills/pdf")

    assert b"pdf" in service2.download(bundle2)["SKILL.md"]
    assert any("/contents/" in url for url in refused.calls)
    assert not any("raw.githubusercontent" in url for url in refused.calls)


def test_the_api_route_is_skipped_when_the_quota_is_gone(tmp_path: Path) -> None:
    """配额用完就**别再往下试**：下一条只会更慢，而错误要说得清怎么办。

    安装是逐个文件取的，花的次数比浏览多——这是"配额"第一次真正会咬人的地方。
    """
    files = {"skills/pdf/SKILL.md": _skill("pdf")}
    github = _FakeGithub(
        tree=_tree_for(files),
        files=files,
        cdn_status=301,
        status={"/contents/": 403},
        headers={"/contents/": {"X-RateLimit-Remaining": "0"}},
    )
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/pdf")

    with pytest.raises(UpstreamError) as excinfo:
        service.download(bundle)

    assert "KYLAB_GITHUB_TOKEN" in str(excinfo.value)


def test_downloads_are_parallel(tmp_path: Path) -> None:
    """文件是并发取的：真实的技能包有几十个文件（pptx 那个 56 个），
    串行就是 56 个 RTT——实测 39 秒对 2 秒。"""
    files = {f"skills/pdf/part{i}.md": "内容" for i in range(12)}
    files["skills/pdf/SKILL.md"] = _skill("pdf")
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")
    service.browse("owner-repo")
    bundle = service.inspect("owner-repo", "skills/pdf")

    in_flight = 0
    peak = 0
    lock = threading.Lock()
    original = service._raw

    def slow_raw(repo: str, sha: str, path: str) -> bytes:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        try:
            return original(repo, sha, path)
        finally:
            with lock:
                in_flight -= 1

    service._raw = slow_raw  # type: ignore[method-assign]
    blobs = service.download(bundle)

    assert len(blobs) == 13
    assert peak > 1


# ------------------------------------------------- 中文简介（v0.28）


def test_browse_translates_descriptions_once_and_caches(tmp_path: Path) -> None:
    """浏览时把英文描述翻成中文简介，**一批只翻一次，结果进缓存**。

    技能生态里绝大多数描述是英文，而这一页是给中文用户看的。翻一次就写进
    缓存里：缓存的有效期（6 小时）正好是"这份清单还有效"的期限。
    """
    files = {
        "skills/pdf/SKILL.md": _skill("pdf", "Work with PDF files"),
        "skills/pptx/SKILL.md": _skill("pptx", "Work with slides"),
    }
    github = _FakeGithub(tree=_tree_for(files), files=files)
    calls: list[list[tuple[str, str]]] = []

    def fake_translator(items):  # type: ignore[no-untyped-def]
        calls.append(list(items))
        return {name: f"中文：{text}" for name, text in items}

    service = SkillSourceService(
        tmp_path / "data", client=github.client(), translator=fake_translator
    )
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo")
    _source, again = service.browse("owner-repo")

    assert [item.summary for item in items] == [
        "中文：Work with PDF files",
        "中文：Work with slides",
    ]
    # 第二次浏览走缓存，不再翻一遍
    assert len(calls) == 1
    assert [item.summary for item in again] == [item.summary for item in items]
    # **原文一个字都没改**：它仍然是模型的触发文本
    assert items[0].description == "Work with PDF files"


def test_without_a_translator_nothing_breaks(tmp_path: Path) -> None:
    """没接翻译（没配模型）时照常浏览，简介为空、界面显示原描述。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf", "Work with PDF files")}
    github = _FakeGithub(tree=_tree_for(files), files=files)
    service = _service(tmp_path, github)
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo")

    assert items[0].summary == ""
    assert items[0].description == "Work with PDF files"


def test_a_failing_translator_does_not_break_browsing(tmp_path: Path) -> None:
    """翻译抛异常时**浏览照常返回**（退回英文描述）——它是增强，不是依赖。"""
    files = {"skills/pdf/SKILL.md": _skill("pdf", "Work with PDF files")}
    github = _FakeGithub(tree=_tree_for(files), files=files)

    def boom(_items):  # type: ignore[no-untyped-def]
        raise RuntimeError("模型不可用")

    service = SkillSourceService(tmp_path / "data", client=github.client(), translator=boom)
    service.add_source("owner/repo")

    _source, items = service.browse("owner-repo")

    assert [item.name for item in items] == ["pdf"]
    assert items[0].summary == ""
