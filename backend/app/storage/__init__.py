"""存储抽象层。

``base.py`` 定义 ``MetaStore`` / ``VectorStore`` / ``FullTextStore`` /
``ObjectStore`` / ``TabularStore`` 接口，实现按存储分目录平级放置：

- ``postgres_impl/`` —— 元数据 + 向量(pgvector) + 全文(tsvector)，主实现；
- ``local_impl/``    —— 本地文件系统对象存储；
- ``s3_impl/``       —— S3 兼容对象存储（MinIO / AWS）；
- ``duckdb_impl/``   —— 表格型文档的结构化副本。

装配见 ``core/storage.py::build_stores()``：postgres_impl + S3/本地目录 + duckdb_impl。
（v0.1.0 瘦身：本机档的 ``sqlite_impl`` / ``split_impl`` 与它们独有那几块方法族
一并拆除，本服务只剩 PG 一条存储形态。）
"""
