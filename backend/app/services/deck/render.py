"""幻灯片写盘层·Python 桥：``DeckPlan`` → ``node scripts/deck/render.mjs`` → ``.pptx``。

**为什么写盘层是 Node 而不是 Python。** 版式、令牌、溢出判定全在 Python 侧的四层里
（`theme_tokens` / `layouts` / `spec` / `content_map`），它们只描述几何与决策，
一行排版库都不 import——所以后端保持零新增依赖。真正把矩形落成 OOXML 的那一步交给
PptxGenJS：它是这一档里唯一实测能写出**原生图表 + 页码字段 + 母版**的办法
（`office.build_pptx` 那条 python-pptx 路径只写文字，见上一轮的结论）。
把 Node 依赖写进 `pyproject.toml` 是另一回事——那是"后端要装 Node"，本轮明确不做。

**桥的职责只有三件**：把计划序列化成 JSON 交给 Node、把 Node 的失败翻译成人话、
把成功摘要带回 Python。接口是 ``node scripts/deck/render.mjs`` 的命令行契约
（见该文件的头注释），其中退出码是**唯一的机器可读信号**：

===========  ==========================================
退出码        含义（翻成给用户/模型看的一句话）
===========  ==========================================
2            计划不合法：第几页哪个槽的问题
3            写盘失败：路径不可写、图表后处理失败
4            缺 Node 依赖：在哪里 ``pnpm install``
其它         渲染器自己崩了（带原始 stderr）
===========  ==========================================

**没有 Node 时降级成一句人话**，照 `app/services/office.py` 那套"可选依赖 + 人话降级"
的处置：``node_requirement()`` 返回缺什么、怎么装，``render_deck`` 把它抛成
:class:`DeckRenderError`——工具层拿到的是"这件事这台机器上做不到、原因是这个"，
而不是把 ``FileNotFoundError`` 翻成"服务内部错误"。

命令行（开发期手工核对入口，与 `content_map._main` 同类）::

    python -m app.services.deck.render plan.json out.pptx
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from app.services.deck.content_map import DeckPlan

__all__ = [
    "NODE_ENV_HINT",
    "RENDER_SCRIPT_REL",
    "TIMEOUT_S",
    "DeckRenderError",
    "RenderResult",
    "find_node",
    "node_requirement",
    "plan_dict",
    "plan_json",
    "render_deck",
    "render_script",
]

#: 渲染脚本（仓库相对路径）。**它是工程脚本，不进 wheel**：打包给 sidecar 补 Node
#: 运行时是另一件事（另立项），所以这里按仓库布局定位，而不是按安装目录。
RENDER_SCRIPT_REL: Final[str] = "scripts/deck/render.mjs"

#: 找 node 的顺序：环境变量 > PATH。桌面壳将来带自己的运行时，就用这个变量指过去。
NODE_ENV_HINT: Final[str] = "KYLAB_NODE"

#: 一条计划最多渲染多久。60 页带图表也就几秒；超了说明进程卡住了，
#: 与其让工具调用悬着，不如报一句"超时"让人知道。
TIMEOUT_S: Final[float] = 180.0

#: 退出码 → 人话前缀。**按码判断而不是按 stderr 措辞**：措辞会改，码是契约。
_FAILURE_LABELS: Final[dict[int, str]] = {
    2: "计划不合法",
    3: "写盘失败",
    4: "缺少 Node 依赖",
}

#: 成功摘要行的前缀（stdout 末行）。
_OK_PREFIX: Final[str] = "KYLAB_DECK_OK "

PlanInput = DeckPlan | Mapping[str, Any] | str


class DeckRenderError(RuntimeError):
    """渲染没做成。``message`` 是给人（也给模型）读的一句话，可直接回给上层。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class RenderResult:
    """一次渲染的结果摘要（来自 Node 的成功摘要行，不是猜的）。"""

    path: Path
    pages: int
    layouts: tuple[str, ...] = ()
    charts: int = 0
    chart_parts: int = 0
    media: int = 0
    warnings: tuple[str, ...] = ()
    patch: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    pruned: tuple[str, ...] = ()

    def summary(self) -> str:
        """给人看的一行（验收与排查都读它）。"""
        lines = [
            f"已写出 {self.path}（{self.pages} 页，版式 {' → '.join(self.layouts)}）",
            f"原生图表 {self.charts} 张 / 图表部件 {self.chart_parts} 个 / 图片 {self.media} 张",
        ]
        for item in self.patch:
            lines.append(
                "  图表字体：{part} 的 a:latin {latin} 处，a:ea {before} → {after} 处".format(
                    part=item.get("part", "?"),
                    latin=item.get("latin", 0),
                    before=item.get("ea_before", 0),
                    after=item.get("ea_after", 0),
                )
            )
        if self.pruned:
            lines.append(
                f"  清掉 {len(self.pruned)} 条悬空的内容类型声明"
                "（PptxGenJS 每调一次 defineSlideMaster 就声明一个母版部件，但只写出第一个）"
            )
        lines.extend(f"  ! {note}" for note in self.warnings)
        return "\n".join(lines)


def _repo_root() -> Path:
    """仓库根：``backend/app/services/deck/render.py`` 往上四层。"""
    return Path(__file__).resolve().parents[4]


def render_script() -> Path:
    """渲染脚本的位置。找不到时由 :func:`node_requirement` 说清"是仓库布局变了"。"""
    return _repo_root() / RENDER_SCRIPT_REL


def find_node() -> str | None:
    """找 node：先看 ``KYLAB_NODE``（桌面壳将来带自己的运行时），再走 PATH。

    单独拆成函数是为了**能被打桩**：测试要证明"没有 Node 时是一句人话"，
    不必真去改 PATH（改 PATH 会影响同进程里别的东西）。
    """
    override = os.environ.get(NODE_ENV_HINT, "").strip()
    if override and Path(override).is_file():
        return override
    return shutil.which("node")


def node_requirement() -> str:
    """缺什么、怎么装；齐了就回空串（与 `office.missing_requirement` 同一手感）。

    **不抛异常**：调用方（工具层）需要的是"这件事现在做不到、原因是这个"，
    而它在同一句话里还要拼上"所以给你 Markdown 版"之类的兜底说明。
    """
    if find_node() is None:
        return (
            "生成 PPT 需要本机有 Node.js（写盘层跑的是 `scripts/deck/render.mjs`）："
            "装一个 Node 20 以上并让它进 PATH，或者用 `KYLAB_NODE` 指向 node 可执行文件"
        )
    if not render_script().is_file():
        return (
            f"生成 PPT 需要写盘层脚本 {RENDER_SCRIPT_REL}："
            "当前这份代码里找不到它（不是完整的仓库检出）"
        )
    return ""


def plan_dict(plan: PlanInput) -> dict[str, Any]:
    """计划 → 普通字典（**写盘层唯一的输入形状**，就是 ``DeckPlan.to_dict()``）。"""
    if isinstance(plan, DeckPlan):
        return plan.to_dict()
    if isinstance(plan, str):
        try:
            parsed = json.loads(plan)
        except json.JSONDecodeError as exc:
            raise DeckRenderError(f"计划不是合法 JSON：{exc}") from None
    elif isinstance(plan, Mapping):
        parsed = dict(plan)
    else:
        raise DeckRenderError(
            f"计划要是一个 DeckPlan、一份 plan 字典或一段 JSON 文本（收到 {type(plan).__name__}）"
        )
    if not isinstance(parsed, Mapping):
        raise DeckRenderError(f"计划要是一个对象（收到 {type(parsed).__name__}）")
    return dict(parsed)


def plan_json(plan: PlanInput) -> str:
    """计划 → JSON 文本，交给 ``render.mjs`` 的 stdin。"""
    return json.dumps(plan_dict(plan), ensure_ascii=False, indent=2, default=str)


def _failure_detail(stderr: str, label: str) -> str:
    """从 stderr 里挑那句人话：跳过我们自己打的 `[渲染警告]`，取最后一条真错误。

    ``label`` 是这类失败的中文名（按退出码给的）。渲染器自己的话往往以同一个词开头
    （"计划不合法：…"），所以这里把重复的那一小段削掉——拼出来是
    "渲染 PPT 失败（计划不合法）：第 1 页的「title」槽缺 rect_in"，而不是同一个词说两遍。
    """
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.startswith("[渲染警告]")
    ]
    if not lines:
        return "渲染器没有给出错误说明"
    detail = lines[-1]
    for prefix in (f"{label}：", f"{label}:", f"{label} ", label):
        if detail.startswith(prefix):
            return detail[len(prefix) :].strip() or detail
    return detail


def render_deck(
    plan: PlanInput,
    out_path: str | Path,
    *,
    base_dir: str | Path | None = None,
    timeout_s: float = TIMEOUT_S,
) -> RenderResult:
    """渲染一份计划。**失败一律抛 :class:`DeckRenderError`，消息是人话。**

    ``base_dir`` 是计划里**相对路径的基准目录**（本地图片的 ``path``）：不传就用当前
    工作目录。绝对路径不受影响。
    """
    problem = node_requirement()
    if problem:
        raise DeckRenderError(problem)

    node = find_node()
    if node is None:  # node_requirement 刚查过；这里再判一次是为了把类型收窄
        raise DeckRenderError(node_requirement() or "找不到 node")
    target = Path(out_path).resolve()
    command = [
        node,
        str(render_script()),
        "--plan",
        "-",
        "--out",
        str(target),
    ]
    if base_dir is not None:
        command += ["--base-dir", str(Path(base_dir).resolve())]

    try:
        finished = subprocess.run(  # noqa: S603 - 数组调用、不走 shell，参数是我们自己拼的
            command,
            input=plan_json(plan),
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            # 桌面壳里没有控制台，不设这个标志的话每次渲染都会闪一个黑框。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeckRenderError(
            f"渲染 PPT 超时（{timeout_s:.0f} 秒没有结束）：计划 {target.name} 太大或渲染器卡住了"
        ) from exc
    except OSError as exc:
        raise DeckRenderError(f"起不了渲染进程（{node}）：{exc}") from exc

    if finished.returncode != 0:
        label = _FAILURE_LABELS.get(finished.returncode, "渲染器出错")
        raise DeckRenderError(
            f"渲染 PPT 失败（{label}）：{_failure_detail(finished.stderr, label)}"
        )

    payload = _read_report(finished.stdout)
    return RenderResult(
        path=Path(str(payload.get("out") or target)),
        pages=int(payload.get("pages") or 0),
        layouts=tuple(str(item) for item in payload.get("layouts") or ()),
        charts=int(payload.get("charts") or 0),
        chart_parts=int(payload.get("chart_parts") or 0),
        media=int(payload.get("media_added") or 0),
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        patch=tuple(dict(item) for item in payload.get("chart_font_patch") or ()),
        pruned=tuple(str(item) for item in payload.get("pruned_overrides") or ()),
    )


def _read_report(stdout: str) -> dict[str, Any]:
    """取成功摘要行。没有它说明渲染器改了输出契约——报出来，别猜。"""
    for line in reversed(stdout.splitlines()):
        if line.startswith(_OK_PREFIX):
            try:
                return dict(json.loads(line[len(_OK_PREFIX) :]))
            except json.JSONDecodeError as exc:
                raise DeckRenderError(f"渲染器返回的摘要不是合法 JSON：{exc}") from None
    raise DeckRenderError("渲染器没有返回结果摘要（它可能被升级成了不兼容的版本）")


def render_requirement() -> str:
    """``node_requirement`` 的别名：与 `office.missing_requirement(kind)` 同一手感。"""
    return node_requirement()


def _main(argv: list[str]) -> int:  # pragma: no cover - 开发期手工核对入口
    import sys

    if len(argv) < 3:
        print("用法：python -m app.services.deck.render <plan.json> <out.pptx>", file=sys.stderr)
        return 2
    plan = Path(argv[1]).read_text(encoding="utf-8")
    try:
        result = render_deck(plan, argv[2], base_dir=Path(argv[1]).resolve().parent)
    except DeckRenderError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print(result.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv))
