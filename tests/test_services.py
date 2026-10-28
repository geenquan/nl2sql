from typing import Any

import pytest
from pydantic import SecretStr

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError, UnsafeOperationError
from app.profiling.profiler import DataProfiler
from app.security.credentials import FileCredentialStore
from app.services.scan_service import ScanService
from app.services.source_service import SourceService
from tests.fakes import FakeConnector


@pytest.fixture
def wiring(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    store = FileCredentialStore(tmp_path / "creds.json")
    created_configs: list[dict[str, Any]] = []
    controls: dict[str, bool] = {"factory_error": False}
    settings = Settings(
        _env_file=None,
        mysql_host="default.internal",
        mysql_port=3307,
        mysql_database="sales",
        mysql_username="readonly",
        mysql_password=SecretStr("default-secret"),
    )

    def factory(params: dict[str, Any]):
        if controls["factory_error"]:
            raise RuntimeError("连接器构建失败")
        created_configs.append(params)
        return FakeConnector(params)

    source_service = SourceService(
        source_repository=repo,
        credential_store=store,
        connector_factory=factory,
        settings=settings,
    )
    scan_service = ScanService(
        source_repository=repo,
        metadata_repository=repo,
        credential_store=store,
        connector_factory=factory,
        profiler=DataProfiler(),
        sample_rows=100,
        max_tables=10,
        max_columns=64,
        max_recorded_errors=50,
    )
    return {
        "repo": repo,
        "store": store,
        "source_service": source_service,
        "scan_service": scan_service,
        "created_configs": created_configs,
        "controls": controls,
        "settings": settings,
    }


VALID_SOURCE = {
    "name": "local-mysql",
    "dialect": "mysql",
    "host": "db.internal",
    "port": 3306,
    "database": "sales",
    "username": "readonly",
    "password": "request-body-secret",
    "schemas": ["sales"],
    "read_only": True,
}


class TestSourceServiceCreate:
    def test_create_persists_source_and_credential_without_password(self, wiring) -> None:
        result = wiring["source_service"].create_source(dict(VALID_SOURCE))
        assert result["name"] == "local-mysql"
        assert result["dialect"] == "mysql"
        assert "password" not in result
        stored = wiring["repo"].get_source(result["source_id"])
        assert stored["credential_ref"].startswith("cred-")
        assert wiring["store"].get(stored["credential_ref"]) == "request-body-secret"

    def test_create_rejects_non_mysql_dialect(self, wiring) -> None:
        with pytest.raises(ValueError, match="mysql"):
            wiring["source_service"].create_source({**VALID_SOURCE, "dialect": "postgres"})

    def test_create_rejects_read_only_false(self, wiring) -> None:
        with pytest.raises(UnsafeOperationError):
            wiring["source_service"].create_source({**VALID_SOURCE, "read_only": False})
        assert wiring["repo"].list_sources() == []  # 未持久化不得产生数据

    def test_duplicate_name_is_rejected(self, wiring) -> None:
        wiring["source_service"].create_source(dict(VALID_SOURCE))
        with pytest.raises(ValueError, match="已存在"):
            wiring["source_service"].create_source(dict(VALID_SOURCE))


class TestSourceServiceConnectionTest:
    def test_request_body_test_success(self, wiring) -> None:
        result = wiring["source_service"].test_connection(
            {k: v for k, v in VALID_SOURCE.items() if k not in ("name", "schemas", "read_only")}
        )
        assert result["ok"] is True
        assert result["dialect"] == "mysql"
        assert "request-body-secret" not in str(result)

    def test_request_body_test_failure_returns_reason_without_secret(self, wiring) -> None:
        result = wiring["source_service"].test_connection(
            {
                "dialect": "mysql",
                "host": "down.internal",
                "port": 3306,
                "database": "sales",
                "username": "readonly",
                "password": "request-body-secret",
            }
        )
        assert result["ok"] is False
        assert result["error"]["reason"] == "unreachable"
        assert "request-body-secret" not in str(result)

    def test_persisted_source_test_uses_stored_credential(self, wiring) -> None:
        created = wiring["source_service"].create_source(dict(VALID_SOURCE))
        result = wiring["source_service"].test_persisted_source(created["source_id"])
        assert result["ok"] is True
        assert (
            wiring["created_configs"][-1]["password"].get_secret_value()
            == "request-body-secret"
        )

    def test_persisted_source_test_missing_source(self, wiring) -> None:
        with pytest.raises(ResourceNotFoundError):
            wiring["source_service"].test_persisted_source("nope")


class TestDefaultSource:
    def test_default_source_registered_from_settings_idempotently(self, wiring) -> None:
        first = wiring["source_service"].register_default_source()
        assert first["already_registered"] is False
        assert first["host"] == "default.internal"
        assert first["port"] == 3307
        assert first["allowed_schemas"] == ["sales"]
        second = wiring["source_service"].register_default_source()
        assert second["already_registered"] is True
        assert second["source_id"] == first["source_id"]
        stored = wiring["store"].get(
            wiring["repo"].get_source(first["source_id"])["credential_ref"]
        )
        assert stored == "default-secret"


class TestScanService:
    def make_source(self, wiring, schemas: list[str]) -> str:
        created = wiring["source_service"].create_source(
            {**VALID_SOURCE, "schemas": schemas, "host": "db.internal"}
        )
        return created["source_id"]

    def test_scan_persists_metadata_profiles_and_partial_status(self, wiring) -> None:
        source_id = self.make_source(wiring, ["sales"])
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "partial"  # broken 表失败
        assert job["tables_planned"] == 3
        assert job["tables_scanned"] == 2
        assert job["tables_failed"] == 1
        assert job["errors"][0]["table"] == "broken"
        assert job["errors"][0]["reason"] == "insufficient_privilege"
        tables = wiring["repo"].list_table_metadata(source_id)
        assert [t["table_name"] for t in tables] == ["customers", "orders"]
        profiles = wiring["repo"].list_column_profiles(tables[0]["table_id"])
        by_col = {p["column_name"]: p for p in profiles}
        assert by_col["user_password"]["sample_values"] == ["***", "***"]
        assert by_col["user_password"]["samples_redacted"] is True
        # 值格式检测：通用列中的邮箱/手机号在持久化前已脱敏
        assert by_col["contact"]["sample_values"] == ["z***@example.com", "l***@example.com"]
        assert by_col["contact"]["samples_redacted"] is True
        assert by_col["remark"]["sample_values"] == ["****5678", "***"]
        dumped = str(profiles)
        for raw in ("raw-secret-1", "zhang_san@example.com", "13812345678", "13912345678"):
            assert raw not in dumped  # SQLite 画像持久化不含原始敏感样本
        assert by_col["id"]["metric_basis"] == "exact"  # 2 行 < 100 采样上限
        # 首次扫描只有部分结果：目录可见但明确标记不完整
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["complete"] is False
        assert published["snapshot_id"] == job["snapshot_id"]

    def test_scan_budget_limits_tables(self, wiring) -> None:
        wiring["scan_service"]._max_tables = 2
        source_id = self.make_source(wiring, ["sales"])
        job = wiring["scan_service"].run_scan(source_id)
        # 字母序前两张是 broken（失败）与 customers（成功）：1 成功 1 失败 1 跳过。
        assert job["tables_planned"] == 3
        assert job["tables_scanned"] == 1
        assert job["tables_failed"] == 1
        assert job["tables_skipped"] == 1
        assert job["status"] == "partial"

    def test_scan_budget_with_zero_success_is_failed(self, wiring) -> None:
        wiring["scan_service"]._max_tables = 1
        source_id = self.make_source(wiring, ["sales"])
        job = wiring["scan_service"].run_scan(source_id)
        # 预算内唯一一张表失败：成功表数为 0 且存在失败 → 判定失败，不发布目录。
        assert job["tables_planned"] == 3
        assert job["tables_scanned"] == 0
        assert job["tables_failed"] == 1
        assert job["tables_skipped"] == 2
        assert job["status"] == "failed"
        assert wiring["repo"].get_published_snapshot(source_id) is None

    def test_scan_connection_failure_records_failed_job(self, wiring) -> None:
        created = wiring["source_service"].create_source(
            {**VALID_SOURCE, "host": "down.internal"}
        )
        job = wiring["scan_service"].run_scan(created["source_id"])
        assert job["status"] == "failed"
        assert job["errors"][0]["reason"] == "unreachable"
        assert job["tables_scanned"] == 0

    def test_scan_restricted_to_allowed_schemas(self, wiring) -> None:
        source_id = self.make_source(wiring, ["other"])
        job = wiring["scan_service"].run_scan(source_id)
        assert job["tables_planned"] == 1
        tables = wiring["repo"].list_table_metadata(source_id)
        assert [t["table_name"] for t in tables] == ["legacy"]

    def test_scan_reports_invisible_schema_as_error(self, wiring) -> None:
        source_id = self.make_source(wiring, ["not_visible"])
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "failed"
        assert job["errors"][0]["reason"] == "schema_not_visible"

    def test_rescan_creates_new_snapshot_replacing_catalog(self, wiring) -> None:
        source_id = self.make_source(wiring, ["other"])
        first = wiring["scan_service"].run_scan(source_id)
        second = wiring["scan_service"].run_scan(source_id)
        assert second["snapshot_id"] != first["snapshot_id"]
        tables_second = wiring["repo"].list_table_metadata(source_id)
        assert len(tables_second) == 1
        assert tables_second[0]["snapshot_id"] == second["snapshot_id"]
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == second["snapshot_id"]
        assert published["complete"] is True  # 完整成功快照才是完整目录

    def test_scan_missing_source(self, wiring) -> None:
        with pytest.raises(ResourceNotFoundError):
            wiring["scan_service"].run_scan("nope")

    def test_scan_missing_credential_fails_job(self, wiring) -> None:
        source_id = self.make_source(wiring, ["sales"])
        wiring["store"].delete(wiring["repo"].get_source(source_id)["credential_ref"])
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "failed"
        assert job["errors"][0]["reason"] == "credential_missing"


class TestScanReadModels:
    def test_overview_and_detail_readers(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        job = wiring["scan_service"].run_scan(source_id)
        overview = wiring["scan_service"].source_overview(source_id)
        assert overview["table_count"] == 2
        assert overview["column_count"] == 8  # 2 表 × 4 列
        assert overview["latest_scan"]["status"] == "partial"
        # 目录来源与最近扫描任务区分开：目录指向自己的快照与完整性标记
        assert overview["catalog"]["snapshot_id"] == job["snapshot_id"]
        assert overview["catalog"]["complete"] is False
        assert overview["catalog"]["status"] == "partial"
        jobs = wiring["scan_service"].list_scans(source_id)
        assert len(jobs) == 1
        detail = wiring["scan_service"].get_table(overview["latest_table_ids"][0])
        assert detail["table_name"] in {"orders", "customers"}
        profile = wiring["scan_service"].get_table_profile(detail["table_id"])
        assert {p["column_name"] for p in profile} == {"id", "user_password", "contact", "remark"}
        with pytest.raises(ResourceNotFoundError):
            wiring["scan_service"].get_table("nope")

    def test_get_scan_job(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        job = wiring["scan_service"].run_scan(source_id)
        loaded = wiring["scan_service"].get_scan(job["job_id"])
        assert loaded["job_id"] == job["job_id"]
        with pytest.raises(ResourceNotFoundError):
            wiring["scan_service"].get_scan("nope")

    def test_list_tables_by_explicit_snapshot_keeps_history_visible(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        partial_job = wiring["scan_service"].run_scan(source_id)
        # 修复 broken 后完整成功重扫
        tables_before = wiring["scan_service"].list_tables(source_id)
        # 用请求体建一个只含 other 的新数据源做完整成功扫描不可行（broken 恒失败），
        # 改为直接查看历史 partial 快照：即使目录已被后续成功快照替代，历史仍可查。
        history = wiring["scan_service"].list_tables(
            source_id, snapshot_id=partial_job["snapshot_id"]
        )
        assert [t["table_name"] for t in history] == ["customers", "orders"]
        assert len(tables_before) == len(history)  # 本例目录尚未被替代


class TestScanPublication:
    """默认目录发布规则：完整成功快照优先，失败/部分/预算截断不替换有效目录。"""

    def test_all_failed_rescan_keeps_valid_catalog(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        first = wiring["scan_service"].run_scan(source_id)
        assert first["status"] == "succeeded"
        FakeConnector.fail_metadata_all = True
        second = wiring["scan_service"].run_scan(source_id)
        assert second["status"] == "failed"
        assert second["tables_scanned"] == 0
        tables = wiring["scan_service"].list_tables(source_id)
        assert [t["table_name"] for t in tables] == ["legacy"]  # 目录未被清空
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == first["snapshot_id"]
        assert published["complete"] is True
        # 最近任务是 failed，但目录来源仍是成功快照——两者区分展示
        overview = wiring["scan_service"].source_overview(source_id)
        assert overview["latest_scan"]["status"] == "failed"
        assert overview["catalog"]["snapshot_id"] == first["snapshot_id"]

    def test_partial_rescan_keeps_complete_catalog_but_keeps_results(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        FakeConnector.broken_fails = False
        first = wiring["scan_service"].run_scan(source_id)
        assert first["status"] == "succeeded"  # 3 张表全部成功
        FakeConnector.broken_fails = True
        partial = wiring["scan_service"].run_scan(source_id)
        assert partial["status"] == "partial"  # 1 张失败、2 张成功
        # 默认目录仍是最近完整成功快照，缺失的 broken 不被当作确认删除
        catalog = wiring["scan_service"].list_tables(source_id)
        assert [t["table_name"] for t in catalog] == ["broken", "customers", "orders"]
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == first["snapshot_id"]
        assert published["complete"] is True
        # 本次部分成功的结果保留，可按快照查看与诊断
        partial_tables = wiring["scan_service"].list_tables(
            source_id, snapshot_id=partial["snapshot_id"]
        )
        assert [t["table_name"] for t in partial_tables] == ["customers", "orders"]
        overview = wiring["scan_service"].source_overview(source_id)
        assert overview["latest_scan"]["status"] == "partial"
        assert overview["catalog"]["snapshot_id"] == first["snapshot_id"]

    def test_budget_truncated_rescan_keeps_complete_catalog(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        FakeConnector.broken_fails = False
        first = wiring["scan_service"].run_scan(source_id)
        assert first["status"] == "succeeded"
        wiring["scan_service"]._max_tables = 1
        truncated = wiring["scan_service"].run_scan(source_id)
        assert truncated["status"] == "partial"  # 1 成功 2 跳过
        assert truncated["tables_skipped"] == 2
        # 被跳过的表不得当作确认删除：目录仍是完整快照的 3 张表
        catalog = wiring["scan_service"].list_tables(source_id)
        assert len(catalog) == 3
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == first["snapshot_id"]
        assert published["complete"] is True

    def test_first_scan_partial_publishes_incomplete(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "partial"
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published is not None
        assert published["complete"] is False
        assert len(wiring["scan_service"].list_tables(source_id)) == 2  # 部分结果可见

    def test_confirmed_empty_schema_publishes_empty_catalog(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other", "empty"])
        first = wiring["scan_service"].run_scan(source_id)
        assert first["status"] == "succeeded"
        assert len(wiring["scan_service"].list_tables(source_id)) == 1  # legacy
        FakeConnector.legacy_dropped = True  # 表被删除
        second = wiring["scan_service"].run_scan(source_id)
        assert second["status"] == "succeeded"
        assert wiring["scan_service"].list_tables(source_id) == []  # 空目录发布，旧表消失
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == second["snapshot_id"]
        assert published["complete"] is True


class TestScanFinalization:
    """扫描生命周期异常收尾：任务不得停留 running，失败快照不替换目录。"""

    def test_connector_creation_failure_finalizes_failed(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        wiring["controls"]["factory_error"] = True
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "failed"
        assert job["finished_at"] is not None
        assert job["errors"][0]["reason"] == "internal_error"
        stored = wiring["repo"].get_scan_job(job["job_id"])
        assert stored["status"] == "failed"
        assert wiring["repo"].get_published_snapshot(source_id) is None

    def test_unexpected_metadata_error_finalizes_failed(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        ok = wiring["scan_service"].run_scan(source_id)
        assert ok["status"] == "succeeded"
        FakeConnector.runtime_metadata_error = True
        job = wiring["scan_service"].run_scan(source_id)
        # 未预期异常按表隔离（table_scan_error）；本例预算内唯一表失败 → 整体 failed
        assert job["status"] == "failed"
        assert job["finished_at"] is not None
        assert job["errors"][0]["reason"] == "table_scan_error"
        message = job["errors"][0]["message"]
        assert "RuntimeError" in message  # 异常类型名用于定位
        assert "legacy" in message  # 表名用于定位
        stored = wiring["repo"].get_scan_job(job["job_id"])
        assert stored["status"] == "failed"  # 未停留在 running
        assert wiring["repo"].get_published_snapshot(source_id)["snapshot_id"] == ok["snapshot_id"]

    def test_profiler_failure_is_isolated_per_table(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other", "sales"])

        class FlakyProfiler:
            calls = 0

            def profile_table(self, table, rows, sample_limit, computed_at=None, max_columns=None):
                FlakyProfiler.calls += 1
                if FlakyProfiler.calls == 1:
                    raise ValueError("画像计算崩溃: 数据形状异常")
                from app.profiling.profiler import DataProfiler

                return DataProfiler().profile_table(
                    table, rows, sample_limit, computed_at=computed_at, max_columns=max_columns
                )

        scan_service = ScanService(
            source_repository=wiring["repo"],
            metadata_repository=wiring["repo"],
            credential_store=wiring["store"],
            connector_factory=lambda params: FakeConnector(params),
            profiler=FlakyProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )
        job = scan_service.run_scan(source_id)
        assert job["status"] == "partial"
        profiler_errors = [e for e in job["errors"] if e["reason"] == "table_scan_error"]
        assert len(profiler_errors) == 1  # 只影响第一张表（legacy），其余表不受影响
        assert profiler_errors[0]["table"] == "legacy"
        assert job["tables_scanned"] == 2  # customers + orders
        assert job["tables_failed"] == 2  # legacy（画像崩溃）+ broken（权限）

    def test_persistence_failure_aborts_scan(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        wiring["scan_service"].run_scan(source_id)  # 先有有效目录
        base_repo = wiring["repo"]

        class FailingSaveRepo:
            """表元数据保存第 2 次起失败，模拟元数据库基础设施故障。"""

            def __init__(self):
                self.saves = 0

            def __getattr__(self, name):
                return getattr(base_repo, name)

            def save_table_metadata(self, metadata):
                self.saves += 1
                if self.saves >= 1:
                    raise RuntimeError("metadata db I/O error")
                return base_repo.save_table_metadata(metadata)

        failing_service = ScanService(
            source_repository=base_repo,
            metadata_repository=FailingSaveRepo(),
            credential_store=wiring["store"],
            connector_factory=lambda params: FakeConnector(params),
            profiler=DataProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )
        job = failing_service.run_scan(source_id)
        assert job["status"] == "failed"  # 基础设施故障停止扫描并收尾
        assert job["finished_at"] is not None
        assert job["errors"][0]["reason"] == "internal_error"
        stored = base_repo.get_scan_job(job["job_id"])
        assert stored["status"] == "failed"
        # 有效目录未被替换
        assert wiring["repo"].get_published_snapshot(source_id)["complete"] is True

    def test_close_failure_does_not_block_finalization(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        FakeConnector.close_error = True
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "succeeded"  # close 异常不影响结果与收尾
        assert job["finished_at"] is not None

    def test_credential_store_corruption_fails_job(self, wiring, tmp_path) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        assert wiring["store"].get(
            wiring["repo"].get_source(source_id)["credential_ref"]
        ) == "request-body-secret"
        # 直接破坏 wiring 使用的凭证文件（与 wiring 同一 tmp_path）
        (tmp_path / "creds.json").write_text("{ broken", encoding="utf-8")
        job = wiring["scan_service"].run_scan(source_id)
        assert job["status"] == "failed"
        assert job["errors"][0]["reason"] == "credential_store_error"
        assert job["finished_at"] is not None


# 各阶段异常中嵌入的敏感载荷（虚构），用于验证错误信息不泄漏业务样本。
LEAKY_PAYLOAD = (
    "contact=zhang_san@example.com phone=13812345678 "
    "token=sk-sample-token-99 pwd=raw-sample-pwd-1"
)
SENSITIVE_FRAGMENTS = (
    "zhang_san@example.com",
    "13812345678",
    "sk-sample-token-99",
    "raw-sample-pwd-1",
)


class LeakyMetadataConnector(FakeConnector):
    def get_table_metadata(self, schema_name, table_name):
        raise RuntimeError(f"inspector crashed on row: {LEAKY_PAYLOAD}")


class LeakyProfiler:
    def profile_table(self, table, rows, sample_limit, computed_at=None, max_columns=None):
        raise ValueError(f"cannot hash value: {LEAKY_PAYLOAD}")


class TestErrorSanitization:
    """未预期异常的对外/持久化消息必须是固定安全文案，不得携带业务样本。"""

    def _build_service(self, wiring, connector_cls=FakeConnector, profiler=None):
        return ScanService(
            source_repository=wiring["repo"],
            metadata_repository=wiring["repo"],
            credential_store=wiring["store"],
            connector_factory=lambda params: connector_cls(params),
            profiler=profiler or DataProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )

    def _assert_no_leak(self, text: str) -> None:
        for fragment in SENSITIVE_FRAGMENTS:
            assert fragment not in text

    def test_metadata_exception_payload_never_leaks(self, wiring, caplog) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        ok = wiring["scan_service"].run_scan(source_id)  # 先有有效目录
        assert ok["status"] == "succeeded"
        service = self._build_service(wiring, connector_cls=LeakyMetadataConnector)
        with caplog.at_level("DEBUG"):
            job = service.run_scan(source_id)
        self._assert_no_leak(str(job))
        self._assert_no_leak(str(caplog.text))  # 日志同样不落原始异常
        assert job["status"] == "failed"  # 唯一预算表失败
        assert job["finished_at"] is not None  # 未停留 running
        error = job["errors"][0]
        assert error["reason"] == "table_scan_error"
        assert "RuntimeError" in error["message"] and "legacy" in error["message"]
        # SQLite 中的任务载荷同样干净
        import json as _json

        raw = (wiring["repo"]).get_scan_job(job["job_id"])
        self._assert_no_leak(_json.dumps(raw, ensure_ascii=False))
        # 有效目录未被替换
        published = wiring["repo"].get_published_snapshot(source_id)
        assert published["snapshot_id"] == ok["snapshot_id"]

    def test_profiler_exception_payload_never_leaks(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])
        service = self._build_service(wiring, profiler=LeakyProfiler())
        job = service.run_scan(source_id)
        self._assert_no_leak(str(job))
        assert job["status"] == "failed"  # 全部预算表失败（broken 权限 + 其余画像崩溃）
        reasons = {e["reason"] for e in job["errors"]}
        assert reasons == {"table_scan_error", "insufficient_privilege"}
        profile_errors = [e for e in job["errors"] if e["reason"] == "table_scan_error"]
        assert len(profile_errors) == 2  # orders + customers 在画像阶段隔离失败
        assert all("计算画像" in e["message"] for e in profile_errors)

    def test_persistence_exception_payload_never_leaks(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["other"])
        wiring["scan_service"].run_scan(source_id)
        base_repo = wiring["repo"]

        class LeakySaveRepo:
            def __getattr__(self, name):
                return getattr(base_repo, name)

            def save_table_metadata(self, metadata):
                raise RuntimeError(f"db io failed: {LEAKY_PAYLOAD}")

        service = ScanService(
            source_repository=base_repo,
            metadata_repository=LeakySaveRepo(),
            credential_store=wiring["store"],
            connector_factory=lambda params: FakeConnector(params),
            profiler=DataProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )
        job = service.run_scan(source_id)
        self._assert_no_leak(str(job))
        assert job["status"] == "failed"  # 基础设施故障中止且不伪装成功
        assert job["finished_at"] is not None
        assert job["errors"][0]["reason"] == "internal_error"
        assert "RuntimeError" in job["errors"][0]["message"]

    def test_connector_creation_exception_payload_never_leaks(self, wiring) -> None:
        source_id = TestScanService().make_source(wiring, ["sales"])

        def leaky_factory(params):
            raise RuntimeError(f"connector build failed: {LEAKY_PAYLOAD}")

        service = ScanService(
            source_repository=wiring["repo"],
            metadata_repository=wiring["repo"],
            credential_store=wiring["store"],
            connector_factory=leaky_factory,
            profiler=DataProfiler(),
            sample_rows=100, max_tables=10, max_columns=64,
        )
        job = service.run_scan(source_id)
        self._assert_no_leak(str(job))
        assert job["status"] == "failed"
        assert job["finished_at"] is not None
        assert job["errors"][0]["reason"] == "internal_error"
        assert "创建连接器" in job["errors"][0]["message"]
        assert wiring["repo"].get_published_snapshot(source_id) is None
