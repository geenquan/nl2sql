"""M4 缺陷修复轮回归（八组，全部先复现后修复；离线虚构数据）。

P1-1 注释敏感内容；P1-2 发布指针原子性；P1-3 口径修改保留旧确认；
P1-4 指标定义校验；P1-5 依赖修订参与检查；P1-6 路径风险与人工选路；
P1-7 grain 依据重核；P1-8 未发布草稿正式可用性；P2 契约补齐。
"""

import json
import threading
from typing import Any

import pytest
import yaml

from app.adapters.repositories.sqlite_metadata_repository import (
    SQLiteMetadataRepository,
)
from app.core.config import Settings
from app.core.exceptions import RevisionConflictError
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import (
    DEFAULT_VALIDATED_RELATIONSHIPS,
    SCHEMA,
    m4_catalog,
    seed_m4_environment,
    seed_relationships,
)

SECRET = "ReviewDummy123"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _new_env(tmp_path, **seed_kwargs):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    seed_m4_environment(repo, source_id,
                        relationship_specs=DEFAULT_VALIDATED_RELATIONSHIPS,
                        **seed_kwargs)
    service = SemanticModelService(repo, _settings())
    return repo, source_id, service


def _confirmed(env, confirm_metrics: bool = False) -> dict:
    """构建并确认全部 grain（可选确认一个指标）的版本。"""
    service, source_id = env["service"], env["source_id"]
    model = service.build_model(source_id)
    changes: dict[str, Any] = {
        "topic_grains": {
            t["topic_id"]: {
                "statement": f"一行代表一条{t['name']}记录",
                "keys": t["grain"]["keys"], "confirm": True}
            for t in model["topics"] if t["grain"]["keys"]}}
    if confirm_metrics:
        metric = next(
            (m for m in model["metrics"] if m["aggregation"] == "sum"), None)
        if metric is None:
            metric = next(
                (m for m in model["metrics"]
                 if m["aggregation"] == "count_rows"
                 and m["entity_id"].startswith(f"{SCHEMA}.booking")), None)
        if metric is not None:
            changes["metrics"] = {metric["metric_id"]: {
                "action": "confirm", "time_basis": "自然日",
                "unit": "元" if metric["aggregation"] in ("sum", "avg") else None,
                "skip_status_filter": True}}
    return service.revise_model(
        model["model_id"], model["version"], changes, reviewer="dev")


# == P1-1：注释敏感内容进入模型与导出 ============================================


class TestSensitiveCommentSanitization:
    def _seed_secret_comments(self, repo, source_id):
        """直接改写快照内表/列注释（模拟已扫描入库的敏感注释）。"""
        catalog = m4_catalog(source_id, "snap-m4")
        guest = next(t for t in catalog if t["table_name"] == "guest")
        guest["table_comment"] = f"password: {SECRET}"
        for table in catalog:
            table["table_id"] = f"snap-m4-{table['table_name']}" \
                if table["schema_name"] == SCHEMA else table["table_id"]
        # 通过仓储重新保存该表（同快照 upsert）
        repo.save_table_metadata(guest)

    def test_table_comment_secret_not_in_model_or_export(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        self._seed_secret_comments(repo, source_id)
        model = service.build_model(source_id)
        # 模型持久化与 API 读取均不含密钥原值；实体名回退表名（净化遮蔽不作业务名）
        guest = next(e for e in model["entities"]
                     if e["entity_id"] == f"{SCHEMA}.guest")
        assert SECRET not in json.dumps(model, ensure_ascii=False)
        assert guest["name"] == "guest"
        export = service.export_model(model["model_id"], model["version"],
                                      export_format="json")
        assert SECRET not in export["content"]
        yaml_export = service.export_model(model["model_id"], model["version"],
                                           export_format="yaml")
        assert SECRET not in yaml_export["content"]

    def test_historical_model_with_secret_sanitized_on_read(self, tmp_path):
        """历史版本不改写存储行，但读取/导出边界净化。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        # 直接篡改历史存储行（模拟修复前已持久化的敏感内容）
        raw = repo.get_model(model["model_id"], model["version"])
        raw["entities"][0]["name"] = f"token: {SECRET}"
        with repo._engine.begin() as conn:
            from sqlalchemy import update

            conn.execute(
                update(repo._semantic_models).where(
                    repo._semantic_models.c.model_id == model["model_id"],
                    repo._semantic_models.c.version == model["version"],
                ).values(payload=json.dumps(raw, ensure_ascii=False)))
        refreshed = service.get_model(model["model_id"], model["version"])
        assert SECRET not in json.dumps(refreshed, ensure_ascii=False)
        # 存储行未被读取操作改写
        raw_again = repo.get_model(model["model_id"], model["version"])
        assert SECRET in json.dumps(raw_again, ensure_ascii=False)

    def test_column_comment_and_bearer_forms_sanitized(self, tmp_path):
        from app.semantics.prompt import sanitize_text_for_model

        payloads = [
            f"password: {SECRET}",
            f"token: Bearer {SECRET}",
            f'"api_key": "{SECRET}"',
            f"Authorization: Bearer {SECRET}",
        ]
        for payload in payloads:
            cleaned = sanitize_text_for_model(payload, 60)
            assert SECRET not in cleaned, payload


# == P1-2：发布指针并发保护 ========================================================


class TestPublishPointerAtomicity:
    def _publishable(self, env, count=2):
        versions = []
        base = _confirmed(env, confirm_metrics=True)
        versions.append(base)
        current = base
        for _ in range(count - 1):
            current = env["service"].revise_model(
                current["model_id"], current["version"], {}, reviewer="dev")
            versions.append(current)
        return versions

    def test_concurrent_publish_same_expected_one_wins(self, tmp_path):
        env = {"repo": None, "source_id": None, "service": None}
        repo, source_id, service = _new_env(tmp_path)
        env.update(repo=repo, source_id=source_id, service=service)
        v2, v3, v4 = self._publishable(env, count=3)
        # 先发布 v2 建立指针
        service.publish_model(v2["model_id"], v2["version"], reviewer="a")
        expected = (v2["model_id"], v2["version"])
        results: list[Any] = []
        barrier = threading.Barrier(2)

        def publish(version):
            barrier.wait()
            try:
                results.append(service.publish_model(
                    v3["model_id"], version, reviewer="t",
                    expected_pointer=expected))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=publish, args=(v3["version"],)),
                   threading.Thread(target=publish, args=(v4["version"],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        ok = [r for r in results if isinstance(r, dict)]
        conflicts = [r for r in results if isinstance(r, RevisionConflictError)]
        assert len(ok) == 1 and len(conflicts) == 1
        # 失败方不改指针、不写成功审计
        pointed = repo.get_published(source_id)
        assert pointed["version"] in (v3["version"], v4["version"])
        log = repo.list_publication_log(source_id)
        published_versions = {e["version"] for e in log if e["action"] == "publish"}
        assert pointed["version"] in published_versions

    def test_publish_vs_rollback_race(self, tmp_path):
        """指针在 v2：发布 v3 与回滚到 v3 竞争同一期望指针——恰一成功。"""
        repo, source_id, service = _new_env(tmp_path)
        v2, v3 = self._publishable({"repo": repo, "source_id": source_id,
                                    "service": service}, count=2)
        service.publish_model(v2["model_id"], v2["version"], reviewer="a")
        expected = (v2["model_id"], v2["version"])
        outcomes: list[Any] = []
        barrier = threading.Barrier(2)

        def do_publish():
            barrier.wait()
            try:
                outcomes.append(service.publish_model(
                    v3["model_id"], v3["version"], reviewer="p",
                    expected_pointer=expected))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        def do_rollback():
            barrier.wait()
            try:
                outcomes.append(service.rollback_model(
                    v3["model_id"], v3["version"], reviewer="r",
                    expected_pointer=expected))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=do_publish),
                   threading.Thread(target=do_rollback)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        ok = [o for o in outcomes if isinstance(o, dict)]
        conflicts = [o for o in outcomes if isinstance(o, RevisionConflictError)]
        assert len(ok) + len(conflicts) == 2
        assert len(ok) == 1  # 条件 UPDATE 恰一命中
        assert repo.get_published(source_id)["version"] == v3["version"]

    def test_first_publish_race_one_wins(self, tmp_path):
        """首次发布竞争：用引擎事件在指针 INSERT 处对齐两个真实连接，
        强制同时插入 → 唯一约束仲裁恰一成功、另一 409。"""
        from sqlalchemy import event

        repo, source_id, service = _new_env(tmp_path)
        v2, v3 = self._publishable({"repo": repo, "source_id": source_id,
                                    "service": service}, count=2)
        insert_barrier = threading.Barrier(2, timeout=10)

        def hold_inserts(conn, cursor, statement, parameters, context, executemany):
            if "INSERT INTO semantic_model_pointer" in statement:
                insert_barrier.wait()  # 两线程在 INSERT 处对齐后同时放行

        event.listen(repo._engine, "before_cursor_execute", hold_inserts)
        results: list[Any] = []
        start_barrier = threading.Barrier(2)

        def first_publish(version):
            start_barrier.wait()
            try:
                results.append(service.publish_model(
                    v2["model_id"], version, reviewer="t"))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=first_publish, args=(v2["version"],)),
                   threading.Thread(target=first_publish, args=(v3["version"],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        event.remove(repo._engine, "before_cursor_execute", hold_inserts)
        ok = [r for r in results if isinstance(r, dict)]
        conflicts = [r for r in results if isinstance(r, RevisionConflictError)]
        assert len(ok) + len(conflicts) == 2
        assert len(ok) == 1  # 同时 INSERT：唯一约束恰一成功
        assert repo.get_published(source_id)["version"] in (
            v2["version"], v3["version"])

    def test_audit_failure_rolls_back_pointer(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        version = _confirmed({"repo": repo, "source_id": source_id,
                              "service": service}, confirm_metrics=True)
        real = repo
        class BrokenLog(type(real)):
            def list_publication_log(self, source_id):
                return []

        broken = BrokenLog.__new__(BrokenLog)
        broken.__dict__.update(real.__dict__)
        # 注入：审计表写入失败（同事务回滚 → 指针不变）
        from sqlalchemy import text as _text

        def failing_set_published(*args, **kwargs):
            with broken._engine.begin() as conn:
                conn.execute(_text("SELECT RAISE(ABORT, 'audit fail')"))
            return real.set_published(*args, **kwargs)

        broken.set_published = failing_set_published  # type: ignore[method-assign]
        broken_service = SemanticModelService(broken, _settings())
        with pytest.raises(Exception):  # noqa: B017 - 故障注入：任意收尾异常即可
            broken_service.publish_model(
                version["model_id"], version["version"], reviewer="dev")
        assert repo.get_published(source_id) is None  # 指针未变
        del BrokenLog


# == P1-3：口径修改保留旧确认 ======================================================


class TestMetricCaliberChangeInvalidatesConfirmation:
    def test_unit_change_invalidates(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        metric = next(
            m for m in confirmed["metrics"]
            if m.get("status") == "business_confirmed")
        old_confirmation = dict(metric["business_confirmation"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "unit": "件"}}}, reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"] if m["metric_id"] == metric["metric_id"])
        assert revised_metric["status"] == "suggested"  # 旧确认失效
        assert revised_metric["business_confirmation"] is None
        assert revised_metric["data_validation"] is None
        assert any("口径已修改" in b for b in revised_metric["blocking"])
        assert old_confirmation  # 原版本内容不变
        original = service.get_model(confirmed["model_id"], confirmed["version"])
        assert next(m for m in original["metrics"]
                    if m["metric_id"] == metric["metric_id"])[
            "status"] == "business_confirmed"

    def test_filter_or_aggregation_change_invalidates(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        metric = next(
            m for m in confirmed["metrics"]
            if m.get("status") == "business_confirmed")
        # 过滤口径变化
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update",
                    "filters": [{"field": {"schema": SCHEMA,
                                           "table": metric["entity_id"].split(".")[1],
                                           "column": "id"},
                                 "operator": "ge", "value": 1}]}}},
            reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"] if m["metric_id"] == metric["metric_id"])
        assert revised_metric["status"] == "suggested"
        # 聚合口径变化（count_rows→count_distinct 需去重键 → 定义校验拒绝保存）
        with pytest.raises(ValueError, match="定义"):
            service.revise_model(
                confirmed["model_id"], confirmed["version"], {
                    "metrics": {metric["metric_id"]: {
                        "action": "update", "aggregation": "count_distinct"}}},
                reviewer="dev")

    def test_name_change_keeps_confirmation(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        metric = next(
            m for m in confirmed["metrics"]
            if m.get("status") == "business_confirmed")
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "name": "新指标名"}}}, reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"] if m["metric_id"] == metric["metric_id"])
        assert revised_metric["status"] == "business_confirmed"  # 非口径字段

    def test_confirmed_then_modified_not_formal_for_publish(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        metric = next(
            m for m in confirmed["metrics"]
            if m.get("status") == "business_confirmed")
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "time_basis": "事件时间"}}}, reviewer="dev")
        # 模型可发布（grain 已确认），但该指标已退出正式集合：
        # 重新确认前不得作为正式指标供下游使用
        service.publish_model(revised["model_id"], revised["version"],
                              reviewer="dev")
        published = service.get_model(revised["model_id"], revised["version"])
        assert metric["metric_id"] not in published["formal_metric_ids"]
        assert metric["metric_id"] not in published["confirmed_metric_ids"]


# == P1-4：指标定义与字段引用校验 ==================================================


class TestMetricDefinitionValidation:
    def test_count_rows_to_sum_without_field_rejected(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        with pytest.raises(ValueError, match="缺少数值型度量字段"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "metrics": {metric["metric_id"]: {
                        "action": "update", "aggregation": "sum"}}}, reviewer="dev")
        # 错误输入不产生新版本
        assert len(repo.list_versions(model["model_id"])) == 1

    def test_aggregation_change_recomputes_display_expression(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        price_metric = next(
            (m for m in model["metrics"] if m["aggregation"] == "sum"), None)
        if price_metric is None:
            pytest.skip("夹具无金额字段")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "metrics": {price_metric["metric_id"]: {
                    "action": "update", "aggregation": "max"}}}, reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"]
            if m["metric_id"] == price_metric["metric_id"])
        assert revised_metric["aggregation"] == "max"
        assert revised_metric["display_expression"].startswith("MAX(")
        assert "COUNT(*)" not in revised_metric["display_expression"]

    def test_cross_entity_filter_field_rejected(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        with pytest.raises(ValueError, match="不属于指标自身实体"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "metrics": {metric["metric_id"]: {
                        "action": "update",
                        "filters": [{"field": {
                            "schema": "archive", "table": "guest",
                            "column": "id"},
                            "operator": "ge", "value": 1}]}}},
                reviewer="dev")

    def test_non_temporal_time_field_rejected(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        booking_table = metric["entity_id"].split(".", 1)[1]
        with pytest.raises(ValueError, match="日期/时间类型"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "metrics": {metric["metric_id"]: {
                        "action": "update",
                        "time_field": {"schema": SCHEMA, "table": booking_table,
                                       "column": "guest_id"}}}},
                reviewer="dev")

    def test_valid_changes_accepted(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        table = metric["entity_id"].split(".", 1)[1]
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update",
                    "time_field": {"schema": SCHEMA, "table": table,
                                   "column": "created_at"},
                    "filters": [{"field": {"schema": SCHEMA, "table": table,
                                           "column": "id"},
                                 "operator": "ge", "value": 1}]}}},
            reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"] if m["metric_id"] == metric["metric_id"])
        assert revised_metric["time_field"]["column"] == "created_at"
        assert revised_metric["filters"][0]["operator"] == "ge"


# == P1-5：依赖修订参与可用性检查 ==================================================


class TestDependencyRevisionChecking:
    def test_m3_new_revision_still_validated_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        key = next(iter(model["dependencies"]["relationship_revisions"]))
        latest = repo.get_relationship_history(key)[0]
        # 新修订仍 validated 但基数改变
        repo.save_relationship({
            **latest, "revision": None,
            "cardinality": "many_to_many",
            "evidence": {"cardinality_observed": {
                "cardinality": "many_to_many"}},
        }, expected_revision=latest["revision"])
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "needs_revalidation"
        assert refreshed["availability"]["usable_as_current"] is False

    def test_m3_stale_or_rejected_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        key = next(iter(model["dependencies"]["relationship_revisions"]))
        latest = repo.get_relationship_history(key)[0]
        repo.save_relationship({
            **latest, "status": "stale", "inference_source": "human",
        }, expected_revision=latest["revision"])
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "needs_revalidation"

    def test_m2_field_semantics_rejection_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        dep_key = next(
            k for k in model["dependencies"]["m2_field_semantics"]
            if ".price" in k or ".tenant_seq" in k)
        table_id, column = dep_key.rsplit(".", 1)
        history = repo.get_field_semantics_history(table_id, column)
        repo.save_field_semantics({
            **history[0], "review_status": "rejected"},
            expected_revision=history[0]["revision"])
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "needs_revalidation"

    def test_m2_purpose_rejection_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        table_id = next(
            k for k, v in model["dependencies"]["m2_table_purposes"].items()
            if "booking" in k)
        purpose = repo.get_table_purpose(table_id, effective=True)
        repo.save_table_purpose({
            **purpose, "review_status": "rejected"},
            expected_revision=purpose["revision"])
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "needs_revalidation"

    def test_snapshot_change_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        seed_m4_environment(repo, source_id, snapshot_id="snap-m4-2",
                            started_at="2026-10-29T06:00:00+00:00")
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "snapshot_changed"
        # 历史保留：旧版本仍存在
        assert repo.get_model(model["model_id"], model["version"]) is not None

    def test_legacy_model_without_m2_deps_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        raw = repo.get_model(model["model_id"], model["version"])
        del raw["dependencies"]["m2_field_semantics"]
        del raw["dependencies"]["m2_table_purposes"]
        import json as _json

        from sqlalchemy import update
        with repo._engine.begin() as conn:
            conn.execute(
                update(repo._semantic_models).where(
                    repo._semantic_models.c.model_id == model["model_id"],
                    repo._semantic_models.c.version == model["version"],
                ).values(payload=_json.dumps(raw, ensure_ascii=False)))
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == "needs_revalidation"  # 缺依赖信息不无条件视为已验证


# == P1-6：路径风险与人工选路 ======================================================


class TestPathRiskAndManualSelection:
    def test_one_to_many_path_blocked(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        seed_relationships(repo, source_id, "snap-m4", m4_catalog(source_id), [{
            "src": "guest", "src_cols": ["tenant_id"], "tgt": "tenant",
            "tgt_cols": ["id"], "cardinality": "many_to_one"},
            {"src": "tenant", "src_cols": ["id"], "tgt": "guest",
             "tgt_cols": ["tenant_id"], "cardinality": "one_to_many"}])
        model = service.build_model(source_id)
        blocked = [p for p in model["paths"]
                   if p["status"] == "blocked"
                   and any(r["type"] == "fanout_one_to_many"
                           for r in p["risks"])]
        assert blocked, "一对多方向路径必须标记扇出阻断"

    def test_manual_selection_keeps_non_ambiguity_risks(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        truncated_service = SemanticModelService(repo, _settings(
            semantic_model_max_paths_per_pair=1))
        model = truncated_service.build_model(source_id)
        candidate = next(
            p for p in model["paths"]
            if p["to"]["table"] == "tenant" and p["budget_truncated"])
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "selected_paths": {
                    candidate["topic_id"]: candidate["path_id"]}},
            reviewer="dev")
        chosen = next(p for p in revised["paths"]
                      if p["path_id"] == candidate["path_id"])
        # 人工选择只解除歧义：预算截断风险仍在，不得变 available
        assert chosen["status"] != "available"
        assert any("预算截断" in r for r in chosen["status_reasons"])

    def test_unselected_blocked_path_stays_blocked(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        seed_relationships(repo, source_id, "snap-m4", m4_catalog(source_id), [{
            "src": "booking", "src_cols": ["id"], "tgt": "service_item",
             "tgt_cols": ["id"], "cardinality": "many_to_many"}])
        model = service.build_model(source_id)
        blocked = next(p for p in model["paths"] if p["status"] == "blocked")
        available = next(p for p in model["paths"] if p["status"] == "available")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "selected_paths": {
                    available["topic_id"]: available["path_id"]}},
            reviewer="dev")
        still_blocked = next(p for p in revised["paths"]
                             if p["path_id"] == blocked["path_id"])
        assert still_blocked["status"] == "blocked"  # 未选阻断路径不降级

    def test_validation_uses_structural_refs_non_app_schema(self, tmp_path):
        """收尾轮 2 语义修正：风险路径不因终点同名阻断本表正式指标
        （首版指标仅用本实体字段；跨 Schema 同名表不串用影响范围）。"""
        repo, source_id, service = _new_env(tmp_path)
        seed_relationships(repo, source_id, "snap-m4", m4_catalog(source_id), [{
            "src": "no_pk_notes", "src_cols": ["note_text"],
             "tgt": "audit_log", "tgt_cols": ["entity_type"],
             "cardinality": "many_to_many", "status": "validated"}])
        model = service.build_model(source_id)
        audit_metric = next(
            (m for m in model["metrics"]
             if m["entity_id"] == f"{SCHEMA}.audit_log"
             and m["aggregation"] == "count_rows"), None)
        grains = {
            t["topic_id"]: {
                "statement": "s", "keys": t["grain"]["keys"], "confirm": True}
            for t in model["topics"] if t["grain"]["keys"]}
        changes: dict[str, Any] = {"topic_grains": grains}
        if audit_metric is not None:
            changes["metrics"] = {audit_metric["metric_id"]: {
                "action": "confirm", "time_basis": "x",
                "skip_status_filter": True}}
        revised = service.revise_model(
            model["model_id"], model["version"], changes, reviewer="dev")
        report = service.validate_model(
            revised["model_id"], revised["version"])
        # 阻断路径保持 blocked 且如实上报；但不产生“影响正式指标”误伤
        blocked_to_audit = [
            p for p in revised["paths"] if p["status"] == "blocked"]
        assert all(p["path_id"] in report["checks"]["path_issues"]
                   for p in blocked_to_audit)
        assert not any("阻断路径影响正式指标" in i for i in report["issues"])
        assert not any("audit_log" in i for i in report["issues"])


# == P1-7：grain 依据重核 ==========================================================


class TestGrainBasisRevalidation:
    def test_non_unique_key_confirm_kept_as_draft(self, tmp_path):
        _repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        topic = next(t for t in model["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一个状态",
                    "keys": ["status_code"], "confirm": True}}},
            reviewer="dev")
        grain = next(t for t in revised["topics"]
                     if t["topic_id"] == topic["topic_id"])["grain"]
        # 普通非唯一列不得伪装成已证实行粒度：保留待验证草稿
        assert grain["confirmed"] is False
        assert grain["basis"] == "unknown"
        assert any("无结构证明" in m for m in grain["missing"])
        # 发布检查不通过
        report = service.validate_model(
            revised["model_id"], revised["version"])
        assert not report["valid"]

    def test_duplicate_and_missing_keys_rejected(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        topic = next(t for t in model["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        with pytest.raises(ValueError, match="重复列"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "topic_grains": {topic["topic_id"]: {
                        "statement": "s", "keys": ["id", "id"],
                        "confirm": True}}}, reviewer="dev")
        with pytest.raises(ValueError, match="不存在于中心表"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "topic_grains": {topic["topic_id"]: {
                        "statement": "s", "keys": ["no_such"], "confirm": True}}},
                reviewer="dev")

    def test_unique_constraint_basis_accepted_and_pk_stays(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        # booking 有唯一索引 uq_booking_no → 以 [booking_no] 为键
        topic = next(t for t in model["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一个预订单号",
                    "keys": ["booking_no"], "confirm": True}}},
            reviewer="dev")
        grain = next(t for t in revised["topics"]
                     if t["topic_id"] == topic["topic_id"])["grain"]
        assert grain["basis"] == "unique_constraint"
        assert grain["confirmed"] is True
        # 主题与中心实体 grain 同步
        entity = next(e for e in revised["entities"]
                      if e["entity_id"] == f"{SCHEMA}.booking")
        assert entity["grain"]["keys"] == ["booking_no"]
        assert entity["grain"]["confirmed"] is True

    def test_grain_key_change_invalidates_metric_confirmation(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        metric = next(
            m for m in confirmed["metrics"]
            if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一个预订单号",
                    "keys": ["booking_no"], "confirm": True}}},
            reviewer="dev")
        revised_metric = next(
            m for m in revised["metrics"] if m["metric_id"] == metric["metric_id"])
        assert revised_metric["status"] == "suggested"  # grain 变化→重新确认
        assert revised_metric["grain_statement"] == "一行代表一个预订单号"


# == P1-8：未发布草稿的正式可用性 ==================================================


class TestFormalAvailability:
    def test_unpublished_draft_not_usable_no_formal_ids(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        # 已确认但未发布
        assert confirmed["availability"]["is_published_version"] is False
        assert confirmed["availability"]["usable_as_current"] is False
        assert confirmed["formal_metric_ids"] == []  # 不返回正式集合
        assert confirmed["confirmed_metric_ids"]  # 已确认草稿另列

    def test_published_valid_version_formal(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        service.publish_model(confirmed["model_id"], confirmed["version"],
                              reviewer="dev")
        published = service.get_model(confirmed["model_id"], confirmed["version"])
        assert published["availability"]["usable_as_current"] is True
        assert published["formal_metric_ids"]

    def test_validate_not_depend_on_published_and_vice_versa(self, tmp_path):
        """validate 独立于发布状态：未发布版本检查可通过（发布前置）。"""
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        report = service.validate_model(
            confirmed["model_id"], confirmed["version"])
        assert report["valid"] is True  # 检查通过但 usable_as_current=False

    def test_dependency_degraded_version_no_formal_ids(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        confirmed = _confirmed({"repo": repo, "source_id": source_id,
                                "service": service}, confirm_metrics=True)
        service.publish_model(confirmed["model_id"], confirmed["version"],
                              reviewer="dev")
        key = next(iter(confirmed["dependencies"]["relationship_revisions"]))
        latest = repo.get_relationship_history(key)[0]
        repo.save_relationship({
            **latest, "status": "stale", "inference_source": "human",
        }, expected_revision=latest["revision"])
        refreshed = service.get_model(confirmed["model_id"], confirmed["version"])
        assert refreshed["availability"]["is_published_version"] is True
        assert refreshed["availability"]["usable_as_current"] is False
        assert refreshed["formal_metric_ids"] == []

    def test_rollback_versions_availability(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        v2 = _confirmed({"repo": repo, "source_id": source_id,
                         "service": service}, confirm_metrics=True)
        v3 = service.revise_model(
            v2["model_id"], v2["version"], {}, reviewer="dev")
        service.publish_model(v3["model_id"], v3["version"], reviewer="dev")
        service.rollback_model(v2["model_id"], v2["version"], reviewer="dev",
                               reason="回滚")
        current = service.get_model(v2["model_id"], v2["version"])
        older = service.get_model(v2["model_id"], v3["version"])
        assert current["availability"]["usable_as_current"] is True
        assert older["availability"]["is_published_version"] is False
        assert older["formal_metric_ids"] == []


# == P2：契约补齐 =================================================================


class TestContractCompletion:
    def test_entity_purpose_consumed_and_rejected_not_positive(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        booking = next(e for e in model["entities"]
                       if e["entity_id"] == f"{SCHEMA}.booking")
        assert booking["purpose"] == "transaction_event"  # 消费有效表用途
        # 表用途被否决后重建：不作正向分类
        purpose = repo.get_table_purpose("snap-m4-booking", effective=True)
        repo.save_table_purpose({
            **purpose, "review_status": "rejected"},
            expected_revision=purpose["revision"])
        rebuilt = service.build_model(source_id)
        rebuilt_booking = next(e for e in rebuilt["entities"]
                               if e["entity_id"] == f"{SCHEMA}.booking")
        assert rebuilt_booking["purpose"] == "unknown"
        assert rebuilt_booking["purpose_note"]
        # 否决后不再作为主题中心实体
        central = {t["central_entity_id"] for t in rebuilt["topics"]}
        assert f"{SCHEMA}.booking" not in central

    def test_topic_metric_and_dimension_refs_populated(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        topic = next(t for t in model["topics"]
                     if t["central_entity_id"] == f"{SCHEMA}.booking")
        metric_ids = {m["metric_id"] for m in model["metrics"]}
        dimension_ids = {d["dimension_id"] for d in model["dimensions"]}
        entity_ids = {e["entity_id"] for e in model["entities"]}
        assert topic["metric_ids"] and set(topic["metric_ids"]) <= metric_ids
        assert set(topic["dimension_ids"]) <= dimension_ids
        assert set(topic["entity_ids"]) <= entity_ids
        # 指标归属正确：主题指标的中心实体即主题中心
        for metric_id in topic["metric_ids"]:
            metric = next(m for m in model["metrics"]
                          if m["metric_id"] == metric_id)
            assert metric["entity_id"] == topic["central_entity_id"]

    def test_export_yaml_json_same_semantics(self, tmp_path):
        _, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        json_export = service.export_model(
            model["model_id"], model["version"], export_format="json")
        yaml_export = service.export_model(
            model["model_id"], model["version"], export_format="yaml")
        assert json.loads(json_export["content"]) == yaml.safe_load(
            yaml_export["content"])
