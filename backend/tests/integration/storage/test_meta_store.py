"""``MetaStore`` 的行为测试（集成）。

覆盖架构里靠元数据兑现的承诺：文件去重（§6.3）、断点续跑的任务租约（§4）、
回收站 7 天（§6.2）、embedding 模型锁（§6.4）、大文件切分（§4.2）。

这套用例是把 921 行的行为契约喂给仓储接口——**当初正是靠它把一个实现换成
另一个实现**（SQLite → PostgreSQL）而不用重写用例。需要 PostgreSQL 测试库
（``KYLAB_TEST_DATABASE_URL``），未配置则整文件跳过。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ConflictError, InvalidRequestError
from app.models.enums import (
    ApiKeyPermission,
    DataSourceKind,
    DocumentStage,
    SharePermission,
    TaskKind,
    TaskState,
    TrashKind,
    UserRole,
)
from app.storage.base import (
    ApiKeyRecord,
    ChatMessageRecord,
    ChunkRecord,
    ConversationRecord,
    DataSourceRecord,
    DocumentPartRecord,
    DocumentRecord,
    ImageRecord,
    KnowledgeBaseRecord,
    MetaStore,
    ParseResultRecord,
    SessionEventRecord,
    SessionRecord,
    ShareRecord,
    TaskRecord,
    TrashRecord,
    UserRecord,
    WebhookRecord,
)


def utc_now() -> datetime:
    """本地时间助手（不从 conftest 导入，避免测试模块之间互相耦合）。"""
    return datetime.now(UTC)


def _chunk(chunk_id: str, ordinal: int, *, document_id: str = "doc_1", kb_id: str = "kb_1",
           text: str = "正文", image_ids: tuple[str, ...] = (),
           questions: tuple[str, ...] = ()) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        knowledge_base_id=kb_id,
        part_id=None,
        ordinal=ordinal,
        text=text,
        content_hash=f"h-{chunk_id}",
        heading_path="第1章 > 1.1",
        page=ordinal + 1,
        image_ids=image_ids,
        questions=questions,
    )


def _task(task_id: str, *, state: TaskState = TaskState.PENDING, attempts: int = 0,
          max_attempts: int = 5, next_run_at=None, document_id: str | None = None,
          kind: TaskKind = TaskKind.PARSE, created_at=None, updated_at=None) -> TaskRecord:
    """document_id 默认为 None：并非所有任务都挂文档（如数据源拉取）。"""
    return TaskRecord(
        id=task_id,
        kind=kind,
        state=state,
        payload={"stage": "parsing"},
        document_id=document_id,
        attempts=attempts,
        max_attempts=max_attempts,
        next_run_at=next_run_at,
        created_at=created_at,
        updated_at=updated_at,
    )


# --------------------------------------------------------------------- 知识库


def test_create_and_get_knowledge_base(store: MetaStore) -> None:
    record = store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_9", name="研究库", embedding_model_id="BAAI/bge-m3",
                            embedding_dim=1024)
    )
    assert record.created_at is not None

    loaded = store.get_knowledge_base("kb_9")
    assert loaded is not None
    assert (loaded.name, loaded.embedding_dim, loaded.chunk_strategy) == ("研究库", 1024, "fixed")
    assert store.get_knowledge_base("nope") is None


def test_list_knowledge_bases_returns_all(store: MetaStore, kb) -> None:
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_2", name="第二个", embedding_model_id="m", embedding_dim=768)
    )
    assert {item.id for item in store.list_knowledge_bases()} == {"kb_1", "kb_2"}


def test_update_knowledge_base_embedding(store: MetaStore, kb) -> None:
    """架构 §6.4：允许换 endpoint，模型 ID 与维度随库更新。"""
    store.update_knowledge_base_embedding(
        "kb_1", model_id="BAAI/bge-m3", dim=1024, base_url="http://127.0.0.1:11434/v1"
    )
    loaded = store.get_knowledge_base("kb_1")
    assert loaded is not None
    assert loaded.embedding_base_url == "http://127.0.0.1:11434/v1"


# --------------------------------------------------------------------- 文档与去重


def test_get_document_by_hash_finds_duplicate(store: MetaStore, kb, document) -> None:
    """架构 §6.3：上传前先按 hash 查重，命中则提醒"检测到相同文件"。"""
    found = store.get_document_by_hash("kb_1", "hash-1")
    assert found is not None
    assert found.id == document.id
    assert store.get_document_by_hash("kb_1", "other") is None


def test_duplicate_document_in_same_kb_is_rejected(store: MetaStore, kb, document) -> None:
    # 断言的是"数据库拒绝了"，不是驱动的报错措辞：
    # SQLite 说 "UNIQUE constraint failed"，PG 说 "duplicate key value violates
    # unique constraint"。按措辞断言会把这个跨后端的行为测试绑死在一个驱动上。
    with pytest.raises(Exception, match=r"(?i)unique"):
        store.create_document(
            DocumentRecord(
                id="doc_dup",
                knowledge_base_id="kb_1",
                name="副本.md",
                source_kind=DataSourceKind.UPLOAD,
                content_hash="hash-1",
                stage=DocumentStage.UPLOADED,
            )
        )


def test_update_document_stage_records_error(store: MetaStore, document) -> None:
    store.update_document_stage("doc_1", DocumentStage.PARSING)
    assert store.get_document("doc_1").stage is DocumentStage.PARSING  # type: ignore[union-attr]

    store.update_document_stage("doc_1", DocumentStage.FAILED, error="云端额度耗尽")
    loaded = store.get_document("doc_1")
    assert loaded is not None
    assert loaded.error == "云端额度耗尽"


def test_list_documents_is_newest_first(store: MetaStore, kb, document) -> None:
    store.create_document(
        DocumentRecord(
            id="doc_2",
            knowledge_base_id="kb_1",
            name="b.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="hash-2",
            stage=DocumentStage.UPLOADED,
        )
    )
    ids = [item.id for item in store.list_documents("kb_1")]
    assert ids[0] == "doc_2" and set(ids) == {"doc_1", "doc_2"}


def test_list_documents_pages_and_counts_with_filters(
    store: MetaStore, kb, document
) -> None:
    """分页与计数用同一套过滤条件：翻到任何一页，总数都不该变。

    这里同时钉住"count 不会因为 limit 变小"——那正是分页界面显示
    "共 N 篇"时最怕的错。
    """
    for index in range(1, 6):
        store.create_document(
            DocumentRecord(
                id=f"doc_p{index}",
                knowledge_base_id="kb_1",
                name=f"合同 {index}.md" if index <= 3 else f"预算 {index}.md",
                source_kind=DataSourceKind.UPLOAD,
                content_hash=f"hash-p{index}",
                stage=DocumentStage.UPLOADED,
            )
        )

    assert store.count_documents("kb_1") == 6  # 含 fixture 的 doc_1
    page_one = store.list_documents("kb_1", limit=2, offset=0)
    page_two = store.list_documents("kb_1", limit=2, offset=2)
    page_three = store.list_documents("kb_1", limit=2, offset=4)
    assert [len(page) for page in (page_one, page_two, page_three)] == [2, 2, 2]
    ids = [item.id for page in (page_one, page_two, page_three) for item in page]
    assert len(ids) == len(set(ids)) == 6  # 页与页不重不漏

    # 过滤条件同时作用于 list 与 count：3 篇合同
    assert store.count_documents("kb_1", q="合同") == 3
    filtered = store.list_documents("kb_1", q="合同", limit=1, offset=1)
    assert len(filtered) == 1 and "合同" in filtered[0].name

    # 越界页回空列表，但总数照旧——界面据此把页码夹回最后一页
    assert store.list_documents("kb_1", limit=2, offset=99) == []
    assert store.count_documents("kb_1") == 6

    # 不过滤 stage 时把 uploaded 与别的阶段分开算
    store.update_document_stage("doc_p1", DocumentStage.INDEXED)
    assert store.count_documents("kb_1", stage=DocumentStage.UPLOADED.value) == 5

    # limit=None（内部调用点）不受分页影响
    assert len(store.list_documents("kb_1")) == 6


def test_pagination_is_stable_when_created_at_ties(store: MetaStore, kb) -> None:
    """同一秒上传的文档 ``created_at`` 相同，只按它排时分页会漏行或重复。

    定序键必须能唯一定序（这里补了 ``id``）：否则 SQLite 对等值行的顺序不作保证，
    两次 LIMIT/OFFSET 查询各排各的，翻页就会出现"第 2 页少了 A、第 3 页又出现 A"。
    """
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(4):
        store.create_document(
            DocumentRecord(
                id=f"doc_tie{index}",
                knowledge_base_id="kb_1",
                name=f"同秒 {index}.md",
                source_kind=DataSourceKind.UPLOAD,
                content_hash=f"hash-tie{index}",
                stage=DocumentStage.UPLOADED,
                created_at=stamp,
            )
        )

    first = store.list_documents("kb_1", limit=2, offset=0)
    second = store.list_documents("kb_1", limit=2, offset=2)
    ids = [item.id for item in (*first, *second)]
    assert len(ids) == len(set(ids)) == 4


# --------------------------------------------------------------------- 子文件


def test_document_parts_are_ordered_by_index(store: MetaStore, document) -> None:
    """架构 §4.2：大文件按页范围切子文件，顺序必须稳定。"""
    store.create_document_parts(
        [
            DocumentPartRecord(id="part_2", document_id="doc_1", part_index=2, page_start=401,
                               page_end=600, stage=DocumentStage.UPLOADED),
            DocumentPartRecord(id="part_1", document_id="doc_1", part_index=1, page_start=201,
                               page_end=400, stage=DocumentStage.UPLOADED),
        ]
    )
    parts = store.list_document_parts("doc_1")
    assert [part.part_index for part in parts] == [1, 2]
    assert parts[0].page_start == 201

    store.update_part_stage("part_1", DocumentStage.INDEXED)
    assert store.list_document_parts("doc_1")[0].stage is DocumentStage.INDEXED


# --------------------------------------------------------------------- chunk


def test_replace_chunks_stores_order_and_image_anchors(store: MetaStore, kb,
                                                       document) -> None:
    """架构 §7：图片不入向量库，只在 chunk 元数据里记锚点。"""
    store.replace_chunks(
        "doc_1",
        [_chunk("c2", 1, image_ids=("img_1", "img_2")), _chunk("c1", 0)],
    )
    chunks = list(store.iter_chunks("doc_1"))
    assert [chunk.chunk_id for chunk in chunks] == ["c1", "c2"]
    assert tuple(chunks[1].image_ids) == ("img_1", "img_2")
    assert chunks[0].heading_path == "第1章 > 1.1"
    assert store.count_chunks("doc_1") == 2
    assert store.count_kb_chunks("kb_1") == 2


def test_count_chunks_by_documents_batches_and_fills_zero(store: MetaStore, kb,
                                                          document) -> None:
    """文档列表页要显示每个文档有多少块：一条查询拿全，且去重、缺的补 0。"""
    store.replace_chunks("doc_1", [_chunk("c1", 0), _chunk("c2", 1)])
    store.create_document(
        DocumentRecord(
            id="doc_2",
            knowledge_base_id="kb_1",
            name="空的.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="hash-2",
            stage=DocumentStage.UPLOADED,
            size_bytes=8,
        )
    )

    counts = store.count_chunks_by_documents(["doc_1", "doc_2", "doc_1", "doc_missing"])

    assert counts == {"doc_1": 2, "doc_2": 0, "doc_missing": 0}
    assert store.count_chunks_by_documents([]) == {}


def test_replace_chunks_removes_previous_version(store: MetaStore, kb, document) -> None:
    """增量更新语义：整体替换而非叠加。"""
    store.replace_chunks("doc_1", [_chunk("c1", 0), _chunk("c2", 1)])
    store.replace_chunks("doc_1", [_chunk("c3", 0)])

    chunks = list(store.iter_chunks("doc_1"))
    assert [chunk.chunk_id for chunk in chunks] == ["c3"]
    assert store.count_kb_chunks("kb_1") == 1


def test_get_chunks_batch_returns_requested_ids(store: MetaStore, kb, document) -> None:
    """检索时向量只给得出 chunk_id，正文得靠批量回表取。"""
    store.replace_chunks("doc_1", [_chunk("c1", 0), _chunk("c2", 1), _chunk("c3", 2)])

    chunks = store.get_chunks(["c1", "c3"])
    assert {chunk.chunk_id for chunk in chunks} == {"c1", "c3"}
    assert store.get_chunks([]) == []
    assert store.get_chunks(["missing"]) == []


def test_replace_chunks_with_empty_list_clears(store: MetaStore, kb, document) -> None:
    store.replace_chunks("doc_1", [_chunk("c1", 0)])
    store.replace_chunks("doc_1", [])
    assert store.count_chunks("doc_1") == 0


# --------------------------------------------------------------------- 图片 / 解析产物


def test_images_are_upserted_and_listed_by_page(store: MetaStore, document) -> None:
    store.add_images(
        [
            ImageRecord(image_id="img_1", document_id="doc_1", storage_path="data/images/1.png",
                        page=3, bbox="10,20,30,40"),
            ImageRecord(image_id="img_2", document_id="doc_1", storage_path="data/images/2.png",
                        page=1),
        ]
    )
    store.add_images(
        [ImageRecord(image_id="img_1", document_id="doc_1",
                     storage_path="data/images/1-v2.png", page=3, caption="架构图")]
    )

    images = store.list_images("doc_1")
    assert [image.image_id for image in images] == ["img_2", "img_1"]
    assert images[1].storage_path == "data/images/1-v2.png"
    assert images[1].caption == "架构图"


def test_parse_result_is_idempotent_and_keeps_probe_meta(store: MetaStore, document) -> None:
    """重新解析应覆盖产物，而不是留下多份（架构 §6：解析产物分层持久化）。"""
    store.save_parse_result(
        ParseResultRecord(
            document_id="doc_1",
            part_id=None,
            parser_name="MinerUCloudParser",
            markdown_path="data/markdown/doc_1.md",
            probe_meta={"coverage": 0.98, "route": "text"},
        )
    )
    store.save_parse_result(
        ParseResultRecord(
            document_id="doc_1",
            part_id=None,
            parser_name="PaddleOCRApiParser",
            markdown_path="data/markdown/doc_1-v2.md",
            probe_meta={"coverage": 0.02, "route": "ocr"},
        )
    )

    loaded = store.get_parse_result("doc_1")
    assert loaded is not None
    assert loaded.parser_name == "PaddleOCRApiParser"
    assert loaded.probe_meta["route"] == "ocr"
    assert loaded.part_id is None


# --------------------------------------------------------------------- 任务队列（断点续跑）


def test_claim_task_marks_running_and_counts_attempt(store: MetaStore) -> None:
    store.enqueue_task(_task("t1"))
    claimed = store.claim_task(owner="worker-1", lease_seconds=60)

    assert claimed is not None
    assert claimed.state is TaskState.RUNNING
    assert claimed.attempts == 1
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at is not None


def test_claim_task_returns_none_when_queue_is_empty(store: MetaStore) -> None:
    assert store.claim_task(owner="worker-1", lease_seconds=60) is None


def test_claimed_task_cannot_be_claimed_twice(store: MetaStore) -> None:
    """并发消费者之间不能双领：领取是单条原子语句。"""
    store.enqueue_task(_task("t1"))
    assert store.claim_task(owner="worker-1", lease_seconds=60) is not None
    assert store.claim_task(owner="worker-2", lease_seconds=60) is None


def test_claim_task_respects_next_run_at(store: MetaStore) -> None:
    """指数退避：未到重试时间的任务不该被领走。"""
    store.enqueue_task(_task("t1", next_run_at=utc_now() + timedelta(minutes=5)))
    assert store.claim_task(owner="worker-1", lease_seconds=60) is None


def test_heartbeat_requires_lease_owner(store: MetaStore) -> None:
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=60)

    assert store.heartbeat_task("t1", owner="worker-1", lease_seconds=60) is True
    assert store.heartbeat_task("t1", owner="worker-2", lease_seconds=60) is False


def test_finish_task_releases_lease(store: MetaStore) -> None:
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=60)
    assert store.finish_task("t1", TaskState.SUCCEEDED, owner="worker-1") is True

    finished = store.list_tasks(TaskState.SUCCEEDED)[0]
    assert finished.lease_owner is None
    assert finished.lease_expires_at is None


@pytest.fixture
def reassign_lease(pg_database):
    """把租约判给另一个消费者。

    ``heartbeat_task`` 做不到这件事：它要求 ``lease_owner = owner``，只能在
    "自己还持有租约"时续期。要制造"被回收后重领"，只能直接改这一行。

    仓储接口都带 owner 校验，正因如此它没法自己制造出"租约易主"这个状态，
    只能绕过接口直接改库——所以这里用的是测试库的连接，不是仓储。
    """

    def _reassign(task_id: str, owner: str) -> None:
        with pg_database.session() as conn:
            conn.execute("update tasks set lease_owner = %s where id = %s", (owner, task_id))

    return _reassign


def test_finish_task_refuses_when_lease_moved_on(store: MetaStore,
                                                 reassign_lease) -> None:
    """租约易主后原消费者写不了终态。

    否则会出现最恶心的一类 bug：新消费者正在跑，旧消费者拿着过期结果回来把状态改成
    "成功"或"失败"，用户看到的状态与文档实际状态完全对不上。
    """
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=60)
    reassign_lease("t1", "worker-2")

    assert store.finish_task("t1", TaskState.SUCCEEDED, owner="worker-1") is False

    still_running = store.list_tasks(TaskState.RUNNING)[0]
    assert still_running.lease_owner == "worker-2", "新主人的租约不该被旧消费者清掉"


def test_reschedule_task_returns_task_to_queue(store: MetaStore) -> None:
    """指数退避的落地：任务回到 PENDING、释放租约、记录失败原因。"""
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=60)

    retry_at = utc_now() + timedelta(seconds=4)
    assert store.reschedule_task("t1", owner="worker-1", next_run_at=retry_at, error="boom") is True

    requeued = store.list_tasks(TaskState.PENDING)[0]
    assert requeued.lease_owner is None
    assert requeued.lease_expires_at is None
    assert requeued.error == "boom"
    assert requeued.next_run_at is not None
    # 未到时间不给领，到了才能领——退避真的生效
    assert store.claim_task(owner="worker-2", lease_seconds=60) is None


def test_reschedule_task_refuses_when_lease_moved_on(store: MetaStore,
                                                     reassign_lease) -> None:
    """同理：旧消费者不能把任务拽回队列，否则它会和新消费者同时被调度。"""
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=60)
    reassign_lease("t1", "worker-2")

    retry_at = utc_now() + timedelta(seconds=4)
    assert store.reschedule_task(
        "t1", owner="worker-1", next_run_at=retry_at, error="boom"
    ) is False
    assert store.list_tasks(TaskState.PENDING) == [], "不该被拽回队列"


def test_reclaim_requeues_expired_task_with_budget_left(store: MetaStore) -> None:
    """进程崩溃后：超时任务回到 PENDING，实现断点续跑（架构 §4）。"""
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-crashed", lease_seconds=1)

    later = utc_now() + timedelta(minutes=10)
    assert store.reclaim_expired_tasks(now=later) == 1

    requeued = store.list_tasks(TaskState.PENDING)
    assert [task.id for task in requeued] == ["t1"]
    assert requeued[0].lease_owner is None
    # 重新领取时 attempts 继续累加，体现"重试了几次"
    reclaimed = store.claim_task(owner="worker-2", lease_seconds=60)
    assert reclaimed is not None and reclaimed.attempts == 2


def test_reclaim_fails_task_that_exhausted_retries(store: MetaStore) -> None:
    store.enqueue_task(_task("t1", attempts=5, max_attempts=5))
    store.claim_task(owner="worker-1", lease_seconds=1)

    later = utc_now() + timedelta(minutes=10)
    assert store.reclaim_expired_tasks(now=later) == 1

    failed = store.list_tasks(TaskState.FAILED)
    assert [task.id for task in failed] == ["t1"]
    assert "最大重试次数" in (failed[0].error or "")


def test_reclaim_ignores_live_lease(store: MetaStore) -> None:
    store.enqueue_task(_task("t1"))
    store.claim_task(owner="worker-1", lease_seconds=600)
    assert store.reclaim_expired_tasks() == 0


def test_task_can_reference_an_existing_document(store: MetaStore, kb, document) -> None:
    store.enqueue_task(_task("t1", document_id="doc_1"))
    assert store.list_tasks()[0].document_id == "doc_1"


def test_task_referencing_missing_document_is_rejected(store: MetaStore) -> None:
    """外键约束生效：任务不能挂到不存在的文档上（也是级联删除可信的前提）。

    同 ``test_duplicate_document_in_same_kb_is_rejected``：按错误种类断言，
    不绑驱动措辞（SQLite "FOREIGN KEY constraint failed" / PG "violates foreign key"）。
    """
    with pytest.raises(Exception, match=r"(?i)foreign key"):
        store.enqueue_task(_task("t1", document_id="doc_not_exist"))


def test_tasks_follow_document_deletion(store: MetaStore, kb, document) -> None:
    """文档删除后其任务一并消失，避免留下永远跑不通的孤儿任务。"""
    store.enqueue_task(_task("t1", document_id="doc_1"))
    store.delete_document("doc_1")
    assert store.list_tasks() == []


# --------------------------------------------------------------------- 数据源 / 凭据 / webhook


def test_data_source_keeps_config_and_etag(store: MetaStore, kb) -> None:
    """架构 §10：定时拉取靠 etag 做变更检测。"""
    store.create_data_source(
        DataSourceRecord(id="ds_1", knowledge_base_id="kb_1", kind=DataSourceKind.RSS,
                         name="技术日报", config={"url": "https://example.com/feed"}, etag="W/abc")
    )
    sources = store.list_data_sources("kb_1")
    assert sources[0].config["url"] == "https://example.com/feed"
    assert sources[0].etag == "W/abc"
    assert sources[0].enabled is True


def test_api_key_is_stored_and_looked_up_by_hash(store: MetaStore, kb) -> None:
    """架构 §3.2：key 绑定知识库范围与只读/读写权限。"""
    store.create_api_key(
        ApiKeyRecord(id="key_1", name="只读客户端", key_hash="sha256:deadbeef",
                     permission=ApiKeyPermission.READONLY, knowledge_base_ids=("kb_1",))
    )
    loaded = store.get_api_key_by_hash("sha256:deadbeef")
    assert loaded is not None
    assert loaded.permission is ApiKeyPermission.READONLY
    assert tuple(loaded.knowledge_base_ids) == ("kb_1",)
    assert store.get_api_key_by_hash("sha256:nope") is None


def test_webhook_round_trip(store: MetaStore) -> None:
    store.create_webhook(
        WebhookRecord(id="wh_1", url="https://example.com/hook",
                      events=("document.indexed",), secret="s3cr3t")
    )
    hooks = store.list_webhooks()
    assert tuple(hooks[0].events) == ("document.indexed",)
    assert hooks[0].enabled is True


# --------------------------------------------------------------------- 回收站 / 设置


def test_trash_keeps_original_for_retention_window(store: MetaStore, document) -> None:
    """架构 §6.2：原文进回收站保留 7 天。"""
    store.add_to_trash(
        TrashRecord(id="tr_1", document_id="doc_1", kind=TrashKind.ORIGINAL,
                    storage_path="data/originals/doc_1.pdf",
                    expires_at=utc_now() + timedelta(days=7))
    )
    assert [item.id for item in store.list_trash()] == ["tr_1"]
    assert store.purge_expired_trash() == []


def test_purge_expired_trash_returns_and_removes_due_items(
    store: MetaStore, document
) -> None:
    store.add_to_trash(
        TrashRecord(id="tr_old", document_id="doc_1", kind=TrashKind.ORIGINAL,
                    storage_path="data/originals/old.pdf",
                    expires_at=utc_now() - timedelta(days=1))
    )
    store.add_to_trash(
        TrashRecord(id="tr_new", document_id="doc_1", kind=TrashKind.IMAGE,
                    storage_path="data/images/new.png",
                    expires_at=utc_now() + timedelta(days=7))
    )

    purged = store.purge_expired_trash()
    assert [item.id for item in purged] == ["tr_old"]
    assert purged[0].storage_path == "data/originals/old.pdf"
    assert [item.id for item in store.list_trash()] == ["tr_new"]


def test_settings_upsert(store: MetaStore) -> None:
    assert store.get_setting("mineru_token") is None
    store.set_setting("mineru_token", "v1")
    store.set_setting("mineru_token", "v2")
    assert store.get_setting("mineru_token") == "v2"


# --------------------------------------------------------------------- 级联


def test_deleting_document_cascades_to_parts_chunks_and_images(store: MetaStore, kb,
                                                               document) -> None:
    store.create_document_parts(
        [DocumentPartRecord(id="part_1", document_id="doc_1", part_index=1, page_start=1,
                            page_end=200, stage=DocumentStage.UPLOADED)]
    )
    store.replace_chunks("doc_1", [_chunk("c1", 0, image_ids=("img_1",))])
    store.add_images(
        [ImageRecord(image_id="img_1", document_id="doc_1", storage_path="data/images/1.png")]
    )

    store.delete_document("doc_1")

    assert store.count_chunks("doc_1") == 0
    assert store.list_document_parts("doc_1") == []
    assert store.list_images("doc_1") == []


def test_deleting_knowledge_base_removes_everything_inside(store: MetaStore, kb,
                                                           document) -> None:
    """架构 §6.2 级联删除在库一级同样成立。"""
    store.replace_chunks("doc_1", [_chunk("c1", 0)])
    store.delete_knowledge_base("kb_1")

    assert store.list_knowledge_bases() == []
    assert store.list_documents("kb_1") == []
    assert store.count_kb_chunks("kb_1") == 0


# --------------------------------------------------------------------- 空输入与未命中路径


def test_empty_inputs_are_no_ops(store: MetaStore, document) -> None:
    """批量写入收到空列表时应直接返回，不产生空事务。"""
    store.create_document_parts([])
    store.add_images([])
    assert store.list_document_parts("doc_1") == []
    assert store.list_images("doc_1") == []


def test_iter_chunks_on_document_without_chunks_returns_empty(store: MetaStore,
                                                              document) -> None:
    assert list(store.iter_chunks("doc_1")) == []


def test_get_parse_result_returns_none_when_absent(store: MetaStore, document) -> None:
    assert store.get_parse_result("doc_1") is None
    assert store.get_parse_result("doc_not_exist") is None


# --------------------------------------------------------------------- 账号（v10：名册升级）


def test_account_fields_round_trip(store: MetaStore) -> None:
    """账号列（username/role/disabled）写入后能原样读回。"""
    store.create_user(
        UserRecord(
            id="user_a",
            name="小又",
            username="you",
            password_hash="argon2$fake",
            role=UserRole.ADMIN,
        )
    )

    loaded = store.find_user_by_username("you")
    assert loaded is not None
    assert (loaded.role, loaded.disabled) == (UserRole.ADMIN, False)
    # 名册语义不丢：按名字找依然有效（operator header 还在用它）
    assert store.find_user_by_name("小又").id == "user_a"  # type: ignore[union-attr]


def test_roster_entry_without_username_cannot_be_looked_up_as_account(
    store: MetaStore,
) -> None:
    """纯名册条目（username 为 NULL）不是账号：按用户名查不到它。"""
    store.create_user(UserRecord(id="user_r", name="仅名册"))

    assert store.find_user_by_username("仅名册") is None
    assert store.get_user("user_r").role is UserRole.MEMBER  # type: ignore[union-attr]


def test_duplicate_username_is_rejected_with_precise_message(store: MetaStore) -> None:
    """唯一索引冲突要分清是 username 还是 name：管理员处理这两件事的方式不同。"""
    store.create_user(UserRecord(id="user_a", name="甲", username="taken"))

    with pytest.raises(ConflictError, match="用户名「taken」已被占用"):
        store.create_user(UserRecord(id="user_b", name="乙", username="taken"))
    # name 冲突的旧文案不能变（名册页靠它提示）
    with pytest.raises(ConflictError, match="已经有叫「甲」的使用者了"):
        store.create_user(UserRecord(id="user_c", name="甲"))


def test_update_password_and_disable(store: MetaStore) -> None:
    store.create_user(UserRecord(id="user_a", name="甲", username="a", password_hash="h1"))

    store.update_user_password("user_a", "h2")
    assert store.get_user("user_a").password_hash == "h2"  # type: ignore[union-attr]

    store.set_user_disabled("user_a", True)
    assert store.get_user("user_a").disabled is True  # type: ignore[union-attr]
    store.set_user_disabled("user_a", False)
    assert store.get_user("user_a").disabled is False  # type: ignore[union-attr]


def test_claim_legacy_ownership_only_touches_ownerless_rows(store: MetaStore, kb) -> None:
    """认领只动 owner 为 NULL 的老数据；已有归属的行绝不能被改（否则 setup 会变成夺权）。"""
    store.create_user(UserRecord(id="user_a", name="管理员", username="admin"))
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_2", name="已有主", embedding_model_id="m",
                            embedding_dim=768, owner_id="user_other")
    )
    store.create_conversation(ConversationRecord(id="conv_1"))

    counts = store.claim_legacy_ownership("user_a")

    assert counts == {"knowledge_bases": 1, "conversations": 1}
    assert store.get_knowledge_base("kb_1").owner_id == "user_a"  # type: ignore[union-attr]
    assert store.get_knowledge_base("kb_2").owner_id == "user_other"  # type: ignore[union-attr]
    assert store.get_conversation("conv_1").owner_id == "user_a"  # type: ignore[union-attr]
    # 再认领一次是空操作：可重入，重复跑不会出错
    assert store.claim_legacy_ownership("user_a") == {"knowledge_bases": 0, "conversations": 0}


# --------------------------------------------------------------------- 登录会话（v10）


def _session(session_id: str, user_id: str = "user_a", *, hours: int = 1) -> SessionRecord:
    return SessionRecord(
        id=session_id, user_id=user_id, expires_at=utc_now() + timedelta(hours=hours)
    )


def test_session_round_trip_and_touch(store: MetaStore) -> None:
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    store.create_session(_session("sess_1"))

    loaded = store.get_session("sess_1")
    assert loaded is not None
    assert loaded.user_id == "user_a"
    assert loaded.last_seen_at is not None

    later = utc_now() + timedelta(hours=2)
    store.touch_session("sess_1", last_seen_at=later, expires_at=later + timedelta(days=7))
    touched = store.get_session("sess_1")
    assert touched is not None
    assert touched.last_seen_at == later

    store.delete_session("sess_1")
    assert store.get_session("sess_1") is None


def test_delete_sessions_for_user_keeps_current(store: MetaStore) -> None:
    """改密吊销：保住当前这条（用户不该被自己的改密动作踢出去），其余全清。"""
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    store.create_user(UserRecord(id="user_b", name="乙", username="b"))
    for session_id in ("s1", "s2", "s3"):
        store.create_session(_session(session_id))
    store.create_session(_session("other", user_id="user_b"))

    revoked = store.delete_sessions_for_user("user_a", except_session_id="s2")

    assert revoked == 2
    assert store.get_session("s2") is not None
    # 别人的会话不能跟着遭殃
    assert store.get_session("other") is not None


def test_sessions_cascade_with_user(store: MetaStore) -> None:
    """删账号必须连会话一起清：留着就是一把永远有效的万能钥匙。"""
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    store.create_session(_session("sess_1"))

    store.delete_user("user_a")

    assert store.get_session("sess_1") is None


def test_delete_user_clears_all_ownership_columns(store: MetaStore, kb, document) -> None:
    """删账号后**所有**归属列都要置空——只清 documents.uploaded_by 而留下
    knowledge_bases.owner_id 等，会造出指向不存在账号的悬空引用。"""
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_2", name="他的库", embedding_model_id="m",
                            embedding_dim=768, owner_id="user_a")
    )
    store.create_conversation(ConversationRecord(id="conv_1", owner_id="user_a"))
    store.create_api_key(
        ApiKeyRecord(id="key_1", name="集成", key_hash="h",
                     permission=ApiKeyPermission.READONLY, created_by="user_a")
    )
    store.create_document(
        DocumentRecord(
            id="doc_2", knowledge_base_id="kb_1", name="他的.md",
            source_kind=DataSourceKind.UPLOAD, content_hash="hash-2",
            stage=DocumentStage.UPLOADED, uploaded_by="user_a",
        )
    )

    store.delete_user("user_a")

    assert store.get_knowledge_base("kb_2").owner_id is None  # type: ignore[union-attr]
    assert store.get_conversation("conv_1").owner_id is None  # type: ignore[union-attr]
    assert store.get_api_key_by_hash("h").created_by is None  # type: ignore[union-attr]
    # 文档保留（数据不丢），只有归属置空
    assert store.get_document("doc_2").uploaded_by is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- 知识库分享（v10）


def test_share_round_trip_and_regrant_updates_permission(store: MetaStore, kb) -> None:
    """重复分享同一库同一人 = 改档位，不是报错也不是插第二条。"""
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    first = store.put_share(
        ShareRecord(kb_id="kb_1", user_id="user_a", permission=SharePermission.READ)
    )

    assert store.list_shares_for_kb("kb_1")[0].permission is SharePermission.READ
    assert store.list_shares_for_user("user_a")[0].kb_id == "kb_1"

    regranted = store.put_share(
        ShareRecord(kb_id="kb_1", user_id="user_a", permission=SharePermission.WRITE)
    )
    shares = store.list_shares_for_kb("kb_1")
    assert len(shares) == 1
    assert shares[0].permission is SharePermission.WRITE
    # 重授只改档位：created_at 保留**首次授予**的时间，不随调整刷新
    assert regranted.created_at == first.created_at

    store.delete_share("kb_1", "user_a")
    assert store.list_shares_for_kb("kb_1") == []


def test_share_with_missing_target_is_rejected(store: MetaStore, kb) -> None:
    """外键违例要翻成领域错误：漏原生驱动异常给上层，调用方没法区分
    "目标不存在"与"数据库坏了"。"""
    with pytest.raises(InvalidRequestError, match="不存在"):
        store.put_share(
            ShareRecord(kb_id="kb_1", user_id="user_ghost", permission=SharePermission.READ)
        )


def test_shares_cascade_with_kb(store: MetaStore, kb) -> None:
    store.create_user(UserRecord(id="user_a", name="甲", username="a"))
    store.put_share(ShareRecord(kb_id="kb_1", user_id="user_a", permission=SharePermission.READ))

    store.delete_knowledge_base("kb_1")

    assert store.list_shares_for_user("user_a") == []


# --------------------------------------------------------------------- 归属列（v10）


def test_api_key_created_by_round_trip(store: MetaStore) -> None:
    store.create_api_key(
        ApiKeyRecord(id="key_1", name="集成", key_hash="h", permission=ApiKeyPermission.READONLY,
                     created_by="user_a")
    )

    loaded = store.get_api_key_by_hash("h")
    assert loaded is not None
    assert loaded.created_by == "user_a"


def test_question_stats_by_documents_counts_chunks_and_questions(
    store: MetaStore, kb, document
) -> None:
    """列表要显示"这份出没出题、出了多少"：一条 GROUP BY 拿全，缺的补 (0, 0)。"""
    store.replace_chunks(
        "doc_1",
        [
            _chunk("c1", 0, questions=("这道题？", "那道题？")),
            _chunk("c2", 1, questions=("第三题？",)),
            _chunk("c3", 2),  # 没出题的段
        ],
    )

    stats = store.question_stats_by_documents(["doc_1", "doc_missing", "doc_1"])

    assert stats == {"doc_1": (2, 3), "doc_missing": (0, 0)}
    assert store.question_stats_by_documents([]) == {}


def test_active_question_documents_only_lists_queued_ones(
    store: MetaStore, kb, document
) -> None:
    """轮询与"生成中…"都靠它：只有排队/在跑的出题任务才算数。"""
    store.enqueue_task(_task("t_run", state=TaskState.RUNNING, document_id="doc_1",
                             kind=TaskKind.QUESTIONS))
    store.enqueue_task(_task("t_done", state=TaskState.SUCCEEDED, document_id="doc_1",
                             kind=TaskKind.QUESTIONS))
    store.enqueue_task(_task("t_parse", state=TaskState.PENDING, document_id="doc_1",
                             kind=TaskKind.PARSE))

    assert store.active_question_documents(["doc_1", "doc_missing"]) == {"doc_1"}
    assert store.active_question_documents([]) == set()


def test_stage_events_batch_groups_by_document(store: MetaStore, kb, document) -> None:
    """列表每行画进度条，所以要**一次取回整页**的事件（逐篇查就是 N+1）。

    缺失的文档不出现（与 ``get_documents_by_ids`` 同一约定），调用方 ``.get(id, [])``。
    """
    store.update_document_stage("doc_1", DocumentStage.PARSING)
    store.update_document_stage("doc_1", DocumentStage.CHUNKING)

    grouped = store.list_document_stage_events_for_documents(
        ["doc_1", "doc_missing", "doc_1"]
    )

    assert set(grouped) == {"doc_1"}
    assert [event.stage for event in grouped["doc_1"]] == ["uploaded", "parsing", "chunking"]
    assert store.list_document_stage_events_for_documents([]) == {}


def test_active_tasks_by_documents_keeps_only_unfinished_ones(
    store: MetaStore, kb, document
) -> None:
    """停滞判据要用它：只有还没结束的任务才有租约可言。

    已成功的那条不能出现——它没有租约，"租约过期"对它无从谈起（把它算进去
    会让列表把一篇正常索引完的文档标成"可能卡住"）。
    """
    store.enqueue_task(_task("t_run", state=TaskState.RUNNING, document_id="doc_1"))
    store.enqueue_task(_task("t_done", state=TaskState.SUCCEEDED, document_id="doc_1"))
    store.enqueue_task(_task("t_global", state=TaskState.PENDING))  # 数据源拉取，不挂文档

    found = store.active_tasks_by_documents(["doc_1", "doc_missing"])

    assert set(found) == {"doc_1"}
    assert found["doc_1"].id == "t_run"
    assert store.active_tasks_by_documents([]) == {}


def test_task_counts_aggregates_in_one_query(store: MetaStore, kb, document) -> None:
    """队列概览必须**在 SQL 里聚合**（负载面板每 2 秒问一次，任务表只增不减）。

    这一条同时钉住"停滞/逾期"的判据：它们与回收过期租约、与任务中心那一列
    用的是同一个条件（租约过期 / 排队超过阈值），不是另算一套。
    """
    from datetime import UTC, datetime, timedelta

    from app.models.enums import TaskKind

    now = datetime.now(UTC)
    store.enqueue_task(_task("t_run", state=TaskState.RUNNING, document_id="doc_1"))
    store.enqueue_task(_task("t_stall", state=TaskState.RUNNING, document_id="doc_1"))
    store.enqueue_task(_task("t_wait", state=TaskState.PENDING, document_id="doc_1"))
    store.enqueue_task(
        _task("t_overdue", state=TaskState.PENDING, document_id="doc_1", kind=TaskKind.QUESTIONS)
    )
    store.enqueue_task(_task("t_done", state=TaskState.SUCCEEDED, document_id="doc_1"))

    # 一条租约有效、一条已过期；t_overdue 的 next_run_at 在过去
    with store._db.session() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE tasks SET lease_owner = 'w', lease_expires_at = %s WHERE id = 't_run'",
            (now + timedelta(minutes=5),),
        )
        conn.execute(
            "UPDATE tasks SET lease_owner = 'w', lease_expires_at = %s WHERE id = 't_stall'",
            (now - timedelta(minutes=5),),
        )
        conn.execute(
            "UPDATE tasks SET next_run_at = %s WHERE id = 't_overdue'",
            (now - timedelta(hours=1),),
        )

    counts = store.task_counts(now=now, overdue_before=now - timedelta(minutes=10))

    assert (counts.running, counts.pending) == (2, 2)
    assert counts.stalled == 1  # 只有租约过期的那条
    assert counts.overdue == 1
    assert counts.pending_by_kind == {"parse": 1, "questions": 1}
    assert counts.oldest_pending_at is not None


def test_purge_finished_tasks_keeps_unfinished_ones(store: MetaStore, kb, document) -> None:
    """清理历史**绝不碰未结束的任务**：删掉排队中的那条，文档就永远不会被处理，
    而且没有任何报错——这是最坏的一类 bug。

    这条同时钉住"只清终态"：succeeded / failed / canceled 才是历史。
    """
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(days=40)
    for task_id, state in (
        ("t_done", TaskState.SUCCEEDED),
        ("t_failed", TaskState.FAILED),
        ("t_canceled", TaskState.CANCELED),
        ("t_pending", TaskState.PENDING),
        ("t_running", TaskState.RUNNING),
    ):
        store.enqueue_task(
            _task(task_id, state=state, document_id="doc_1", created_at=old, updated_at=old)
        )

    removed = store.purge_finished_tasks(before=datetime.now(UTC) - timedelta(days=30))

    assert removed == 3
    left = {task.id for task in store.list_tasks()}
    assert left == {"t_pending", "t_running"}


def test_purge_stage_events_only_drops_old_ones(store: MetaStore, kb, document) -> None:
    """老事件可以清（时间线退化成"只有当前这一步"），**近期的必须留着**——
    正在跑的那篇的时间线还得看。"""
    from datetime import UTC, datetime, timedelta

    store.update_document_stage("doc_1", DocumentStage.PARSING)
    with store._db.session() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE document_stage_events SET entered_at = %s",
            (datetime.now(UTC) - timedelta(days=100),),
        )
    store.update_document_stage("doc_1", DocumentStage.CHUNKING)

    removed = store.purge_stage_events(before=datetime.now(UTC) - timedelta(days=90))

    assert removed == 2  # uploaded + parsing
    stages = [event.stage for event in store.list_document_stage_events("doc_1")]
    assert stages == ["chunking"]


def test_get_settings_batch_returns_only_stored_keys(store: MetaStore) -> None:
    """批量读设置：**一条 SQL** 拿多个键（每次请求的路径上省掉 N−1 次往返）。

    只返回"库里真有值"的键——没值的键不出现在结果里（不是空串），
    调用方按同一套优先级（库 > 引导值 > 默认值）补齐。
    """
    store.set_setting("mineru.token", "tk-1")
    store.set_setting("llm.temperature", "0.5")

    got = store.get_settings(["mineru.token", "llm.temperature", "llm.enable_thinking"])

    assert got == {"mineru.token": "tk-1", "llm.temperature": "0.5"}
    assert store.get_settings([]) == {}


# ---------------------------------------------------------------- 会话事件日志（P0-2）


def _event(conversation_id: str, kind: str, payload: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    return SessionEventRecord(
        conversation_id=conversation_id, kind=kind, payload=dict(payload or {})
    )


def test_session_events_get_a_contiguous_seq_per_conversation(store: MetaStore) -> None:
    """``seq`` 由**写那个事务**算，按会话各自从 1 起、逐条加一。

    为什么不让调用方给：序号一旦由数据库之外的一方决定，并发写就会撞上唯一约束
    （而那是"事件丢了"的样子，不是"重试一下就好"的样子）。
    """
    store.create_conversation(ConversationRecord(id="conv_1"))
    store.create_conversation(ConversationRecord(id="conv_2"))

    first = store.append_session_events(
        [_event("conv_1", "turn/start"), _event("conv_1", "step"), _event("conv_1", "turn/end")]
    )
    assert [item.seq for item in first] == [1, 2, 3]
    # 另一条会话的序号**互不影响**（seq 是会话内的，不是全局的）
    other = store.append_session_events([_event("conv_2", "turn/start")])
    assert [item.seq for item in other] == [1]
    # 接着写：从上次的 max 继续
    assert [item.seq for item in store.append_session_events([_event("conv_1", "thinking")])] == [4]


def test_session_events_are_append_only(store: MetaStore) -> None:
    """同一 ``seq`` 不会出现两条：唯一约束当场拒掉第二次写。

    这是"只追加"的**机械保证**——不是靠"我们的代码不更新事件"这句话，
    而是数据库不许。P0-2 的验收之一正是这一条。
    """
    store.create_conversation(ConversationRecord(id="conv_1"))
    store.append_session_events([_event("conv_1", "turn/start")])

    with (
        store._db.session() as conn,  # type: ignore[attr-defined]
        pytest.raises(Exception, match="uq_session_events_seq"),
    ):
        conn.execute(
            "INSERT INTO session_events (conversation_id, seq, kind, payload)"
            " VALUES (%s, %s, %s, %s)",
            ("conv_1", 1, "step", "{}"),
            )


def test_list_session_events_returns_them_in_seq_order_and_filters(store: MetaStore) -> None:
    """按 ``seq`` 正序读；``kinds`` 非空时只取那几种。"""
    store.create_conversation(ConversationRecord(id="conv_1"))
    store.append_session_events(
        [
            _event("conv_1", "turn/start", {"query": "问题"}),
            _event("conv_1", "step", {"label": "组织回答"}),
            _event("conv_1", "interrupted", {"reason": "用户停止"}),
        ]
    )

    everything = store.list_session_events("conv_1")
    assert [item.kind for item in everything] == ["turn/start", "step", "interrupted"]
    assert everything[0].payload == {"query": "问题"}
    assert everything[0].id is not None, "bigserial 主键入库时由数据库给"

    filtered = store.list_session_events("conv_1", kinds=["interrupted", "turn/start"])
    assert [item.kind for item in filtered] == ["turn/start", "interrupted"]


def test_appending_a_turn_writes_messages_and_events_together(store: MetaStore) -> None:
    """``append_turn`` 的**全部意义**：消息与事件要么都在、要么都不在。

    曾经的窗口（"消息在、事件不在"）会让回看时看到一条没有过程记录的回答
    ——而过程恰恰是 v0.25 起用户回来要找的东西。
    """
    store.create_conversation(ConversationRecord(id="conv_1"))
    messages = (
        ChatMessageRecord(id="m1", conversation_id="conv_1", role="user", content="问题"),
        ChatMessageRecord(
            id="m2",
            conversation_id="conv_1",
            role="assistant",
            content="回答",
            steps=({"phase": "answer", "label": "组织回答", "status": "done"},),
        ),
    )
    store.append_turn(
        messages=messages,
        events=[_event("conv_1", "turn/start"), _event("conv_1", "turn/end")],
    )

    assert [item.id for item in store.list_messages("conv_1")] == ["m1", "m2"]
    assert [item.kind for item in store.list_session_events("conv_1")] == ["turn/start", "turn/end"]


def test_session_events_cascade_with_the_conversation(store: MetaStore) -> None:
    """会话删了，它的事件跟着走（与消息同一规则）。"""
    store.create_conversation(ConversationRecord(id="conv_1"))
    store.append_session_events([_event("conv_1", "turn/start")])

    store.delete_conversation("conv_1")

    assert store.list_session_events("conv_1") == []
