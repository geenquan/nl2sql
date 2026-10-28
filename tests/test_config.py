from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_env_file_and_environment_override(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / ".env"
    config_file.write_text(
        'MYSQL_HOST=db.internal\nMYSQL_PORT=3307\nMYSQL_PASSWORD=test-db-secret\n'
        'ES_HOSTS=\'["https://es.internal:9200"]\'\nES_PASSWORD=test-es-secret\n'
        'LLM_API_KEY=test-llm-secret\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_HOST", "override.internal")
    settings = Settings(_env_file=config_file)
    assert settings.mysql_host == "override.internal"
    assert settings.mysql_port == 3307
    assert settings.es_hosts == ["https://es.internal:9200"]
    assert settings.llm_api_key.get_secret_value() == "test-llm-secret"
    for secret in ("test-db-secret", "test-es-secret", "test-llm-secret"):
        assert secret not in repr(settings)
        assert secret not in settings.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [{"mysql_port": 65536}, {"es_bulk_batch_size": 0}, {"llm_timeout_seconds": -1}],
)
def test_invalid_connection_limits_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_current_configuration_names(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / ".env"
    config_file.write_text(
        "BIZ_MYSQL_HOST=system.internal\nBIZ_MYSQL_DB=system_db\n"
        "BIZ_MYSQL_USER=system_user\nBIZ_MYSQL_PASSWORD=test-system-secret\n"
        "DATA_MYSQL_HOST=source.internal\nDATA_MYSQL_PORT=3307\n"
        "DATA_MYSQL_DB=source_db\nDATA_MYSQL_USER=source_reader\n"
        "DATA_MYSQL_PASSWORD=test-source-secret\n"
        "ES_HOST=https://search.internal:9200\nES_USER=search_user\n"
        "DEEPSEEK_ENABLED=true\nDEEPSEEK_BASE_URL=https://model.example/v1\n"
        "DEEPSEEK_MODEL=test-model\nDEEPSEEK_API_KEY=test-model-secret\n"
        "LOGIN_ENABLED=true\nJWT_SECRET=test-jwt-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_MYSQL_HOST", "override.internal")
    settings = Settings(_env_file=config_file)
    assert settings.biz_mysql_host == "system.internal"
    assert settings.biz_mysql_db == "system_db"
    assert settings.mysql_host == "override.internal"
    assert settings.mysql_port == 3307
    assert settings.mysql_database == "source_db"
    assert settings.mysql_username == "source_reader"
    assert settings.es_hosts == ["https://search.internal:9200"]
    assert settings.es_username == "search_user"
    assert settings.deepseek_enabled is True
    assert settings.llm_api_base == "https://model.example/v1"
    assert settings.llm_model == "test-model"
    assert settings.login_enabled is True
    for field, secret in (
        (settings.biz_mysql_password, "test-system-secret"),
        (settings.mysql_password, "test-source-secret"),
        (settings.llm_api_key, "test-model-secret"),
        (settings.jwt_secret, "test-jwt-secret"),
    ):
        assert field.get_secret_value() == secret
        assert secret not in settings.model_dump_json()


def test_explicit_es_hosts_takes_priority() -> None:
    settings = Settings(
        _env_file=None, es_host="https://single.internal:9200",
        es_hosts=["https://multi.internal:9200"],
    )
    assert settings.es_hosts == ["https://multi.internal:9200"]


def test_m1_budget_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)
    assert settings.credential_store_path == "./.credential_store.json"
    assert 0 < settings.profile_sample_rows <= 10000
    assert settings.scan_max_tables > 0
    assert settings.profile_max_columns > 0
    assert settings.scan_max_recorded_errors > 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_sample_rows": 0},
        {"profile_sample_rows": 100001},
        {"scan_max_tables": -1},
        {"profile_max_columns": 0},
    ],
)
def test_invalid_m1_budgets_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_budgets_can_be_overridden_by_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROFILE_SAMPLE_ROWS", "123")
    monkeypatch.setenv("SCAN_MAX_TABLES", "7")
    settings = Settings(_env_file=None)
    assert settings.profile_sample_rows == 123
    assert settings.scan_max_tables == 7


def test_sensitive_column_keywords_default_empty_and_env_override(tmp_path, monkeypatch) -> None:
    assert Settings(_env_file=None).sensitive_column_keywords == []
    monkeypatch.setenv("SENSITIVE_COLUMN_KEYWORDS", '["campus", "payroll"]')
    settings = Settings(_env_file=None)
    assert settings.sensitive_column_keywords == ["campus", "payroll"]


def test_m2_semantics_budgets_default_and_env(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.semantics_max_columns_per_run > 0
    assert settings.semantics_llm_max_calls >= 1
    assert settings.semantics_input_char_limit > 0
    assert settings.semantics_send_samples is False  # 样本外发默认关闭
    assert settings.semantics_search_default_limit > 0
    monkeypatch.setenv("SEMANTICS_SEND_SAMPLES", "true")
    monkeypatch.setenv("SEMANTICS_MAX_COLUMNS_PER_RUN", "12")
    overridden = Settings(_env_file=None)
    assert overridden.semantics_send_samples is True
    assert overridden.semantics_max_columns_per_run == 12


@pytest.mark.parametrize(
    "overrides",
    [
        {"semantics_max_columns_per_run": 0},
        {"semantics_llm_max_calls": 0},
        {"semantics_input_char_limit": 0},
        {"semantics_search_default_limit": 0},
    ],
)
def test_invalid_m2_budgets_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_m2_llm_deadline_config() -> None:
    assert Settings(_env_file=None).semantics_llm_deadline_seconds > 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, semantics_llm_deadline_seconds=0)
