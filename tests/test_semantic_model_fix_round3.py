"""M4 净化链路收尾修补回归（两处遗漏，先在旧实现失败；离线虚构数据）。

一、实体 grain 的读取净化（历史模型 statement/missing 泄漏）；
二、新版本保存前统一净化（继承文本与待验证草稿分支的持久化泄漏）。
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.repositories.sqlite_metadata_repository import (
    SQLiteMetadataRepository,
)
from app.api import deps
from app.core.config import Settings
from app.main import app
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import (
    DEFAULT_VALIDATED_RELATIONSHIPS,
    SCHEMA,
    seed_m4_environment,
)

SECRET = "Round3DummySecret"


def _settings() -> Settings:
    return Settings(_env_file=None)


def _new_env(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    seed_m4_environment(repo, source_id,
                        relationship_specs=DEFAULT_VALIDATED_RELATIONSHIPS)
    return repo, source_id, SemanticModelService(repo, _settings())


def _stored_payload(repo, model_id: str, version: int) -> str:
    with repo._engine.connect() as conn:
        row = conn.execute(
            repo._semantic_models.select().where(
                repo._semantic_models.c.model_id == model_id,
                repo._semantic_models.c.version == version,
            )).first()
    return row.payload


def _overwrite_stored_model(repo, payload: dict[str, Any]) -> None:
    from sqlalchemy import update

    with repo._engine.begin() as conn:
        conn.execute(
            update(repo._semantic_models).where(
                repo._semantic_models.c.model_id == payload["model_id"],
                repo._semantic_models.c.version == payload["version"],
            ).values(payload=json.dumps(payload, ensure_ascii=False)))


def _legacy_model_with_grain_secrets(service, repo, source_id) -> dict[str, Any]:
    """构造历史模型：同一虚构口令在主题与实体的 grain.statement/missing 中。"""
    model = service.build_model(source_id)
    payload = json.loads(json.dumps(model))
    entity = next(e for e in payload["entities"]
                  if e["entity_id"] == f"{SCHEMA}.booking")
    entity["grain"]["statement"] = f"一行代表一条 password: {SECRET} 记录"
    entity["grain"]["missing"] = [f"token: {SECRET} 待补充"]
    topic = next(t for t in payload["topics"]
                 if t["central_entity_id"] == entity["entity_id"])
    topic["grain"]["statement"] = f"一行代表一条 password: {SECRET} 记录"
    topic["grain"]["missing"] = [f"token: {SECRET} 待补充"]
    _overwrite_stored_model(repo, payload)
    return payload


class TestEntityGrainReadSanitization:
    def test_entity_grain_sanitized_across_all_exits(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        legacy = _legacy_model_with_grain_secrets(service, repo, source_id)
        model_id, version = legacy["model_id"], legacy["version"]
        before = _stored_payload(repo, model_id, version)

        # get / list / versions / 导出均无原值
        for dumped in (
            json.dumps(service.get_model(model_id, version), ensure_ascii=False),
            json.dumps(service.list_models(source_id), ensure_ascii=False),
            json.dumps(service.list_versions(model_id), ensure_ascii=False),
            service.export_model(model_id, version, export_format="json")["content"],
            service.export_model(model_id, version, export_format="yaml")["content"],
        ):
            assert SECRET not in dumped
        # API 响应（依赖覆盖 + 真实服务）
        overrides = dict(app.dependency_overrides)
        app.dependency_overrides[deps.get_semantic_model_service] = lambda: service
        try:
            with TestClient(app) as client:
                response = client.get(
                    f"/api/v1/semantic-models/{model_id}?version={version}")
                assert response.status_code == 200
                assert SECRET not in response.text
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)
        # 历史存储行逐字节不变
        assert _stored_payload(repo, model_id, version) == before
        # grain 结构化字段保持不变（table/keys/basis 与对象 ID）
        refreshed = service.get_model(model_id, version)
        old_entity = next(e for e in legacy["entities"]
                          if e["entity_id"] == f"{SCHEMA}.booking")
        new_entity = next(e for e in refreshed["entities"]
                          if e["entity_id"] == f"{SCHEMA}.booking")
        assert new_entity["grain"]["table"] == old_entity["grain"]["table"]
        assert new_entity["grain"]["keys"] == old_entity["grain"]["keys"]
        assert new_entity["grain"]["basis"] == old_entity["grain"]["basis"]
        assert new_entity["entity_id"] == old_entity["entity_id"]
        assert [t["topic_id"] for t in refreshed["topics"]] == \
            [t["topic_id"] for t in legacy["topics"]]


class TestPersistTimeSanitization:
    def _legacy_with_secret_name(self, repo, service, source_id) -> dict[str, Any]:
        model = service.build_model(source_id)
        payload = json.loads(json.dumps(model))
        payload["entities"][0]["name"] = f"token: {SECRET}"
        payload["topics"][0]["name"] = f"api_key: {SECRET}"
        _overwrite_stored_model(repo, payload)
        return payload

    def test_revision_from_sensitive_history_persists_sanitized(self, tmp_path):
        """从含敏感文本的历史版本创建新修订：新版本数据库载荷不含原值，
        原版本存储行不变。"""
        repo, source_id, service = _new_env(tmp_path)
        legacy = self._legacy_with_secret_name(repo, service, source_id)
        before = _stored_payload(repo, legacy["model_id"], legacy["version"])
        # API 响应已净化（读取边界）——用作修订输入的名称取自净化后视图
        refreshed = service.get_model(legacy["model_id"], legacy["version"])
        topic = next(t for t in refreshed["topics"] if t["grain"]["keys"])
        revised = service.revise_model(
            legacy["model_id"], legacy["version"], {
                "topic_names": {topic["topic_id"]: "正常新名称"}},
            reviewer="dev", note=f"password: {SECRET}")
        # 新版本存储载荷不含原值（含继承自历史版本的实体名/主题名）
        stored_v2 = _stored_payload(repo, revised["model_id"], revised["version"])
        assert SECRET not in stored_v2, "新版本持久化载荷仍含原始口令"
        # 原版本存储行不变
        assert _stored_payload(repo, legacy["model_id"], legacy["version"]) == before
        # API 与导出不含原值
        assert SECRET not in json.dumps(revised, ensure_ascii=False)
        assert SECRET not in service.export_model(
            revised["model_id"], revised["version"], export_format="yaml")["content"]

    def test_draft_grain_branch_persists_sanitized(self, tmp_path):
        """非唯一 grain 键进入待验证草稿：statement 原样写入新版本的
        路径被堵——存储载荷与响应均无原始口令；草稿语义保持。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        topic = next(t for t in model["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": f"一行代表一条 token: {SECRET} 记录",
                    "keys": ["status_code"], "confirm": True}}},
            reviewer="dev")
        stored = _stored_payload(repo, revised["model_id"], revised["version"])
        assert SECRET not in stored, "待验证草稿分支的 statement 未净化即持久化"
        assert SECRET not in json.dumps(revised, ensure_ascii=False)
        # 草稿语义保持：confirmed=false、basis=unknown、缺失说明、发布阻断
        revised_topic = next(t for t in revised["topics"]
                             if t["topic_id"] == topic["topic_id"])
        assert revised_topic["grain"]["confirmed"] is False
        assert revised_topic["grain"]["basis"] == "unknown"
        assert revised_topic["grain"]["missing"]
        report = service.validate_model(
            revised["model_id"], revised["version"])
        assert not report["valid"]
        with pytest.raises(ValueError, match="发布检查未通过"):
            service.publish_model(revised["model_id"], revised["version"],
                                  reviewer="dev")

    def test_normal_text_structures_and_filters_preserved(self, tmp_path):
        """普通业务文本、结构化引用与过滤值保持原有语义（不被无差别改写）。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        table = metric["entity_id"].split(".", 1)[1]
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "topic_names": {model["topics"][0]["topic_id"]: "预订主题"},
                "metrics": {metric["metric_id"]: {
                    "action": "update",
                    "filters": [{"field": {"schema": SCHEMA, "table": table,
                                           "column": "id"},
                                 "operator": "in", "value": [1, 2, 3]}]}},
            }, reviewer="dev", note="普通说明")
        stored = json.loads(_stored_payload(
            repo, revised["model_id"], revised["version"]))
        # 普通文本原样
        assert any(t["name"] == "预订主题" for t in stored["topics"])
        assert stored["change_note"] == "普通说明"
        # 结构化引用与类型化过滤值不变
        stored_metric = next(m for m in stored["metrics"]
                             if m["metric_id"] == metric["metric_id"])
        assert stored_metric["filters"][0]["value"] == [1, 2, 3]
        assert stored_metric["filters"][0]["field"]["column"] == "id"
        assert stored_metric["measure_field"] is None
        # 确认失效/发布门槛行为不变：未确认 grain 仍阻断发布
        report = service.validate_model(revised["model_id"], revised["version"])
        assert not report["valid"]
