#!/usr/bin/env python3
"""Query a kylab knowledge base from the command line.

Why this exists alongside the MCP server: **not every agent runtime can speak MCP.**
MCP needs a client that understands the protocol; a shell and an HTTP endpoint are
available almost everywhere. So this Skill ships both paths to the same retrieval —
MCP for clients that support it, this script for the rest.

It deliberately prints **the passages**, not a generated answer. kylab's value is
traceable sources; a script that summarised them would throw away the thing that makes
the result usable for verification.

Usage:
    python kylab_query.py --query "眼轴怎么监测"
    python kylab_query.py --query "..." --kb kb_abc123 --top-k 8
    python kylab_query.py --list                      # which libraries exist
    python kylab_query.py --query "..." --json        # machine-readable

Exit codes: 0 success (even with zero hits — that is a valid finding),
1 usage/transport error, 2 authentication required.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TOP_K = 6
TIMEOUT_SECONDS = 30


def _request(
    base_url: str, path: str, *, token: str | None, method: str = "GET", body: dict | None = None
) -> object:
    url = f"{base_url.rstrip('/')}/api/v1{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        # 401/403 is worth its own exit code: it is the one failure the caller can
        # actually fix, and it is otherwise easy to mistake for "no results".
        if exc.code in (401, 403):
            print(
                f"需要凭据（HTTP {exc.code}）。kylab 的接口一律要凭据（v0.11 起没有开关）：\n"
                "  在控制台「设置 → API 密钥」建一把**只读**密钥，"
                "然后设 KYLAB_API_KEY，或给本脚本传 --token。\n"
                "  （登录会话令牌也可以，但它只属于浏览器；给脚本用请用 API Key。）",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        print(f"请求失败（HTTP {exc.code}）：{detail[:400]}", file=sys.stderr)
        raise SystemExit(1) from exc
    except urllib.error.URLError as exc:
        print(
            f"连不上 kylab（{base_url}）：{exc.reason}\n"
            "  确认后端在跑：cd backend && uv run uvicorn app.main:app",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def list_knowledge_bases(base_url: str, token: str | None) -> list[dict]:
    payload = _request(base_url, "/knowledge-bases", token=token)
    return payload.get("items", []) if isinstance(payload, dict) else []


def search(
    base_url: str, token: str | None, *, query: str, kb_ids: list[str], top_k: int
) -> dict:
    body: dict = {"query": query, "top_k": top_k}
    if kb_ids:
        body["kb_ids"] = kb_ids
    else:
        # 留空 = 查全部。Agent 往往不知道有哪些库，逼它先列一遍是多余的一步——
        # 但**库为空时要明说**，否则"没搜到"会被误读成"库里没有这份资料"。
        everything = [item["id"] for item in list_knowledge_bases(base_url, token)]
        if not everything:
            return {"hits": [], "filtered_out": 0, "_no_knowledge_base": True}
        body["kb_ids"] = everything
    return _request(base_url, "/search", token=token, method="POST", body=body)


def _print_hits(result: dict, *, query: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if result.get("_no_knowledge_base"):
        print("这个 kylab 实例里还没有任何知识库——先建库并上传文档。")
        return

    hits = result.get("hits", [])
    if not hits:
        # 空结果**不是错误**，但要说清它意味着什么，以及下一步该看哪里。
        print(f"没有找到与「{query}」相关的原文片段。")
        print("可以试：换个说法再查；或确认文档已经处理完（未 indexed 的文档搜不到）。")
        return

    print(f"查询：{query}")
    print(f"命中 {len(hits)} 段：\n")
    for index, hit in enumerate(hits, start=1):
        location = [hit.get("document_name") or hit.get("document_id")]
        if hit.get("page") is not None:
            location.append(f"第 {hit['page']} 页")
        if hit.get("heading_path"):
            location.append(str(hit["heading_path"]))
        print(f"[{index}] {' · '.join(location)}   （相关度 {hit.get('score')}）")
        text = (hit.get("text") or "").strip().replace("\n", " ")
        print(f"    {text[:400]}{'…' if len(text) > 400 else ''}\n")

    filtered = result.get("filtered_out")
    if filtered:
        print(f"（另有 {filtered} 段被过滤：低于阈值、不满足筛选，或已被人工禁用）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="查询 kylab 知识库并打印原文出处（不生成回答）",
    )
    parser.add_argument("--query", "-q", help="检索词，自然语言即可")
    parser.add_argument(
        "--kb",
        action="append",
        default=[],
        metavar="KB_ID",
        help="限定知识库（可重复）；不传则查全部",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返回条数")
    parser.add_argument("--base-url", default=os.environ.get("KYLAB_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--token",
        default=os.environ.get("KYLAB_API_KEY"),
        help="API Key（只读档即可）；默认读 KYLAB_API_KEY 环境变量",
    )
    parser.add_argument("--list", action="store_true", help="列出知识库后退出")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（给程序读）")
    args = parser.parse_args(argv)

    if args.list:
        items = list_knowledge_bases(args.base_url, args.token)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        elif not items:
            print("还没有知识库。")
        else:
            for item in items:
                print(f"{item['id']}  {item['name']}  （{item.get('embedding_model_id', '?')}）")
        return 0

    if not args.query:
        parser.error("要么给 --query，要么用 --list 看有哪些库")

    result = search(
        args.base_url,
        args.token,
        query=args.query,
        kb_ids=args.kb,
        top_k=max(1, args.top_k),
    )
    _print_hits(result, query=args.query, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
