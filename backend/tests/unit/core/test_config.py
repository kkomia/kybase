"""``app/core/config.py`` 的单元测试。

镜像同构：``app/core/config.py`` → ``tests/unit/core/test_config.py``（工程规范 §5.1）。

``local`` 标记：这个文件里的用例全是"读一遍配置对象"，既不需要 PostgreSQL 也不需要
SQLite。不标的话它们会在没有测试库的机器上被 ``conftest`` 的兜底夹具整批跳过，
而"配置解析对不对"跟有没有库毫不相干。
"""


import pytest

from app.core.config import API_VERSION, Settings, get_settings

pytestmark = pytest.mark.local


def test_api_version_is_v1() -> None:
    """路径版本前缀是对外契约，改动需按工程规范 §3.2 走版本升级。"""
    assert API_VERSION == "v1"


def test_defaults_are_local_and_safe() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_name == "kylab"
    assert settings.host == "127.0.0.1"
    assert settings.data_dir.name == "data"


def test_cors_origin_list_splits_and_trims() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test ,")  # type: ignore[call-arg]
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_secret_defaults_are_empty() -> None:
    """凭据类配置不得有硬编码默认值（工程规范 §6：密钥禁止入库）。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.mineru_token is None
    assert settings.paddleocr_token is None
    # 模型凭据（embedding / llm 的 key）已归注册表，连字段都不在这里了（v0.8）
    assert not hasattr(settings, "embedding_api_key")
    assert not hasattr(settings, "llm_api_key")


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


# ---------------------------------------------------------------- 只有一个部署形态
#
# 原先这一节验的是"服务器档 / 本机档"两选一（默认 server、本机档拒绝连接串、
# 未知档位不许静默退回）。**本机档与 `deployment` 这个字段随边车一起拆除了**：
# 这个进程就是知识库服务本体，只跑在服务器上，所以"这一份数据写哪儿"不再有两处答案。
# 留下的是一条朴素的守卫：连接串仍然是普通的可配字段（不给它加第二条路由）。


def test_database_url_is_the_only_place_metadata_lives() -> None:
    """元数据只有一个家：``KYLAB_DATABASE_URL``。

    "本机档用 SQLite"那一档已经不存在了——所以这里不再有任何"另一个真相源"可拒绝。
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql://u:p@nas:5432/kylab"
    )
    assert settings.database_url == "postgresql://u:p@nas:5432/kylab"
    assert not hasattr(settings, "deployment")
    assert not hasattr(settings, "local_db")
