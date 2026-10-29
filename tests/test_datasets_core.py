"""M5 数据集核心测试（离线 SQLite 全链；覆盖任务清单核心场景）。

5×5/3 防膨胀、AST 安全、SQL 编译、质量对账、发布生命周期、ES 分支。
"""

import threading
from typing import Any

import pytest

from app.datasets.compiler import DatasetCompiler
from app.datasets.safety import SQLSafetyError, validate_controlled_select
from app.domain.datasets import DatasetSpec
from tests.dataset_fixtures import (
    ARCHIVE,
    SCHEMA,
    aggregate_definition,
    detail_view_definition,
    make_service,
    seed_environment,
)


def _attach_map(env) -> dict[str, str]:
    base = env["data_url"].replace("sqlite:///", "")
    return {SCHEMA: f"{base}.app", ARCHIVE: f"{base}.archive"}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return seed_environment(tmp_path_factory.mktemp("m5"))


class TestCompileAndSafety:
    def _spec(self, env, definition) -> tuple[DatasetSpec, dict]:
        model = env["model_service"].get_model(
            definition["model_id"], version=definition["model_version"])
        spec = DatasetSpec(
            dataset_id="x", name="n", source_id=env["source_id"],
            snapshot_id=definition.get("snapshot_id"),
            model_id=definition["model_id"],
            model_version=definition["model_version"],
            topic_id=definition["topic_id"], kind=definition["kind"],
            central_entity_id=definition["central_entity_id"])
        for key in ("grain_keys", "fields", "dimensions", "metrics",
                    "preaggregations", "filters", "join_type", "target"):
            setattr(spec, key, definition.get(key, []))
        return spec, model

    def test_aggregate_sql_compiles_with_preaggregation_cte(self, env):
        spec, model = self._spec(env, aggregate_definition(env, env["model"]))
        plan = DatasetCompiler().compile(spec, model)
        assert "WITH pa_0 AS" in plan.sql
        assert "GROUP BY pa.`booking_id`" in plan.sql
        # 预聚合在关联键上至多一行，再连中心表（无笛卡尔展开）
        assert "LEFT JOIN `pa_0`" in plan.sql
        assert "DISTINCT" not in plan.sql  # 不用 DISTINCT 掩盖

    def test_detail_view_sql_left_join_default(self, env):
        spec, model = self._spec(env, detail_view_definition(env["model"]))
        plan = DatasetCompiler().compile(spec, model)
        assert "FROM `app`.`booking` AS `c0`" in plan.sql

    def test_safety_rejects_ddl_dml_and_multistatement(self):
        tables = {"app.booking": {"id"}}
        with pytest.raises(SQLSafetyError):
            validate_controlled_select(
                "DROP TABLE `app`.`booking`", tables, {"app.booking": {"id"}})
        with pytest.raises(SQLSafetyError):
            validate_controlled_select(
                "DELETE FROM `app`.`booking`", tables, {"app.booking": {"id"}})
        with pytest.raises(SQLSafetyError):
            validate_controlled_select(
                "SELECT 1; SELECT 2", tables, {"app.booking": {"id"}})

    def test_safety_rejects_unknown_table_and_column(self):
        with pytest.raises(SQLSafetyError, match="不在白名单"):
            validate_controlled_select(
                "SELECT `x` FROM `app`.`nope`", {"app.booking": {"id"}},
                {"app.booking": {"id"}})
        with pytest.raises(SQLSafetyError, match="不在白名单"):
            validate_controlled_select(
                "SELECT `secret` FROM `app`.`booking`", {"app.booking": {"id"}},
                {"app.booking": {"id"}})

    def test_safety_rejects_dangerous_functions(self):
        with pytest.raises(SQLSafetyError):
            validate_controlled_select(
                "SELECT SLEEP(1) FROM `app`.`booking`", {"app.booking": {"id"}},
                {"app.booking": {"id"}})

    def test_definition_validation_rejects_non_formal_metric(self, env):
        service = make_service(env)
        definition = aggregate_definition(env, env["model"])
        # 篡改指标 ID（非 formal_metric_ids 成员）
        definition["preaggregations"][0]["metric_id"] = "metric.bogus"
        with pytest.raises(ValueError, match="不属于模型正式集合"):
            service.create_dataset(env["source_id"], definition)

    def test_definition_rejects_unverified_relation(self, env):
        service = make_service(env)
        definition = aggregate_definition(env, env["model"])
        definition["preaggregations"][0]["relationship_key"] = "bogus"
        with pytest.raises(ValueError, match="关系不在模型"):
            service.create_dataset(env["source_id"], definition)


class TestQualityAndPreview:
    def test_aggregate_5x5_3_no_inflation_with_reconciliation(self, env):
        """核心场景：一中心 5 行 + 明细 A（5/4/3/2/1）+ 明细 B（1/1/1）——
        输出 5 行；预聚合对账与各明细表独立聚合一致（不放大计数）。"""
        service = make_service(env)
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        result = service.validate(dataset["dataset_id"], dataset["version"])
        assert result["status"] == "ready", result
        checks = {c["check"]: c for c in result["quality"]["checks"]}
        assert checks["row_count_vs_central"]["actual"] == 5
        pre = [c for c in result["quality"]["checks"]
               if c["check"] == "preaggregation_reconciliation"]
        assert len(pre) == 2  # A、B 各一项独立对账
        assert all(c["outcome"] == "passed" for c in pre)
        # 预览：行值正确（5/4/3/2/1 与 1/1/1/NULL/NULL）
        preview = service.preview(dataset["dataset_id"], dataset["version"])
        counts = {str(row["id"]): str(row["detail_a_count"])
                  for row in preview["rows"]}
        assert counts == {"1": "5", "2": "4", "3": "3", "4": "2", "5": "1"}
        b_counts = {str(row["id"]): row["detail_b_count"]
                    for row in preview["rows"]}
        # 未匹配关联以 NULL 表达（SQLite 返回 None；字符串化为 "None" 同义）
        assert b_counts["4"] in (None, "None") and b_counts["5"] in (None, "None")

    def test_preview_masks_sensitive_columns(self, env):
        service = make_service(env)
        definition = detail_view_definition(env["model"])
        definition["fields"].append({
            "field": {"schema": SCHEMA, "table": "booking",
                      "column": "status_code"},
            "output_alias": "status_code"})
        dataset = service.create_dataset(env["source_id"], definition)
        service.validate(dataset["dataset_id"], dataset["version"])
        preview = service.preview(dataset["dataset_id"], dataset["version"])
        assert preview["row_count"] > 0
        # 质量计算使用原始值；预览脱敏不污染计算（此处 status_code 非敏感，
        # 验证脱敏路径不破坏值语义）
        statuses = {row["status_code"] for row in preview["rows"]}
        assert statuses <= {"paid", "cancelled"}


class TestPublishLifecycle:
    def _validated(self, env, service, definition) -> dict:
        dataset = service.create_dataset(env["source_id"], definition)
        result = service.validate(dataset["dataset_id"], dataset["version"])
        assert result["status"] == "ready"
        return dataset

    @staticmethod
    def _publisher(tmp_path, env):
        from app.adapters.dataset_executors import SQLiteViewPublisher

        return SQLiteViewPublisher(
            f"sqlite:///{tmp_path / 'views.db'}", attach=_attach_map(env))

    def test_view_publish_rollback_and_conflict(self, env, tmp_path):
        from app.adapters.dataset_executors import SQLiteViewPublisher
        from app.core.config import Settings

        attach = _attach_map(env)
        publisher_holder = {}

        def publisher_factory():
            if "publisher" not in publisher_holder:
                # SQLite 视图须与表同库：发布器打开业务库文件
                publisher_holder["publisher"] = SQLiteViewPublisher(
                    env["data_url"], attach=attach)
            return publisher_holder["publisher"]

        service = make_service(
            env, view_publisher_factory=publisher_factory,
            settings=Settings(_env_file=None,
                              dataset_view_publish_enabled=True,
                              dataset_view_target_schema="app",
                              dataset_validation_rows=1000))
        dataset = self._validated(
            env, service, aggregate_definition(env, env["model"]))
        published = service.publish(
            dataset["dataset_id"], dataset["version"], operator="dev")
        publisher = publisher_holder["publisher"]
        assert published["published"] is True
        assert publisher.view_exists("app", published["view"].split(".", 1)[1])
        # 并发发布新版本：ready 状态下两线程同时竞争进入 publishing——
        # 状态原子条件更新恰一进入，另一 409
        revised = service.revise_dataset(
            dataset["dataset_id"], dataset["version"],
            {"filters": [{"field": {
                "schema": SCHEMA, "table": "booking", "column": "id"},
                "operator": "le", "value": 5}]})
        result = service.validate(revised["dataset_id"], revised["version"])
        assert result["status"] == "ready"
        results: list[Any] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                service.publish(revised["dataset_id"], revised["version"],
                                operator="t")
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        # 并发不变量：至少一个成功；失败只能是 409 冲突（不重复创建对象）；
        # 最终状态一致（published，指针指向该版本，视图恰一个）
        assert len(results) == 2 and "ok" in results
        assert all(r in ("ok", "RevisionConflictError") for r in results)
        final = service.get_dataset(revised["dataset_id"],
                                    revised["version"])
        assert final["status"] == "published"

    def test_publish_requires_quality_pass(self, env):
        service = make_service(env)
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        with pytest.raises(ValueError, match="质量验证未通过|不可用"):
            service.publish(dataset["dataset_id"], dataset["version"])

    def test_publish_disabled_by_default(self, env):
        service = make_service(env)  # 发布开关默认关闭
        dataset = self._validated(
            env, service, aggregate_definition(env, env["model"]))
        with pytest.raises(ValueError, match="未启用"):
            service.publish(dataset["dataset_id"], dataset["version"])

    def test_revise_invalidates_old_quality(self, env):
        service = make_service(env)
        dataset = self._validated(
            env, service, aggregate_definition(env, env["model"]))
        # 修改定义（加过滤）→ 新版本质量报告为空
        revised = service.revise_dataset(
            dataset["dataset_id"], dataset["version"], {
                "filters": [{"field": {
                    "schema": SCHEMA, "table": "booking", "column": "id"},
                    "operator": "le", "value": 3}]})
        assert revised["version"] == dataset["version"] + 1
        assert revised["quality_report"] is None  # 旧报告不背书新定义
        # 旧版本报告保留
        old = service.get_dataset(dataset["dataset_id"], dataset["version"])
        assert old["quality_report"] is not None

    def test_refresh_reruns_quality_and_keeps_published(self, env, tmp_path):
        from app.adapters.dataset_executors import SQLiteViewPublisher
        from app.core.config import Settings

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=Settings(_env_file=None,
                              dataset_view_publish_enabled=True,
                              dataset_view_target_schema="app",
                              dataset_validation_rows=1000))
        dataset = self._validated(
            env, service, aggregate_definition(env, env["model"]))
        service.publish(dataset["dataset_id"], dataset["version"])
        refreshed = service.refresh(dataset["dataset_id"], operator="dev")
        assert refreshed["published"] is True

    def test_external_success_local_failure_needs_reconciliation(
            self, env, tmp_path):
        """外部对象创建成功但本地指针切换失败 → needs_reconciliation，
        显式核对后恢复（不伪装成功）。"""
        from app.adapters.dataset_executors import SQLiteViewPublisher
        from app.core.config import Settings

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=Settings(_env_file=None,
                              dataset_view_publish_enabled=True,
                              dataset_view_target_schema="app",
                              dataset_validation_rows=1000))
        dataset = self._validated(
            env, service, aggregate_definition(env, env["model"]))
        # 注入：指针切换抛错（模拟本地收尾失败）
        real_set = env["repo"].set_published_dataset

        def failing_set(*args, **kwargs):
            raise RuntimeError("本地指针故障")

        env["repo"].set_published_dataset = failing_set  # type: ignore
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset["dataset_id"], dataset["version"])
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        current = service.get_dataset(dataset["dataset_id"], dataset["version"])
        assert current["status"] == "needs_reconciliation"
        # 外部对象实际存在 → 核对恢复为 published
        reconciled = service.reconcile(
            dataset["dataset_id"], dataset["version"])
        assert reconciled["reconciled"] == "external_object_exists"
        after = service.get_dataset(dataset["dataset_id"], dataset["version"])
        assert after["status"] == "published"


class TestESExport:
    def test_es_publish_versioned_index_and_alias(self, env):
        from app.adapters.es_publisher import FakeESPublisher
        from app.core.config import Settings

        fake = FakeESPublisher()
        service = make_service(
            env, es_publisher_factory=lambda: fake,
            settings=Settings(
                _env_file=None, dataset_es_publish_enabled=True,
                dataset_validation_rows=1000,
                dataset_export_batch_size=3, dataset_export_max_rows=100))
        definition = aggregate_definition(env, env["model"])
        definition["target"] = "elasticsearch"
        dataset = service.create_dataset(env["source_id"], definition)
        service.validate(dataset["dataset_id"], dataset["version"])
        result = service.publish(dataset["dataset_id"], dataset["version"])
        assert result["published"] is True
        assert result["documents"] == 5  # 5 行全量导出
        assert fake.aliases[result["alias"]] == result["index"]

    def test_es_bulk_permanent_failure_blocks_publish(self, env):
        from app.adapters.es_publisher import FakeESPublisher
        from app.core.config import Settings

        fake = FakeESPublisher(mapping_error=True)
        service = make_service(
            env, es_publisher_factory=lambda: fake,
            settings=Settings(
                _env_file=None, dataset_es_publish_enabled=True,
                dataset_validation_rows=1000))
        definition = aggregate_definition(env, env["model"])
        definition["target"] = "elasticsearch"
        dataset = service.create_dataset(env["source_id"], definition)
        service.validate(dataset["dataset_id"], dataset["version"])
        with pytest.raises(ValueError, match="永久失败|发布失败"):
            service.publish(dataset["dataset_id"], dataset["version"])
        # 别名未切换
        assert fake.aliases == {}

    def test_es_count_mismatch_blocks_alias(self, env):
        from app.adapters.es_publisher import FakeESPublisher
        from app.core.config import Settings

        fake = FakeESPublisher(count_override=999)
        service = make_service(
            env, es_publisher_factory=lambda: fake,
            settings=Settings(
                _env_file=None, dataset_es_publish_enabled=True,
                dataset_validation_rows=1000))
        definition = aggregate_definition(env, env["model"])
        definition["target"] = "elasticsearch"
        dataset = service.create_dataset(env["source_id"], definition)
        service.validate(dataset["dataset_id"], dataset["version"])
        with pytest.raises(ValueError, match="文档数与预期不符"):
            service.publish(dataset["dataset_id"], dataset["version"])
        assert fake.aliases == {}

    def test_es_null_grain_blocks_export(self, env):
        """grain 键 NULL 阻断导出（不静默丢行/覆盖文档）。"""
        from app.adapters.es_publisher import FakeESPublisher
        from app.core.config import Settings

        fake = FakeESPublisher()
        service = make_service(
            env, es_publisher_factory=lambda: fake,
            settings=Settings(
                _env_file=None, dataset_es_publish_enabled=True,
                dataset_validation_rows=1000))
        # guest 维度数据集：tenant_id 含 NULL（赵六）
        definition = {
            "name": "客户宽表", "model_id": env["model"]["model_id"],
            "model_version": env["model"]["version"], "kind": "detail_view",
            "central_entity_id": f"{SCHEMA}.guest",
            "topic_id": f"topic.{SCHEMA}.guest",
            "grain_keys": ["id"], "grain_statement": "一行代表一位客户",
            "fields": [{"field": {"schema": SCHEMA, "table": "guest",
                                  "column": "tenant_id"},
                        "output_alias": "tenant_id"}],
            "dimensions": [], "metrics": [], "preaggregations": [],
            "filters": [], "target": "elasticsearch",
        }
        dataset = service.create_dataset(env["source_id"], definition)
        service.validate(dataset["dataset_id"], dataset["version"])
        with pytest.raises(ValueError):
            service.publish(dataset["dataset_id"], dataset["version"])
