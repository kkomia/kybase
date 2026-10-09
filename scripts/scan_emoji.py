"""扫描源码中的 emoji 字符（项目硬性禁令：UI 全链路禁 emoji）。

与 ``skills/code-quality-check/scripts/scan_emoji.py`` 同源，仓库内自带副本以便 CI 自包含；
差别是显式把标准输出切到 UTF-8，避免 GBK 控制台下的 UnicodeEncodeError。

用法：python scripts/scan_emoji.py <目录> [<目录>...]
退出码：0 = 未发现；1 = 发现 emoji（打印 文件:行:列 与字符）。
"""

import re
import sys
from pathlib import Path

# 常见 emoji Unicode 区段（杂项符号、象形文字、表情、交通符号、增补、变体选择符、区旗、键帽等）
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 各类象形/表情符号区
    "\U00002600-\U000027BF"  # 杂项符号 + 装饰符号
    "\U0001F1E6-\U0001F1FF"  # 区旗指示符
    "\U00002B00-\U00002BFF"  # 箭头与杂项符号
    "\U0000FE00-\U0000FE0F"  # 变体选择符
    "\U00002000-\U0000206F"  # 与 emoji 组合的极少字符，见白名单过滤
    "\U00002190-\U000021FF"  # 箭头（前端常见误用）
    "]"
)

# 文本排版合法字符白名单（引号、破折号、省略号、箭头、数学符号等不误报）
WHITELIST = set("“”‘’—–…·•‹›«»←→↑↓↔‖")

TEXT_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".html", ".css",
    ".json", ".md", ".toml", ".yaml", ".yml", ".sql",
}

SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__", ".venv", "venv"}


def scan_file(path: Path) -> list[tuple[int, int, str]]:
    """返回 ``[(行, 列, 字符)]``。"""
    hits: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return hits
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in EMOJI_RE.finditer(line):
            char = match.group()
            if char in WHITELIST:
                continue
            hits.append((lineno, match.start() + 1, char))
    return hits


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/scan_emoji.py <目录> [<目录>...]", file=sys.stderr)
        return 2

    # Windows 默认 GBK 控制台无法输出 emoji 字符，会导致报告阶段崩溃
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    total = 0
    for root_arg in sys.argv[1:]:
        root = Path(root_arg)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            for lineno, col, char in scan_file(path):
                total += 1
                print(f"{path}:{lineno}:{col}  发现 emoji {char!r} (U+{ord(char):04X})")

    if total:
        print(f"\n共发现 {total} 处 emoji，违反《前端设计规范》禁令。")
        return 1
    print("未发现 emoji，检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
