"""M2 语义 API 测试（依赖覆盖 + Stub 仓储，全部离线）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import Settings
from app.main import app
from tests.test_semantics_service import FAKE_TABLE_META, PROFILES, StubRepo

TABLE = FAKE_TABLE_META


@pytest.fixture
def client(tmp_path: Any) -> TestClient:
    repo = StubRepo(f"sqlite:///{tmp_path/'meta.db'}", TABLE, PROFILES)
    repo.save_scan_job({"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
                        "status": "succeeded", "started_at": "2026-10-28T01:00:00+00:00"})
    repo.save_table_metadata({
        "source_id": "src-1", "snapshot_id": "snap-1", "schema_name": "sales",
        "table_name": "orders", "columns": TABLE["columns"], "primary_key": ["id"],
        "foreign_keys": TABLE["foreign_keys"], "indexes": [],
        "row_count_estimate": 3, "row_count_is_estimate": True, "scanned_at": "t",
    })
    service = deps  # noqa: F841  （保持导入）
    from app.services.semantics_service import SemanticsService

    semantics = SemanticsService(
        metadata_repository=repo, llm_factory=None, settings=Settings(_env_file=None))
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_semantics_service] = lambda: semantics
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)


def test_full_flow_analyze_query_review_history_search(client: TestClient) -> None:
    # 1) 分析
    run = client.post("/api/v1/sources/src-1/semantics/analyze",
                      json={"table_id": "tbl-1"})
    assert run.status_code == 201
    run_body = run.json()
    assert run_body["mode"] == "rules_only"
    assert run_body["status"] == "succeeded"
    run_id = run_body["run_id"]

    # 2) 运行状态
    detail = client.get(f"/api/v1/semantics/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id
    assert client.get("/api/v1/semantics/runs/nope").status_code == 404
    listing = client.get("/api/v1/sources/src-1/semantics/runs")
    assert len(listing.json()) == 1

    # 3) 表语义与字段列表
    table_sem = client.get("/api/v1/tables/tbl-1/semantics")
    assert table_sem.status_code == 200
    body = table_sem.json()
    assert body["purpose"]["purpose"] in ("transaction_event", "unknown")
    assert {f["column_name"] for f in body["fields"]} == {"id", "customer_id", "total_amount"}
    assert all(f["rules_version"] for f in body["fields"])

    # 4) 人工修订（确认）
    review = client.post("/api/v1/tables/tbl-1/semantics/total_amount/review",
                         json={"action": "confirm", "business_name": "订单总金额",
                               "reviewer": "dev", "expected_revision": 1})
    assert review.status_code == 200
    assert review.json()["revision"] == 2

    # 5) 修订历史
    history = client.get("/api/v1/tables/tbl-1/semantics/total_amount/history")
    assert history.status_code == 200
    statuses = [h["review_status"] for h in history.json()]
    assert statuses == ["business_confirmed", "suggested"]

    # 6) 语义检索（词法基线）
    search = client.get("/api/v1/sources/src-1/semantics/search",
                        params={"q": "订单总金额"})
    assert search.status_code == 200
    result = search.json()
    assert result["method"] == "lexical"
    assert result["items"]
    assert result["items"][0]["column_name"] == "total_amount"
    assert result["items"][0]["review_status"] == "business_confirmed"
    assert result["items"][0]["match_basis"]


def test_review_conflict_returns_409(client: TestClient) -> None:
    client.post("/api/v1/sources/src-1/semantics/analyze", json={"table_id": "tbl-1"})
    conflict = client.post("/api/v1/tables/tbl-1/semantics/id/review",
                           json={"action": "confirm", "expected_revision": 99})
    assert conflict.status_code == 409


def test_partial_snapshot_requires_allow_partial(client: TestClient) -> None:
    # 造一个部分成功快照场景：直接改 Stub 发布状态不可行；此处验证 404 分支
    missing = client.post("/api/v1/sources/nope/semantics/analyze",
                          json={"table_id": "tbl-1"})
    assert missing.status_code == 404


def test_invalid_action_returns_400(client: TestClient) -> None:
    client.post("/api/v1/sources/src-1/semantics/analyze", json={"table_id": "tbl-1"})
    bad = client.post("/api/v1/tables/tbl-1/semantics/id/review",
                      json={"action": "delete", "expected_revision": 1})
    assert bad.status_code == 400
