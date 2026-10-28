"""API 组装根：把配置、适配器和服务装配起来。

只有本模块知道 MySQLConnector / SQLiteMetadataRepository / FileCredentialStore
的具体实现；路由与服务通过 port 协作，测试用 dependency_overrides 替换。
"""

from functools import lru_cache
from typing import Any

from pydantic import SecretStr

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings, get_settings
from app.ports.connectors import ConnectorFactory, DataSourceConnector
from app.profiling.profiler import DataProfiler
from app.security.credentials import CredentialStore, FileCredentialStore
from app.services.scan_service import ScanService
from app.services.source_service import SourceService


@lru_cache
def _settings() -> Settings:
    return get_settings()


@lru_cache
def _metadata_repository() -> SQLiteMetadataRepository:
    return SQLiteMetadataRepository(_settings().metadata_database_url)


@lru_cache
def _credential_store() -> FileCredentialStore:
    return FileCredentialStore(_settings().credential_store_path)


@lru_cache
def _connector_factory() -> ConnectorFactory:
    settings = _settings()

    def factory(params: dict[str, Any]) -> DataSourceConnector:
        config = MySQLConnectionConfig(
            host=params["host"],
            port=int(params["port"]),
            database=params["database"],
            username=params["username"],
            password=params.get("password") or SecretStr(""),
            charset=params.get("charset", settings.mysql_charset),
            connect_timeout_seconds=params.get(
                "connect_timeout_seconds", settings.mysql_connect_timeout_seconds
            ),
            read_timeout_seconds=params.get(
                "read_timeout_seconds", settings.mysql_read_timeout_seconds
            ),
        )
        return MySQLConnector(config)

    return factory


def get_credential_store() -> CredentialStore:
    return _credential_store()


def get_source_service() -> SourceService:
    settings = _settings()
    return SourceService(
        source_repository=_metadata_repository(),
        credential_store=_credential_store(),
        connector_factory=_connector_factory(),
        settings=settings,
    )


def get_scan_service() -> ScanService:
    settings = _settings()
    return ScanService(
        source_repository=_metadata_repository(),
        metadata_repository=_metadata_repository(),
        credential_store=_credential_store(),
        connector_factory=_connector_factory(),
        profiler=DataProfiler(sensitive_keywords=settings.sensitive_column_keywords),
        sample_rows=settings.profile_sample_rows,
        max_tables=settings.scan_max_tables,
        max_columns=settings.profile_max_columns,
        max_recorded_errors=settings.scan_max_recorded_errors,
    )
