"""业务服务层。

纪律（工程规范 §3.3）：本包禁止直接写 SQL，存储访问一律经 ``storage/`` 的
Repository 接口——这是 SQLite 迁移 PostgreSQL 的保险费。
"""
