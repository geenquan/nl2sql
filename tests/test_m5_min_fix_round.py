"""M5 最小修复轮回归测试（五项 + 执行预算收口；全部离线 SQLite）。

项 1：条件 AVG 分母排除 NULL 度量值（预聚合路径，多行成组才可观察）
项 2：中心指标独立对账（各指标自身过滤；基准 AVG=和/计数口径）
项 3：回滚尊重数据集级发布占用
项 4：核对完成指针提交（不只改状态）
项 5：正式预算接入（单次消耗/期限闸/字节上限/preview 与导出接线/
     MySQL 执行器与装配工厂转发）
附加：ES needs_reconciliation 不保留 active 锁（不卡死恢复路径）
"""

import time
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.exceptions import (
    RevisionConflictError,
    ValidationDeadlineExceededError,
    ValidationStatementBudgetExceeded,
)
from tests.dataset_fixtures import (
    SCHEMA,
    detail_relation_key,
    make_service,
    seed_environment,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture()
def env(tmp_path):
    return seed_environment(tmp_path)


def _attach_map(env: dict[str, Any]) -> dict[str, str]:
    base = env["data_url"].replace("sqlite:///", "")
    return {SCHEMA: f"{base}.app", "archive": f"{base}.archive"}


def _metric(model: dict[str, Any], entity_table: str,
            aggregation: str) -> dict[str, Any]:
    for metric in model.get("metrics", []):
        if (metric.get("aggregation") == aggregation
                and metric.get("entity_id") == f"{SCHEMA}.{entity_table}"):
            return metric
    pytest.fail(f"夹具缺少指标：{entity_table}.{aggregation}")


def _revise_and_publish(env: dict[str, Any],
                        metric_changes: dict[str, dict[str, Any]]) -> dict:
    """M4 修订（更新+确认）并发布，返回新发布的模型。"""
    model_service = env["model_service"]
    model = model_service.get_model(
        env["model"]["model_id"], version=env["model"]["version"])
    revised = model_service.revise_model(
        model["model_id"], model["version"], {"metrics": metric_changes},
        reviewer="dev")
    confirms = {
        metric_id: {**change, "action": "confirm",
                    "time_basis": change.get("time_basis", "自然日"),
                    "skip_status_filter": True}
        for metric_id, change in metric_changes.items()
        if change.get("action") == "update"
    }
    reconfirmed = model_service.revise_model(
        revised["model_id"], revised["version"], {"metrics": confirms},
        reviewer="dev") if confirms else revised
    model_service.publish_model(
        reconfirmed["model_id"], reconfirmed["version"], reviewer="dev")
    return model_service.get_model(
        reconfirmed["model_id"], reconfirmed["version"])


# == 项 1：条件 AVG 分母 ==========================================================


class TestConditionalAvgDenominator:
    def test_avg_denominator_excludes_null_measure(self, env):
        """detail_a 指标 avg(qty) 过滤 line_amount 非空（条件全通过），
        注入 2 行 qty=NULL：分母必须只计非 NULL 度量行。

        booking 1 的 qty：1..5 + NULL + NULL → 正确 AVG=15/5=3；
        旧缺陷（分母计条件行）= 15/7（SQLite 整除=2）≠ 3。
        """
        base = env["data_url"].replace("sqlite:///", "")
        engine = create_engine(env["data_url"])
        with engine.begin() as conn:
            conn.execute(text(f"ATTACH DATABASE '{base}.app' AS app"))
            conn.execute(text(
                "INSERT INTO app.detail_a (id, booking_id, line_amount, qty) "
                "VALUES (100, 1, '9.99', NULL), (101, 1, '9.99', NULL)"))
        engine.dispose()

        model = env["model_service"].get_model(
            env["model"]["model_id"], version=env["model"]["version"])
        # detail_a 无数值 sum 候选：把 count_rows 指标改为 avg(qty) 口径
        qty_metric = _metric(model, "detail_a", "count_rows")
        published = _revise_and_publish(env, {
            qty_metric["metric_id"]: {
                "action": "update", "aggregation": "avg", "unit": "件",
                "measure_field": {"schema": SCHEMA, "table": "detail_a",
                                  "column": "qty"},
                "filters": [{"field": {
                    "schema": SCHEMA, "table": "detail_a",
                    "column": "line_amount"}, "operator": "not_null"}]},
        })
        definition = {
            "name": "AVG 分母口径", "model_id": published["model_id"],
            "model_version": published["version"], "kind": "aggregate",
            "central_entity_id": f"{SCHEMA}.booking",
            "topic_id": f"topic.{SCHEMA}.booking",
            "grain_keys": ["id"], "grain_statement": "一行代表一次预订",
            "fields": [], "dimensions": [], "metrics": [],
            "preaggregations": [{
                "metric_id": qty_metric["metric_id"],
                "output_alias": "qty_avg",
                "relationship_key": detail_relation_key(env, "detail_a"),
                "scope_note": "line_amount 非空口径下按关联键预聚合的平均数量"}],
            "filters": [], "target": "view",
        }
        service = make_service(env)
        dataset = service.create_dataset(env["source_id"], definition)
        result = service.validate(dataset["dataset_id"], dataset["version"])
        assert result["status"] == "ready", (
            f"质量验证未通过：{result['quality']}")
        pre_checks = [c for c in result["quality"]["checks"]
                      if c["check"] == "preaggregation_reconciliation"]
        assert pre_checks and all(c["outcome"] == "passed" for c in pre_checks)
        preview = service.preview(dataset["dataset_id"], dataset["version"])
        by_id = {str(row["id"]): row.get("qty_avg")
                 for row in preview["rows"]}
        assert float(by_id["1"]) == 3.0, (
            f"AVG 分母未排除 NULL 度量行：{by_id['1']}")
        # booking 2 qty 1..4 → 平均 2.5
        assert by_id["2"] is not None
        # 结构断言：编译产物的 AVG 分母含 IS NOT NULL 守卫
        plan = env["repo"].get_latest_plan(dataset["dataset_id"],
                                           dataset["version"])
        assert plan and "IS NOT NULL" in plan["sql"], (
            plan["sql"] if plan else "缺少编译计划")


# == 项 2：中心指标独立对账 =======================================================


class TestCenterMetricReconciliation:
    def test_each_metric_uses_own_filters(self, env):
        """两个中心指标各带不同过滤：对账基准按各自口径独立计算。

        - avg(tenant_seq | status='paid')：b1=1, b2=2, b4=3, b5=2, b3=NULL
        - count(guest_id>=2)：b3/b4/b5=1，b1/b2=0
        若基准忽略/合并指标过滤（旧缺陷），值不符 → 不 ready。
        """
        model = env["model_service"].get_model(
            env["model"]["model_id"], version=env["model"]["version"])
        seq_metric = _metric(model, "booking", "sum")
        count_metric = _metric(model, "booking", "count_rows")
        published = _revise_and_publish(env, {
            seq_metric["metric_id"]: {
                "action": "update", "aggregation": "avg", "unit": "序号",
                "filters": [{"field": {
                    "schema": SCHEMA, "table": "booking",
                    "column": "status_code"}, "operator": "eq",
                    "value": "paid"}]},
            count_metric["metric_id"]: {
                "action": "update",
                "filters": [{"field": {
                    "schema": SCHEMA, "table": "booking",
                    "column": "guest_id"}, "operator": "ge", "value": 2}]},
        })
        definition = {
            "name": "中心指标独立对账", "model_id": published["model_id"],
            "model_version": published["version"], "kind": "aggregate",
            "central_entity_id": f"{SCHEMA}.booking",
            "topic_id": f"topic.{SCHEMA}.booking",
            "grain_keys": ["id"], "grain_statement": "一行代表一次预订",
            "fields": [], "dimensions": [],
            "metrics": [
                {"metric_id": seq_metric["metric_id"],
                 "output_alias": "seq_avg"},
                {"metric_id": count_metric["metric_id"],
                 "output_alias": "cnt_ge2"},
            ],
            "preaggregations": [], "filters": [], "target": "view",
        }
        service = make_service(env)
        dataset = service.create_dataset(env["source_id"], definition)
        result = service.validate(dataset["dataset_id"], dataset["version"])
        assert result["status"] == "ready", (
            f"独立对账未通过：{result['quality']}")
        recon = [c for c in result["quality"]["checks"]
                 if c["check"] == "metric_reconciliation"]
        assert len(recon) == 2 and all(c["outcome"] == "passed"
                                       for c in recon)
        preview = service.preview(dataset["dataset_id"], dataset["version"])
        rows = {str(row["id"]): row for row in preview["rows"]}

        def _num(value):
            return None if value in (None, "None") else float(value)

        assert _num(rows["1"]["seq_avg"]) == 1.0
        assert _num(rows["2"]["seq_avg"]) == 2.0
        assert _num(rows["4"]["seq_avg"]) == 3.0
        assert _num(rows["3"]["seq_avg"]) is None  # cancelled 不在 paid 口径
        assert _num(rows["1"]["cnt_ge2"]) == 0
        assert _num(rows["3"]["cnt_ge2"]) == 1
        assert _num(rows["4"]["cnt_ge2"]) == 1


# == 项 3：回滚尊重发布占用 ======================================================


class TestRollbackLock:
    def test_rollback_blocked_while_lock_held(self, env, tmp_path):
        from app.adapters.dataset_executors import SQLiteViewPublisher

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=_settings(
                dataset_view_publish_enabled=True,
                dataset_view_target_schema="app",
                dataset_validation_rows=1000))
        from tests.dataset_fixtures import aggregate_definition

        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        service.validate(dataset["dataset_id"], dataset["version"])
        service.publish(dataset["dataset_id"], dataset["version"])
        dataset_id = dataset["dataset_id"]

        # 他方持有数据集级占用 → 回滚被拒（无外部副作用）
        assert env["repo"].acquire_publish_lock(
            dataset_id, "blocker-op", "publish")
        with pytest.raises(RevisionConflictError):
            service.rollback(dataset_id, dataset["version"])
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == dataset["version"]
        # 释放后回滚成功
        assert env["repo"].release_publish_lock(dataset_id, "blocker-op")
        result = service.rollback(dataset_id, dataset["version"])
        assert result["rolled_back"] is True


# == 项 4：核对完成指针提交 ======================================================


class TestReconcilePointerCommit:
    def test_reconcile_commits_pointer_and_status(self, env):
        from app.adapters.dataset_executors import SQLiteViewPublisher

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=_settings(
                dataset_view_publish_enabled=True,
                dataset_view_target_schema="app",
                dataset_validation_rows=1000))
        from tests.dataset_fixtures import aggregate_definition

        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)

        real_set = env["repo"].set_published_dataset

        def failing_set(*args, **kwargs):
            raise RuntimeError("本地指针故障")

        env["repo"].set_published_dataset = failing_set  # type: ignore
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        # 指针未被失败发布切换
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_object_exists"
        assert reconciled.get("pointer_restored") is True
        # 修复点：核对恢复必须提交指针（不只是改状态）
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version
        assert service.get_dataset(dataset_id, version)["status"] \
            == "published"


# == 项 5：正式预算接入 ==========================================================


class TestFormalBudget:
    def test_single_consumption_per_statement(self, env):
        """每条语句恰好消耗一次额度（回归：双重扣减使限额减半）。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor
        from app.relationships.execution import StatementBudget

        budget = StatementBudget(2)
        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.booking"}, {f"{SCHEMA}.booking": {"id"}},
            statement_budget=budget, attach=_attach_map(env))
        try:
            executor.execute(
                f"SELECT `id` FROM `{SCHEMA}`.`booking`", {},
                max_rows=10, timeout_ms=1000)
            executor.execute(
                f"SELECT `id` FROM `{SCHEMA}`.`booking` WHERE `id` > 0", {},
                max_rows=10, timeout_ms=1000)
            with pytest.raises(ValidationStatementBudgetExceeded):
                executor.execute(
                    f"SELECT `id` FROM `{SCHEMA}`.`booking` WHERE `id` > 1",
                    {}, max_rows=10, timeout_ms=1000)
            assert budget.count == 2, "每条语句应只消耗一次额度"
        finally:
            executor.close()

    def test_deadline_blocks_statement_before_send(self, env):
        """总期限耗尽：语句发出前被拒（预算不消耗）。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor
        from app.relationships.execution import StatementBudget

        budget = StatementBudget(5)
        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.booking"}, {f"{SCHEMA}.booking": {"id"}},
            statement_budget=budget,
            deadline_monotonic=time.monotonic() - 1,
            attach=_attach_map(env))
        try:
            with pytest.raises(ValidationDeadlineExceededError):
                executor.execute(
                    f"SELECT `id` FROM `{SCHEMA}`.`booking`", {},
                    max_rows=10, timeout_ms=1000)
            assert budget.count == 0, "被期限拒绝的语句不应消耗预算"
        finally:
            executor.close()

    def test_byte_cap_marks_truncated(self, env):
        """结果字节上限：超限即停并如实标记截断。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor

        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.booking"}, {f"{SCHEMA}.booking": {"id"}},
            max_result_bytes=2, attach=_attach_map(env))
        try:
            rows, truncated = executor.execute(
                f"SELECT `id` FROM `{SCHEMA}`.`booking`", {},
                max_rows=10, timeout_ms=1000)
            assert truncated is True
            assert len(rows) >= 1  # 已取批次如实返回
        finally:
            executor.close()

    def test_validate_deadline_not_ready(self, env):
        """期限耗尽的验证运行不得进入 ready。"""
        from app.relationships.execution import StatementBudget
        from tests.dataset_fixtures import aggregate_definition

        service = make_service(env)
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))

        original = service._new_run_context

        def expired_context():
            return {"budget": StatementBudget(100),
                    "deadline": time.monotonic() - 1,
                    "max_result_bytes": None}

        service._new_run_context = expired_context  # type: ignore
        try:
            result = service.validate(
                dataset["dataset_id"], dataset["version"])
        finally:
            service._new_run_context = original  # type: ignore
        assert result["status"] == "failed_validation"

    def test_preview_receives_run_context(self, env):
        """预览执行器带运行上下文（预算/期限/字节上限）构建。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor
        from tests.dataset_fixtures import aggregate_definition

        captured: dict[str, Any] = {}
        attach = _attach_map(env)

        def factory(payload, model, cfg, run_context=None):
            captured["ctx"] = run_context
            tables = {
                f"{(e.get('source') or {}).get('schema')}."
                f"{(e.get('source') or {}).get('table')}".lower(): {
                    a["field"]["column"].lower()
                    for a in e.get("attributes", [])}
                for e in model.get("entities", [])}
            return SQLiteControlledExecutor(
                env["data_url"], set(tables.keys()), tables, attach=attach,
                statement_budget=(run_context or {}).get("budget"),
                deadline_monotonic=(run_context or {}).get("deadline"),
                max_result_bytes=(run_context or {}).get("max_result_bytes"))

        service = make_service(env)
        service._executor_factory = factory  # type: ignore
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        service.validate(dataset["dataset_id"], dataset["version"])
        captured.clear()
        service.preview(dataset["dataset_id"], dataset["version"])
        ctx = captured.get("ctx")
        assert ctx and all(ctx.get(k) is not None
                           for k in ("budget", "deadline", "max_result_bytes"))

    def test_mysql_executor_accepts_run_context(self):
        """MySQL 执行器接受并保存期限/字节上限（装配工厂传参不 TypeError）。"""
        from app.adapters.dataset_executors import MySQLControlledExecutor

        executor = MySQLControlledExecutor(
            host="127.0.0.1", port=3306, database="app",
            username="ro", password="secret",
            allowed_tables={"app.booking"},
            allowed_columns={"app.booking": {"id"}},
            statement_budget=None,
            deadline_monotonic=time.monotonic() + 60,
            max_result_bytes=1000)
        try:
            assert executor._deadline is not None
            assert executor._max_result_bytes == 1000
        finally:
            executor.close()

    def test_deps_factory_forwards_run_context(self, monkeypatch):
        """组装根工厂把 run_context 三要素转发给 MySQL 执行器。"""
        import app.adapters.dataset_executors as executors_module
        from app.api import deps

        recorded: dict[str, Any] = {}

        class Recorder:
            def __init__(self, **kwargs):
                recorded.update(kwargs)

            def close(self):
                return None

        monkeypatch.setattr(
            executors_module, "MySQLControlledExecutor", Recorder)

        class FakeSourceRepo:
            @staticmethod
            def get_source(source_id):
                return {"credential_ref": "cred-1", "host": "127.0.0.1",
                        "port": 3306, "database": "app", "username": "ro"}

        class FakeCredentialStore:
            @staticmethod
            def get(ref):
                return "secret"

        monkeypatch.setattr(deps, "_metadata_repository",
                            lambda: FakeSourceRepo())
        monkeypatch.setattr(deps, "_credential_store",
                            lambda: FakeCredentialStore())
        model = {"entities": [{"source": {"schema": "app",
                                          "table": "booking"},
                               "attributes": [
                                   {"field": {"column": "id"}}]}]}
        budget = object()
        deps._dataset_executor_factory(
            {"source_id": "s1"}, model, Settings(_env_file=None),
            run_context={"budget": budget, "deadline": 1.5,
                         "max_result_bytes": 99})
        assert recorded["statement_budget"] is budget
        assert recorded["deadline_monotonic"] == 1.5
        assert recorded["max_result_bytes"] == 99


# == 附加：ES 失败发布不保留 active 锁 ===========================================


class TestESLockRelease:
    def test_needs_reconciliation_releases_lock(self, env):
        """ES 发布外部索引已建但计数不符 → needs_reconciliation；
        锁必须释放（否则 reconcile 永远无法取得占用，恢复路径死锁）。"""
        from app.adapters.es_publisher import FakeESPublisher
        from tests.dataset_fixtures import aggregate_definition

        fake = FakeESPublisher(count_override=999)
        service = make_service(
            env, es_publisher_factory=lambda: fake,
            settings=_settings(
                dataset_es_publish_enabled=True,
                dataset_validation_rows=1000))
        definition = aggregate_definition(env, env["model"])
        definition["target"] = "elasticsearch"
        dataset = service.create_dataset(env["source_id"], definition)
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        with pytest.raises(ValueError, match="文档数与预期不符"):
            service.publish(dataset_id, version)
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        # 修复点：不保留 active 锁
        assert env["repo"].get_active_publish_lock(dataset_id) is None
        # 恢复路径可以取得占用并运行（不被残留锁拒绝）
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "expected_unknown"
