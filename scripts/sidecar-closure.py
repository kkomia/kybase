"""**只读**算边车的真实导入闭包（P4-3）。

为什么要有它：客户端运行时的依赖清单**必须算出来**，不能"读代码看出来" ✗ ——
实测证明两者不等：`typing_extensions`（单文件 .py）、`_cffi_backend`（根目录 .pyd）、
`pydantic_settings`、`jieba`（模块级导入链）都是静态看不出来、只有真跑才会炸的东西 ✓。

它**只 import、只统计** ✓：不装包、不打网络、不改运行时、不杀进程 ✓
（`.shots/sidecar-import-closure.py` 那个会 `pip install` ✗，镜像那条会卡死 >10 分钟 ✗ —— 别用那个）。

用法（cwd = `backend/`）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\sidecar-closure.py            # 人看的清单
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\sidecar-closure.py --json     # 机器用（含发行包）

判据（P4-3 Phase B）：**改了惰性导入之后重跑本脚本** ✓ —— 期望被治掉的包
（`psycopg` / `duckdb` / `boto3` / `botocore` / `jieba` …）从 `sys.modules` 里消失 ✓✓。
"看代码觉得不该有" **不是**判据 ✗。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import app.sidecar  # noqa: E402,F401  ← 这一行就是要测的那条链 ✓

STDLIB = set(sys.stdlib_module_names)


def build_import_map() -> dict[str, list[str]]:
    """import 名 → `发行包==版本`（按各发行包的文件清单推 ✓）。"""
    mapping: dict[str, list[str]] = {}
    for dist in md.distributions():
        try:
            name = (dist.metadata["Name"] or "").strip()
        except Exception:  # noqa: BLE001 - 元数据坏了不该让脚本挂掉
            continue
        if not name:
            continue
        for file in dist.files or []:
            parts = str(file).split("/")
            if len(parts) > 1 and parts[0].endswith(".dist-info"):
                continue
            head = parts[0]
            if head.endswith(".py"):
                head = head[:-3]
            elif head.endswith(".pyd") or head.endswith(".pyc"):
                head = head.split(".")[0]
            if head and head.isidentifier():
                mapping.setdefault(head, []).append(f"{name}=={dist.version}")
    for import_name, dists in md.packages_distributions().items():
        if import_name.isidentifier():
            for dist_name in dists:
                try:
                    mapping.setdefault(import_name, []).append(
                        f"{dist_name}=={md.version(dist_name)}"
                    )
                except md.PackageNotFoundError:
                    continue
    return {key: sorted(set(value)) for key, value in mapping.items()}


def collect() -> list[dict[str, str]]:
    by_import = build_import_map()
    rows: list[dict[str, str]] = []
    for name in sorted({module.split(".")[0] for module in sys.modules if module}):
        if name in STDLIB or name in ("app", "sitecustomize", "usercustomize"):
            continue
        module = sys.modules.get(name)
        origin = str(getattr(module, "__file__", "") or getattr(module, "__path__", "") or "")
        if "site-packages" not in origin and "dist-packages" not in origin:
            continue
        dists = by_import.get(name, [])
        rows.append(
            {
                "import": name,
                "dist": dists[0] if dists else "(未在 site-packages 元数据里找到)",
                "origin": origin,
            }
        )
    # 再补一类：**site-packages 根目录下的扩展**（`_cffi_backend` 那种下划线开头、会被上面漏掉 ✓）
    for name, module in sorted(sys.modules.items()):
        if not name.startswith("_") or "." in name:
            continue
        origin = str(getattr(module, "__file__", "") or "")
        if "site-packages" in origin:
            rows.append({"import": name, "dist": "(扩展模块)", "origin": origin})
    return rows


def main() -> None:
    # 控制台是 GBK：输出里有 ✓ 之类的字符会 UnicodeEncodeError ✗ → 显式用 UTF-8 打 ✓
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="算边车的只读导入闭包")
    parser.add_argument("--json", action="store_true", help="按 JSON 输出（含发行包清单）")
    parser.add_argument("--out", default="", help="把 JSON 也写到这个路径")
    args = parser.parse_args()

    rows = collect()
    dists = sorted({row["dist"] for row in rows if not row["dist"].startswith("(")})
    payload = {"modules": rows, "dists": dists, "count": len(rows)}

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print("=== 非标准库顶层模块（实测闭包）===")
    for row in rows:
        print(f"{row['import']:<24} {row['dist']}")
    print(f"\n合计 {len(rows)} 个顶层模块")
    print("\n=== 要装配的发行包（离线拷贝按这个列表 ✓）===")
    for dist in dists:
        print(f"  {dist}")


if __name__ == "__main__":
    main()
