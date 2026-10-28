import json

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.ports.repositories import MetadataRepository, SourceRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteMetadataRepository:
    return SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")


def test_repo_implements_both_ports(repo: SQLiteMetadataRepository) -> None:
    assert isinstance(repo, MetadataRepository)
    assert isinstance(repo, SourceRepository)


class TestSourceRepository:
    def test_create_and_get_roundtrip_without_password(self, repo) -> None:
        source_id = repo.create_source(
            {
                "name": "local-mysql",
                "dialect": "mysql",
                "host": "127.0.0.1",
                "port": 3306,
                "database": "sales",
                "username": "readonly",
                "credential_ref": "cred-1",
                "allowed_schemas": ["sales"],
                "read_only": True,
                "is_default": False,
            }
        )
        stored = repo.get_source(source_id)
        assert stored is not None
        assert stored["name"] == "local-mysql"
        assert stored["credential_ref"] == "cred-1"
        assert stored["allowed_schemas"] == ["sales"]
        assert "password" not in stored
        assert "password" not in json.dumps(stored)

    def test_get_missing_source_returns_none(self, repo) -> None:
        assert repo.get_source("nope") is None

    def test_list_and_find_by_name(self, repo) -> None:
        repo.create_source({"name": "a", "dialect": "mysql", "credential_ref": "c1"})
        repo.create_source({"name": "b", "dialect": "mysql", "credential_ref": "c2"})
        names = [s["name"] for s in repo.list_sources()]
        assert names == ["a", "b"]
        assert repo.find_source_by_name("b")["credential_ref"] == "c2"
        assert repo.find_source_by_name("zz") is None

    def test_duplicate_name_rejected(self, repo) -> None:
        repo.create_source({"name": "a", "dialect": "mysql", "credential_ref": "c1"})
        with pytest.raises(ValueError):
            repo.create_source({"name": "a", "dialect": "mysql", "credential_ref": "c2"})


def table_payload(source_id: str, snapshot_id: str, table: str, **overrides):
    payload = {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "schema_name": "sales",
        "table_name": table,
        "table_comment": None,
        "columns": [
            {"name": "id", "data_type": "bigint", "nullable": False,
             "comment": "主键", "ordinal_position": 1, "default_value": None}
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
        "indexes": [{"name": "PRIMARY", "columns": ["id"], "unique": True}],
        "row_count_estimate": 10,
        "row_count_is_estimate": True,
        "scanned_at": "2026-10-27T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class TestTableMetadata:
    def test_save_returns_id_and_get_roundtrip(self, repo) -> None:
        table_id = repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        stored = repo.get_table_metadata(table_id)
        assert stored["table_name"] == "orders"
        assert stored["columns"][0]["comment"] == "主键"
        assert stored["indexes"][0]["unique"] is True
        assert stored["row_count_is_estimate"] is True
        assert stored["primary_key"] == ["id"]

    def test_same_snapshot_upsert_deduplicates(self, repo) -> None:
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        first = repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        second = repo.save_table_metadata(
            table_payload("src-1", "snap-1", "orders", row_count_estimate=99)
        )
        assert first == second
        rows = repo.list_table_metadata("src-1")
        assert len(rows) == 1
        assert rows[0]["row_count_estimate"] == 99

    def test_list_returns_only_published_snapshot(self, repo) -> None:
        repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        repo.save_table_metadata(table_payload("src-1", "snap-1", "customers"))
        repo.save_table_metadata(table_payload("src-1", "snap-2", "orders"))
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00",
             "finished_at": "2026-10-27T01:01:00+00:00"}
        )
        repo.save_scan_job(
            {"job_id": "job-2", "source_id": "src-1", "snapshot_id": "snap-2",
             "status": "succeeded", "started_at": "2026-10-27T02:00:00+00:00",
             "finished_at": "2026-10-27T02:01:00+00:00"}
        )
        rows = repo.list_table_metadata("src-1")
        assert [r["table_name"] for r in rows] == ["orders"]  # snap-2 中 customers 已不存在
        # 指定历史快照仍可查看（保留历史，不删除旧数据）
        history = repo.list_table_metadata("src-1", snapshot_id="snap-1")
        assert [r["table_name"] for r in history] == ["customers", "orders"]

    def test_published_snapshot_ignores_failed_jobs(self, repo) -> None:
        repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        repo.save_scan_job(
            {"job_id": "job-2", "source_id": "src-1", "snapshot_id": "snap-2",
             "status": "failed", "started_at": "2026-10-27T02:00:00+00:00"}
        )
        published = repo.get_published_snapshot("src-1")
        assert published is not None
        assert published["snapshot_id"] == "snap-1"
        assert published["complete"] is True

    def test_partial_does_not_replace_succeeded_catalog(self, repo) -> None:
        repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        repo.save_table_metadata(table_payload("src-1", "snap-2", "orders"))
        repo.save_scan_job(
            {"job_id": "job-2", "source_id": "src-1", "snapshot_id": "snap-2",
             "status": "partial", "started_at": "2026-10-27T02:00:00+00:00",
             "tables_scanned": 1, "tables_failed": 1}
        )
        published = repo.get_published_snapshot("src-1")
        assert published["snapshot_id"] == "snap-1"  # 默认目录仍是最近完整成功快照
        assert published["complete"] is True
        assert repo.list_table_metadata("src-1")[0]["snapshot_id"] == "snap-1"

    def test_partial_publishes_when_no_succeeded_exists(self, repo) -> None:
        repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "partial", "started_at": "2026-10-27T01:00:00+00:00",
             "tables_scanned": 1, "tables_failed": 1}
        )
        published = repo.get_published_snapshot("src-1")
        assert published is not None
        assert published["snapshot_id"] == "snap-1"
        assert published["complete"] is False  # 首次扫描只有部分结果：可见但明确不完整

    def test_succeeded_empty_schema_publishes_empty_catalog(self, repo) -> None:
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        published = repo.get_published_snapshot("src-1")
        assert published is not None and published["complete"] is True
        assert repo.list_table_metadata("src-1") == []  # 确认空目录可以发布

    def test_no_published_snapshot_without_success(self, repo) -> None:
        assert repo.get_published_snapshot("src-1") is None
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "failed", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        assert repo.get_published_snapshot("src-1") is None
        assert repo.list_table_metadata("src-1") == []


class TestColumnProfiles:
    def test_profiles_saved_and_replaced_per_table(self, repo) -> None:
        table_id = repo.save_table_metadata(table_payload("src-1", "snap-1", "orders"))
        repo.save_column_profiles(
            table_id,
            [{"column_name": "id", "null_ratio": 0.0, "metric_basis": "exact"}],
        )
        assert repo.list_column_profiles(table_id)[0]["column_name"] == "id"
        repo.save_column_profiles(
            table_id,
            [
                {"column_name": "id", "null_ratio": 0.1, "metric_basis": "sample"},
                {"column_name": "amount", "null_ratio": None, "metric_basis": "skipped"},
            ],
        )
        profiles = repo.list_column_profiles(table_id)
        assert [p["column_name"] for p in profiles] == ["id", "amount"]
        assert profiles[0]["null_ratio"] == 0.1


class TestScanJobs:
    def test_job_upsert_and_listing(self, repo) -> None:
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "running", "started_at": "2026-10-27T01:00:00+00:00"}
        )
        repo.save_scan_job(
            {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
             "status": "partial", "started_at": "2026-10-27T01:00:00+00:00",
             "finished_at": "2026-10-27T01:02:00+00:00",
             "tables_planned": 3, "tables_scanned": 2, "tables_failed": 1,
             "errors": [{"schema": "sales", "table": "bad", "reason": "insufficient_privilege",
                          "message": "SELECT command denied"}]}
        )
        job = repo.get_scan_job("job-1")
        assert job["status"] == "partial"
        assert job["tables_failed"] == 1
        assert job["errors"][0]["table"] == "bad"
        repo.save_scan_job(
            {"job_id": "job-0", "source_id": "src-1", "snapshot_id": "snap-0",
             "status": "succeeded", "started_at": "2026-10-27T00:00:00+00:00"}
        )
        assert [j["job_id"] for j in repo.list_scan_jobs("src-1")] == ["job-1", "job-0"]
        assert repo.get_scan_job("missing") is None
