"""历史画像样本清理脚本测试：不回显样本、保留统计、幂等。"""

import json
import subprocess
import sys
from pathlib import Path

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "clear_profile_samples.py"


def seed_profiles(tmp_path: Path) -> tuple[SQLiteMetadataRepository, str, str]:
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    repo.save_scan_job(
        {"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
         "status": "succeeded", "started_at": "2026-10-27T01:00:00+00:00"}
    )
    table_id = repo.save_table_metadata(
        {"source_id": "src-1", "snapshot_id": "snap-1", "schema_name": "s",
         "table_name": "t", "columns": [], "primary_key": [], "foreign_keys": [],
         "indexes": [], "row_count_estimate": 1, "row_count_is_estimate": True,
         "scanned_at": "2026-10-27T01:00:00+00:00"}
    )
    repo.save_column_profiles(
        table_id,
        [
            {"column_name": "email_col", "null_ratio": 0.1, "distinct_count_estimate": 9,
             "sample_values": ["someone@example.com"], "samples_redacted": False,
             "metric_basis": "sample"},
            {"column_name": "cnt", "null_ratio": 0.0, "distinct_count_estimate": 3,
             "sample_values": [], "samples_redacted": False, "metric_basis": "exact"},
        ],
    )
    return repo, table_id, f"sqlite:///{tmp_path/'meta.db'}"


def run_script(db_url: str, *extra: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", db_url, *extra],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_dry_run_reports_without_modifying(tmp_path: Path) -> None:
    repo, _table_id, db_url = seed_profiles(tmp_path)
    output = run_script(db_url)  # 默认预览
    assert "含样本值：1" in output
    assert "预览" in output
    profiles = {p["column_name"]: p for p in repo.list_column_profiles(
        repo.list_table_metadata("src-1")[0]["table_id"]
    )}
    assert profiles["email_col"]["sample_values"] == ["someone@example.com"]  # 未被修改


def test_apply_clears_samples_keeps_stats_and_never_prints_them(tmp_path: Path) -> None:
    repo, _table_id, db_url = seed_profiles(tmp_path)
    output = run_script(db_url, "--apply")
    assert "已清空样本值" in output
    assert "someone@example.com" not in output  # 绝不回显样本内容
    table_id = repo.list_table_metadata("src-1")[0]["table_id"]
    profiles = {p["column_name"]: p for p in repo.list_column_profiles(table_id)}
    cleared = profiles["email_col"]
    assert cleared["sample_values"] == []
    assert cleared["samples_redacted"] is True
    assert cleared["sample_values_cleared"] is True
    assert cleared["null_ratio"] == 0.1  # 统计指标保留
    assert cleared["distinct_count_estimate"] == 9
    untouched = profiles["cnt"]
    assert untouched.get("sample_values_cleared") is not True  # 无样本的画像不受影响


def test_apply_is_idempotent(tmp_path: Path) -> None:
    _repo, _table_id, db_url = seed_profiles(tmp_path)
    run_script(db_url, "--apply")
    second = run_script(db_url, "--apply")
    assert "含样本值：0" in second  # 第二次已无样本可清


def test_raw_payload_is_valid_json_and_flags_set(tmp_path: Path) -> None:
    import sqlite3

    _repo, _table_id, db_url = seed_profiles(tmp_path)
    run_script(db_url, "--apply")
    db = sqlite3.connect(tmp_path / "meta.db")
    payloads = [json.loads(row[0]) for row in db.execute("SELECT payload FROM column_profiles")]
    cleared = [p for p in payloads if p.get("sample_values_cleared")]
    assert len(cleared) == 1
    assert cleared[0]["cleared_reason"] == "masking-rules-upgraded-reprofile-required"
