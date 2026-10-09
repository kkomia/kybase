"""``app/core/config.py`` 的单元测试。

镜像同构：``app/core/config.py`` → ``tests/unit/core/test_config.py``（工程规范 §5.1）。

``local`` 标记：这个文件里的用例全是"读一遍配置对象"，既不需要 PostgreSQL 也不需要
SQLite。不标的话它们会在没有测试库的机器上被 ``conftest`` 的兜底夹具整批跳过——
而"本机档配了 ``database_url`` 要当场拒绝"正是那台机器最该验的一条。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

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


# ------------------------------------------------------------------ 部署档（M2 §4.1）


def test_deployment_defaults_to_server() -> None:
    """**默认必须还是服务器档**：既有部署（NAS 上的网页端与 API）一位行为都不变。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.deployment == "server"
    assert settings.local_db is None


def test_server_deployment_keeps_accepting_a_database_url() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="server", database_url="postgresql://u:p@nas:5432/kylab"
    )
    assert settings.database_url == "postgresql://u:p@nas:5432/kylab"


def test_local_deployment_accepts_the_local_db_path(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "kylab.db"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", local_db=target
    )
    assert settings.local_db == target


def test_local_deployment_rejects_a_database_url() -> None:
    """两个真相源要**当场**拒绝：本机档说"元数据在 SQLite"，连接串说"在 PostgreSQL"。

    两个都在场时，"会话写到哪儿"取决于哪段代码先读哪个字段——而失败形态是
    **数据被写进另一个库**（不报错的那一类）。所以这条必须在构造 Settings 时就红，
    而不是等到某个请求才发现。
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            deployment="local",
            database_url="postgresql://kylab:secret@nas:5432/kylab",
        )
    assert "KYLAB_DATABASE_URL" in str(excinfo.value)


def test_unknown_deployment_is_rejected() -> None:
    """档位是两选一的字面量：写错（比如 ``local-free``）不许静默退回服务器档。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, deployment="local-first")  # type: ignore[call-arg]
