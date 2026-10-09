"""``app/core/exceptions.py`` 的单元测试。

镜像同构：``app/core/exceptions.py`` → ``tests/unit/core/test_exceptions.py``。
对外错误信封（``{code, message}``）是前端消费的契约，字段名必须稳定。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import (
    ConflictError,
    InvalidRequestError,
    KylabError,
    NotFoundError,
    register_exception_handlers,
)
from app.storage.split_impl import KnowledgeBaseUnavailable


def test_base_error_defaults_to_500() -> None:
    error = KylabError()
    assert error.code == "internal_error"
    assert error.http_status == 500
    assert error.detail == "服务内部错误"


def test_subclasses_carry_their_own_status() -> None:
    assert (NotFoundError().code, NotFoundError().http_status) == ("not_found", 404)
    assert (ConflictError().code, ConflictError().http_status) == ("conflict", 409)
    assert (InvalidRequestError().code, InvalidRequestError().http_status) == (
        "invalid_request",
        422,
    )


def test_custom_message_overrides_default() -> None:
    error = NotFoundError("知识库 kb_9 不存在")
    assert error.detail == "知识库 kb_9 不存在"
    assert str(error) == "知识库 kb_9 不存在"


def test_all_errors_share_the_base_type() -> None:
    """services 层只需捕获 KylabError 就能覆盖所有领域错误。"""
    for error in (NotFoundError(), ConflictError(), InvalidRequestError()):
        assert isinstance(error, KylabError)


@pytest.mark.parametrize("error_class", [NotFoundError, ConflictError, InvalidRequestError])
def test_every_error_has_a_machine_readable_code(error_class: type[KylabError]) -> None:
    code = error_class().code
    assert code and code.islower() and " " not in code


@pytest.mark.local
def test_knowledge_base_unavailable_maps_to_503() -> None:
    """本机档没有知识库数据源（在 NAS 上）→ **503 + 那句话**。

    **不是 500**："我们出错了"会把"去设置里看「知识库连接」/ 去服务器上做"这条出路藏起来；
    **也不是空结果**：空结果会被读成"查过了，库里没有"——而它其实没查过。
    这条用例走的是真处理器（注册到一张最小的 app 上再发一次请求），不是读常量。
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/kb")
    def _kb() -> None:
        raise KnowledgeBaseUnavailable()

    with TestClient(app) as client:
        response = client.get("/kb")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "knowledge_base_unavailable"
    assert "NAS" in body["message"]
    # M3 起那句话的出路是**去设置里看**（不再是"等 M3"）：后半个判据跟着改
    assert "知识库连接" in body["message"]
