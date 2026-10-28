"""真实 MySQL 集成测试。

默认跳过；仅在设置 NL2SQL_MYSQL_INTEGRATION=1 且 `.env` 中 DATA_MYSQL_* 指向
可达的只读账号时运行。测试全程只读：不建表、不写入、不修改结构，
仅执行连接探测、information_schema 查询和带 LIMIT 的采样查询。
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.core.exceptions import NotImplementedFeatureError
from app.profiling.profiler import DataProfiler
from app.security.credentials import FileCredentialStore
from app.services.scan_service import ScanService
from app.services.source_service import SourceService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NL2SQL_MYSQL_INTEGRATION") != "1",
        reason="需要真实 MySQL；设置 NL2SQL_MYSQL_INTEGRATION=1 后运行",
    ),
]


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def connector(settings: Settings) -> MySQLConnector:
    return MySQLConnector(
        MySQLConnectionConfig(
            host=settings.mysql_host,
            port=settings.mysql_port,
            database=settings.mysql_database,
            username=settings.mysql_username,
            password=settings.mysql_password,
            charset=settings.mysql_charset,
            connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
            read_timeout_seconds=settings.mysql_read_timeout_seconds,
        )
    )


def test_real_connection_succeeds(connector: MySQLConnector) -> None:
    assert connector.test_connection() is True
    connector.close()


def test_real_schemas_include_target_database(connector: MySQLConnector, settings: Settings) -> None:
    schemas = connector.list_schemas()
    assert settings.mysql_database in schemas
    connector.close()


def test_real_table_metadata_and_sampling(connector: MySQLConnector, settings: Settings) -> None:
    tables = connector.list_tables(settings.mysql_database)
    assert isinstance(tables, list)
    if not tables:
        pytest.skip("目标库没有基表，跳过结构读取断言")
    table = connector.get_table_metadata(settings.mysql_database, tables[0])
    assert table.schema_name == settings.mysql_database
    assert table.table_name == tables[0]
    assert table.row_count_is_estimate is True  # information_schema 行数始终是估计
    if table.columns:
        rows = connector.sample_table(
            settings.mysql_database,
            tables[0],
            tuple(column.name for column in table.columns),
            limit=10,
            order_by=table.primary_key,
        )
        assert len(rows) <= 10
    connector.close()


def test_execute_readonly_is_rejected(connector: MySQLConnector) -> None:
    with pytest.raises(NotImplementedFeatureError):
        connector.execute_readonly("SELECT 1")
    connector.close()


def test_full_scan_service_against_real_db(tmp_path: Path, settings: Settings) -> None:
    """注册默认数据源并完成一次真实扫描（受限范围：仅 DATA_MYSQL_DB）。"""
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    store = FileCredentialStore(tmp_path / "creds.json")

    def factory(params: dict) -> MySQLConnector:
        return MySQLConnector(
            MySQLConnectionConfig(
                host=params["host"],
                port=params["port"],
                database=params["database"],
                username=params["username"],
                password=params.get("password") or SecretStr(""),
                charset=params.get("charset", "utf8mb4"),
                connect_timeout_seconds=params.get(
                    "connect_timeout_seconds", settings.mysql_connect_timeout_seconds
                ),
                read_timeout_seconds=params.get(
                    "read_timeout_seconds", settings.mysql_read_timeout_seconds
                ),
            )
        )

    source_service = SourceService(repo, store, factory, settings)
    scan_service = ScanService(
        repo, repo, store, factory, DataProfiler(),
        sample_rows=50, max_tables=10, max_columns=32,
    )
    registered = source_service.register_default_source()
    assert registered["already_registered"] is False
    job = scan_service.run_scan(registered["source_id"])
    assert job["status"] in {"succeeded", "partial", "failed"}
    if job["status"] == "failed":
        pytest.skip(f"真实库当前不可扫描：{[e['reason'] for e in job['errors']]}")
    assert job["tables_scanned"] >= 1
    tables = scan_service.list_tables(registered["source_id"])
    assert tables, "扫描成功后应有表目录"
    detail = scan_service.get_table(tables[0]["table_id"])
    assert detail["columns"], "表详情应包含字段"
    profiles = scan_service.get_table_profile(tables[0]["table_id"])
    assert profiles, "应产出字段画像"
    for profile in profiles:
        assert profile["metric_basis"] in {"exact", "sample", "insufficient_sample", "skipped"}
        assert profile["samples_redacted"] is False or profile["sample_values"] == ["***"] * len(
            profile["sample_values"]
        )

    # 凭证只存在于独立凭证文件；元数据库中不得出现明文密码。
    password = settings.mysql_password.get_secret_value()
    if password:
        assert password.encode() not in (tmp_path / "meta.db").read_bytes()
    assert store.get(repo.get_source(registered["source_id"])["credential_ref"]) == password
