"""API 组装根：把配置、适配器和服务装配起来。

只有本模块知道 MySQLConnector / SQLiteMetadataRepository / FileCredentialStore
的具体实现；路由与服务通过 port 协作，测试用 dependency_overrides 替换。
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from pydantic import SecretStr

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings, get_settings
from app.llm.deepseek_client import DeepSeekClient
from app.ports.connectors import ConnectorFactory, DataSourceConnector, ProbeFactory
from app.ports.llm import LLMClient
from app.profiling.profiler import DataProfiler
from app.security.credentials import CredentialStore, FileCredentialStore
from app.services.dataset_service import DatasetService
from app.services.relationship_service import RelationshipService
from app.services.scan_service import ScanService
from app.services.semantic_model_service import SemanticModelService
from app.services.semantics_service import SemanticsService
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
        # M3 验证运行注入语句计数（可选；不注入时行为与历史一致）
        return MySQLConnector(config,
                              statement_counter=params.get("statement_counter"))

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


@lru_cache
def _llm_factory() -> Callable[[], LLMClient] | None:
    settings = _settings()
    if not settings.deepseek_enabled:
        return None  # 规则模式；不伪装模型推断

    def factory() -> LLMClient:
        return DeepSeekClient(
            api_base=settings.llm_api_base,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            max_output_tokens=settings.llm_max_tokens,
        )

    return factory


def get_semantics_service() -> SemanticsService:
    return SemanticsService(
        metadata_repository=_metadata_repository(),
        llm_factory=_llm_factory(),
        settings=_settings(),
    )


@lru_cache
def _probe_factory() -> ProbeFactory:
    settings = _settings()

    def factory(params: dict[str, Any]) -> MySQLRelationshipProbe:
        # 与 Connector 相同参数（含密码 SecretStr）；探查复用连接级 READ ONLY。
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
        return MySQLRelationshipProbe(
            config, statement_counter=params.get("statement_counter"))

    return factory


def get_semantic_model_service() -> SemanticModelService:
    return SemanticModelService(
        metadata_repository=_metadata_repository(),
        settings=_settings(),
    )


def _dataset_executor_factory(payload: dict, model: dict,
                              settings) -> object:
    """构建受控只读执行器（白名单来自模型实体；源库连接）。"""
    from app.adapters.dataset_executors import MySQLControlledExecutor

    tables: dict[str, set[str]] = {}
    for entity in model.get("entities", []):
        source = entity.get("source") or {}
        key = f"{source.get('schema')}.{source.get('table')}".lower()
        tables[key] = {
            a["field"]["column"].lower()
            for a in entity.get("attributes", [])}
    source = _metadata_repository().get_source(payload["source_id"])
    from app.security.credentials import get_credential_store

    store = get_credential_store()
    password = store.get(source["credential_ref"]) or ""
    return MySQLControlledExecutor(
        host=source["host"], port=int(source["port"]),
        database=source["database"], username=source["username"],
        password=password, allowed_tables=set(tables.keys()),
        allowed_columns=tables,
        read_timeout=settings.mysql_read_timeout_seconds)


def _view_publisher_factory():
    from app.adapters.dataset_executors import MySQLViewPublisher

    settings = _settings()
    dsn = settings.dataset_view_target_dsn.get_secret_value()
    if not dsn or not settings.dataset_view_target_schema:
        raise ValueError("视图发布目标未配置（dataset_view_target_dsn/schema）")
    # DSN 形如 user:password@host:port
    credentials, _, hostport = dsn.rpartition("@")
    username, _, password = credentials.partition(":")
    host, _, port = hostport.partition(":")
    return MySQLViewPublisher(host=host, port=int(port or 3306),
                              username=username, password=password)


def _es_publisher_factory():
    from app.adapters.es_publisher import ESHttpPublisher

    settings = _settings()
    return ESHttpPublisher(
        hosts=settings.es_hosts, username=settings.es_username,
        password=settings.es_password, api_key=settings.es_api_key,
        timeout_seconds=settings.es_request_timeout_seconds,
        verify_certs=settings.es_verify_certs)


def get_dataset_service() -> DatasetService:
    return DatasetService(
        metadata_repository=_metadata_repository(),
        semantic_models=get_semantic_model_service(),
        settings=_settings(),
        executor_factory=_dataset_executor_factory,
        view_publisher_factory=_view_publisher_factory,
        es_publisher_factory=_es_publisher_factory,
    )


def get_relationship_service() -> RelationshipService:
    return RelationshipService(
        metadata_repository=_metadata_repository(),
        source_repository=_metadata_repository(),
        credential_store=_credential_store(),
        connector_factory=_connector_factory(),
        probe_factory=_probe_factory(),
        settings=_settings(),
    )
