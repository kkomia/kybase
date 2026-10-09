"""PostgreSQL 存储实现（v0.12 起唯一主实现，SQLite 已退役）。

由 ``app/core/storage.py::build_stores()`` 装配。装配点唯一，``services/`` 只依赖
``app/storage/base.py`` 的接口，所以这次从 SQLite 切过来业务层一行未改
（``scripts/check_layering.py`` 的 L2 规则守着这条边界）。

- ``connection.py``   连接池与事务上下文
- ``schema.py``       schema 基线的创建与启动校验（缺扩展、版本不符都启动即失败）
- ``schema.sql``      基线 DDL（取代 SQLite 那 24 条增量迁移，不迁移数据）
- ``meta_store.py``   元数据仓储
- ``vector_store.py`` 向量仓储（pgvector + HNSW，一库一表）
- ``fulltext_store.py`` 全文仓储（tsvector 生成列 + jieba 预切词）
- ``validate.py``     运维自检入口：``python -m app.storage.postgres_impl.validate``
"""
