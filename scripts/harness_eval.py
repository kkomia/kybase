"""Harness 评分：把 §12.338 那批能力用例固化成**可重复、带 harness 名**的分数。

## 为什么要有它（Kimi 六步循环里我们唯一缺的那一步）

`agent-harness` 的六步是：任务接入 → 提示词组装 → 动作解析 → 执行与观察 → 控制流 →
**记录与评分**；读分三原则是**防数据污染**、**警惕古德哈特定律**、**必须附 harness 名**
（`docs/归档/调研/Kimi-Resources-能力与实现-照搬清单.md` 第 11 条，出处 L36-42/48-58/74-78）。
我们前五步都有（`isolation.py` / `prompt.py` / `tool_loop.py` 的拒执行、截断标注、
步数与墙钟），**第六步只有 `session_events` 的只追加日志**——于是"改 harness 有没有用"
不可度量（§12.338 那批是人工跑的）。这个脚本补的就是那一步。

## 三条原则落在哪

| 原则 | 落法 |
| --- | --- |
| **必须附 harness 名** | 每份记分卡带 `harness_id = <git 短哈希[-dirty]> + <配置摘要>`；摘要覆盖模型档 / 思考档 / 权限档 / `kb_ids` 与从 `tool_loop.py` 读出的步数与墙钟预算。**两份结果能不能比，先看这两串名字** |
| **防数据污染** | 每条用例**新建一条会话**、记分卡逐条写明会话 id；输出目录非空必须 `--force`（不默认盖上一轮） |
| **警惕古德哈特** | 分数只由**过程**算（步数 / 工具次数 / 是否报错 / 是否交付），**不评回答好坏**——评正确性会把模型能力混进 harness 分数。判据全在 `harness_cases.py`（改它＝改卷子） |

## 可重复性

`score_trace` 是**纯函数**（只读一份已落盘的轨迹，不读时钟、不碰磁盘），所以：

- `run` 结束把自己产出的轨迹**重评一遍**并与运行时评分**逐字节比对**（`_self_check`）；
- `verify --from <dir>` 把历史轨迹评两遍并与 `scorecard.json` 对齐。

**会变的是模型那一侧**（采样），靠 `--runs N` 出 min/中位/max 分布来标注——
不许把采样抖动当成 harness 改动的效果。

## 用法（详见 docs/归档/调研/harness-评分口径-v0.1.md）

    python scripts/harness_eval.py list                    # 看任务集与判据
    python scripts/harness_eval.py run  --label before      # 真跑（令牌：KYLAB_HARNESS_TOKEN / --token-file / .shots/.token）
    python scripts/harness_eval.py run  --label after --config thinking_effort=high
    python scripts/harness_eval.py compare .cache/harness/before .cache/harness/after
    python scripts/harness_eval.py verify --from .cache/harness/before   # 可重复性验收
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_cases import CASES, CASES_BY_ID, Case

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
DEFAULT_OUT = REPO / ".cache" / "harness"
DEFAULT_API = "http://127.0.0.1:8000/api/v1"
GIT = shutil.which("git") or "git"

#: 一轮提问最多等多久收完（与 `.shots/case-api-runner.cjs` 同一个量级）。
DEFAULT_TURN_TIMEOUT = 600.0

#: 参与配置摘要的项：`--config` 用的就是这些名字。`kb_ids` 也在这里——
#: 它决定"这一轮用不用知识库"，而那件事会改变工具选择与步数（§12.338 那批统一关掉它，
#: 不带进摘要的话"关库里跑"与"开库里跑"会顶着同一个 harness 名）。
CONFIG_KEYS: tuple[str, ...] = (
    "model_pk", "thinking", "thinking_effort", "permission", "agent_mode", "kb_ids",
)

#: 从 `tool_loop.py` 抠出来的预算常量。它们**不在任何配置里**，但改一个数字就换了 harness
#: （`CONVERGE_RATIO` 那条就是 §12.338 方案 3），所以也在摘要里。
BUDGET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("max_steps", r"^DEFAULT_MAX_STEPS\s*=\s*(\d+)"),
    ("max_seconds", r"^DEFAULT_MAX_SECONDS\s*=\s*([\d.]+)"),
    ("converge_ratio", r"^CONVERGE_RATIO\s*=\s*([\d.]+)"),
)


# ---------------------------------------------------------------- 评分（纯函数）


def score_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """一份轨迹 → 分数。**纯函数**：不读时钟、不碰磁盘、不看别的轨迹。

    "同一命令跑两次分数一致"就靠这一层：随机只可能来自模型（轨迹里的数）。
    """
    case = CASES_BY_ID.get(str(trace.get("case")))
    checks: dict[str, bool] = {
        name: bool(judge(trace)) for name, judge in (case.checks if case else ())
    }
    calls = [step for step in _all_steps(trace) if step.get("tool") and step.get("status") != "running"]
    tools = [str(step.get("tool")) for step in _all_steps(trace) if step.get("tool")]
    artifacts = [str(item.get("name") or "") for item in trace.get("artifacts", [])]
    durations = [int(turn.get("total_ms") or 0) for turn in trace.get("turns", [])]
    firsts = [turn.get("first_token_ms") for turn in trace.get("turns", []) if turn.get("first_token_ms")]
    return {
        "case": trace.get("case"),
        # **没有判据的用例不许判过**：空判据等于恒真（古德哈特那条原则的边角）
        "passed": bool(checks) and all(checks.values()),
        "checks": checks,
        "steps": len(calls),
        "tool_calls": len(tools),
        "tools": sorted(set(tools)),
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
        "errors": [turn["error"] for turn in trace.get("turns", []) if turn.get("error")],
        "total_ms": sum(durations),
        "first_token_ms": min(firsts) if firsts else None,
        "answer_chars": sum(int(turn.get("answer_chars") or 0) for turn in trace.get("turns", [])),
        "conversation_id": trace.get("conversation_id"),
    }


def _all_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for turn in trace.get("turns", []) for step in turn.get("steps", [])]


def _spread(values: list[int]) -> dict[str, Any]:
    """一个指标的分布。**随机列必须给分布**：单值会把抖动读成"没变"。"""
    if not values:
        return {"min": None, "median": None, "max": None}
    return {"min": min(values), "median": float(statistics.median(values)), "max": max(values)}


def scorecard(traces: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """一组轨迹 → 记分卡。**每次运行先单独评、再聚合**——先平均再判会把
    "两次里过一次"抹成"看起来过了"，而抖动正是要量的东西。"""
    by_case: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        by_case.setdefault(str(trace.get("case")), []).append(score_trace(trace))
    cases: list[dict[str, Any]] = []
    for case in CASES:
        runs = by_case.get(case.id, [])
        if not runs:
            continue
        cases.append(
            {
                "case": case.id,
                "why": case.why,
                "runs": len(runs),
                "pass_rate": round(sum(1 for run in runs if run["passed"]) / len(runs), 3),
                "steps": _spread([run["steps"] for run in runs]),
                "tool_calls": _spread([run["tool_calls"] for run in runs]),
                "total_ms": _spread([run["total_ms"] for run in runs]),
                "artifacts": _spread([run["artifact_count"] for run in runs]),
                "tools": sorted({tool for run in runs for tool in run["tools"]}),
                "failed_checks": sorted(
                    {name for run in runs for name, ok in run["checks"].items() if not ok}
                ),
                "conversation_ids": [run["conversation_id"] for run in runs],
            }
        )
    runs_total = sum(item["runs"] for item in cases)
    passed = sum(round(item["pass_rate"] * item["runs"]) for item in cases)
    return {
        "harness_id": meta.get("harness_id"),
        "harness": meta.get("harness"),
        "config": meta.get("config"),
        "label": meta.get("label"),
        "ran_at": meta.get("ran_at"),
        "case_count": len(cases),
        "run_count": runs_total,
        "pass_rate": round(passed / runs_total, 3) if runs_total else None,
        "cases": cases,
    }


# ---------------------------------------------------------------- harness 名


def _git_revision() -> str:
    """短哈希 + **脏标记**：工作树改过而没提交时，哈希本身不再说明版本。"""
    try:
        head = subprocess.run(
            [GIT, "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
        ).stdout.strip()
        dirty = subprocess.run(
            [GIT, "status", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{head or 'unknown'}{'-dirty' if dirty else ''}"


def _budget_digest() -> dict[str, str]:
    try:
        text = (BACKEND / "app" / "services" / "tool_loop.py").read_text(encoding="utf-8")
    except OSError:
        return {name: "" for name, _ in BUDGET_PATTERNS}
    found: dict[str, str] = {}
    for name, pattern in BUDGET_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE)
        found[name] = match.group(1) if match else ""
    return found


def config_digest(config: dict[str, Any]) -> dict[str, Any]:
    """这一轮跑在什么配置下 + 它的摘要。**摘要只用来"一眼看出不是同一个"**，
    每一项的值也存下来——人要能读到改的是什么，不能只看见一串哈希。"""
    facts: dict[str, Any] = {key: config.get(key) for key in CONFIG_KEYS}
    facts.update(_budget_digest())
    facts["api"] = config.get("api")
    facts["digest"] = hashlib.sha1(
        json.dumps(facts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    return facts


def harness_id(config: dict[str, Any]) -> dict[str, str]:
    """harness 名 = git 版本 + 配置摘要。**两份记分卡能不能比先看它。**"""
    revision = _git_revision()
    return {
        "revision": revision,
        "config_digest": config_digest(config)["digest"],
        "harness_id": f"{revision}+{config_digest(config)['digest']}",
    }


# ---------------------------------------------------------------- 真跑（SSE）


class Runner:
    """建会话 → 发提问 → 收完 SSE → 读交付物。"""

    def __init__(self, api: str, token: str, config: dict[str, Any], timeout: float) -> None:
        self.api, self.token, self.config, self.timeout = api, token, config, timeout

    def _json(self, path: str, payload: dict[str, Any] | None = None, *, method: str = "POST") -> Any:
        request = urllib.request.Request(
            f"{self.api}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}") from exc

    def new_conversation(self) -> str:
        """**每条用例一条新会话**——防数据污染的第一件事。"""
        created = self._json("/conversations", {"kb_ids": [], "model_pk": None, "workspace_id": None})
        return str(created["id"])

    def turn(self, conversation_id: str, prompt: str) -> dict[str, Any]:
        """一条提问：收完整的 SSE，记下每一步与每一次工具调用。"""
        profile = {key: self.config.get(key) for key in ("thinking", "thinking_effort", "model_pk")}
        body = {
            "query": prompt,
            "kb_ids": self.config.get("kb_ids") or [],
            "conversation_id": conversation_id,
            **{key: value for key, value in profile.items() if value is not None},
        }
        request = urllib.request.Request(
            f"{self.api}/chat/stream",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        started, steps, answer, first, error, completed = time.monotonic(), [], "", None, None, False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    elapsed = int((time.monotonic() - started) * 1000)
                    kind = event.get("type")
                    if kind == "delta":
                        answer += event.get("text") or ""
                        first = first if first is not None else elapsed
                    elif kind == "step":
                        steps.append(
                            {
                                "phase": event.get("phase"),
                                "label": event.get("label"),
                                "detail": (event.get("detail") or "")[:300],
                                "status": event.get("status"),
                                "tool": event.get("tool") or "",
                                "kind": event.get("kind") or "",
                                "outcome": event.get("outcome") or "",
                                "artifacts": [i.get("name") for i in event.get("artifacts") or []],
                            }
                        )
                    elif kind == "done":
                        answer, completed = event.get("answer") or answer, True
                    elif kind == "error":
                        error = event.get("message") or "后端报错（无 message）"
        except Exception as exc:  # noqa: BLE001 - 探针要把任何失败都记成这一轮的结果
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return {
            "prompt": prompt, "steps": steps, "answer": answer, "answer_chars": len(answer),
            "first_token_ms": first, "total_ms": int((time.monotonic() - started) * 1000),
            "error": error, "completed": completed,
        }

    def artifacts(self, conversation_id: str) -> list[dict[str, Any]]:
        listed = self._json(f"/conversations/{conversation_id}/artifacts", method="GET")
        items = listed.get("items") if isinstance(listed, dict) else listed
        return [
            {"name": item.get("name"), "kind": item.get("kind"), "size_bytes": item.get("size_bytes")}
            for item in (items or [])
        ]

    def run_case(self, case: Case) -> dict[str, Any]:
        conversation = self.new_conversation()
        turns = [self.turn(conversation, prompt) for prompt in case.prompts]
        try:
            files = self.artifacts(conversation)
        except Exception as exc:  # noqa: BLE001
            files = []
            turns[-1]["error"] = turns[-1].get("error") or f"读交付物失败：{exc}"
        return {
            "case": case.id,
            "why": case.why,
            "conversation_id": conversation,
            "harness": self.config.get("harness_id"),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "turns": turns,
            "artifacts": files,
        }


# ---------------------------------------------------------------- 输出


def _load_traces(directory: Path) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                traces.append(json.loads(line))
    return traces


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def render_markdown(card: dict[str, Any]) -> str:
    lines = [
        f"# Harness 记分卡：{card.get('label')}",
        "",
        f"- **harness 名**：`{card.get('harness_id')}`（git 版本 + 配置摘要）",
        (
            f"- 跑于：{card.get('ran_at')}；通过率 **{card.get('pass_rate')}**"
            f"（{card.get('case_count')} 条用例 / {card.get('run_count')} 次运行）"
        ),
        f"- 配置：`{json.dumps(card.get('config') or {}, ensure_ascii=False)}`",
        "",
        "| 用例 | 通过率 | 步数 min/中位/max | 工具次数 min/中位/max | 耗时s(中位) | 交付物 | 没过的判据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in card["cases"]:

        def spread(key: str, row: dict[str, Any] = item) -> str:
            """默认参数把 row 绑住：写成闭包会被 B023 抓（循环变量在定义之后才读）。"""
            return f"{row[key]['min']}/{row[key]['median']}/{row[key]['max']}"

        lines.append(
            f"| {item['case']} | {item['pass_rate']} | {spread('steps')} | {spread('tool_calls')} "
            f"| {(item['total_ms']['median'] or 0) / 1000:.1f} | {item['artifacts']['median']} "
            f"| {'、'.join(item['failed_checks']) or '—'} |"
        )
    lines += ["", "> 每条用例跑在哪条会话上（防数据污染要能追）："]
    lines += [f"> - {item['case']}：{'、'.join(item['conversation_ids'])}" for item in card["cases"]]
    return "\n".join(lines) + "\n"


def write_scorecard(out_dir: Path, card: dict[str, Any]) -> None:
    (out_dir / "scorecard.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "scorecard.md").write_text(render_markdown(card), encoding="utf-8")


def print_table(card: dict[str, Any]) -> None:
    print(f"\nharness 名：{card.get('harness_id')}")
    print(f"通过率：{card.get('pass_rate')}（{card.get('case_count')} 条 / {card.get('run_count')} 次运行）")
    print(f"{'用例':<8}{'通过率':<8}{'步数中位':<10}{'工具':<6}{'耗时s':<8}{'交付物':<8}没过的判据")
    for item in card["cases"]:
        print(
            f"{item['case']:<8}{item['pass_rate']:<8}{item['steps']['median']!s:<10}"
            f"{item['tool_calls']['median']:<6}{(item['total_ms']['median'] or 0) / 1000:<8.1f}"
            f"{item['artifacts']['median']:<8}{'、'.join(item['failed_checks'])}"
        )


# ---------------------------------------------------------------- 子命令


def _self_check(traces: list, live: list, out_dir: Path) -> None:
    """把**落盘的轨迹**重评一遍，与运行时那份逐字节比对。

    评分层混进时间/顺序/随机的话，这里当场抛——**不一致要报出来而不是修掉**。
    """
    rescored = [score_trace(trace) for trace in _load_traces(out_dir)]
    if json.dumps(rescored, sort_keys=True) != json.dumps(live, sort_keys=True):
        raise SystemExit("评分不可重复：同一批轨迹重评的结果与运行时不一致（score_trace 里有随机）")


def _config_facts(args: argparse.Namespace) -> dict[str, Any]:
    """这一轮的配置事实。**`kb_ids` 缺省必须是空列表而不是 None**：
    实测 `--config` 没给这一项时 dict 里是 `None`，而请求体里 `kb_ids: None` 与 `[]`
    语义不同（一个是"没说"、一个是"关掉知识库"），记分卡里也会印成 `null`。"""
    facts: dict[str, Any] = {"api": args.api, **{k: args.config.get(k) for k in CONFIG_KEYS}}
    facts["kb_ids"] = args.config.get("kb_ids") or []
    return facts


def command_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else DEFAULT_OUT / args.label
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        print(f"目录非空：{out_dir}（要覆盖加 --force）——防数据污染：不默认盖上一轮")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    token = resolve_token(args)
    if not token:
        print("没有令牌：设 KYLAB_HARNESS_TOKEN，或 --token-file，或放一份 .shots/.token")
        return 2

    config: dict[str, Any] = _config_facts(args)
    config.update(harness_id(config))
    selected = [CASES_BY_ID[i] for i in args.cases.split(",") if i in CASES_BY_ID] or list(CASES)
    runner = Runner(args.api, token, config, args.turn_timeout)

    traces: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    for run_index in range(args.runs):
        for case in selected:
            print(f"[{case.id} run {run_index + 1}/{args.runs}] 跑 …", flush=True)
            trace = runner.run_case(case)
            traces.append(trace)
            live.append(score_trace(trace))
            last = live[-1]
            print(
                f"  {'过' if last['passed'] else '不过'}  步数={last['steps']} "
                f"工具={last['tool_calls']} 交付物={last['artifact_count']} "
                f"耗时={last['total_ms'] / 1000:.1f}s"
                + (f"  错误={last['errors'][:1]}" if last["errors"] else ""),
                flush=True,
            )
    (out_dir / "traces.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in traces) + "\n", encoding="utf-8"
    )
    card = scorecard(
        traces,
        {
            "harness_id": config["harness_id"], "harness": harness_id(config),
            "config": config, "label": args.label, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    _self_check(traces, live, out_dir)
    write_scorecard(out_dir, card)
    print(f"\n记分卡：{out_dir / 'scorecard.json'}（harness {config['harness_id']}）")
    print_table(card)
    return 0


def command_score(args: argparse.Namespace) -> int:
    directory = Path(args.from_dir)
    traces = _load_traces(directory)
    if not traces:
        print(f"{directory} 里没有 *.jsonl 轨迹")
        return 2
    previous = _read_json(directory / "scorecard.json") or {}
    card = scorecard(
        traces,
        {
            "harness_id": previous.get("harness_id", "unknown"), "harness": previous.get("harness"),
            "config": previous.get("config"), "label": args.label or previous.get("label") or directory.name,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    write_scorecard(directory, card)
    print_table(card)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    directory = Path(args.from_dir)
    traces = _load_traces(directory)
    if not traces:
        print(f"{directory} 里没有 *.jsonl 轨迹")
        return 2
    first = [score_trace(trace) for trace in traces]
    second = [score_trace(trace) for trace in traces]
    same = json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    card = scorecard(traces, {"label": directory.name})
    stored = _read_json(directory / "scorecard.json")
    matches = stored is None or json.dumps(
        [item["pass_rate"] for item in card["cases"]], sort_keys=True
    ) == json.dumps([item["pass_rate"] for item in stored.get("cases", [])], sort_keys=True)
    print(f"同一批轨迹评两遍一致：{same}")
    print(f"与落盘记分卡的通过率一致：{matches}")
    if not same or not matches:
        print("不一致 → 评分层不是纯函数，或记分卡与轨迹不同源")
        return 1
    print(f"轨迹 {len(traces)} 份，逐条通过率：")
    for item in card["cases"]:
        print(f"  {item['case']}: {item['pass_rate']}")
    return 0


def _cell(item: dict[str, Any] | None) -> str:
    if item is None:
        return "—"
    return (
        f"{item['pass_rate']} / {item['steps']['median']} / {item['tool_calls']['median']} / "
        f"{(item['total_ms']['median'] or 0) / 1000:.1f} / {item['artifacts']['median']}"
    )


def _describe_delta(before: dict[str, Any], after: dict[str, Any]) -> str:
    """只报能比的那几样，**不把采样抖动说成改进**。"""
    parts: list[str] = []
    for key, label in (
        ("pass_rate", "通过率"), ("tool_calls", "工具"), ("artifacts", "交付物"), ("steps", "步数"),
    ):
        left = before[key] if key == "pass_rate" else before[key]["median"]
        right = after[key] if key == "pass_rate" else after[key]["median"]
        if left != right:
            parts.append(f"{label} {left}→{right}")
    return "；".join(parts) or "无差异"


def command_compare(args: argparse.Namespace) -> int:
    left, right = _read_json(Path(args.left) / "scorecard.json"), _read_json(
        Path(args.right) / "scorecard.json"
    )
    if not left or not right:
        print("两边都要有 scorecard.json（先 run / score）")
        return 2
    lines = [
        f"# Harness 对照：{left.get('label')} → {right.get('label')}",
        "",
        "| | 改前 | 改后 |",
        "| --- | --- | --- |",
        f"| harness 名 | `{left.get('harness_id')}` | `{right.get('harness_id')}` |",
        f"| 通过率 | {left.get('pass_rate')} | {right.get('pass_rate')} |",
        "",
        "| 用例 | 改前 通过/步数/工具/耗时s/交付物 | 改后 通过/步数/工具/耗时s/交付物 | 变化 |",
        "| --- | --- | --- | --- |",
    ]
    left_cases = {item["case"]: item for item in left.get("cases", [])}
    right_cases = {item["case"]: item for item in right.get("cases", [])}
    order = {case.id: index for index, case in enumerate(CASES)}
    # **并集去重**：`[*a, *b]` 在两边键相同时会把同一行印两遍（两边的用例本来就该一样）
    for case_id in sorted(set(left_cases) | set(right_cases), key=lambda cid: order.get(cid, 99)):
        if case_id in left_cases and case_id in right_cases:
            before, after = left_cases[case_id], right_cases[case_id]
            delta = _describe_delta(before, after)
        elif case_id in right_cases:
            before, after, delta = None, right_cases[case_id], "只跑了改后"
        else:
            before, after, delta = left_cases[case_id], None, "只跑了改前"
        lines.append(f"| {case_id} | {_cell(before)} | {_cell(after)} | {delta} |")
    lines += [
        "",
        "## 怎么读这张表",
        "",
        "- **通过率**按**每次运行**算（`--runs N` 时是 N 次的通过率），不是按用例算；",
        (
            "- 单次运行的**步数 / 耗时**是**随机列**（同一 harness 两次也会不同）——"
            "`--runs` 大于 1 时表里给的是中位数，`scorecard.json` 里有 min/中位/max 三档；"
        ),
        "- **工具次数与交付物**才是**harness 改了什么**最稳的读数（它们由判据管着，不由采样管着）；",
        (
            "- 两个 `harness 名` 不同才谈得上比较；相同就说明**配置与代码都没变**，"
            "那两份分数只差在采样上。"
        ),
    ]
    text = "\n".join(lines)
    (Path(args.right) / "compare.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def command_list(args: argparse.Namespace) -> int:
    """看任务集与判据（**卷子要能被人读**——古德哈特那条原则的落点）。"""
    for case in CASES:
        print(f"\n{case.id}  {case.why}")
        for prompt in case.prompts:
            print(f"  提问：{prompt[:100]}{'…' if len(prompt) > 100 else ''}")
        for name, _ in case.checks:
            print(f"  判据：{name}")
    return 0


def command_identity(args: argparse.Namespace) -> int:
    config = _config_facts(args)
    print(json.dumps(
        {"identity": harness_id(config), "config": config_digest(config)}, ensure_ascii=False, indent=1
    ))
    return 0


def resolve_token(args: argparse.Namespace) -> str:
    """令牌三档：环境变量 → `--token-file` → `.shots/.token`（与既有探针同一份）。"""
    if os.environ.get("KYLAB_HARNESS_TOKEN"):
        return os.environ["KYLAB_HARNESS_TOKEN"].strip()
    candidate = Path(args.token_file) if args.token_file else REPO / ".shots" / ".token"
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _config_from_argv(parser: argparse.ArgumentParser, argv: list[str] | None) -> dict[str, str]:
    """从**命令行本身**扫 `--config k=v`，而不是从 argparse 的命名空间取。

    为什么不走 argparse：`--config` 在顶层与子命令上都有时，**后解析到的那份默认值 `[]`
    会把先解析到的值盖掉**（argparse 对同名 dest 是覆盖、不是合并）。实测就是这个形状：
    `--config x=1 identity` 里的 `x=1` 凭空消失。位置怎么写都扫一遍，也就只有一处判定。
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    values: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] == "--config":
            if index + 1 >= len(tokens):
                parser.error("--config 后面要跟 key=value")
            values.append(tokens[index + 1])
            index += 2
            continue
        if tokens[index].startswith("--config="):
            values.append(tokens[index].split("=", 1)[1])
        index += 1
    parsed: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not key.strip() or not sep:
            parser.error(f"--config 要写成 key=value（收到 {item!r}）")
        parsed[key.strip()] = value.strip()
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness 评分（见 docs/归档/调研/harness-评分口径-v0.1.md）")
    parser.add_argument("--api", default=os.environ.get("KYLAB_HARNESS_API", DEFAULT_API))
    # 顶层也挂一个（只为让 `--config … <子命令>` 这种写法**能被 argparse 接受**）：
    # 真正的取值由 `_config_from_argv` 从 argv 里扫，不读这个命名空间（见那里的理由）。
    parser.add_argument("--config", action="append", default=[], help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    def _sub(name: str, help_text: str) -> argparse.ArgumentParser:
        """建子命令并挂上 `--config`（**两处都挂**：`run --config …` 是直觉写法，
        只在顶层挂的话那样写会被判成"无法识别的参数"——实测第一次跑就踩了）。"""
        created = sub.add_parser(name, help=help_text)
        created.add_argument(
            "--config", action="append", default=[], help="覆盖一项配置（可重复）：--config thinking_effort=low"
        )
        return created

    run = _sub("run", "真跑一遍并出记分卡")
    run.add_argument("--label", default="local")
    run.add_argument("--out", default="")
    run.add_argument("--runs", type=int, default=1, help="每条用例跑几次（测抖动）")
    run.add_argument("--cases", default="", help="只跑这几条，逗号分隔")
    run.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT)
    run.add_argument("--token-file", default="")
    run.add_argument("--force", action="store_true", help="允许覆盖非空输出目录")
    run.set_defaults(func=command_run)

    score = _sub("score", "只重算（读轨迹目录，不跑模型）")
    score.add_argument("--from", dest="from_dir", required=True)
    score.add_argument("--label", default="")
    score.set_defaults(func=command_score)

    verify = _sub("verify", "可重复性验收（同批轨迹评两遍）")
    verify.add_argument("--from", dest="from_dir", required=True)
    verify.set_defaults(func=command_verify)

    compare = _sub("compare", "两份记分卡出对照表")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.set_defaults(func=command_compare)

    _sub("list", "看任务集与判据").set_defaults(func=command_list)
    _sub("identity", "只打印 harness 名").set_defaults(func=command_identity)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = _config_from_argv(parser, argv)  # 见 `_config_from_argv` 的理由
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
