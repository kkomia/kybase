"""中文分词（存储层共享）的单元测试。

镜像同构：``app/storage/text.py`` → ``tests/unit/storage/test_text.py``。

两个全文实现共用同一份切词，所以这里测的是那个共享契约。
"""

from app.storage.text import tokenize


def test_tokenize_splits_chinese_into_words() -> None:
    tokens = tokenize("知识库检索服务").split()
    assert "检索" in tokens
    assert all(token.strip() for token in tokens)


def test_tokenize_keeps_latin_words() -> None:
    tokens = tokenize("使用 sqlite-vec 做向量检索").split()
    assert any("sqlite" in token.lower() for token in tokens)


def test_tokenize_drops_blank_tokens() -> None:
    assert tokenize("   \n\t  ") == ""
