"""请求校验错误不得回显请求体。

SecretStr 只保护成功解析后的模型；解析失败时 FastAPI 默认把原始 input
放回 422 响应。这里统一拦截 RequestValidationError，只保留 loc/type/msg。
注意：FastAPI 在校验请求体之前就会解析路由依赖，因此必须覆盖依赖，
否则真实装配会在项目根创建 metadata.db。
"""


import pytest
from fastapi.testclient import TestClient

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.api import deps
from app.core.config import Settings
from app.main import app
from app.profiling.profiler import DataProfiler
from app.security.credentials import FileCredentialStore
from app.services.scan_service import ScanService
from app.services.source_service import SourceService
from tests.fakes import FakeConnector


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    tmp_path = tmp_path_factory.mktemp("validation")
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    store = FileCredentialStore(tmp_path / "creds.json")
    settings = Settings(_env_file=None)
    factory = lambda params: FakeConnector(params)
    app.dependency_overrides[deps.get_source_service] = lambda: SourceService(
        repo, store, factory, settings
    )
    app.dependency_overrides[deps.get_scan_service] = lambda: ScanService(
        repo, repo, store, factory, DataProfiler(), sample_rows=10, max_tables=5, max_columns=8
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _post(client: TestClient, body: dict):
    return client.post("/api/v1/sources/test", json=body)


def test_missing_required_field_does_not_echo_password(client: TestClient) -> None:
    response = _post(
        client,
        {
            "dialect": "mysql",
            "host": "h",
            "database": "d",
            "username": "u",
            "password": "LeakMe-123456",
        },  # 缺 port
    )
    assert response.status_code == 422
    assert "LeakMe-123456" not in response.text
    assert '"input"' not in response.text
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "port"]
    assert error["type"] == "missing"


def test_invalid_port_still_locatable_without_secret(client: TestClient) -> None:
    response = _post(
        client,
        {
            "dialect": "mysql",
            "host": "h",
            "port": "not-a-port",
            "database": "d",
            "username": "u",
            "password": "LeakMe-654321",
        },
    )
    assert response.status_code == 422
    assert "LeakMe-654321" not in response.text
    assert "port" in str(response.json()["detail"][0]["loc"])


def test_password_type_error_does_not_echo_nested_secret(client: TestClient) -> None:
    response = _post(
        client,
        {
            "dialect": "mysql",
            "host": "h",
            "port": 3306,
            "database": "d",
            "username": "u",
            "password": {"nested": "Nested-Key-789"},
        },
    )
    assert response.status_code == 422
    assert "Nested-Key-789" not in response.text
    assert "password" in str(response.json()["detail"][0]["loc"])


def test_nested_input_with_secret_is_not_echoed(client: TestClient) -> None:
    # schemas 是 create 模型的 list[str] 字段；传 dict 触发类型错误。
    response = client.post(
        "/api/v1/sources",
        json={
            "name": "x",
            "dialect": "mysql",
            "host": "h",
            "port": 3306,
            "database": "d",
            "username": "u",
            "password": "ok-password",
            "schemas": {"inner": "Inner-Secret-xyz"},
        },
    )
    assert response.status_code == 422
    assert "Inner-Secret-xyz" not in response.text
    assert "schemas" in str(response.json()["detail"][0]["loc"])


def test_error_entries_have_only_safe_fields(client: TestClient) -> None:
    response = _post(client, {"dialect": "mysql"})  # 缺大量字段
    assert response.status_code == 422
    for error in response.json()["detail"]:
        assert set(error) <= {"loc", "type", "msg"}
        assert isinstance(error["msg"], str)
        assert len(error["msg"]) <= 300


def test_create_source_endpoint_shares_sanitized_handler(client: TestClient) -> None:
    response = client.post(
        "/api/v1/sources",
        json={"name": "x", "dialect": "mysql", "password": "Create-Leak-1"},  # 缺 host 等
    )
    assert response.status_code == 422
    assert "Create-Leak-1" not in response.text


# 结构合法但语义非法（如 dialect=postgres）走业务层 400，由 tests/test_sources_api.py
# 在依赖覆盖下覆盖；此处不触真实依赖装配。
