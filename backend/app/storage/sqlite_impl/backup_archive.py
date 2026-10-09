r"""快照打包的**存储层那一半**（M5 阶段 2，方案 §2.3 / §2.4）。

两件事，一件是全场最关键的：

1. ``dump_scrubbed_db(db, dest)`` —— **在线备份 → 擦洗 → ``VACUUM`` → 读数**。
   本机库里有明文凭据（``model_providers.api_key``、``mcp_servers.env`` / ``headers``、
   ``app_settings`` 的 ``SECRET_KEYS``），所以"打包"必须带一道擦洗，否则"秘密只进
   钥匙串"这条纪律会被备份绕过去（方案 §2.4 / 风险 R1）。擦洗做在**副本**上，
   源库一个字节不动。
2. ``read_snapshot_db(db_path)`` —— 从一份快照库里读出"包里有什么"（窄投影：
   会话 / 产物 Key / 设置键 / 计数）。

**为什么这两件事住在这里**（而不是打包那个服务里）：它们是 SQLite 方言——``backup()``
在线备份 API、``PRAGMA secure_delete``、``VACUUM``、``schema_metadata`` 那一列。
``scripts/check_layering.py`` 的 L2 规则不许 ``services/`` import ``sqlite3``，
所以方言只许住在 ``app/storage/sqlite_impl/``，服务层只许见
``app.storage.base`` 的两份协议（``LocalSnapshotArchiver`` / ``SnapshotSource``）。

**擦洗的四条口径**（方案 §2.4，逐条都有判据用例钉着）：

1. ``PRAGMA secure_delete = ON``（**副本**连接，必须在下面那些 UPDATE/DELETE **之前**）；
2. 白名单外清列 / 删行：

   - ``model_providers.api_key`` → ``''``（恢复后要重配，见方案 §3.4 的报告）；
   - ``mcp_servers.env`` / ``headers`` → ``'{}'``；
   - ``app_settings`` 里 ``runtime_config.SECRET_KEYS`` ∪ 前缀 ``provider.`` /
     ``model.`` / ``backup.`` 的键 → ``DELETE``。

   **判据是两个常量本身、不手抄**：``SECRET_KEYS``（``_secret_keys()`` 现取）与
   ``base.SNAPSHOT_EXCLUDED_SETTING_PREFIXES``，两者都作为**参数**传进 SQL
   （不拼进 SQL 文本——那样既是注入面，也让"判据"变成一段不可核对的字符串）。
   将来新增一个 SECRET_KEY，它自动被排除：这是机械防漂。
3. ``VACUUM``（**必须**）：SQLite 的 UPDATE/DELETE 不覆盖旧页内容，明文会残留在空闲页里，
   ``secure_delete`` + ``VACUUM`` 之后才真的洗掉；
4. 副本**单文件、不带 ``-wal``**：备份之后把副本切到 ``journal_mode=DELETE``
   （**必须在备份之后**——在线备份连页 1 的头一起拷，先设后拷会被覆盖），
   打包前还有一条"目录里只有 ``kylab.db``"的断言（在打包那一层——那条断言说的是
   "交给打包器的那份东西长什么样"）。

**判据用例（最强的那个）**：造一份含哨兵串（``kylab_sk_SENTINEL…``）的本机库 →
打快照 → 在**包解开后的原始字节**里 grep 哨兵串 → 必须 0 命中
（``tests/unit/services/test_backup_snapshot.py``）。本模块自己的用例还多一条：
**同一份库如果不擦洗，那些哨兵串在副本字节里是查得到的**——那证明那条 grep 真的查得出
东西，而不是在查一份本来就没有秘密的库。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.base import (
    SNAPSHOT_EXCLUDED_SETTING_PREFIXES,
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    SessionEventRecord,
    SnapshotArtifactRef,
    SnapshotConversationRef,
    SnapshotDbView,
    SnapshotDumpReport,
    SnapshotFormatError,
    SnapshotRedaction,
    StorageError,
)
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.schema import SCHEMA_VERSION, current_version

__all__ = [
    "dump_scrubbed_db",
    "iter_snapshot_transfers",
    "local_schema_version",
    "read_snapshot_db",
]

logger = logging.getLogger(__name__)

_CREDENTIAL_SETTINGS_TABLE = "app_settings"
"""凭据类设置住的那张表：这份报告里"被清的是哪张表"这个名字（方案 §2.4 的第三条判据）。

判据本身不是它（那是 ``SECRET_KEYS`` 与三个前缀），这条只是那两处报告要写的表名。
下面两条 SQL 里写的是字面量 ``app_settings``——SQL 文本必须是常量，表名不该被拼进去
（那正是 ``S608`` 要拦的形状）。
"""

_EXCLUDED_SETTINGS_PREDICATE = (
    "key IN (SELECT value FROM json_each(?))"
    " OR EXISTS (SELECT 1 FROM json_each(?) AS prefix"
    "            WHERE lower(substr(app_settings.key, 1, length(prefix.value)))"
    "                  = prefix.value)"
)
"""擦洗 ``app_settings`` 的**判据本身**，两个 ``?`` 分别是凭据键与三个前缀。

写成"键清单 + 前缀清单当参数"，而不是把键名拼进 SQL 文本：拼进去的话，这段判据就成了
一段**不可核对**的字符串（用例只能拿它自己去比它自己），而参数化之后用例可以直接断言
"把 ``SECRET_KEYS`` 里每个键都种进库、打一份快照，包里一个都不剩"。

前缀按小写比（前缀本身就是小写的）＝**大小写不敏感**：``Provider.X`` 这类大小写变体
一并排除。方向偏保守——宁可多排除一个键，也不漏一个凭据。
"""

# 下面两条是**同一段判据**（`_EXCLUDED_SETTINGS_PREDICATE`）的两个动作：查出来报数、
# 删掉。拼接的是模块级常量（没有任何外部输入进 SQL 文本），参数永远走 `?`——
# 这正是 `noqa: S608` 允许的形状（与 `postgres_impl` / `duckdb_impl` 里那几处同一口径）。
_SELECT_EXCLUDED_SETTINGS = (
    "SELECT key FROM app_settings WHERE " + _EXCLUDED_SETTINGS_PREDICATE + " ORDER BY key"  # noqa: S608
)

_DELETE_EXCLUDED_SETTINGS = (
    "DELETE FROM app_settings WHERE " + _EXCLUDED_SETTINGS_PREDICATE  # noqa: S608
)

_COUNTS_SQL = """
SELECT
    (SELECT COUNT(*) FROM conversations)          AS conversations,
    (SELECT COUNT(*) FROM chat_messages)          AS messages,
    (SELECT COUNT(*) FROM session_events)         AS session_events,
    (SELECT COUNT(*) FROM notes)                  AS notes,
    (SELECT COUNT(*) FROM workspaces)             AS workspaces,
    (SELECT COUNT(*) FROM scheduled_tasks)        AS scheduled_tasks,
    (SELECT COUNT(*) FROM conversation_artifacts) AS artifacts,
    (SELECT COUNT(*) FROM app_settings)           AS settings
"""
"""库侧事实的行数（一次查询给全，键名就是 manifest ``counts`` 段的那一批）。

**不逐个 SELECT**：八个计数器分八次查除了八次往返，还要在 Python 里拼一个字典，
而拼错键名这件事没有任何东西会红——一次查询回来的列名就是那份名字。
"""


def _secret_keys() -> frozenset[str]:
    """``runtime_config`` 的 **``SECRET_KEYS`` 常量本身**（方案 §2.4 的判据）。

    函数内 import，而不是模块级：存储层不该在模块级反向依赖 ``services``（这条反向依赖
    只允许存在一处、只为一个常量），而"现取"与"模块级取"在防漂上是同一件事——对象只有
    一个。将来在 ``SECRET_KEYS`` 里加一个键，擦洗自动跟上，这里一个字不用改。
    """
    from app.services.runtime_config import SECRET_KEYS

    return SECRET_KEYS


def _scrub_params() -> tuple[str, str]:
    """判据参数（凭据键 / 三个前缀），两个都是 JSON 数组文本。

    排序是刻意的：报告里那份"删了哪几个键"因此稳定（同一个库两次擦洗给同样的清单），
    用例也能拿它当预期值。
    """
    return (
        json.dumps(sorted(_secret_keys())),
        json.dumps(sorted(SNAPSHOT_EXCLUDED_SETTING_PREFIXES)),
    )


class _ConnectionAsDatabase:
    """把一条**已开着的**连接包装成 ``current_version`` 要的那个形状（只为读版本号）。

    ``current_version`` 收的是 ``Database``（它只用 ``db.read()`` 借一条连接），而这里手上
    已经有一条副本连接——为读一个整数再开一个 ``Database``，会走 ``open()`` 那套
    （它会把库切到 WAL，而那正是副本不要的东西）。所以给一个最小适配器：
    借出**同一条**连接，不开新连接。版本号的读法仍然只有 ``current_version`` 一处
    （在本模块里再手写一遍 ``SELECT value FROM schema_metadata`` 就是第二个真相源）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self._conn


def _open_sink(dest: Path) -> sqlite3.Connection:
    """建副本的连接（``journal_mode`` 由 ``dump_scrubbed_db`` 在备份之后再切，见那里的说明）。

    ``isolation_level=None``（自动提交）与 ``Database._connect`` 同一个理由：默认的
    "隐式 BEGIN"会让 ``PRAGMA`` 落在一个未提交的事务里而**静默失效**（``connection.py``
    那句实测结论）。事务由本模块显式 ``BEGIN IMMEDIATE`` / ``COMMIT``。
    """
    for leftover in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm"), Path(f"{dest}-journal")):
        if leftover.exists():
            leftover.unlink()
    conn = sqlite3.connect(dest, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _scrub(conn: sqlite3.Connection) -> tuple[SnapshotRedaction, ...]:
    """在副本上擦洗，返回"洗掉了什么"（**按表名排序，稳定**）。

    调用方已经把这三条动作包在一个事务里：要么一起生效，要么一起回滚。半截擦洗
    （密钥清了、MCP 那份没清）在报告里会是一份骗人的清单。
    """
    entries: list[SnapshotRedaction] = []

    # ① 模型凭据：列清空、行留着。恢复之后这一列是空的，报告里如实说"要重配"（R13）——
    #    "删掉整个供应商记录"是另一件事（用户还得重新填地址与模型名）。
    cursor = conn.execute("UPDATE model_providers SET api_key = '' WHERE api_key <> ''")
    if cursor.rowcount:
        entries.append(
            SnapshotRedaction(table="model_providers", column="api_key", rows=cursor.rowcount)
        )

    # ② MCP 的任意键值对：两列一起清成空对象（方案 §2.3 的 redacted 段就是这么一条）。
    cursor = conn.execute(
        "UPDATE mcp_servers SET env = '{}', headers = '{}' WHERE env <> '{}' OR headers <> '{}'"
    )
    if cursor.rowcount:
        entries.append(
            SnapshotRedaction(table="mcp_servers", column="env,headers", rows=cursor.rowcount)
        )

    # ③ 设置里的凭据键：**整行删**（方案 §2.4）。逐个键报数而不是并成一堆：恢复报告要说得清
    #    "少了哪几个键"，而 app_settings 是主键表，一条键就是一行。
    params = _scrub_params()
    found = [str(row["key"]) for row in conn.execute(_SELECT_EXCLUDED_SETTINGS, params)]
    if found:
        conn.execute(_DELETE_EXCLUDED_SETTINGS, params)
        entries.extend(
            SnapshotRedaction(table=_CREDENTIAL_SETTINGS_TABLE, key=key, rows=1) for key in found
        )

    return tuple(entries)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """库侧事实的行数（``_COUNTS_SQL`` 一次查询）。

    ``dict(row)`` 而不是 ``for key in row``：``sqlite3.Row`` 的迭代给的是**值**（它像元组），
    而这里要的是列名——``dict(row)`` 走的是它 ``keys()`` 那条路。
    """
    row = conn.execute(_COUNTS_SQL).fetchone()
    return {name: int(value) for name, value in dict(row).items()} if row is not None else {}


def _refuse_newer_schema(version: int, db_path: Path) -> int:
    """快照库的 schema 版本：比本机新就**当场拒绝**（照 ``schema.py`` 那条"不降级"纪律）。"""
    if version <= 0:
        raise SnapshotFormatError(
            f"这份快照里的库没有版本号（读到 {version}，{db_path}）："
            "它不是本应用打出来的库，或者被打断了"
        )
    if version > SCHEMA_VERSION:
        raise SnapshotFormatError(
            f"这份快照的 schema 版本是 {version}，高于本机已知的 {SCHEMA_VERSION}；"
            "快照是被更新版应用打的，请升级应用，而不是按旧结构猜着恢复"
        )
    return version


def dump_scrubbed_db(db: Database, dest: str | Path) -> SnapshotDumpReport:
    """把 ``db`` **在线备份**到 ``dest``（源库不变），在副本上擦洗，``VACUUM``，读回数。

    顺序**不许换**（方案 §2.4 的四条）：

    ``backup()`` → ``journal_mode=DELETE`` → ``secure_delete=ON`` → 删 / 清 → ``VACUUM``

    - 先备份：用 ``sqlite3.Connection.backup()``（在线备份 API）而不是照文件拷贝——
      WAL 模式下库文件只是数据的一部分（最近的提交在 ``-wal`` 里），照文件拷会得到一份
      "少了最后一截"的库。在线备份走同一套页协议，拷出来的是一致的快照，
      **复制期间不停边车**（与 ``schema.py`` 迁移前备份同一个手法）。
    - 先擦洗再 ``VACUUM``：``VACUUM`` 会**重建**整个库文件，所以它必须发生在删 / 清之后
      ——反过来做，被删掉的明文就还留在旧页里等着被捡走。

    源库一个字节不动：全程只读它（``backup()`` 的源端），所有写都落在 ``dest`` 上。
    """
    db_path = Path(dest)
    if db_path.resolve() == db.path.resolve():
        # _open_sink 会先删掉已存在的那条路径——落在源库上就等于把用户的库删了。
        # 调用方本该给一个空目录（打包那一层给的是临时工作目录），这条拦的是写错参数。
        raise StorageError(
            f"快照副本不能落在源库自己的路径上（{db_path}）：换一个目录（打包时会用临时目录）"
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    source = db.connection()
    sink = _open_sink(db_path)
    try:
        source.backup(sink)

        # 副本切成回滚日志模式，且**必须在 backup() 之后**做：在线备份连页 1 的头一起拷，
        # 而 journal_mode 就在那个头里——先设后拷会被拷回来的头覆盖（实测：副本又变回 WAL）。
        # 为什么非要切：WAL 库要有 ``-shm`` 才能读，而这份副本将来会被**只读打开**
        # （阶段 5 的按点恢复读的就是它）——"必须能写才能读"的副本在恢复现场就是个坑。
        # 顺带把 backup 期间可能产生的 ``-wal`` 检查点收掉（切模式时 SQLite 自己做）。
        sink.execute("PRAGMA journal_mode = DELETE")

        # secure_delete 在连接上设，必须在下面那些 UPDATE/DELETE 之前：它管的是"被改、
        # 被删的那些页腾出来时要不要先抹零"。只靠 VACUUM 也能收掉重建之后的空闲页，
        # 但两份保险都在，才是方案 §2.4 要的那一条。
        sink.execute("PRAGMA secure_delete = ON")
        sink.execute("BEGIN IMMEDIATE")
        try:
            redacted = _scrub(sink)
        except BaseException:
            sink.execute("ROLLBACK")
            raise
        else:
            sink.execute("COMMIT")

        # VACUUM 不能在事务里（SQLite 直接报错）：它重建整个库文件，于是擦洗腾出来的那些
        # 页连同它们的明文一起消失——这一步才是"真的洗掉"。
        sink.execute("VACUUM")

        counts = _counts(sink)
        version = current_version(_ConnectionAsDatabase(sink)) or 0
    finally:
        sink.close()

    report = SnapshotDumpReport(
        path=db_path,
        bytes=db_path.stat().st_size,
        schema_version=version,
        counts=counts,
        redacted=redacted,
    )
    logger.info(
        "快照副本已擦洗：%s（%d 字节，schema v%d，洗掉 %d 项）",
        db_path,
        report.bytes,
        report.schema_version,
        len(report.redacted),
    )
    return report


def local_schema_version() -> int:
    """本机库的 schema 版本（``schema.SCHEMA_VERSION`` 本身，**不另抄一个数**）。

    给的是"这一层认得的版本"：打包时写进 manifest、读一份快照时用来判"认不认识"。
    两处判据（``read_snapshot_db`` / ``iter_snapshot_transfers``）用的也是这一个常量，
    所以"拒绝更新的快照"这条纪律不会因为谁少改了一处而失效。
    """
    return SCHEMA_VERSION


def _read_only(db_path: Path) -> sqlite3.Connection:
    """只读打开一份快照库（``mode=ro``）。

    只读是刻意的（``SnapshotSource.read_snapshot_db`` 那份契约）：快照是观察对象，
    读它不该产生 ``-journal`` / ``-shm``，也不该给"顺手改一下"留下任何可能。
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def read_snapshot_db(db_path: str | Path) -> SnapshotDbView:
    """读一份快照库（``<解包目录>/db/kylab.db``），回四项窄投影。

    读的是**文件**而不是本机库：调用方给的是一条路径（阶段 5 里那是解包出来的 staging
    目录）。所以这个方法不碰"当前库"——它住在这里是因为"SQLite 方言只许住在
    ``sqlite_impl/``"这条纪律，而不是因为它读的是本机库。

    版本不认识（比本机新 / 没有版本号）→ ``SnapshotFormatError``，**不猜着读**
    （方案 §2.3 的兼容判据：旧代码读新结构的结果是静默的数据损坏）。
    """
    path = Path(db_path)
    conn = _read_only(path)
    try:
        version = _refuse_newer_schema(current_version(_ConnectionAsDatabase(conn)) or 0, path)
        counts = _counts(conn)
        conversations = tuple(
            SnapshotConversationRef(
                id=str(row["id"]),
                title=str(row["title"] or ""),
                messages=int(row["messages"]),
                updated_at_ms=int(row["updated_at_ms"]),
            )
            for row in conn.execute(
                "SELECT c.id, c.title, c.updated_at_ms,"
                "       (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id)"
                "           AS messages"
                "  FROM conversations c"
                " ORDER BY c.updated_at_ms DESC, c.id"
            )
        )
        artifacts = tuple(
            SnapshotArtifactRef(
                id=str(row["id"]),
                conversation_id=str(row["conversation_id"]),
                name=str(row["name"]),
                format=str(row["format"]),
                size_bytes=int(row["size_bytes"]),
                storage=str(row["storage"]),
                location=str(row["location"]),
                workspace_id=row["workspace_id"],
            )
            for row in conn.execute(
                "SELECT id, conversation_id, name, format, size_bytes, storage, location,"
                "       workspace_id"
                "  FROM conversation_artifacts"
                " ORDER BY created_at_ms, id"
            )
        )
        settings = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM app_settings ORDER BY key")
        }
        model_provider_names = tuple(
            str(row["name"]) for row in conn.execute("SELECT name FROM model_providers ORDER BY id")
        )
    finally:
        conn.close()

    return SnapshotDbView(
        schema_version=version,
        counts=counts,
        conversations=conversations,
        artifacts=artifacts,
        settings_keys=tuple(settings),
        settings=settings,
        model_provider_names=model_provider_names,
    )


# ---------------------------------------------------------------- 按会话读全量（恢复用）


def _load(ms: int | None) -> datetime | None:
    """列里的 UTC 毫秒 → aware ``datetime``（与 ``meta_store._load`` 同一个口径）。

    在这一层重写一遍是刻意的：``meta_store`` 的那个 helper 是私有的，而"存储层去
    import 服务层"（``conversation_export._load_ms``）是**反向依赖**——两行换算不值得
    把依赖方向反过来。
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def _json_list(raw: Any) -> tuple[dict[str, Any], ...]:
    """JSON 数组列 → 记录里的元组（解不出来就当空——**形状坏了不该让恢复整条崩**）。

    与 ``conversation_export`` 里那几处 ``tuple(dict(item) for item in ...)`` 同一口径：
    快照库里的这几列是本应用自己写进去的，坏了说明库被改过；那时"少几段引用"比
    "整条会话恢复不了"好，而且它不会静默——会话正文与其余字段照旧完整。
    """
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _conversation_from_row(row: sqlite3.Row) -> tuple[ConversationRecord, str, str | None]:
    """``conversations`` 一行 → ``(记录, 摘要, 摘要上界)``。

    字段映射与 ``conversation_export.conversation_from_data`` **逐字段对齐**（同一批
    记录类型、同一套三态 / JSON 列解码）：两条来源（NAS 的 NDJSON 流与快照库）产出
    同一种 ``ConversationTransfer``，导入器才只需要认一件事。
    """
    kb_ids: tuple[str, ...] = ()
    try:
        parsed = json.loads(row["kb_ids"] or "[]")
    except (TypeError, ValueError):
        parsed = []
    if isinstance(parsed, list):
        kb_ids = tuple(str(item) for item in parsed)
    record = ConversationRecord(
        id=str(row["id"]),
        title=str(row["title"] or ""),
        kb_ids=kb_ids,
        owner_id=row["owner_id"],
        model_pk=row["model_pk"],
        # 三态列：NULL 保持 None（"跟随全局默认"是一个真实状态，不折成 False）
        thinking=None if row["thinking"] is None else bool(row["thinking"]),
        thinking_effort=row["thinking_effort"],
        pinned=bool(row["pinned"]),
        workspace_id=row["workspace_id"],
        archived_at=_load(row["archived_at_ms"]),
        created_at=_load(row["created_at_ms"]),
        updated_at=_load(row["updated_at_ms"]),
    )
    return record, str(row["context_summary"] or ""), row["summary_upto"]


def _messages_of(conn: sqlite3.Connection, conversation_id: str) -> list[ChatMessageRecord]:
    """``created_at_ms, rowid`` 升序——与 ``SqliteMetaStore.list_messages`` 逐字一致。"""
    return [
        ChatMessageRecord(
            id=str(row["id"]),
            conversation_id=conversation_id,
            role=str(row["role"]),
            content=str(row["content"] or ""),
            sources=_json_list(row["sources"]),
            steps=_json_list(row["steps"]),
            thinking=str(row["thinking"] or ""),
            attachments=_json_list(row["attachments"]),
            created_at=_load(row["created_at_ms"]),
        )
        for row in conn.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY created_at_ms, rowid",
            (conversation_id,),
        )
    ]


def _events_of(conn: sqlite3.Connection, conversation_id: str) -> list[SessionEventRecord]:
    """``ORDER BY seq``——**不按时间戳**，与 ``list_session_events`` 同一条理由：
    同一毫秒里的一批并发工具调用，只有 seq 分得出先后。
    """
    return [
        SessionEventRecord(
            conversation_id=conversation_id,
            kind=str(row["kind"]),
            payload=dict(json.loads(row["payload"] or "{}")),
            seq=int(row["seq"]),
            id=int(row["id"]) if row["id"] is not None else None,
            created_at=_load(row["created_at_ms"]),
        )
        for row in conn.execute(
            "SELECT * FROM session_events WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        )
    ]


def _artifacts_of(
    conn: sqlite3.Connection, conversation_id: str
) -> list[ConversationArtifactRecord]:
    """``created_at_ms, id`` 升序——与 ``list_artifacts`` 逐字一致。"""
    return [
        ConversationArtifactRecord(
            id=str(row["id"]),
            conversation_id=conversation_id,
            name=str(row["name"] or ""),
            format=str(row["format"] or ""),
            size_bytes=int(row["size_bytes"] or 0),
            storage=str(row["storage"]),
            # location **保原样**：对象档是 data_dir 下的 Key，工作区档是那台机器上的
            # 绝对路径（恢复报告据此说"哪几份的字节没跟过来"）
            location=str(row["location"] or ""),
            workspace_id=row["workspace_id"],
            owner_id=row["owner_id"],
            knowledge_base_id=row["knowledge_base_id"],
            document_id=row["document_id"],
            created_at=_load(row["created_at_ms"]),
        )
        for row in conn.execute(
            "SELECT * FROM conversation_artifacts WHERE conversation_id = ?"
            " ORDER BY created_at_ms, id",
            (conversation_id,),
        )
    ]


def iter_snapshot_transfers(db_path: str | Path) -> Iterator[ConversationTransfer]:
    """逐条读一份快照库的全量会话（M5 阶段 5：按点恢复的输入）。

    实现与纪律：

    - **只读打开**（``_read_only``，与 ``read_snapshot_db`` 同一个口子）——快照是观察
      对象，读它不产生 ``-journal`` / ``-wal``；
    - **生成器**：会话一条一条读、消息/事件/产物各一次查询，内存里最多一条会话
      （与 ``HttpExportSource`` 的生产者同一条上界纪律）；
    - **顺序**：会话按 ``updated_at_ms DESC, id``（与 ``read_snapshot_db`` 的会话清单
      一致）；三张子表按上面的 helper 各自对齐存储层那三条 ``ORDER BY``；
    - 版本不认识（比本机新 / 没有版本号）→ ``SnapshotFormatError``，**不猜着读**。

    调用方（``services/backup_restore.SnapshotFileSource``）把它当"另一个导出流"用：
    导入器对来源的唯一要求就是"逐条吐 ``ConversationTransfer``"。
    """
    path = Path(db_path)
    conn = _read_only(path)
    try:
        _refuse_newer_schema(current_version(_ConnectionAsDatabase(conn)) or 0, path)
        ids = [
            str(row["id"])
            for row in conn.execute("SELECT id FROM conversations ORDER BY updated_at_ms DESC, id")
        ]
        for conversation_id in ids:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - 刚列出来就没了，说明库在被改
                continue
            record, summary, summary_upto = _conversation_from_row(row)
            yield ConversationTransfer(
                conversation=record,
                summary=summary,
                summary_upto=summary_upto,
                messages=_messages_of(conn, conversation_id),
                events=_events_of(conn, conversation_id),
                artifacts=_artifacts_of(conn, conversation_id),
            )
    finally:
        conn.close()
