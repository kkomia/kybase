"""存储后端自检（运维入口）。

用途：部署后、排查"库到底通不通/扩展有没有/schema 对不对"时跑一条命令，
不必先起整个应用。也是启动路径上调用的同一套校验（``prepare``），
所以这里通过就意味着应用能起来。

    python -m app.storage.postgres_impl.validate
    python -m app.storage.postgres_impl.validate "postgresql://用户:口令@主机:5432/库"

缺省 DSN 取 ``KYLAB_DATABASE_URL``；未配置时明确报错并退出（不静默跳过）。
"""

from __future__ import annotations

import sys

from app.core.config import get_settings
from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.schema import SCHEMA_VERSION, prepare

_TABLE_COUNT_SQL = """
select count(*) as n
  from information_schema.tables
 where table_schema = 'public' and table_type = 'BASE TABLE'
"""


def _redact(dsn: str) -> str:
    """连接串脱敏：只留协议、主机端口与库名，口令与用户名都不打印。"""
    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dsn = args[0] if args else get_settings().database_url

    if not dsn:
        print("未配置 KYLAB_DATABASE_URL。存储后端在 PG 实现落地后为必填。")
        return 2

    print(f"目标：{_redact(dsn)}")
    db = Database(dsn)
    try:
        db.open()
    except Exception as exc:
        print(f"[FAIL] 连接失败：{exc}")
        return 1

    try:
        with db.read() as conn:
            row = conn.execute("select version() as v").fetchone()
            print(f"[OK  ] 已连接：{row['v'].split(' on ')[0] if row else '未知版本'}")

        version = prepare(db)
        print(f"[OK  ] pgvector 等必需扩展齐备，schema 版本 = {version}")

        with db.read() as conn:
            tables = conn.execute(_TABLE_COUNT_SQL).fetchone()
            print(f"[OK  ] public 基础表 {tables['n']} 张（含扩展自建表）")

        with db.read() as conn:
            # 借一次真实的 KNN 往返确认向量能力可用，而不只是"扩展装了"
            conn.execute("select '[1,2,3]'::vector <=> '[1,2,4]'::vector as d").fetchone()
            print("[OK  ] pgvector 距离运算可用")
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1
    finally:
        db.close()

    print(f"\n结论：存储后端可用（schema 版本 v{SCHEMA_VERSION}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
