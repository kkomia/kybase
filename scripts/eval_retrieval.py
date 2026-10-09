"""检索质量跑分：按金标集给检索打分。

用法（在 backend/ 下）：

```
# 1) 从一个知识库的语料生成金标集草稿（问题由已有模型按语料提出），人工填好"应该命中谁"
uv run python ../scripts/eval_retrieval.py suggest --kb kb_xxx --token <会话令牌> -o golden.jsonl

# 2) 打分（默认跑 hybrid；--compare 一次跑三种模式做对比）
uv run python ../scripts/eval_retrieval.py run golden.jsonl --kb kb_xxx --token <会话令牌> --compare
```

**走 HTTP 而不是进程内直连**：进程内要占 DuckDB 的文件锁，服务在跑时根本起不来；
而且 HTTP 版本可以对着远程实例跑（部署后复测同一份金标集）。
令牌从参数或环境变量 ``KYLAB_TOKEN`` 取，不写进任何文件。

**为什么打印逐条明细**：只看汇总数没法定位问题。逐条里能看到"这个词漏了哪一份文档"，
那才是指向修什么的线索（是切块切断了它？还是这个库压根没收那份文件？）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# app 包在 backend/ 下；门禁从 backend/ 调脚本时天然可导入，从仓库根调则不是
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.retrieval_eval import (  # noqa: E402
    EvalCase,
    EvalSummary,
    evaluate,
    summarize,
)

MODES = ("hybrid", "vector", "fulltext")


def _load_cases(path: Path) -> list[EvalCase]:
    """读 JSONL 金标集。一行一条：``{"query": ..., "expect": [...], "note": ...}``。"""
    cases: list[EvalCase] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
        query = str(raw.get("query", "")).strip()
        if not query:
            raise SystemExit(f"{path}:{lineno} 缺少 query")
        cases.append(
            EvalCase(
                query=query,
                expect=tuple(str(item) for item in raw.get("expect", []) or []),
                note=str(raw.get("note", "")),
            )
        )
    if not cases:
        raise SystemExit(f"{path} 里没有可用的评测条目")
    return cases


def _search(base: str, token: str, kb_id: str, mode: str, top_k: int, rerank: bool):  # type: ignore[no-untyped-def]
    import httpx

    client = httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )

    def search(query: str) -> list[str]:
        response = client.post(
            "/api/v1/search",
            json={
                "query": query,
                "kb_ids": [kb_id],
                "top_k": top_k,
                "mode": mode,
                "rerank": rerank,
            },
        )
        response.raise_for_status()
        # 文档名可能为空（记录被删等）：用 id 兜底，至少不是空串参与匹配
        return [
            hit.get("document_name") or hit["document_id"]
            for hit in response.json().get("hits", [])
        ]

    return search


def _print_report(label: str, summary: EvalSummary, *, verbose: bool, top_k: int) -> None:
    print(f"\n=== {label}（{summary.total} 条，top_k={top_k}）===")
    if verbose:
        for result in summary.results:
            mark = "OK " if result.hit else "MISS"
            rank = (
                f"1/{int(1 / result.reciprocal_rank)}" if result.reciprocal_rank else "—"
            )
            print(f"  [{mark}] {result.case.query}")
            print(f"         首个命中名次={rank} recall={result.recall:.2f}")
            if result.missed:
                print(f"         漏掉：{'、'.join(result.missed)}")
    print(
        f"  hit@{top_k} = {summary.hit_rate:.1%}"
        f"   recall@{top_k} = {summary.mean_recall:.3f}"
        f"   MRR = {summary.mrr:.3f}"
    )


def _cmd_run(args: argparse.Namespace) -> int:
    cases = _load_cases(Path(args.golden))
    modes = MODES if args.compare else (args.mode,)
    summaries: dict[str, EvalSummary] = {}
    for mode in modes:
        search = _search(args.base, args.token, args.kb, mode, args.top_k, args.rerank)
        summaries[mode] = evaluate(cases, search)
        _print_report(mode, summaries[mode], verbose=args.verbose, top_k=args.top_k)

    if len(summaries) > 1:
        print("\n=== 模式对比（hit@k / recall / MRR）===")
        print(f"  {'模式':<10}{'hit':>8}{'recall':>9}{'MRR':>8}")
        for mode, summary in summaries.items():
            print(
                f"  {mode:<10}{summary.hit_rate:>7.1%}"
                f"{summary.mean_recall:>9.3f}{summary.mrr:>8.3f}"
            )
    if args.min_hit is not None:
        best = max(item.hit_rate for item in summaries.values())
        if best < args.min_hit:
            print(f"\n!! 最好的一档 hit 率 {best:.1%} 低于阈值 {args.min_hit:.1%}")
            return 1
    return 0


def _cmd_suggest(args: argparse.Namespace) -> int:
    """从语料生成金标集草稿：问题交给已有模型提，"应该命中谁"留空等人填。"""
    import httpx

    client = httpx.Client(
        base_url=args.base.rstrip("/"),
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=120.0,
    )
    params = {"kb_ids": args.kb, "limit": args.limit, "refresh": "true"}
    response = client.get("/api/v1/chat/suggested-questions", params=params)
    response.raise_for_status()
    questions = response.json().get("questions", [])
    if not questions:
        print("!! 没生成出问题：确认这个库有内容、且配置了对话模型", file=sys.stderr)
        return 1
    out = Path(args.output)
    lines = [
        json.dumps({"query": item, "expect": [], "note": "待填：应该命中哪几份文档"}, ensure_ascii=False)
        for item in questions
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {out}：{len(questions)} 条草稿。请把 expect 填成文档名里有辨识度的一段。")
    print("提示：只填问题不填 expect 的条目按满分计，所以填完再跑 run。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量跑分（金标集）")
    parser.add_argument("--base", default=os.environ.get("KYLAB_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("KYLAB_TOKEN", ""))
    sub = parser.add_subparsers(dest="command", required=True)

    suggest = sub.add_parser("suggest", help="从语料生成金标集草稿")
    suggest.add_argument("--kb", required=True)
    suggest.add_argument("--limit", type=int, default=8)
    suggest.add_argument("-o", "--output", default="golden.jsonl")
    suggest.set_defaults(func=_cmd_suggest)

    run = sub.add_parser("run", help="按金标集打分")
    run.add_argument("golden")
    run.add_argument("--kb", required=True)
    run.add_argument("--mode", default="hybrid", choices=MODES)
    run.add_argument("--compare", action="store_true", help="三种模式各跑一遍做对比")
    run.add_argument("--top-k", type=int, default=8)
    run.add_argument("--rerank", action="store_true")
    run.add_argument("--min-hit", type=float, default=None, help="低于这个 hit 率就返回 1")
    run.add_argument("-v", "--verbose", action="store_true", help="打印逐条明细")
    run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    if not args.token:
        print("!! 需要令牌：--token 或环境变量 KYLAB_TOKEN", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
