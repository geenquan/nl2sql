"""数据源用例编排（M1）。

数据源选择规则（见 docs/configuration.md）：
- 以 source_id 操作的接口（测试连接、扫描、目录查询）一律使用已持久化的数据源配置；
- `POST /sources/test` 仅使用请求体中的连接参数做一次性测试，不落库；
- `POST /sources/default` 显式把 `DATA_MYSQL_*`（Settings.mysql_*）注册为名为
  default 的数据源；幂等，不会自动在启动时注册。
"""

import time
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import (
    DataSourceConnectionError,
    ResourceNotFoundError,
    UnsafeOperationError,
)
from app.ports.connectors import ConnectorFactory, DataSourceConnector
from app.ports.repositories import SourceRepository
from app.security.credentials import CredentialStore

DEFAULT_SOURCE_NAME = "default"


class SourceService:
    """数据源用例编排。只依赖端口，不直接操作数据库驱动。"""

    def __init__(
        self,
        source_repository: SourceRepository,
        credential_store: CredentialStore,
        connector_factory: ConnectorFactory,
        settings: Settings,
    ) -> None:
        self._repository = source_repository
        self._credentials = credential_store
        self._connector_factory = connector_factory
        self._settings = settings

    # -- 创建与查询 ---------------------------------------------------------

    def create_source(self, source_request: dict[str, Any]) -> dict[str, Any]:
        dialect = str(source_request.get("dialect", "")).lower()
        if dialect != "mysql":
            raise ValueError("首版仅支持 mysql 数据源")
        if not source_request.get("read_only", True):
            raise UnsafeOperationError("数据源必须只读；read_only=false 不被允许")
        password = source_request.get("password")
        if password is None:
            raise ValueError("缺少密码")
        if isinstance(password, SecretStr):
            password = password.get_secret_value()
        credential_ref = f"cred-{uuid4().hex[:16]}"
        record = {
            "name": source_request["name"],
            "dialect": "mysql",
            "host": source_request["host"],
            "port": source_request["port"],
            "database": source_request["database"],
            "username": source_request["username"],
            "credential_ref": credential_ref,
            "allowed_schemas": source_request.get("schemas") or [],
            "read_only": True,
            "is_default": False,
        }
        # 先写凭证再建数据源；数据源创建失败时回收凭证，避免留下不可用配置却报告成功。
        self._credentials.put(credential_ref, password)
        try:
            source_id = self._repository.create_source(record)
        except Exception:
            self._credentials.delete(credential_ref)
            raise
        return {"source_id": source_id, **record}

    def list_sources(self) -> list[dict[str, Any]]:
        return self._repository.list_sources()

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = self._repository.get_source(source_id)
        if source is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        return source

    def register_default_source(self) -> dict[str, Any]:
        """把 env/`DATA_MYSQL_*` 配置注册为名为 default 的数据源（幂等）。"""
        existing = self._repository.find_source_by_name(DEFAULT_SOURCE_NAME)
        if existing is not None:
            return {**existing, "already_registered": True}
        settings = self._settings
        record = {
            "name": DEFAULT_SOURCE_NAME,
            "dialect": "mysql",
            "host": settings.mysql_host,
            "port": settings.mysql_port,
            "database": settings.mysql_database,
            "username": settings.mysql_username,
            "credential_ref": f"cred-{uuid4().hex[:16]}",
            "allowed_schemas": [settings.mysql_database],
            "read_only": True,
            "is_default": True,
        }
        self._credentials.put(
            record["credential_ref"], settings.mysql_password.get_secret_value()
        )
        try:
            source_id = self._repository.create_source(record)
        except Exception:
            self._credentials.delete(record["credential_ref"])
            raise
        return {"source_id": source_id, **record, "already_registered": False}

    # -- 连接测试 -------------------------------------------------------------

    def test_connection(self, source_request: dict[str, Any]) -> dict[str, Any]:
        """对请求体连接参数做一次性测试；不持久化任何数据。"""
        dialect = str(source_request.get("dialect", "")).lower()
        if dialect != "mysql":
            raise ValueError("首版仅支持 mysql 数据源")
        password = source_request.get("password")
        if isinstance(password, SecretStr):
            password = password.get_secret_value()
        params = self._connection_params(
            host=source_request["host"],
            port=source_request["port"],
            database=source_request["database"],
            username=source_request["username"],
            password=password or "",
        )
        return self._probe(params)

    def test_persisted_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_source(source_id)
        password = self._credentials.get(source["credential_ref"])
        if password is None:
            return {
                "ok": False,
                "dialect": source["dialect"],
                "source_id": source_id,
                "name": source["name"],
                "error": {
                    "reason": "credential_missing",
                    "message": "凭证不存在或已被删除，无法建立连接",
                },
            }
        params = self._connection_params(
            host=source["host"],
            port=source["port"],
            database=source["database"],
            username=source["username"],
            password=password,
        )
        result = self._probe(params)
        return {"source_id": source_id, "name": source["name"], **result}

    def _connection_params(
        self,
        *,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        return {
            "dialect": "mysql",
            "host": host,
            "port": port,
            "database": database,
            "username": username,
            "password": SecretStr(password),
            "charset": self._settings.mysql_charset,
            "connect_timeout_seconds": self._settings.mysql_connect_timeout_seconds,
            "read_timeout_seconds": self._settings.mysql_read_timeout_seconds,
        }

    def _probe(self, params: dict[str, Any]) -> dict[str, Any]:
        connector: DataSourceConnector = self._connector_factory(params)
        started = time.perf_counter()
        try:
            connector.test_connection()
        except DataSourceConnectionError as exc:
            return {
                "ok": False,
                "dialect": params["dialect"],
                "error": {"reason": exc.reason, "message": str(exc)},
            }
        finally:
            connector.close()
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "dialect": params["dialect"], "latency_ms": latency_ms}
