"""M5 恢复修复轮回归测试（三组问题；全部离线 SQLite + ES 替身 + 故障注入）。

修复一（恢复依据）：A 完整 ES 发布恢复 / B 空数据(expected=0) / C 不完整导出
修复二（待恢复占用）：D pending 阻断绕过 / E 历史恢复不覆盖新发布 /
    F 恢复并发认领 / G 恢复事务回滚
修复三（期限与字节）：H 最后一页超期 / I 字节预算 / J 单次 timeout_ms /
    K 生产装配路径
既有行为（L）：普通发布/回滚/刷新新版本等由既有测试文件覆盖，另含冒烟。
"""

import threading
import time
from typing import Any

import pytest

from app.adapters.dataset_executors import SQLiteControlledExecutor
from app.core.config import Settings
from app.core.exceptions import (
    RevisionConflictError,
    ValidationDeadlineExceededError,
    ValidationQueryTimeoutError,
    ValidationResultBudgetExceededError,
)
from tests.dataset_fixtures import (
    SCHEMA,
    aggregate_definition,
    detail_view_definition,
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


def _es_service(env, fake, **settings_overrides) -> Any:
    return make_service(
        env, es_publisher_factory=lambda: fake,
        settings=_settings(dataset_es_publish_enabled=True,
                           dataset_validation_rows=1000,
                           **settings_overrides))


def _es_definition(env) -> dict[str, Any]:
    definition = aggregate_definition(env, env["model"])
    definition["target"] = "elasticsearch"
    return definition


def _published_run(repo, dataset_id: str) -> dict[str, Any]:
    runs = [r for r in repo.list_dataset_runs(dataset_id)
            if r.get("run_type") == "publish"]
    assert runs, "缺少发布运行记录"
    return runs[0]


def _inject_pointer_failure(env):
    """注入本地指针提交失败（外部副作用全部成功后）。"""
    real_set = env["repo"].set_published_dataset

    def failing_set(*args, **kwargs):
        raise RuntimeError("本地指针故障")

    env["repo"].set_published_dataset = failing_set  # type: ignore
    return real_set


# == 修复一：恢复依据 ============================================================


class TestFullESRecovery:
    def test_external_complete_local_failure_recovers(self, env):
        """A：5 条全部导出+count 正确+alias 已切换，本地提交失败 →
        恢复依据已持久化 → 取消故障后 reconcile 成功。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)

        real_set = _inject_pointer_failure(env)
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore

        # 恢复依据已持久化（不是只存在于内存 run 对象）
        run = _published_run(env["repo"], dataset_id)
        persisted = env["repo"].get_dataset_run(run["run_id"])
        recovery = persisted["result"]["recovery"]
        assert recovery["export_complete"] is True
        assert recovery["expected_documents"] == 5
        assert recovery["count_verified"] is True
        assert recovery["phase"] == "alias_switched"
        assert recovery["previous_pointer_recorded"] is True
        assert recovery["previous_pointer"] is None
        assert recovery["index"] and recovery["alias"]

        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

        # 故障取消后核对恢复：指针正确、状态 published、占用完成
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_index_complete"
        assert reconciled["pointer_restored"] is True
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version
        assert service.get_dataset(dataset_id, version)["status"] \
            == "published"
        assert env["repo"].get_active_publish_lock(dataset_id) is None
        # alias 未被核对重复切换
        assert len(fake.alias_history) == 1
        assert fake.aliases[recovery["alias"]] == recovery["index"]


class TestEmptyDataRecovery:
    def test_expected_documents_zero_recovers(self, env):
        """B：expected_documents=0 是有效值（完整导出 0 行可恢复；
        不能按 truthy 把 0 当未知）。清空中心表使数据集合法为空
        （过滤出空窗口会被行数对账正确拒绝，属另一语义）。"""
        from sqlalchemy import create_engine, text

        base = env["data_url"].replace("sqlite:///", "")
        engine = create_engine(env["data_url"])
        with engine.begin() as conn:
            conn.execute(text(f"ATTACH DATABASE '{base}.app' AS app"))
            conn.execute(text("DELETE FROM app.booking"))
        engine.dispose()

        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        definition = detail_view_definition(env["model"])
        definition["target"] = "elasticsearch"
        dataset = service.create_dataset(env["source_id"], definition)
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        result = service.validate(dataset_id, version)
        assert result["status"] == "ready", result["quality"]

        real_set = _inject_pointer_failure(env)
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        run = _published_run(env["repo"], dataset_id)
        recovery = env["repo"].get_dataset_run(run["run_id"])["result"][
            "recovery"]
        assert recovery["export_complete"] is True
        assert recovery["expected_documents"] == 0

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_index_complete"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version
        assert service.get_dataset(dataset_id, version)["status"] \
            == "published"


class TestIncompleteExportNotRecovered:
    def test_bulk_failure_keeps_pending_not_published(self, env):
        """C：bulk 中途失败（导出未完整）——即使索引存在也不恢复。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher(bulk_failure_items=1)
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        with pytest.raises(ValueError, match="永久失败|发布失败"):
            service.publish(dataset_id, version)
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        run = _published_run(env["repo"], dataset_id)
        recovery = env["repo"].get_dataset_run(run["run_id"])["result"][
            "recovery"]
        assert recovery["export_complete"] is False
        assert recovery["expected_documents"] is None

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "export_incomplete"
        after = service.get_dataset(dataset_id, version)
        assert after["status"] == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None


# == 修复二：待恢复占用 ==========================================================


class TestPendingOccupancy:
    def test_pending_blocks_publish_and_rollback(self, env):
        """D：v1 外部成功本地失败进入 pending 后，v2 发布与回滚均被拒绝，
        且没有新的外部副作用。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        definition = _es_definition(env)
        dataset = service.create_dataset(env["source_id"], definition)
        dataset_id, v1 = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, v1)
        service.publish(dataset_id, v1)  # v1 正常发布（指针/别名就位）
        v1_index = fake.aliases[
            f"nl2sql_wide_{dataset_id}"]
        alias_name = f"nl2sql_wide_{dataset_id}"

        # v2 发布：count 不符 → 外部索引已建，进入 pending
        fake._count_override = 999  # type: ignore[attr-defined]
        revised = service.revise_dataset(dataset_id, v1, {"join_type": "left"})
        v2 = revised["version"]
        service.validate(dataset_id, v2)
        with pytest.raises(ValueError, match="文档数与预期不符"):
            service.publish(dataset_id, v2)
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

        # 回滚被拒绝（占用中；且不切换别名）
        with pytest.raises(RevisionConflictError):
            service.rollback(dataset_id, v1)
        # v2 重发布被拒绝
        with pytest.raises(Exception):  # noqa: B017 - 状态/占用二重拒绝
            service.publish(dataset_id, v2)
        assert fake.aliases[alias_name] == v1_index, "被拒绝的操作切了别名"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == v1


class TestHistoricalRecoveryNoOverwrite:
    def test_legacy_needs_reconciliation_rejected_and_new_publish_kept(
            self, env):
        """E：旧运行无占用（历史数据），当前指针已是 v2——
        reconcile(v1) 保守拒绝，不修改 v2 指针与别名。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        definition = _es_definition(env)
        dataset = service.create_dataset(env["source_id"], definition)
        dataset_id, v1 = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, v1)
        service.publish(dataset_id, v1)
        # v2 正常发布 → 指针 v2
        revised = service.revise_dataset(dataset_id, v1, {"join_type": "left"})
        v2 = revised["version"]
        service.validate(dataset_id, v2)
        service.publish(dataset_id, v2)
        alias_name = f"nl2sql_wide_{dataset_id}"
        v2_index = fake.aliases[alias_name]

        # 历史场景：直接构造 v1 needs_reconciliation（无 pending 占用）
        env["repo"].update_dataset_status(dataset_id, v1, "publishing")
        env["repo"].update_dataset_status(dataset_id, v1,
                                          "needs_reconciliation")
        with pytest.raises(RevisionConflictError):
            service.reconcile(dataset_id, v1)
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == v2
        assert fake.aliases[alias_name] == v2_index
        assert service.get_dataset(dataset_id, v1)["status"] \
            == "needs_reconciliation"

    def test_pointer_moved_conflict_keeps_pointer(self, env):
        """E'：有恢复依据但指针已被移到其他版本 → 明确冲突，
        不改写指针、不切别名，占用退回 pending。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        real_set = _inject_pointer_failure(env)
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        # 指针被外部移到 v999（≠ previous_pointer=None）
        assert env["repo"].set_published_dataset(
            env["source_id"], dataset_id, 999, operator="external")

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "pointer_conflict"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == 999
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"


class TestRecoveryConcurrency:
    def test_two_threads_exactly_one_claims(self, env):
        """F：两个真实线程竞争同一 pending——恰一认领，失败方不改结果。"""
        repo = env["repo"]
        dataset_id = "ds-claim-race"
        assert repo.acquire_publish_lock(dataset_id, "op-race", "publish")
        assert repo.transition_publish_lock(
            dataset_id, "op-race", "active", "pending")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            results.append(repo.claim_publish_reconciliation(
                dataset_id, "op-race"))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(results) == [False, True]
        lock = repo.get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "recovering"


class TestRecoveryTransaction:
    def test_status_update_failure_rolls_back_everything(self, env, monkeypatch):
        """G：恢复提交事务中注入故障——指针/状态/审计/占用无部分提交。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        real_set = _inject_pointer_failure(env)
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        assert env["repo"].get_active_publish_lock(dataset_id)["status"] \
            == "pending"

        # 注入：事务内状态更新失败（指针已写但状态步骤抛错 → 全部回滚）
        def failing_status(*args, **kwargs):
            raise RuntimeError("状态更新故障")

        monkeypatch.setattr(
            env["repo"], "_set_dataset_status_in_conn", failing_status)
        with pytest.raises(RuntimeError, match="状态更新故障"):
            service.reconcile(dataset_id, version)
        monkeypatch.undo()

        # 无部分提交：指针未写、状态未变、无恢复审计、占用退回 pending
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        audits = [row for row in env["repo"]._aux_rows(
            dataset_id, "publish_log")
            if row.get("action") == "reconcile"]
        assert audits == []
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

        # 故障移除后同一恢复可再次成功（pending 可再认领）
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_index_complete"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version

    def test_repeat_reconcile_no_duplicate_audit(self, env):
        """同一恢复重复提交不重复插入审计（幂等）。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        real_set = _inject_pointer_failure(env)
        try:
            with pytest.raises(RuntimeError, match="本地指针故障"):
                service.publish(dataset_id, version)
        finally:
            env["repo"].set_published_dataset = real_set  # type: ignore
        assert service.reconcile(dataset_id, version)[
            "reconciled"] == "external_index_complete"
        # 指针已就位后再跑完整恢复提交：already_committed 幂等
        run = _published_run(env["repo"], dataset_id)
        outcome = env["repo"].complete_dataset_reconciliation(
            env["source_id"], dataset_id, version, run["run_id"], None)
        assert outcome == "already_committed"
        audits = [row for row in env["repo"]._aux_rows(
            dataset_id, "publish_log")
            if row.get("action") == "reconcile"]
        assert len(audits) == 1


# == 修复三：期限与字节 ==========================================================


class TestLastPageExpiry:
    def test_last_page_expired_no_bulk_no_alias_no_publish(self, env):
        """H：第二页（最后 2 行）查询越过截止——不 bulk、不切别名、
        不提交指针、不返回 published=True。"""
        from app.adapters.es_publisher import FakeESPublisher

        fake = FakeESPublisher()
        attach = _attach_map(env)
        deadline_box = {"deadline": None}
        execute_calls = {"n": 0}

        def factory(payload, model, cfg, run_context=None):
            tables = {
                f"{(e.get('source') or {}).get('schema')}."
                f"{(e.get('source') or {}).get('table')}".lower(): {
                    a["field"]["column"].lower()
                    for a in e.get("attributes", [])}
                for e in model.get("entities", [])}
            return _SlowPageExecutor(
                env["data_url"], set(tables.keys()), tables,
                attach=attach, slow_on_call=2,
                deadline_box=deadline_box,
                statement_budget=(run_context or {}).get("budget"),
                deadline_monotonic=(run_context or {}).get("deadline"),
                max_result_bytes=(run_context or {}).get("max_result_bytes"),
                execute_calls=execute_calls)

        from app.services.dataset_service import DatasetService

        service = DatasetService(
            metadata_repository=env["repo"],
            semantic_models=env["model_service"],
            settings=_settings(dataset_es_publish_enabled=True,
                               dataset_validation_rows=1000,
                               dataset_export_batch_size=3),
            executor_factory=factory,
            es_publisher_factory=lambda: fake)
        original_context = service._new_run_context
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)  # 正常期限验证通过

        def short_context():
            context = original_context()
            context["deadline"] = time.monotonic() + 0.35
            deadline_box["deadline"] = context["deadline"]
            return context

        # 只在发布阶段启用短期限与"慢第二页"（验证阶段不受影响）
        execute_calls["n"] = 0
        service._new_run_context = short_context  # type: ignore
        try:
            with pytest.raises(ValidationDeadlineExceededError):
                service.publish(dataset_id, version)
        finally:
            service._new_run_context = original_context  # type: ignore

        assert execute_calls["n"] == 2, "应在第二页查询后停止"
        assert fake.bulk_calls == 1, "第二页超期后不得 bulk"
        assert fake.aliases == {}, "不得切换别名"
        # ES 单请求超时不超过运行剩余期限（deadline=0.35s → ≤350ms）
        assert fake.request_timeouts, "ES 调用应携带剩余期限超时"
        assert all(t is not None and t <= 400
                   for t in fake.request_timeouts), fake.request_timeouts
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None
        after = service.get_dataset(dataset_id, version)
        assert after["status"] != "published"
        # 已发生外部副作用（索引+首批 bulk）→ 进入待核对而非静默成功
        assert after["status"] == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"


class _SlowPageExecutor(SQLiteControlledExecutor):
    """第 N 次查询前越过期限（模拟最后一页查询耗时超过剩余期限）。"""

    def __init__(self, *args, slow_on_call: int = 2,
                 deadline_box: dict | None = None,
                 execute_calls: dict | None = None, **kwargs):
        self._slow_on_call = slow_on_call
        self._deadline_box = deadline_box or {}
        self._execute_calls = execute_calls if execute_calls is not None \
            else {"n": 0}
        super().__init__(*args, **kwargs)

    def execute(self, sql, params, *, max_rows, timeout_ms):
        self._execute_calls["n"] += 1
        deadline = self._deadline_box.get("deadline")
        if self._execute_calls["n"] == self._slow_on_call and deadline:
            while time.monotonic() < deadline + 0.1:
                time.sleep(0.02)
        return super().execute(sql, params, max_rows=max_rows,
                               timeout_ms=timeout_ms)


class TestByteBudget:
    def test_ascii_under_cap_passes(self, env):
        from app.adapters.dataset_executors import SQLiteControlledExecutor

        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.booking"},
            {f"{SCHEMA}.booking": {"booking_no"}}, attach=_attach_map(env))
        try:
            rows, truncated = executor.execute(
                f"SELECT `booking_no` FROM `{SCHEMA}`.`booking`", {},
                max_rows=10, timeout_ms=5000)
            assert len(rows) == 5 and not truncated
        finally:
            executor.close()

    def test_chinese_counted_as_utf8_bytes(self, env):
        """中文按 UTF-8 字节数计：字符数达标但字节数超限必须拒绝。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor

        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.guest"},
            {f"{SCHEMA}.guest": {"guest_name"}}, attach=_attach_map(env))
        try:
            # 5 个名字共 13 个字符、37 个 UTF-8 字节：字符口径会放行
            rows, _ = executor.execute(
                f"SELECT `guest_name` FROM `{SCHEMA}`.`guest`", {},
                max_rows=10, timeout_ms=5000)
            assert sum(len(str(r["guest_name"])) for r in rows) == 13
            assert sum(len(str(r["guest_name"]).encode("utf-8"))
                       for r in rows) == 37
            executor._max_result_bytes = 30
            with pytest.raises(ValidationResultBudgetExceededError):
                executor.execute(
                    f"SELECT `guest_name` FROM `{SCHEMA}`.`guest`", {},
                    max_rows=10, timeout_ms=5000)
        finally:
            executor.close()

    def test_single_row_over_cap_rejected_immediately(self, env):
        from app.adapters.dataset_executors import SQLiteControlledExecutor

        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.guest"},
            {f"{SCHEMA}.guest": {"guest_name"}},
            max_result_bytes=5, attach=_attach_map(env))
        try:
            # 单行"张三"=6 字节 > 5：首行即拒绝，不返回任何超限数据
            with pytest.raises(ValidationResultBudgetExceededError):
                executor.execute(
                    f"SELECT `guest_name` FROM `{SCHEMA}`.`guest`", {},
                    max_rows=10, timeout_ms=5000)
        finally:
            executor.close()


class TestSingleQueryTimeout:
    def test_timeout_ms_exceeded_with_run_deadline_remaining(self, env):
        """J：运行总期限尚有余量，单次查询超过 timeout_ms →
        失败（可注入时钟确定性触发），不返回完整结果。"""
        from app.adapters.dataset_executors import SQLiteControlledExecutor

        executor = SQLiteControlledExecutor(
            env["data_url"], {f"{SCHEMA}.booking"},
            {f"{SCHEMA}.booking": {"id"}}, attach=_attach_map(env))
        try:
            # 时钟注入：每次读数前进 50ms；timeout_ms=100 → 第 3 次读数越限
            tick = {"t": 1000.0}

            def fast_clock() -> float:
                tick["t"] += 0.05
                return tick["t"]

            executor._monotonic = fast_clock
            with pytest.raises(ValidationQueryTimeoutError):
                executor.execute(
                    f"SELECT `id` FROM `{SCHEMA}`.`booking`", {},
                    max_rows=10, timeout_ms=100)
            # 运行总期限未设置：不得误报为运行超期
        finally:
            executor.close()

    def test_timeout_hint_injected_for_mysql(self):
        """MySQL 方言：单查询期限注入服务端优化器提示（SELECT 前缀）。"""
        from app.adapters.dataset_executors import _mysql_timeout_hint

        hinted = _mysql_timeout_hint("SELECT `id` FROM `app`.`booking`", 250)
        assert "MAX_EXECUTION_TIME(250)" in hinted
        assert hinted.startswith("SELECT /*+")
        # WITH/CTE 语句不注入（提示仅对顶层 SELECT 生效）
        with_sql = "WITH c AS (SELECT 1) SELECT * FROM c"
        assert _mysql_timeout_hint(with_sql, 250) == with_sql


class TestProductionAssemblyPath:
    def test_deps_factory_context_affects_behavior(self, env, monkeypatch):
        """K：deps 正式执行器工厂构建的执行器，运行上下文真实生效
        （不只得断言构造参数）：过期期限阻断执行、字节上限拒绝结果。"""
        import app.adapters.dataset_executors as executors_module
        from app.adapters.dataset_executors import SQLiteControlledExecutor
        from app.api import deps

        attach = _attach_map(env)

        class DepsSqliteExecutor(SQLiteControlledExecutor):
            # 与 MySQLControlledExecutor 同参签名，SQLite 实现
            def __init__(self, host, port, database, username, password,
                         allowed_tables, allowed_columns,
                         statement_budget=None, connect_timeout=10,
                         read_timeout=30, deadline_monotonic=None,
                         max_result_bytes=None):
                super().__init__(
                    env["data_url"], allowed_tables, allowed_columns,
                    statement_budget=statement_budget, attach=attach,
                    deadline_monotonic=deadline_monotonic,
                    max_result_bytes=max_result_bytes)

        monkeypatch.setattr(
            executors_module, "MySQLControlledExecutor", DepsSqliteExecutor)

        class FakeSourceRepo:
            @staticmethod
            def get_source(source_id):
                return {"credential_ref": "cred-1", "host": "127.0.0.1",
                        "port": 3306, "database": SCHEMA, "username": "ro"}

        class FakeCredentialStore:
            @staticmethod
            def get(ref):
                return "secret"

        monkeypatch.setattr(deps, "_metadata_repository",
                            lambda: FakeSourceRepo())
        monkeypatch.setattr(deps, "_credential_store",
                            lambda: FakeCredentialStore())
        model = {"entities": [{
            "source": {"schema": SCHEMA, "table": "guest"},
            "attributes": [{"field": {"column": "id"}},
                           {"field": {"column": "guest_name"}}]}]}
        payload = {"source_id": "s1"}
        sql = f"SELECT `guest_name` FROM `{SCHEMA}`.`guest`"

        # 期限已过：语句发出前被拒
        executor = deps._dataset_executor_factory(
            payload, model, Settings(_env_file=None),
            run_context={"budget": None,
                         "deadline": time.monotonic() - 1,
                         "max_result_bytes": None})
        try:
            with pytest.raises(ValidationDeadlineExceededError):
                executor.execute(sql, {}, max_rows=5, timeout_ms=5000)
        finally:
            executor.close()

        # 字节上限：超限结果被拒绝（中文 UTF-8 口径）
        executor = deps._dataset_executor_factory(
            payload, model, Settings(_env_file=None),
            run_context={"budget": None, "deadline": None,
                         "max_result_bytes": 10})
        try:
            with pytest.raises(ValidationResultBudgetExceededError):
                executor.execute(sql, {}, max_rows=5, timeout_ms=5000)
        finally:
            executor.close()


# == L：既有正确行为冒烟 =========================================================


class TestExistingBehaviorSmoke:
    def test_view_publish_and_rollback_still_work(self, env, tmp_path):
        from app.adapters.dataset_executors import SQLiteViewPublisher

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=_settings(
                dataset_view_publish_enabled=True,
                dataset_view_target_schema="app",
                dataset_validation_rows=1000))
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        service.publish(dataset_id, version)
        assert service.get_dataset(dataset_id, version)["status"] == "published"
        rolled = service.rollback(dataset_id, version)
        assert rolled["rolled_back"] is True

    def test_refresh_creates_new_version_and_publishes(self, env):
        from app.adapters.dataset_executors import SQLiteViewPublisher

        publisher = SQLiteViewPublisher(
            env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=_settings(
                dataset_view_publish_enabled=True,
                dataset_view_target_schema="app",
                dataset_validation_rows=1000))
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        dataset_id = dataset["dataset_id"]
        service.validate(dataset_id, dataset["version"])
        service.publish(dataset_id, dataset["version"])
        refreshed = service.refresh(dataset_id, operator="dev")
        assert refreshed["refreshed"] is True
        assert refreshed["version"] > dataset["version"]
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == refreshed["version"]
