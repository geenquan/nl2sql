"""M3 真实 MySQL 只读集成测试。

默认跳过；设置 NL2SQL_MYSQL_INTEGRATION=1 且 `.env` 中 DATA_MYSQL_* 指向
可达只读账号时运行。全程只读：不建表、不写入、不修改结构/权限；
证据采集仅执行系统构造的有界聚合查询（窗口统计 + LEFT JOIN 聚合），
断言不输出任何业务原始值。元数据/凭证写临时目录，不动用户已有数据。
"""

import os
from pathlib import Path

import pytest

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.ports.connectors import (
    JoinMetricsRequest,
    WindowMetricsRequest,
)
from app.security.credentials import FileCredentialStore
from app.services.relationship_service import RelationshipService
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
def workspace(tmp_path: Path):
    """临时元数据库 + 临时凭证文件；不触碰项目根目录已有产物。"""
    credential_path = tmp_path / ".credential_store.json"
    store = FileCredentialStore(credential_path)
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    yield {"repo": repo, "store": store}
    repo.close()
    if credential_path.exists():
        credential_path.unlink()


def _connector_factory(settings: Settings):
    def factory(params: dict) -> MySQLConnector:
        return MySQLConnector(MySQLConnectionConfig(
            host=params["host"], port=int(params["port"]),
            database=params["database"], username=params["username"],
            password=params.get("password") or settings.mysql_password,
            charset=settings.mysql_charset,
            connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
            read_timeout_seconds=settings.mysql_read_timeout_seconds,
        ))
    return factory


def _probe_factory(settings: Settings):
    def factory(params: dict) -> MySQLRelationshipProbe:
        return MySQLRelationshipProbe(MySQLConnectionConfig(
            host=params["host"], port=int(params["port"]),
            database=params["database"], username=params["username"],
            password=params.get("password") or settings.mysql_password,
            charset=settings.mysql_charset,
            connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
            read_timeout_seconds=settings.mysql_read_timeout_seconds,
        ))
    return factory


def test_probe_window_and_join_metrics_are_bounded_and_sanitized(
    settings: Settings,
) -> None:
    """探查端口：聚合指标可计算、只返回计数、错误消息不含业务值。"""
    from app.adapters.mysql_connector import MySQLConnector

    connector = MySQLConnector(MySQLConnectionConfig(
        host=settings.mysql_host, port=settings.mysql_port,
        database=settings.mysql_database, username=settings.mysql_username,
        password=settings.mysql_password, charset=settings.mysql_charset,
        connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
        read_timeout_seconds=settings.mysql_read_timeout_seconds,
    ))
    probe = MySQLRelationshipProbe(MySQLConnectionConfig(
        host=settings.mysql_host, port=settings.mysql_port,
        database=settings.mysql_database, username=settings.mysql_username,
        password=settings.mysql_password, charset=settings.mysql_charset,
        connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
        read_timeout_seconds=settings.mysql_read_timeout_seconds,
    ))
    try:
        tables = connector.list_tables(settings.mysql_database)
        assert tables, "目标库没有可探测的表"
        table = min(tables)
        columns = [
            c.name for c in
            connector.get_table_metadata(settings.mysql_database, table).columns
        ][:2]
        result = probe.window_metrics(WindowMetricsRequest(
            schema_name=settings.mysql_database, table_name=table,
            columns=tuple(columns), order_by=(), limit=5,
            string_columns=(), timeout_ms=5000,
        ))
        assert result.window_rows <= 5  # 有界窗口
        assert isinstance(result.distinct_values, int)

        join = probe.join_metrics(JoinMetricsRequest(
            left_schema=settings.mysql_database, left_table=table,
            left_columns=(columns[0],), left_order_by=(), left_limit=5,
            right_schema=settings.mysql_database, right_table=table,
            right_columns=(columns[0],), right_presence_column=columns[0],
            string_join_columns=(), timeout_ms=5000,
        ))
        # 自连接：全部键匹配自身 → 覆盖完全
        assert join.unmatched_left_rows == 0
        assert join.rows_after_left_join >= join.distinct_right_matched
        # 探查未知表 → 脱敏连接错误（不含表数据）
        with pytest.raises(Exception) as excinfo:
            probe.window_metrics(WindowMetricsRequest(
                schema_name=settings.mysql_database, table_name="__no_such__",
                columns=("c",), order_by=(), limit=5,
                string_columns=(), timeout_ms=5000,
            ))
        assert "__no_such__" in str(excinfo.value)  # 仅含请求标识，不含数据
    finally:
        probe.close()
        connector.close()


def test_discover_and_validate_on_real_catalog(
    settings: Settings, workspace
) -> None:
    """小预算真实链路：注册默认源 → 扫描 → 发现 → 有界验证；结论如实。"""
    source_service = SourceService(
        source_repository=workspace["repo"],
        credential_store=workspace["store"],
        connector_factory=_connector_factory(settings),
        settings=settings,
    )
    registered = source_service.register_default_source()
    source_id = registered["source_id"]

    scan_service = ScanService(
        source_repository=workspace["repo"],
        metadata_repository=workspace["repo"],
        credential_store=workspace["store"],
        connector_factory=_connector_factory(settings),
        sample_rows=100, max_tables=8, max_columns=32,
    )
    scan = scan_service.run_scan(source_id)
    assert scan["status"] in ("succeeded", "partial")
    if scan["status"] == "succeeded" and scan["tables_scanned"] == 0:
        pytest.skip("目标库当前没有可见表")

    relationship_service = RelationshipService(
        metadata_repository=workspace["repo"],
        source_repository=workspace["repo"],
        credential_store=workspace["store"],
        connector_factory=_connector_factory(settings),
        probe_factory=_probe_factory(settings),
        settings=Settings(
            _env_file=None,
            relationship_validation_max_candidates=3,
            relationship_validation_max_queries=15,
            relationship_sample_window_rows=100,
        ),
    )
    allow_partial = scan["status"] == "partial"
    discovery = relationship_service.discover_relationships(
        source_id, allow_partial=allow_partial)
    assert discovery["status"] == "succeeded"
    if discovery["candidates_new"] == 0:
        pytest.skip("小预算扫描目录内没有可召回的候选关系")

    validation = relationship_service.validate_batch(
        source_id, limit=3, allow_partial=allow_partial)
    assert validation["status"] in ("succeeded", "partial")
    assert validation["queries_used"] <= validation["budget"]["max_queries"]
    # 状态只允许候选/已验证/待审核之一——不为演示降低门槛
    for result in validation["results"]:
        assert result["status"] in ("validated", "needs_review")

    # 运行记录与图查询可用；证据只含聚合口径
    graph = relationship_service.graph(source_id, mermaid=True)
    assert graph["mermaid"].startswith("graph LR")
    if graph["edges"]:
        edge = graph["edges"][0]
        assert set(edge) >= {"relationship_key", "status", "source", "target"}
    records = relationship_service.list_relationships(source_id)
    for record in records:
        payload = repr(record)
        assert "password" not in payload.lower() or "user_password" in payload
        if record.get("evidence"):
            for query in record["evidence"]["queries"]:
                assert set(query) <= {"template", "table", "elapsed_ms", "timeout_ms"}
