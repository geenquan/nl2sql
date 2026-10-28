"""M4 语义模型服务测试：构建/修订/检查/发布/回滚/差异/导出/依赖降级。

全部离线：临时 SQLite + 确定性夹具（不连真实库/模型）。
"""

import json
import threading
from typing import Any

import pytest
import yaml

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError, RevisionConflictError
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import (
    DEFAULT_VALIDATED_RELATIONSHIPS,
    SCHEMA,
    m4_catalog,
    seed_m4_environment,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture
def env(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    seed_m4_environment(repo, source_id,
                        relationship_specs=DEFAULT_VALIDATED_RELATIONSHIPS)
    service = SemanticModelService(repo, _settings())
    return {"repo": repo, "source_id": source_id, "service": service}


def _booking_topic(model: dict) -> dict:
    return next(t for t in model["topics"]
                if t["central_entity_id"] == f"{SCHEMA}.booking")


class TestBuild:
    def test_build_creates_draft_model(self, env):
        model = env["service"].build_model(env["source_id"])
        assert model["version"] == 1 and model["status"] == "draft"
        entity_ids = {e["entity_id"] for e in model["entities"]}
        assert f"{SCHEMA}.booking" in entity_ids
        assert f"{SCHEMA}.booking_item" in entity_ids  # 桥接/明细
        assert "archive.guest" in entity_ids  # 跨 Schema 同名表
        # 主题来自 M2 用途（主数据/事件/明细）
        central_ids = {t["central_entity_id"] for t in model["topics"]}
        assert f"{SCHEMA}.booking" in central_ids
        assert f"{SCHEMA}.tenant" in central_ids
        # 候选指标全部 suggested（未确认口径不冒充）
        assert model["metrics"]
        assert all(m["status"] == "suggested" for m in model["metrics"])
        # 依赖记录 M3 修订
        assert model["dependencies"]["relationship_revisions"]

    def test_attributes_carry_m2_and_unknown(self, env):
        model = env["service"].build_model(env["source_id"])
        guest = next(e for e in model["entities"]
                     if e["entity_id"] == f"{SCHEMA}.guest")
        by_column = {a["field"]["column"]: a for a in guest["attributes"]}
        assert by_column["tenant_id"]["semantic_category"] == \
            "foreign_identifier_candidate"
        # 无有效信号的语义按 unknown 处理（夹具给 phone 生成了 unknown 类别）
        assert by_column["phone"]["semantic_category"] == "unknown"
        assert by_column["phone"]["evidence"][0]["type"] == "m2_semantics"

    def test_composite_grain_keys_ordered(self, env):
        model = env["service"].build_model(env["source_id"])
        item = next(e for e in model["entities"]
                    if e["entity_id"] == f"{SCHEMA}.booking_item")
        assert item["grain"]["keys"] == ["booking_id", "item_id"]  # 有序组合键
        assert item["grain"]["basis"] == "primary_key"
        assert not item["grain"]["confirmed"]

    def test_unknown_grain_table_flagged(self, env):
        model = env["service"].build_model(env["source_id"])
        notes = next(e for e in model["entities"]
                     if e["entity_id"] == f"{SCHEMA}.no_pk_notes")
        assert notes["grain"]["basis"] == "unknown"
        assert any("样本唯一率" in m for m in notes["grain"]["missing"])

    def test_cross_schema_same_name_not_merged(self, env):
        model = env["service"].build_model(env["source_id"])
        entities = {e["entity_id"]: e for e in model["entities"]}
        assert f"{SCHEMA}.guest" in entities and "archive.guest" in entities
        # 属性不串用：各自列集合独立
        app_columns = {a["field"]["column"]
                       for a in entities[f"{SCHEMA}.guest"]["attributes"]}
        archive_columns = {a["field"]["column"]
                           for a in entities["archive.guest"]["attributes"]}
        assert app_columns != archive_columns

    def test_partial_snapshot_requires_flag(self, env, tmp_path):
        env["repo"].save_scan_job({
            "job_id": "job-partial-m4", "source_id": env["source_id"],
            "snapshot_id": "snap-partial-m4", "status": "partial",
            "started_at": "2026-10-29T02:00:00+00:00",
            "finished_at": "2026-10-29T02:00:01+00:00",
            "tables_planned": 12, "tables_scanned": 5, "tables_failed": 1,
            "tables_skipped": 0, "errors": [],
        })
        with pytest.raises(ValueError, match="allow_partial"):
            env["service"].build_model(
                env["source_id"], snapshot_id="snap-partial-m4")
        model = env["service"].build_model(
            env["source_id"], snapshot_id="snap-partial-m4", allow_partial=True)
        assert model["dependencies"]["input_complete"] is False
        assert model["dependencies"]["partial_missing"]

    def test_snapshot_of_other_source_rejected(self, env, tmp_path):
        other_id = env["repo"].create_source({
            "name": "other", "dialect": "mysql", "host": "h", "port": 3306,
            "database": "other", "username": "u", "credential_ref": "c2",
        })
        env["repo"].save_scan_job({
            "job_id": f"job-{other_id}", "source_id": other_id,
            "snapshot_id": "snap-other", "status": "succeeded",
            "started_at": "2026-10-29T03:00:00+00:00", "finished_at": None,
        })
        with pytest.raises(ResourceNotFoundError):
            env["service"].build_model(env["source_id"], snapshot_id="snap-other")

    def test_empty_catalog_builds_empty_model(self, tmp_path):
        repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'e.db'}")
        source_id = repo.create_source({
            "name": "empty", "dialect": "mysql", "host": "h", "port": 1,
            "database": "d", "username": "u", "credential_ref": "c"})
        repo.save_scan_job({
            "job_id": "job-e", "source_id": source_id, "snapshot_id": "snap-e",
            "status": "succeeded", "started_at": "2026-10-29T00:00:00+00:00",
            "finished_at": None, "tables_planned": 0, "tables_scanned": 0,
            "tables_failed": 0, "tables_skipped": 0, "errors": []})
        service = SemanticModelService(repo, _settings())
        model = service.build_model(source_id)
        assert model["entities"] == [] and model["topics"] == []

    def test_rejected_semantics_not_positive(self, env):
        # 把 guest.tenant_id 语义改为 rejected 后重建
        history = env["repo"].get_field_semantics_history("snap-m4-guest", "tenant_id")
        latest = history[0]
        env["repo"].save_field_semantics({
            **latest, "review_status": "rejected", "category": "status"},
            expected_revision=latest["revision"])
        model = env["service"].build_model(env["source_id"])
        guest = next(e for e in model["entities"]
                     if e["entity_id"] == f"{SCHEMA}.guest")
        attribute = next(a for a in guest["attributes"]
                         if a["field"]["column"] == "tenant_id")
        assert attribute["semantic_category"] == "unknown"  # rejected 不作正向
        assert attribute["m2_review_status"] == "rejected"


class TestRelationshipsAndPaths:
    def test_only_validated_relationships_used(self, env):
        seed_extra = [
            {"src": "booking", "src_cols": ["status_code"], "tgt": "guest",
             "tgt_cols": ["status_code"], "cardinality": "unknown",
             "status": "candidate"},
            {"src": "booking", "src_cols": ["tenant_seq"], "tgt": "tenant",
             "tgt_cols": ["id"], "cardinality": "many_to_one",
             "status": "needs_review"},
        ]
        from tests.semantic_model_fixtures import seed_relationships
        seed_relationships(env["repo"], env["source_id"], "snap-m4",
                           m4_catalog(env["source_id"]), seed_extra)
        model = env["service"].build_model(env["source_id"])
        keys = {r["relationship_key"] for r in model["relationships"]}
        # 候选/待审关系不进入模型
        assert all("status_code" not in k for k in keys)
        used = {r["source"]["columns"][0] for r in model["relationships"]}
        assert "tenant_seq" not in used

    def test_composite_mapping_order_preserved(self, env):
        from tests.semantic_model_fixtures import seed_relationships
        report_fact = {
            "table_id": "snap-m4-report_fact", "source_id": env["source_id"],
            "snapshot_id": "snap-m4", "schema_name": SCHEMA,
            "table_name": "report_fact", "table_comment": None,
            "columns": [{"name": "b_key", "data_type": "int", "nullable": False},
                        {"name": "i_key", "data_type": "int", "nullable": False}],
            "primary_key": ["b_key", "i_key"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }
        env["repo"].save_table_metadata(report_fact)
        catalog = m4_catalog(env["source_id"]) + [report_fact]
        seed_relationships(env["repo"], env["source_id"], "snap-m4", catalog, [{
            "src": "booking_item", "src_cols": ["booking_id", "item_id"],
            "tgt": "report_fact", "tgt_cols": ["b_key", "i_key"],
            "cardinality": "many_to_one"}])
        model = env["service"].build_model(env["source_id"])
        rel = next(r for r in model["relationships"]
                   if r["source"]["table"] == "booking_item"
                   and len(r["source"]["columns"]) == 2)
        assert rel["source"]["columns"] == ["booking_id", "item_id"]
        assert rel["target"]["columns"] == ["b_key", "i_key"]  # 有序映射

    def test_single_path_available_and_ambiguous_path_flagged(self, env):
        model = env["service"].build_model(env["source_id"])
        booking_paths = [p for p in model["paths"]
                         if p["topic_id"] == f"topic.{SCHEMA}.booking"]
        by_target = {}
        for path in booking_paths:
            by_target.setdefault(path["to"]["table"], []).append(path)
        # 单路径：booking→guest（many_to_one，无风险）→ available
        assert any(p["status"] == "available"
                   for p in by_target.get("guest", []))
        # 竞争路径：booking→tenant 有两条（经 guest 与经 room）→ 歧义待审
        tenant_paths = by_target.get("tenant", [])
        assert len(tenant_paths) >= 2
        assert all(p["status"] == "needs_review" for p in tenant_paths)
        assert all(any("多条路径" in r for r in p["status_reasons"])
                   for p in tenant_paths)

    def test_many_to_many_path_blocked(self, env):
        from tests.semantic_model_fixtures import seed_relationships
        seed_relationships(env["repo"], env["source_id"], "snap-m4",
                           m4_catalog(env["source_id"]), [{
                               "src": "booking", "src_cols": ["id"],
                               "tgt": "service_item", "tgt_cols": ["id"],
                               "cardinality": "many_to_many",
                               "status": "validated"}])
        model = env["service"].build_model(env["source_id"])
        blocked = [p for p in model["paths"]
                   if p["to"]["table"] == "service_item"]
        assert blocked and all(p["status"] == "blocked" for p in blocked)
        assert any(r["type"] == "many_to_many" for p in blocked for r in p["risks"])

    def test_budget_truncated_paths_reported(self, env):
        service = SemanticModelService(env["repo"], _settings(
            semantic_model_max_paths_per_pair=1))
        model = service.build_model(env["source_id"])
        tenant_paths = [p for p in model["paths"]
                        if p["to"]["table"] == "tenant"]
        assert any(p["budget_truncated"] for p in tenant_paths)
        assert any(any("预算截断" in r for r in p["status_reasons"])
                   for p in tenant_paths if p["budget_truncated"])


class TestReviseAndConfirm:
    def _built(self, env) -> dict:
        model = env["service"].build_model(env["source_id"])
        return model

    def test_grain_confirmation_and_metric_confirmation(self, env):
        model = self._built(env)
        topic = _booking_topic(model)
        amount_metric = next(
            (m for m in model["metrics"]
             if m["aggregation"] == "sum" and m["entity_id"] == topic[
                 "central_entity_id"]), None)
        if amount_metric is None:  # 夹具无金额字段时退化为 count_rows
            amount_metric = next(
                m for m in model["metrics"]
                if m["aggregation"] == "count_rows"
                and m["entity_id"] == topic["central_entity_id"])
        revised = env["service"].revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一次预订",
                    "keys": topic["grain"]["keys"], "confirm": True}},
            }, reviewer="dev-a", note="确认粒度")
        new_topic = next(t for t in revised["topics"]
                         if t["topic_id"] == topic["topic_id"])
        assert new_topic["grain"]["confirmed"] is True
        assert new_topic["grain"]["statement"] == "一行代表一次预订"
        assert new_topic["grain"]["confirmed_by"] == "dev-a"
        assert revised["based_on_version"] == model["version"]
        # 原版本不可变：v1 grain 仍未确认
        original = env["service"].get_model(model["model_id"], model["version"])
        assert _booking_topic(original)["grain"]["confirmed"] is False

    def test_metric_confirm_requires_unit_and_time_basis(self, env):
        model = self._built(env)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "sum")
        with pytest.raises(ValueError, match="单位"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {"action": "confirm"}}})
        with pytest.raises(ValueError, match="时间口径"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {
                    "action": "confirm", "unit": "元"}}})

    def test_invalid_field_aggregation_and_operators_rejected(self, env):
        model = self._built(env)
        metric = next(m for m in model["metrics"] if m["aggregation"] == "count_rows")
        with pytest.raises(ValueError, match="不存在的字段"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {
                    "action": "update",
                    "time_field": {"schema": SCHEMA, "table": "booking",
                                   "column": "no_such_col"}}}})
        with pytest.raises(ValueError, match="非法聚合函数"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {
                    "action": "update", "aggregation": "SUM(*)--"}}})
        with pytest.raises(ValueError, match="非法过滤操作符"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {
                    "action": "update",
                    "filters": [{"field": {"schema": SCHEMA, "table": "booking",
                                           "column": "id"},
                                 "operator": "LIKE", "value": "%x%"}]}}})
        # 数值字段过滤值类型核验（SQL 字符串不作为表达式接收）
        with pytest.raises(ValueError, match="数字"):
            env["service"].revise_model(
                model["model_id"], model["version"],
                {"metrics": {metric["metric_id"]: {
                    "action": "update",
                    "filters": [{"field": {"schema": SCHEMA, "table": "booking",
                                           "column": "id"},
                                 "operator": "eq", "value": "1 OR 1=1"}]}}})

    def test_selected_path_disambiguation(self, env):
        model = self._built(env)
        topic = _booking_topic(model)
        candidates = [p for p in model["paths"]
                      if p["topic_id"] == topic["topic_id"]
                      and p["to"]["table"] == "tenant"]
        revised = env["service"].revise_model(
            model["model_id"], model["version"],
            {"selected_paths": {topic["topic_id"]: candidates[0]["path_id"]}})
        chosen = next(p for p in revised["paths"]
                      if p["path_id"] == candidates[0]["path_id"])
        assert chosen["status"] == "available"
        assert all("多条路径" not in r for r in chosen["status_reasons"])
        other = next(p for p in revised["paths"]
                     if p["path_id"] == candidates[1]["path_id"])
        assert other["status"] == "needs_review"

    def test_concurrent_same_base_revision_one_wins(self, env):
        model = self._built(env)
        topic = _booking_topic(model)
        changes = {"topic_names": {topic["topic_id"]: "预订主题"}}
        barrier = threading.Barrier(2)
        results: list[Any] = []

        def revise():
            barrier.wait()
            try:
                results.append(env["service"].revise_model(
                    model["model_id"], model["version"], changes, reviewer="t"))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=revise) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert sum(1 for r in results if isinstance(r, dict)) == 1
        assert sum(1 for r in results
                   if isinstance(r, RevisionConflictError)) == 1

    def test_rebuild_does_not_overwrite_human_versions(self, env):
        model = self._built(env)
        topic = _booking_topic(model)
        revised = env["service"].revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一次预订", "confirm": True,
                    "keys": topic["grain"]["keys"]}}})
        rebuilt = env["service"].build_model(env["source_id"])  # 重新构建=新草稿
        assert rebuilt["version"] == revised["version"] + 1
        assert rebuilt["based_on_version"] is None  # 全新构建，不基于人工版本
        # 人工版本内容保留
        kept = env["service"].get_model(model["model_id"], revised["version"])
        assert _booking_topic(kept)["grain"]["confirmed"] is True
        # 重构建草稿回到未确认
        assert _booking_topic(rebuilt)["grain"]["confirmed"] is False


class TestPublishRollbackDiffExport:
    def _confirmed_model(self, env) -> dict:
        model = env["service"].build_model(env["source_id"])
        return env["service"].revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {t["topic_id"]: {
                    "statement": f"一行代表一条{t['name']}记录",
                    "keys": t["grain"]["keys"] or ["id"], "confirm": True}
                    for t in model["topics"]},
            }, reviewer="dev")

    def test_publish_requires_confirmed_grain_and_valid_dependencies(self, env):
        model = env["service"].build_model(env["source_id"])
        report = env["service"].validate_model(model["model_id"], model["version"])
        assert not report["valid"]
        assert any("grain" in i for i in report["issues"])
        with pytest.raises(ValueError, match="发布检查未通过"):
            env["service"].publish_model(
                model["model_id"], model["version"], reviewer="dev")

    def test_full_publish_rollback_flow(self, env):
        confirmed = self._confirmed_model(env)
        report = env["service"].validate_model(
            confirmed["model_id"], confirmed["version"])
        assert report["valid"], report["issues"]
        published = env["service"].publish_model(
            confirmed["model_id"], confirmed["version"],
            reviewer="dev", note="首次发布")
        assert published["published"] is True
        assert env["service"].get_published(env["source_id"])["version"] == \
            confirmed["version"]
        # 新版本（改主题名）→ 差异 → 发布 → 回滚
        revised = env["service"].revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_names": {_booking_topic(confirmed)["topic_id"]: "预订主题"}},
            reviewer="dev")
        diff = env["service"].diff_models(
            confirmed["model_id"], confirmed["version"], revised["version"])
        assert diff["topics"]["changed"]  # 主题名变更被检出
        env["service"].publish_model(
            revised["model_id"], revised["version"], reviewer="dev")
        rollback = env["service"].rollback_model(
            confirmed["model_id"], confirmed["version"],
            reviewer="dev", reason="回滚演示")
        assert rollback["rolled_back"] is True
        assert env["service"].get_published(env["source_id"])["version"] == \
            confirmed["version"]
        log = env["service"].publication_log(env["source_id"])
        actions = [entry["action"] for entry in log]
        assert "publish" in actions and "rollback" in actions

    def test_publish_pointer_concurrent_conflict(self, env):
        confirmed = self._confirmed_model(env)
        env["service"].publish_model(
            confirmed["model_id"], confirmed["version"], reviewer="dev-a",
            expected_pointer=None)
        revised = env["service"].revise_model(
            confirmed["model_id"], confirmed["version"],
            {"topic_names": {_booking_topic(confirmed)["topic_id"]: "X"}})
        # 期望指针与实际不符 → 409
        with pytest.raises(RevisionConflictError):
            env["service"].publish_model(
                revised["model_id"], revised["version"], reviewer="dev-b",
                expected_pointer=(confirmed["model_id"], 999))

    def test_diff_detects_metric_changes(self, env):
        confirmed = self._confirmed_model(env)
        metric = next(
            (m for m in confirmed["metrics"] if m["aggregation"] == "sum"),
            next(m for m in confirmed["metrics"]
                 if m["aggregation"] == "count_rows"))
        revised = env["service"].revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "unit": "件", "time_basis": "自然日",
                    "skip_status_filter": True}}},
            reviewer="dev")
        diff = env["service"].diff_models(
            confirmed["model_id"], confirmed["version"], revised["version"])
        assert metric["metric_id"] in diff["metrics"]["changed"]

    def test_export_json_yaml_consistent_and_safe(self, env):
        confirmed = self._confirmed_model(env)
        env["service"].publish_model(
            confirmed["model_id"], confirmed["version"], reviewer="dev")
        json_export = env["service"].export_model(
            confirmed["model_id"], confirmed["version"], export_format="json")
        yaml_export = env["service"].export_model(
            confirmed["model_id"], confirmed["version"], export_format="yaml")
        json_payload = json.loads(json_export["content"])
        yaml_payload = yaml.safe_load(yaml_export["content"])
        assert json_payload == yaml_payload  # 同一份模型语义
        assert json_payload["schema_version"] == "semantic-model-v1"
        assert json_payload["formal_metric_ids"] == []
        content = json_export["content"] + yaml_export["content"]
        assert "password" not in content.lower()
        assert "sample_values" not in content
        assert "credential" not in content.lower()

    def test_build_failure_leaves_pointer_and_models_unchanged(self, env):
        confirmed = self._confirmed_model(env)
        env["service"].publish_model(
            confirmed["model_id"], confirmed["version"], reviewer="dev")
        versions_before = {
            (m["model_id"], m["version"])
            for m in env["repo"].list_models(env["source_id"])}
        real = env["repo"]
        from app.core.exceptions import CredentialStoreError

        class BrokenSave(type(real)):
            def save_model(self, model):
                raise CredentialStoreError("模拟持久化故障")

        broken = BrokenSave.__new__(BrokenSave)
        broken.__dict__.update(real.__dict__)
        service = SemanticModelService(broken, _settings())
        with pytest.raises(CredentialStoreError):
            service.build_model(env["source_id"])
        # 指针与既有版本不变（失败不产生半个模型）
        assert env["service"].get_published(env["source_id"])["version"] == \
            confirmed["version"]
        assert {
            (m["model_id"], m["version"])
            for m in env["repo"].list_models(env["source_id"])
        } == versions_before


class TestDependencyDegradation:
    def test_relation_stale_degrades_availability(self, env):
        model = env["service"].build_model(env["source_id"])
        key = next(iter(model["dependencies"]["relationship_revisions"]))
        latest = env["repo"].get_relationship_history(key)[0]
        env["repo"].save_relationship({
            **latest, "status": "stale", "inference_source": "human",
        }, expected_revision=latest["revision"])
        refreshed = env["service"].get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == \
            "needs_revalidation"
        assert refreshed["availability"]["usable_as_current"] is False
        report = env["service"].validate_model(
            model["model_id"], model["version"])
        assert not report["valid"]

    def test_snapshot_changed_degrades_and_blocks_publish(self, env, tmp_path):
        confirmed_model = env["service"].build_model(env["source_id"])
        topic = _booking_topic(confirmed_model)
        confirmed = env["service"].revise_model(
            confirmed_model["model_id"], confirmed_model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一次预订", "confirm": True,
                    "keys": topic["grain"]["keys"]}}})
        # 新快照成为发布目录 → 旧模型 snapshot_changed
        from tests.semantic_model_fixtures import seed_m4_environment
        seed_m4_environment(env["repo"], env["source_id"], snapshot_id="snap-m4-2",
                            started_at="2026-10-29T04:00:00+00:00")
        refreshed = env["service"].get_model(
            confirmed["model_id"], confirmed["version"])
        assert refreshed["availability"]["dependency_status"] == "snapshot_changed"
        with pytest.raises(ValueError, match="发布检查未通过"):
            env["service"].publish_model(
                confirmed["model_id"], confirmed["version"], reviewer="dev")
        # 旧模型仍可导出（历史查看），但明确标记不可用作当前
        exported = env["service"].export_model(
            confirmed["model_id"], confirmed["version"])
        assert "availability" in exported["content"]
        assert refreshed["availability"]["usable_as_current"] is False
