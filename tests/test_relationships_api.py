"""M3 关系 API 测试（依赖覆盖 + 真实服务 + 合成数据，全部离线）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.api import deps
from app.main import app
from app.services.relationship_service import RelationshipService
from tests.relationship_fixtures import (
    SCHEMA,
    FakeM3Connector,
    FakeRelationshipProbe,
    seed_catalog,
    seed_profiles,
    seed_semantics,
    synthetic_catalog,
)
from tests.test_relationships_service import FakeCredentialStore, _settings


@pytest.fixture
def context(tmp_path: Any):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    catalog = synthetic_catalog(source_id=source_id)
    seed_catalog(repo, catalog, source_id=source_id)
    seed_profiles(repo, catalog)
    seed_semantics(repo, catalog)
    settings = _settings(relationship_validation_max_candidates=15)
    service = RelationshipService(
        metadata_repository=repo, source_repository=repo,
        credential_store=FakeCredentialStore({"cred-1": "secret"}),
        connector_factory=lambda params: FakeM3Connector(catalog),
        probe_factory=lambda params: FakeRelationshipProbe(),
        settings=settings,
    )
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_relationship_service] = lambda: service
    with TestClient(app) as test_client:
        yield {"client": test_client, "service": service, "source_id": source_id,
               "repo": repo}
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)


class TestDiscoveryEndpoints:
    def test_discover_and_list(self, context):
        client, source_id = context["client"], context["source_id"]
        response = client.post(f"/api/v1/sources/{source_id}/relationships/discover",
                               json={})
        assert response.status_code == 201
        run = response.json()
        assert run["run_type"] == "discovery" and run["status"] == "succeeded"
        assert run["candidates_new"] >= 9

        listing = client.get(f"/api/v1/sources/{source_id}/relationships")
        assert listing.status_code == 200
        items = listing.json()
        assert len(items) >= 9
        assert all(item["status"] == "candidate" for item in items)

    def test_discover_partial_requires_flag(self, context):
        client, source_id = context["client"], context["source_id"]
        response = client.post(
            f"/api/v1/sources/{source_id}/relationships/discover",
            json={"snapshot_id": "no-such-snapshot"})
        assert response.status_code == 404

    def test_runs_listing(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        runs = client.get(
            f"/api/v1/sources/{source_id}/relationships/runs?run_type=discovery")
        assert runs.status_code == 200
        assert len(runs.json()) == 1


class TestValidationEndpoints:
    def test_validate_batch_and_detail(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        response = client.post(
            f"/api/v1/sources/{source_id}/relationships/validate", json={"limit": 12})
        assert response.status_code == 201
        run = response.json()
        assert run["candidates_validated"] >= 8

        listing = client.get(
            f"/api/v1/sources/{source_id}/relationships?status=validated").json()
        key = next(item["relationship_key"] for item in listing
                   if item["source_columns"] == ["guest_id"])
        detail = client.get(f"/api/v1/relationships/{key}").json()
        assert detail["status"] == "validated"
        assert detail["evidence"]["coverage"]["forward"]["ratio"] == 1.0
        assert detail["validation"]["scope"]["cardinality"] == "many_to_one"

    def test_single_validate_endpoint(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        item = client.get(f"/api/v1/sources/{source_id}/relationships").json()[0]
        response = client.post(
            f"/api/v1/relationships/{item['relationship_key']}/validate"
            f"?source_id={source_id}", json={})
        assert response.status_code == 201
        assert response.json()["run_type"] == "validation"


class TestReviewEndpoints:
    def test_review_conflict_409(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        item = client.get(f"/api/v1/sources/{source_id}/relationships").json()[0]
        response = client.post(
            f"/api/v1/relationships/{item['relationship_key']}/review",
            json={"action": "confirm", "expected_revision": 99})
        assert response.status_code == 409

    def test_review_confirm_and_history(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        item = client.get(f"/api/v1/sources/{source_id}/relationships").json()[0]
        key = item["relationship_key"]
        response = client.post(
            f"/api/v1/relationships/{key}/review",
            json={"action": "confirm", "expected_revision": item["revision"],
                  "reviewer": "alice", "note": "业务确认"})
        assert response.status_code == 200
        assert response.json()["revision"] == 2
        history = client.get(f"/api/v1/relationships/{key}/history").json()
        assert [h["revision"] for h in history] == [2, 1]
        assert history[0]["inference_source"] == "human"
        assert history[0]["reviewed_by"] == "alice"

    def test_invalid_action_400(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        item = client.get(f"/api/v1/sources/{source_id}/relationships").json()[0]
        response = client.post(
            f"/api/v1/relationships/{item['relationship_key']}/review",
            json={"action": "unknown", "expected_revision": 1})
        assert response.status_code == 400


class TestStaleEndpoints:
    def test_stale_revalidate_flow(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        client.post(f"/api/v1/sources/{source_id}/relationships/validate",
                    json={"limit": 12})
        listing = client.get(
            f"/api/v1/sources/{source_id}/relationships?status=validated").json()
        item = next(x for x in listing if x["source_columns"] == ["guest_id"])
        key = item["relationship_key"]

        stale = client.post(f"/api/v1/relationships/{key}/stale",
                            json={"reason": "演示过期", "expected_revision":
                                  item["revision"]})
        assert stale.status_code == 200
        assert client.get(f"/api/v1/relationships/{key}").json()["status"] == "stale"

        revalidated = client.post(
            f"/api/v1/relationships/{key}/revalidate?source_id={source_id}", json={})
        assert revalidated.status_code == 201
        assert client.get(f"/api/v1/relationships/{key}").json()["status"] == "validated"


class TestGraphEndpoints:
    def test_graph_and_paths(self, context):
        client, source_id = context["client"], context["source_id"]
        client.post(f"/api/v1/sources/{source_id}/relationships/discover", json={})
        client.post(f"/api/v1/sources/{source_id}/relationships/validate",
                    json={"limit": 12})
        graph = client.get(f"/api/v1/sources/{source_id}/relationships/graph")
        assert graph.status_code == 200
        body = graph.json()
        assert len(body["nodes"]) >= 8
        assert len(body["edges"]) >= 9

        mermaid = client.get(
            f"/api/v1/sources/{source_id}/relationships/graph?mermaid=true").json()
        assert mermaid["mermaid"].startswith("graph LR")

        tables = context["repo"].list_table_metadata(source_id)
        by_name = {t["table_name"]: t["table_id"] for t in tables}
        paths = client.get(
            f"/api/v1/sources/{source_id}/relationships/paths"
            f"?from_table_id={by_name['booking_item']}&to_table_id={by_name['tenant']}"
            f"&max_hops=3")
        assert paths.status_code == 200
        result = paths.json()
        assert result["path_count"] >= 1
        first_path_hops = result["paths"][0][:-1]
        assert first_path_hops[0]["from_table_id"] == by_name["booking_item"]
        assert first_path_hops[-1]["to_table_id"] == by_name["tenant"]

    def test_missing_run_404(self, context):
        response = context["client"].get("/api/v1/relationships/runs/no-such")
        assert response.status_code == 404
