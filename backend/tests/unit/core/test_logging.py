"""日志初始化的单元测试。

**这个文件的首要目的是钉住编码**：本项目是中文的，而中文 Windows 的
默认控制台编码是 GBK。日志里出现中文文件名时 `StreamHandler.emit` 会抛
`UnicodeEncodeError`，logger 打出一串乱码加调用栈——真正的那条业务日志就没了。

实景触发过：一个叫 `团队名单.csv` 的文件摄入时，日志变成了
`锟斤拷 %s 写锟斤拷峁癸拷锟斤拷锟斤拷锟?%d 锟斤拷` 加一段栈。
**触发条件偏偏是"出问题的时候"，也就是最需要日志的时候。**
"""

from __future__ import annotations

import io
import logging
import sys

from app.core.logging import setup_logging


def test_streams_are_switched_to_utf8() -> None:
    setup_logging("INFO")
    # **别断言 == "utf-8"**：某些环境（比如已经强制 UTF-8 的 CI）本来就是这个值，
    # 但更常见的是 pytest 把 stdout 换成了自己的捕获对象。这里要断言的是
    # "能编出中文"，而不是"编码字符串长什么样"——后者是手段，前者才是目的。
    encoding = getattr(sys.stdout, "encoding", "") or ""
    assert "utf" in encoding.lower() or encoding.lower().startswith("cp65001"), encoding


def test_chinese_filename_does_not_break_logging() -> None:
    """真实回归：把当初炸掉的那条日志原样打一遍。

    断言方式是**换掉 stream 再检查有没有异常**，而不是"看输出里有没有中文"——
    因为这里要证明的是 `emit` 不再抛，而不是文本长什么样。
    """
    setup_logging("INFO")

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("app.services.ingest")
    logger.addHandler(handler)
    try:
        # 当初炸掉的那一行（照抄 app/services/ingest.py 的 `_store_tabular_copy`）
        logger.info("文档 %s 写入结构化副本 %d 行", "团队名单.csv", 3)
    finally:
        logger.removeHandler(handler)

    assert "团队名单.csv" in buffer.getvalue()
    assert "3 行" in buffer.getvalue()


def test_setup_is_idempotent() -> None:
    """重复调用不叠加 handler。

    应用启动 + 测试夹具都会调它；叠起来的话每条日志会打两遍，
    而"日志重复"看着像代码里有两条一样的话。
    """
    setup_logging("INFO")
    setup_logging("INFO")
    tagged = [h for h in logging.getLogger().handlers if getattr(h, "_kylab_stdout_handler", False)]
    assert len(tagged) == 1


def test_does_not_remove_foreign_handlers() -> None:
    """只摘自己装的 handler。

    `root.handlers.clear()` 会把 uvicorn / pytest caplog / 容器日志采集
    装的 handler 一并摘掉，表现为"日志莫名其妙没了"。
    """
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        setup_logging("INFO")
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_level_is_applied() -> None:
    setup_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING
    setup_logging("INFO")


# --------------------------------------------------- 文件日志（照 QwenPaw 补上）


def test_file_handler_writes_and_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """文件日志要真的写下来，且**同一个路径只挂一次**。

    重复挂的后果是每条日志写两遍 + 泄漏文件描述符；而 `lifespan` 在同一个进程里
    跑多次是真会发生的（测试、reload）。
    """
    from app.core.logging import add_file_handler

    path = add_file_handler(tmp_path / "logs" / "kylab.log")
    add_file_handler(tmp_path / "logs" / "kylab.log")

    logging.getLogger("app.tests.file").warning("写进文件的这一条")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = path.read_text(encoding="utf-8")
    assert text.count("写进文件的这一条") == 1
    # 文件那一份带相对路径与行号（用来回溯"哪一行打的"）
    assert "test_logging.py:" in text


def test_file_handler_follows_the_root_level(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """不另配级别时跟随根日志器。

    两处各配一套级别，最后一定会出现"控制台有、文件里没有"——
    而那正是去翻文件时最不愿意遇到的情况。
    """
    from app.core.logging import add_file_handler

    setup_logging("WARNING")
    try:
        path = add_file_handler(tmp_path / "kylab.log")
        logging.getLogger("app.tests.level").info("这条不该进文件")
        logging.getLogger("app.tests.level").warning("这条该进文件")
        for handler in logging.getLogger().handlers:
            handler.flush()

        text = path.read_text(encoding="utf-8")
        assert "这条该进文件" in text
        assert "这条不该进文件" not in text
    finally:
        setup_logging("INFO")


def test_log_file_lives_under_the_data_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """日志落在数据目录里（``data/logs/``），与其它运行期产物同一个地方。

    放当前工作目录的话，从不同目录启动就会把日志写到不同地方——
    排查时"日志去哪了"会变成一个额外的问题。
    """
    from app.core.logging import log_file_for

    assert log_file_for(tmp_path) == tmp_path / "logs" / "kylab.log"


def test_size_setting_is_parsed_and_bad_values_fall_back(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """大小可配（``5mb`` 这类写法），**写错就用默认值并留一条警告**。

    静默用默认值会让"我明明设了、它没照做"变成一个没人能解释的现象。
    """
    from app.core import logging as kylab_logging

    monkeypatch.setenv(kylab_logging._LOG_MAX_SIZE_ENV, "512k")
    assert kylab_logging._max_bytes() == 512 * 1024
    monkeypatch.setenv(kylab_logging._LOG_MAX_SIZE_ENV, "1m")
    assert kylab_logging._max_bytes() == 1024**2
    monkeypatch.setenv(kylab_logging._LOG_MAX_SIZE_ENV, "5MBx")
    assert kylab_logging._max_bytes() == kylab_logging.LOG_MAX_BYTES
    monkeypatch.setenv(kylab_logging._LOG_BACKUP_COUNT_ENV, "-1")
    assert kylab_logging._backup_count() == kylab_logging.LOG_BACKUP_COUNT


def test_sanitize_escapes_newlines_so_one_event_stays_one_line() -> None:
    """不可信的值里有换行时，**一条日志必须还是一行**。

    文件名、模型返回的文本、外部服务给的报错都可能带换行。不转义的话，
    一条日志会被伪装成好几条——排查时按行数数就会数错，
    而且多出来的那几行看起来像我们自己打的。
    """
    from app.core.logging import sanitize_log_value

    assert sanitize_log_value("团队名单\n.csv") == "团队名单\\n.csv"
    assert sanitize_log_value("a\r\nb") == "a\\r\\nb"
    assert sanitize_log_value(3) == "3"
