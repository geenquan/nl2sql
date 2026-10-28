"""M4 语义模型 API 测试（依赖覆盖 + 真实服务 + 确定性夹具，全部离线）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.api import deps
from app.core.config import Settings
from app.main import app
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import (
    DEFAULT_VALIDATED_RELATIONSHIPS,
    SCHEMA,
    seed_m4_environment,
)


@pytest.fixture
def context(tmp_path: Any):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    seed_m4_environment(repo, source_id,
                        relationship_specs=DEFAULT_VALIDATED_RELATIONSHIPS)
    service = SemanticModelService(repo, Settings(_env_file=None))
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_semantic_model_service] = lambda: service
    with TestClient(app) as client:
        yield {"client": client, "service": service, "source_id": source_id,
               "repo": repo}
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)


class TestSemanticModelApi:
    def test_full_lifecycle(self, context):
        client, source_id = context["client"], context["source_id"]

        # 1) 构建
        response = client.post(f"/api/v1/sources/{source_id}/semantic-models/build",
                               json={})
        assert response.status_code == 201
        model = response.json()
        model_id = model["model_id"]
        assert model["version"] == 1 and model["topics"]

        # 2) 列表 / 详情 / 版本
        listing = client.get(f"/api/v1/sources/{source_id}/semantic-models")
        assert listing.status_code == 200 and len(listing.json()) == 1
        detail = client.get(f"/api/v1/semantic-models/{model_id}")
        assert detail.status_code == 200
        assert detail.json()["availability"]["dependency_status"] == "ok"
        versions = client.get(f"/api/v1/semantic-models/{model_id}/versions")
        assert versions.status_code == 200 and len(versions.json()) == 1

        # 3) 直接发布被拒（grain 未确认）
        validation = client.post(
            f"/api/v1/semantic-models/{model_id}/validate?version=1")
        assert validation.status_code == 200
        assert validation.json()["valid"] is False
        rejected = client.post(
            f"/api/v1/semantic-models/{model_id}/publish?version=1",
            json={"reviewer": "dev"})
        assert rejected.status_code == 400

        # 4) 修订：确认全部 grain
        grains = {
            t["topic_id"]: {
                "statement": f"一行代表一条{t['name']}记录",
                "keys": t["grain"]["keys"] or ["id"], "confirm": True}
            for t in model["topics"] if t["grain"]["keys"]}
        revised = client.post(
            f"/api/v1/semantic-models/{model_id}/revise",
            json={"base_version": 1, "reviewer": "dev",
                  "topic_grains": grains})
        assert revised.status_code == 200
        assert revised.json()["version"] == 2

        # 5) 检查通过 → 发布
        validation2 = client.post(
            f"/api/v1/semantic-models/{model_id}/validate?version=2")
        assert validation2.json()["valid"] is True, validation2.json()
        published = client.post(
            f"/api/v1/semantic-models/{model_id}/publish?version=2",
            json={"reviewer": "dev", "note": "首次发布"})
        assert published.status_code == 200
        current = client.get(
            f"/api/v1/sources/{source_id}/semantic-models/published")
        assert current.status_code == 200
        assert current.json()["version"] == 2

        # 6) 再修订 → 差异 → 回滚
        topic = next(t for t in revised.json()["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        revised2 = client.post(
            f"/api/v1/semantic-models/{model_id}/revise",
            json={"base_version": 2, "reviewer": "dev",
                  "topic_names": {topic["topic_id"]: "预订主题"}})
        assert revised2.status_code == 200
        diff = client.get(
            f"/api/v1/semantic-models/{model_id}/diff?version_a=2&version_b=3")
        assert diff.status_code == 200
        assert diff.json()["topics"]["changed"]
        rollback = client.post(
            f"/api/v1/semantic-models/{model_id}/rollback",
            json={"target_version": 2, "reviewer": "dev", "reason": "回滚演示"})
        assert rollback.status_code == 200
        assert client.get(
            f"/api/v1/sources/{source_id}/semantic-models/published"
        ).json()["version"] == 2

        # 7) 导出 JSON/YAML
        for export_format in ("json", "yaml"):
            export = client.get(
                f"/api/v1/semantic-models/{model_id}/export"
                f"?version=2&format={export_format}")
            assert export.status_code == 200
            assert export.json()["format"] == export_format
            assert export.json()["content"]

        # 8) 审计日志
        log = client.get(
            f"/api/v1/sources/{source_id}/semantic-models/publication-log")
        assert log.status_code == 200
        actions = {entry["action"] for entry in log.json()}
        assert {"publish", "rollback"} <= actions

    def test_revise_conflict_409(self, context):
        client, source_id = context["client"], context["source_id"]
        model = client.post(
            f"/api/v1/sources/{source_id}/semantic-models/build", json={}).json()
        model_id = model["model_id"]
        topic = model["topics"][0]
        body = {"base_version": 1, "reviewer": "dev",
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一条记录",
                    "keys": topic["grain"]["keys"] or ["id"], "confirm": True}}}
        first = client.post(f"/api/v1/semantic-models/{model_id}/revise", json=body)
        assert first.status_code == 200
        stale = client.post(f"/api/v1/semantic-models/{model_id}/revise", json=body)
        assert stale.status_code == 409

    def test_not_found_404_and_invalid_changes_400(self, context):
        client, source_id = context["client"], context["source_id"]
        assert client.get("/api/v1/semantic-models/no-such").status_code == 404
        model = client.post(
            f"/api/v1/sources/{source_id}/semantic-models/build", json={}).json()
        bad = client.post(
            f"/api/v1/semantic-models/{model['model_id']}/revise",
            json={"base_version": 1, "topic_grains": {
                "topic.unknown": {"statement": "x", "confirm": True}}})
        assert bad.status_code == 400
        export_bad = client.get(
            f"/api/v1/semantic-models/{model['model_id']}/export"
            f"?version=1&format=xml")
        assert export_bad.status_code == 422

    def test_partial_snapshot_gate(self, context):
        client, source_id = context["client"], context["source_id"]
        context["repo"].save_scan_job({
            "job_id": "job-p", "source_id": source_id, "snapshot_id": "snap-p",
            "status": "partial", "started_at": "2026-10-29T05:00:00+00:00",
            "finished_at": "2026-10-29T05:00:01+00:00", "tables_planned": 5,
            "tables_scanned": 3, "tables_failed": 1, "tables_skipped": 0,
            "errors": []})
        blocked = client.post(
            f"/api/v1/sources/{source_id}/semantic-models/build",
            json={"snapshot_id": "snap-p"})
        assert blocked.status_code == 400
        allowed = client.post(
            f"/api/v1/sources/{source_id}/semantic-models/build",
            json={"snapshot_id": "snap-p", "allow_partial": True})
        assert allowed.status_code == 201
        assert allowed.json()["dependencies"]["input_complete"] is False
