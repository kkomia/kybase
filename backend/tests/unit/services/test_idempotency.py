"""幂等键：重放、内容不符、进行中（M4 T4.3）。

镜像同构：``app/services/idempotency.py`` → ``tests/unit/services/test_idempotency.py``。

要挡的是这个具体场景：客户端 POST 上传后网络断了，它不知道服务端收没收到，
于是重试。没有幂等键，同一份文件就进两次。

四条判定各有一个用例，其中**"同键不同内容"最容易被漏掉**——
静默放行会让客户端拿着一个它以为成功、实际是别人结果的响应。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConflictError
from app.services.idempotency import IdempotencyService, fingerprint


@pytest.fixture
def service(bundle) -> IdempotencyService:  # type: ignore[no-untyped-def]
    return IdempotencyService(bundle)


def test_first_request_claims_the_key(service: IdempotencyService) -> None:
    claim = service.begin("key-1", "hash-a")
    assert not claim.is_replay, "首次请求不该被判成重放"


def test_replay_returns_the_stored_response(service: IdempotencyService) -> None:
    """重放要回上次那份响应：客户端还需要 document_id 与 task_id 才能继续。"""
    service.begin("key-2", "hash-a")
    service.complete("key-2", {"document": {"id": "doc_1"}, "task_id": "task_1"})

    claim = service.begin("key-2", "hash-a")

    assert claim.is_replay
    assert claim.replay == {"document": {"id": "doc_1"}, "task_id": "task_1"}


def test_same_key_with_different_content_is_rejected(service: IdempotencyService) -> None:
    """**这条最要紧**：同键换内容必须报错，不能当重放。

    否则客户端用它以为的"重试"把文件 A 的键配上了文件 B 的内容，
    却拿到 A 的 document_id——它会以为 B 传上去了。
    """
    service.begin("key-3", "hash-of-A")
    service.complete("key-3", {"document": {"id": "doc_A"}})

    with pytest.raises(ConflictError) as excinfo:
        service.begin("key-3", "hash-of-B")

    assert "不同的请求内容" in str(excinfo.value)


def test_in_flight_key_reports_conflict_not_empty(service: IdempotencyService) -> None:
    """占住但没跑完时回 409，不能回空。

    回空会让客户端以为失败又重试，而重试还是 409——把一次正常处理变成死循环。
    """
    service.begin("key-4", "hash-a")  # 故意不调 complete

    with pytest.raises(ConflictError) as excinfo:
        service.begin("key-4", "hash-a")

    assert "正在处理中" in str(excinfo.value)


def test_different_keys_do_not_interfere(service: IdempotencyService) -> None:
    assert not service.begin("key-5", "hash-a").is_replay
    assert not service.begin("key-6", "hash-a").is_replay


def test_conflict_then_get_race_is_reported_as_retryable(bundle) -> None:
    """抢键失败却查不到记录时，必须给"请重试"，不能崩也不能当成新请求。

    这个窗口很小但真实存在（键在 create 与 get 之间被清理）。这里用假 store
    直接造出这个状态——比去删库更准确地指向要验的那条分支。
    """
    from app.services.idempotency import IdempotencyService as Service

    class VanishingMeta:
        """create 永远说"已存在"，get 永远说"没有"。"""

        def create_idempotency_key(self, record):  # type: ignore[no-untyped-def]
            raise ConflictError("已被占用")

        def get_idempotency_key(self, key):  # type: ignore[no-untyped-def]
            return None

    fake = type("Stores", (), {"meta": VanishingMeta()})()
    with pytest.raises(ConflictError) as excinfo:
        Service(fake).begin("key-8", "hash-a")  # type: ignore[arg-type]

    assert "重试" in str(excinfo.value)


def test_complete_failure_does_not_break_the_business_result(bundle) -> None:
    """挂响应失败不该抛出去。

    业务已经成功了，此时报错会让客户端以为失败并重试，而重试又因为键已存在
    回 409"处理中"——把一次成功变成一次困扰。
    """

    class BrokenMeta:
        def create_idempotency_key(self, record):  # type: ignore[no-untyped-def]
            return record

        def save_idempotent_response(self, key, response):  # type: ignore[no-untyped-def]
            raise RuntimeError("存储抖动")

    fake = type("Stores", (), {"meta": BrokenMeta()})()
    service = IdempotencyService(fake)  # type: ignore[arg-type]

    service.begin("key-9", "hash-a")
    service.complete("key-9", {"ok": True})  # 不该抛


# --------------------------------------------------------------------- 指纹


def test_fingerprint_separates_parts_unambiguously() -> None:
    """拼接歧义：``("ab","c")`` 与 ``("a","bc")`` 直接拼都是 ``"abc"``。

    真踩过这类问题——两个不同的上传会被判成同一个请求，于是第二个被当成重放、
    直接回第一个的 document_id。
    """
    assert fingerprint("ab", "c") != fingerprint("a", "bc")
    assert fingerprint("", "abc") != fingerprint("abc", "")


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    assert fingerprint("kb_1", "a.pdf") == fingerprint("kb_1", "a.pdf")
    assert fingerprint("kb_1", "a.pdf") != fingerprint("kb_1", "b.pdf")
    assert fingerprint("kb_1", "a.pdf") != fingerprint("kb_2", "a.pdf")
