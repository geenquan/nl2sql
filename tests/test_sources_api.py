from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.api import deps
from app.core.config import Settings
from app.main import app
from app.profiling.profiler import DataProfiler
from app.security.credentials import FileCredentialStore
from app.services.scan_service import ScanService
from app.services.source_service import SourceService
from tests.fakes import FakeConnector

CREATE_BODY = {
    "name": "local-mysql",
    "dialect": "mysql",
    "host": "db.internal",
    "port": 3306,
    "database": "sales",
    "username": "readonly",
    "password": "api-test-secret",
    "schemas": ["sales"],
    "read_only": True,
}


@pytest.fixture
def client(tmp_path: Any) -> TestClient:
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    store = FileCredentialStore(tmp_path / "creds.json")
    settings = Settings(
        _env_file=None,
        mysql_host="default.internal",
        mysql_database="sales",
        mysql_username="readonly",
        mysql_password=SecretStr("default-secret"),
    )
    factory = lambda params: FakeConnector(params)
    source_service = SourceService(repo, store, factory, settings)
    scan_service = ScanService(
        repo, repo, store, factory, DataProfiler(),
        sample_rows=100, max_tables=10, max_columns=64,
    )
    app.dependency_overrides[deps.get_source_service] = lambda: source_service
    app.dependency_overrides[deps.get_scan_service] = lambda: scan_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestSourceLifecycle:
    def test_create_source_returns_201_without_password(self, client: TestClient) -> None:
        response = client.post("/api/v1/sources", json=CREATE_BODY)
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "local-mysql"
        assert data["credential_ref"].startswith("cred-")
        assert "password" not in data
        assert "api-test-secret" not in response.text

    def test_create_rejects_unknown_dialect(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/sources", json={**CREATE_BODY, "dialect": "oracle"}
        )
        assert response.status_code == 400

    def test_create_rejects_writable_source(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/sources", json={**CREATE_BODY, "read_only": False}
        )
        assert response.status_code == 400

    def test_list_and_get_source_cards(self, client: TestClient) -> None:
        created = client.post("/api/v1/sources", json=CREATE_BODY).json()
        listing = client.get("/api/v1/sources")
        assert listing.status_code == 200
        assert [s["name"] for s in listing.json()] == ["local-mysql"]
        detail = client.get(f"/api/v1/sources/{created['source_id']}")
        assert detail.status_code == 200
        assert detail.json()["table_count"] == 0
        assert detail.json()["latest_scan"] is None

    def test_get_missing_source_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/sources/nope").status_code == 404


class TestConnectionTest:
    def test_request_body_test_ok(self, client: TestClient) -> None:
        body = {k: v for k, v in CREATE_BODY.items() if k not in ("name", "schemas", "read_only")}
        response = client.post("/api/v1/sources/test", json=body)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "api-test-secret" not in response.text

    def test_request_body_test_failure_sanitized(self, client: TestClient) -> None:
        body = {
            "dialect": "mysql", "host": "down.internal", "port": 3306,
            "database": "sales", "username": "readonly", "password": "api-test-secret",
        }
        response = client.post("/api/v1/sources/test", json=body)
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["error"]["reason"] == "unreachable"
        assert "api-test-secret" not in response.text

    def test_persisted_source_test(self, client: TestClient) -> None:
        created = client.post("/api/v1/sources", json=CREATE_BODY).json()
        response = client.post(f"/api/v1/sources/{created['source_id']}/test")
        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestDefaultSource:
    def test_register_default_is_idempotent(self, client: TestClient) -> None:
        first = client.post("/api/v1/sources/default")
        assert first.status_code == 200
        assert first.json()["already_registered"] is False
        assert first.json()["host"] == "default.internal"
        assert "default-secret" not in first.text
        second = client.post("/api/v1/sources/default")
        assert second.status_code == 200
        assert second.json()["already_registered"] is True
        assert second.json()["source_id"] == first.json()["source_id"]


class TestScanFlow:
    def test_full_scan_catalog_and_profile(self, client: TestClient) -> None:
        created = client.post("/api/v1/sources", json=CREATE_BODY).json()
        source_id = created["source_id"]

        scan = client.post(f"/api/v1/sources/{source_id}/scans")
        assert scan.status_code == 201
        job = scan.json()
        assert job["status"] == "partial"
        assert job["tables_scanned"] == 2
        assert job["errors"][0]["table"] == "broken"

        job_detail = client.get(f"/api/v1/scans/{job['job_id']}")
        assert job_detail.status_code == 200
        assert job_detail.json()["job_id"] == job["job_id"]
        assert client.get("/api/v1/scans/nope").status_code == 404

        scans_list = client.get(f"/api/v1/sources/{source_id}/scans")
        assert scans_list.status_code == 200
        assert len(scans_list.json()) == 1

        schemas = client.get(f"/api/v1/sources/{source_id}/schemas")
        assert schemas.status_code == 200
        assert schemas.json() == [{"schema_name": "sales", "table_count": 2}]

        catalog = client.get(f"/api/v1/sources/{source_id}/tables")
        assert catalog.status_code == 200
        items = catalog.json()
        assert [t["table_name"] for t in items] == ["customers", "orders"]
        assert items[0]["row_count_is_estimate"] is True
        assert items[0]["column_count"] == 4

        # 数据源卡片区分“最近扫描任务”与“当前目录来源”
        card = client.get(f"/api/v1/sources/{source_id}").json()
        assert card["latest_scan"]["status"] == "partial"
        assert card["catalog"]["snapshot_id"] == job["snapshot_id"]
        assert card["catalog"]["complete"] is False  # 首次只有部分结果，明确标记不完整

        table_id = items[0]["table_id"]
        detail = client.get(f"/api/v1/tables/{table_id}")
        assert detail.status_code == 200
        table = detail.json()
        assert table["primary_key"] == ["id"]
        assert [c["name"] for c in table["columns"]] == [
            "id", "user_password", "contact", "remark",
        ]

        profile = client.get(f"/api/v1/tables/{table_id}/profile")
        assert profile.status_code == 200
        profiles = {p["column_name"]: p for p in profile.json()}
        assert profiles["user_password"]["sample_values"] == ["***", "***"]
        assert profiles["user_password"]["samples_redacted"] is True
        # 值格式检测：API 响应不含原始邮箱/手机号样本
        assert profiles["contact"]["sample_values"] == ["z***@example.com", "l***@example.com"]
        assert profiles["remark"]["sample_values"] == ["****5678", "***"]
        assert profiles["id"]["metric_basis"] == "exact"
        assert profiles["id"]["profile_version"] == "v1"
        for raw in ("raw-secret-1", "zhang_san@example.com", "13812345678", "13912345678"):
            assert raw not in profile.text

        assert client.get("/api/v1/tables/nope").status_code == 404
        assert client.get("/api/v1/tables/nope/profile").status_code == 404

    def test_catalog_survives_all_failed_rescan_and_history_viewable(self, client: TestClient) -> None:
        created = client.post("/api/v1/sources", json=CREATE_BODY).json()
        source_id = created["source_id"]
        FakeConnector.broken_fails = False
        ok = client.post(f"/api/v1/sources/{source_id}/scans").json()
        assert ok["status"] == "succeeded"
        complete_snapshot = ok["snapshot_id"]

        FakeConnector.broken_fails = True
        failed = client.post(f"/api/v1/sources/{source_id}/scans").json()
        assert failed["status"] == "partial"  # broken 失败但 customers/orders 成功
        partial_snapshot = failed["snapshot_id"]

        # 默认目录仍是完整成功快照（3 张表）
        catalog = client.get(f"/api/v1/sources/{source_id}/tables").json()
        assert [t["table_name"] for t in catalog] == ["broken", "customers", "orders"]
        card = client.get(f"/api/v1/sources/{source_id}").json()
        assert card["catalog"]["snapshot_id"] == complete_snapshot
        assert card["catalog"]["complete"] is True
        assert card["latest_scan"]["status"] == "partial"

        # 部分成功快照保留，可显式查看用于诊断
        history = client.get(
            f"/api/v1/sources/{source_id}/tables",
            params={"snapshot_id": partial_snapshot},
        ).json()
        assert [t["table_name"] for t in history] == ["customers", "orders"]

    def test_scan_missing_source_404(self, client: TestClient) -> None:
        assert client.post("/api/v1/sources/nope/scans").status_code == 404

    def test_tables_filter_by_schema(self, client: TestClient) -> None:
        created = client.post("/api/v1/sources", json={**CREATE_BODY, "schemas": ["other"]}).json()
        client.post(f"/api/v1/sources/{created['source_id']}/scans")
        catalog = client.get(
            f"/api/v1/sources/{created['source_id']}/tables", params={"schema": "other"}
        )
        assert [t["table_name"] for t in catalog.json()] == ["legacy"]
        empty = client.get(
            f"/api/v1/sources/{created['source_id']}/tables", params={"schema": "none"}
        )
        assert empty.json() == []


class TestScanErrorSanitizationApi:
    def test_scan_errors_api_response_has_no_business_payload(self, tmp_path) -> None:
        """扫描错误经 GET /scans/{id} 返回时不得包含异常携带的业务样本。"""
        from pydantic import SecretStr as _SecretStr

        from app.core.config import Settings as _Settings
        from tests.fakes import FakeConnector as _Fake
        from tests.test_services import LEAKY_PAYLOAD, SENSITIVE_FRAGMENTS

        class LeakyConnector(_Fake):
            def get_table_metadata(self, schema_name, table_name):
                raise RuntimeError(f"inspector crashed: {LEAKY_PAYLOAD}")

        repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
        store = FileCredentialStore(tmp_path / "creds.json")
        source_service = SourceService(
            repo, store, lambda params: LeakyConnector(params),
            _Settings(_env_file=None, mysql_password=_SecretStr("x")),
        )
        scan_service = ScanService(
            repo, repo, store, lambda params: LeakyConnector(params), DataProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )
        overrides = dict(app.dependency_overrides)
        app.dependency_overrides[deps.get_source_service] = lambda: source_service
        app.dependency_overrides[deps.get_scan_service] = lambda: scan_service
        try:
            with TestClient(app) as client:
                created = client.post("/api/v1/sources", json=CREATE_BODY).json()
                scan = client.post(f"/api/v1/sources/{created['source_id']}/scans")
                assert scan.status_code == 201
                job = scan.json()
                assert job["status"] == "failed"
                detail = client.get(f"/api/v1/scans/{job['job_id']}")
                assert detail.status_code == 200
                for fragment in SENSITIVE_FRAGMENTS:
                    assert fragment not in detail.text
                    assert fragment not in scan.text
                error = detail.json()["errors"][0]
                assert error["reason"] == "table_scan_error"
                assert "读取表结构" in error["message"] and "RuntimeError" in error["message"]
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)


class TestCredentialStoreUnavailable:
    def test_credential_store_failure_returns_safe_503(self, tmp_path) -> None:
        """凭证存储读写故障统一转换为 CredentialStoreError → API 503 固定安全消息。"""
        from pydantic import SecretStr as _SecretStr

        from app.core.config import Settings as _Settings
        from tests.fakes import FakeConnector as _Fake

        repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
        broken_store = FileCredentialStore(tmp_path)  # 目录路径：读写必然失败
        source_service = SourceService(
            repo, broken_store, lambda params: _Fake(params),
            _Settings(_env_file=None, mysql_password=_SecretStr("x")),
        )
        scan_service = ScanService(
            repo, repo, broken_store, lambda params: _Fake(params), DataProfiler(),
            sample_rows=10, max_tables=5, max_columns=8,
        )
        overrides = dict(app.dependency_overrides)
        app.dependency_overrides[deps.get_source_service] = lambda: source_service
        app.dependency_overrides[deps.get_scan_service] = lambda: scan_service
        try:
            with TestClient(app) as client:
                response = client.post("/api/v1/sources", json=CREATE_BODY)
                assert response.status_code == 503
                assert "读取失败" in response.text
                assert "api-test-secret" not in response.text  # 不回显请求凭证
                listing = client.get("/api/v1/sources")
                assert listing.status_code == 200
                assert listing.json() == []  # 未持久化不得产生数据源
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)
