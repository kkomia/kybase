"""日志初始化。

**只清理自己上次装的 handler**，而不是 ``root.handlers.clear()``：
后者会把宿主进程（uvicorn、pytest 的 caplog、容器日志采集）装的 handler 一并摘掉，
表现为"日志莫名其妙没了"。幂等：重复调用不会叠加输出。

v0.21 起补上 QwenPaw（``utils/logging.py``）那套"agent 跑起来之后要看日志"的做法，
四件事，每一件都是为了回答一个具体问题：

1. **滚动文件日志**（:func:`add_file_handler`）：控制台日志活不过一次重启，
   而"昨天晚上那次对话为什么慢"只能从文件里翻。5 MiB × 3 份，可用
   ``KYLAB_LOG_MAX_SIZE`` / ``KYLAB_LOG_MAX_BACKUPS`` 调——默认值照抄 QwenPaw，
   它们是从"日志要看、但别把磁盘吃光"这个平衡里来的。
2. **Windows 上容忍轮转失败**（``_SafeRotatingFileHandler``）：Windows 的
   ``os.rename`` 在文件被别的进程打开时抛 ``PermissionError``（日志查看器、
   编辑器预览都算）。原生的 ``RotatingFileHandler`` 会让这一次 emit 直接失败
   ——也就是**最需要日志的时候它不写了**。这里的处置是"这轮不轮转、接着写"。
3. **控制台配色**（``_ColorFormatter``）：只在真终端上开（``isatty``）。
   被重定向到文件或管道时 ANSI 转义会变成一串乱码塞进日志里。
4. **``sanitize_log_value``**：把**不可信的值**里的换行转义成 ``\\n``。
   文件里的姓名、模型返回的文本、外部服务给的错误都可能带换行——不转义的话，
   一条日志会被伪装成好几条，而"日志行"是我们排查时唯一能信的计数单位。

文件那一份**只由应用启动时挂**（:func:`add_file_handler`）：库代码不该决定往磁盘写什么。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import ClassVar

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
#: 文件那一份的格式：多**相对路径:行号**（`app/services/chat.py:1234`）。
#: 与 console 那份分开是有意的——终端的宽度要留给消息本身，
#: 而文件是拿来 grep 与回溯的，"哪一行打的"在那里才值钱。
_FILE_FORMAT = "%(asctime)s | %(levelname)s | %(pathname)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_TAG = "_kylab_stdout_handler"
_FILE_HANDLER_TAG = "_kylab_file_handler"

#: 单个日志文件多大之后轮转，以及留几份备份。与 QwenPaw 的默认值一致：
#: 5 MiB × 3 大约够看几天的量，又不至于在长期运行的机器上无限长。
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
_LOG_MAX_SIZE_ENV = "KYLAB_LOG_MAX_SIZE"
_LOG_BACKUP_COUNT_ENV = "KYLAB_LOG_MAX_BACKUPS"

#: 日志文件的默认相对位置（相对数据目录）。
LOG_FILE_NAME = "kylab.log"

_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

#: ``5mb`` / ``512k`` / ``1048576`` 这类写法。大小写成配置是想让人**不用改代码**
#: 就能调（磁盘紧张的机器想留少一点，排查期想留多一点）。
_SIZE_PATTERN = re.compile(r"^\s*(\d+)\s*([kmgt]?i?b?)?\s*$", re.I)
_SIZE_FACTORS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}


def setup_logging(level: str = "INFO") -> None:
    """配置根日志器，应用启动时调用一次。

    控制台这一份用**行缓冲的 stdout**：容器与 CI 采集的是 stdout，
    而 stderr 常常被当成"有错误"的信号（我们这里不是）。
    """
    _force_utf8_streams()
    _enable_windows_ansi()

    root = logging.getLogger()
    for handler in [item for item in root.handlers if getattr(item, _HANDLER_TAG, False)]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    setattr(handler, _HANDLER_TAG, True)  # 给 handler 打标记，便于下次识别并替换

    root.addHandler(handler)
    root.setLevel(_level_of(level))


def add_file_handler(path: Path | str, *, level: str | None = None) -> Path:
    """挂一个**滚动文件**日志，返回实际用的路径。

    幂等（同一个路径只挂一次）：``lifespan`` 在同一个进程里可能跑多次
    （测试、reload），重复挂会让每条日志写两遍并泄漏文件描述符。

    ``level`` 留空就跟随根日志器：两个地方各配一套级别，最后一定会出现
    "控制台有、文件里没有"——而那正是去翻文件时最不愿遇到的。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()

    root = logging.getLogger()
    for handler in root.handlers:
        base = getattr(handler, "baseFilename", None)
        if base is not None and Path(base).resolve() == resolved:
            return resolved

    handler = _SafeRotatingFileHandler(
        resolved,
        encoding="utf-8",
        maxBytes=_max_bytes(),
        backupCount=_backup_count(),
    )
    handler.setLevel(_level_of(level) if level else root.level or logging.INFO)
    # 文件里带**相对路径:行号**：控制台看着短，文件是用来 grep 与回溯的，
    # 一行里能看出"哪一行打的"很省事（QwenPaw 的 PlainFormatter 也是这么做的）
    handler.setFormatter(_PlainFormatter(_FILE_FORMAT, _DATE_FORMAT))
    setattr(handler, _FILE_HANDLER_TAG, True)
    root.addHandler(handler)
    return resolved


def log_file_for(data_dir: Path | str) -> Path:
    """数据目录下的日志文件路径（``data/logs/kylab.log``）。

    放在数据目录里而不是当前工作目录：这个项目所有运行期产生的东西都在
    ``data/``（它被 gitignore 挡住），日志属于同一类——放外面会出现
    "启动目录不同、日志就不知道去哪了"。
    """
    return Path(data_dir) / "logs" / LOG_FILE_NAME


def sanitize_log_value(value: object) -> str:
    """把值里的换行转义掉，**在它进日志之前**。

    为什么需要：写日志的东西里面有一半是**别人给的文本**——文件名、模型返回的
    一段话、外部服务抛回来的报错。它们中间的一个换行就能让一行日志变成两行，
    于是 `grep` 出来的"日志行"不再是"一次事件"，而按行统计也就没意义了。
    这不是理论问题：一个带换行的错误消息会让排查的人在日志里看到一条
    看起来像我们自己打的、其实来自外部的行。

    只转义换行（``\\r`` / ``\\n``），不动其它字符——日志要能读，不必"安全编码"。
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


# ------------------------------------------------------------------ handler


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """轮转失败时**接着写**，而不是让这一次 emit 丢掉。

    Windows 上 ``os.rename`` 在目标文件被别的进程打开时抛 ``PermissionError``
    （打开日志的编辑器、跟随查看的工具都算）。原生实现会让这条日志直接打不出来
    ——而日志最常被打开的时候，正是出问题的时候。

    处置是"这次不轮转"：关掉再打开流（保持写入），等下一次超限再试。
    代价是文件可能暂时超过上限，两害相权取"日志不丢"。
    """

    def doRollover(self) -> None:  # pragma: no cover - 需要真锁住文件才能触发
        try:
            super().doRollover()
        except PermissionError:
            if self.stream:
                self.stream.close()
            self.stream = self._open()


class _ColorFormatter(logging.Formatter):
    """给级别上色。**只在真终端上开**：重定向到文件时 ANSI 会变成乱码。"""

    COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[34m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m\033[97m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not _is_a_tty(sys.stdout):
            return text
        color = self.COLORS.get(record.levelno, "")
        if not color:
            return text
        return f"{color}{text}{self.RESET}"


class _PlainFormatter(logging.Formatter):
    """文件那一份：行号用**相对当前工作目录**的路径（绝对路径太长且会泄露部署布局）。"""

    def format(self, record: logging.LogRecord) -> str:
        original = record.pathname
        try:
            cwd = os.getcwd()
            record.pathname = os.path.relpath(original, cwd)
        except (OSError, ValueError):  # pragma: no cover - 跨盘符等边缘情况
            pass
        try:
            return super().format(record)
        finally:
            record.pathname = original


# ------------------------------------------------------------------ 内部


def _level_of(level: str | int) -> int:
    """级别名 → 数字。认不出的名字回 ``INFO``（启动不该因为拼错一个级别就失败）。"""
    if isinstance(level, int):
        return level
    return _LEVELS.get(str(level).strip().lower(), logging.INFO)


def _is_a_tty(stream: object) -> bool:
    probe = getattr(stream, "isatty", None)
    try:
        return bool(probe and probe())
    except Exception:  # pragma: no cover - 关掉的流、奇怪的包装对象
        return False


def _parse_size(value: str) -> int:
    """``5mb`` / ``512k`` / ``1048576`` → 字节数。**看不懂就抛**，由调用方回退默认值。

    不静默接受：一个写错的 ``KYLAB_LOG_MAX_SIZE=5MBx`` 如果被当成 0 或默认值，
    表现是"我明明设了、它没照做"——而这类配置错误正是最容易被放过的。
    """
    match = _SIZE_PATTERN.fullmatch(value or "")
    if match is None:
        raise ValueError("要写成字节数或带单位的大小（如 5mb、512k）")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("大小要大于 0")
    factor = _SIZE_FACTORS.get((match.group(2) or "").lower())
    if factor is None:  # pragma: no cover - 正则已经限定了后缀
        raise ValueError(f"不认识的大小单位：{match.group(2)}")
    return amount * factor


def _max_bytes() -> int:
    raw = os.getenv(_LOG_MAX_SIZE_ENV)
    if raw is None:
        return LOG_MAX_BYTES
    try:
        return _parse_size(raw)
    except ValueError as exc:
        # 配置写错**不能影响启动**，但要留痕：静默用默认值会让"我设了没生效"
        # 变成一个没人能解释的现象
        logging.getLogger(__name__).warning(
            "%s=%r 用不了（%s），改用默认值 %d 字节", _LOG_MAX_SIZE_ENV, raw, exc, LOG_MAX_BYTES
        )
        return LOG_MAX_BYTES


def _backup_count() -> int:
    raw = os.getenv(_LOG_BACKUP_COUNT_ENV)
    if raw is None:
        return LOG_BACKUP_COUNT
    try:
        count = int(raw.strip())
        if count < 0:
            raise ValueError("份数不能是负数")
        return count
    except ValueError as exc:
        logging.getLogger(__name__).warning(
            "%s=%r 用不了（%s），改用默认值 %d", _LOG_BACKUP_COUNT_ENV, raw, exc, LOG_BACKUP_COUNT
        )
        return LOG_BACKUP_COUNT


def _enable_windows_ansi() -> None:
    """把 Windows 控制台切到"认识 ANSI 转义"。

    现代 Windows Terminal 默认就支持，但老一点的 conhost 不会——那时彩色日志会
    显示成 ``←[32m`` 这样的字面量。开错了也不影响什么，所以整段包在 try 里。
    """
    if os.name != "nt":  # pragma: no cover - 只有 Windows 会走
        return
    try:  # pragma: no cover - 需要真控制台
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return


def _force_utf8_streams() -> None:
    """把 stdout / stderr 切到 UTF-8。

    **这是个中文项目，而中文 Windows 的默认控制台编码是 GBK（cp936）。**
    实测：日志里出现一个中文文件名（`团队名单.csv`）就会让
    ``StreamHandler.emit`` 抛 ``UnicodeEncodeError``，logger 打出
    "--- Logging error ---" 加一长串调用栈。

    **它不会让进程崩溃**（``logging`` 自己吞掉了异常），所以这不是"致命 bug"——
    但它的实际伤害很像致命 bug：

    - 日志里看到的是一串乱码（``锟斤拷``）加一段无关的栈，**真正的那条业务日志没了**；
    - 那段栈会进 stderr，而编排/CI/采集器常常把"stderr 有输出"当成失败信号
      或者至少当成噪声——**在被误导这一点上，它与真崩溃等价**；
    - 触发条件偏偏是"出问题的时候"：文件名带中文、错误信息带中文。
      也就是说，**最需要日志的那一刻，日志最不可靠**。

    ``errors="replace"`` 而不是默认的 ``strict``：万一还有编不出来的字符
    （比如终端字体不支持的生僻字），宁可显示成 ``?`` 也不要再抛一次。
    日志的价值在"能读"，不在"一字不差"。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 被重定向成非 TextIOWrapper（某些容器/CI 的管道）时没有这个接口，跳过
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 已关闭的流等边缘情况
            continue
