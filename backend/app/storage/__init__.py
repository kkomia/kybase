"""存储抽象层。

``base.py`` 定义 ``MetaStore`` / ``VectorStore`` / ``FullTextStore`` /
``ObjectStore`` / ``TabularStore`` 接口，实现按存储分目录平级放置：

- ``postgres_impl/`` —— 元数据 + 向量(pgvector) + 全文(tsvector)，**服务器档**的主实现；
- ``sqlite_impl/``   —— 元数据（会话/笔记/设置/工作区/…），**本机档**实现，
  落 ``<data_dir>/kylab.db``（M2「会话落本机」阶段 1）。另含**四块本机独有**的
  方法族（都不在 ``MetaStore`` 上——服务器档要么没有那两张表、要么没有那条动作，
  各自登记在 ``sqlite_impl`` 的同名常量里）：旧会话导入台账
  （``imports`` / ``import_items``，M2，``LOCAL_LEDGER_METHODS``）、知识库元数据快照
  （``kb_meta_cache``，M4，``LOCAL_CACHE_METHODS``）、快照打包与读回
  （M5 阶段 2，``LOCAL_SNAPSHOT_METHODS``）、备份待传队列
  （``backup_snapshots``，M5 阶段 3，``LOCAL_BACKUP_METHODS``）；
- ``split_impl/``    —— 按域分流的 ``RouterMetaStore``：本机域走 sqlite_impl，
  知识库域转给 KB 侧实现（M2 阶段 2）；
- ``local_impl/``    —— 本地文件系统对象存储；
- ``s3_impl/``       —— S3 兼容对象存储（MinIO / AWS）；
- ``duckdb_impl/``   —— 表格型文档的结构化副本。

档位由 ``KYLAB_DEPLOYMENT`` 决定（``core/storage.py::build_stores()`` 按档装配）：
服务器档（默认）用 postgres_impl + S3/本地目录 + duckdb_impl，本机档用
sqlite_impl + split_impl + 本地目录，且**不 import** psycopg / boto3 / duckdb。
"""
