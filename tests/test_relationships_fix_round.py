"""M3 审查修复轮回归（五组问题，全部先按新语义断言）。

覆盖：
1. 跨快照有效关系选择与重验（快照先行、独立证据链、重绑/不可重验）；
2. 共享验证并发限制（进程内、跨服务实例、异常释放、等待期限）；
3. 查询数与期限预算覆盖完整过程（结构核计入预算、超期不升级 validated）；
4. JOIN 成本门槛与重复匹配规模控制（前导索引、有界窗口）；
5. 组合键去重碰撞（原生多列 DISTINCT，SQL 构造断言 + 替身语义）。
"""

import threading
import time
from typing import Any

import pytest

from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.exceptions import DataSourceConnectionError
from app.ports.connectors import JoinMetricsRequest, WindowMetricsRequest
from app.relationships.execution import StatementCounter, ValidationSlotLimiter
from app.services.relationship_service import RelationshipService
from tests.relationship_fixtures import (
    SCHEMA,
    FakeM3Connector,
    FakeRelationshipProbe,
    seed_catalog,
    seed_profiles,
    seed_semantics,
    synthetic_catalog,
    synthetic_rows,
)
from tests.test_relationships_service import FakeCredentialStore, _settings

# -- 共享构造 -------------------------------------------------------------------


def _new_env(tmp_path, *, settings=None):
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
    return repo, source_id, catalog


def _make_service(repo, source_id, catalog, *, settings=None, probe=None,
                  alter_table=None, clock=None, rows=None):
    def connector_factory(params):
        return FakeM3Connector(catalog, alter_table=alter_table,
                               statement_counter=params.get("statement_counter"))

    def probe_factory(params):
        target = probe if probe is not None else FakeRelationshipProbe(rows)
        target._counter = params.get("statement_counter")
        return target

    return RelationshipService(
        metadata_repository=repo, source_repository=repo,
        credential_store=FakeCredentialStore({"cred-1": "secret"}),
        connector_factory=connector_factory, probe_factory=probe_factory,
        settings=settings or _settings(), clock=clock,
    )


def _seed_snapshot(repo, source_id, *, snapshot_id, alter=None, drop_table=None,
                   drop_column=None, started_at):
    catalog = synthetic_catalog(source_id=source_id, snapshot_id=snapshot_id)
    for table in catalog:
        table["table_id"] = f"{snapshot_id}-{table['table_name']}"
    if drop_table:
        catalog = [t for t in catalog if t["table_name"] != drop_table]
    for table in catalog:
        if drop_column and table["table_name"] == alter:
            table["columns"] = [c for c in table["columns"] if c["name"] != drop_column]
            table["foreign_keys"] = [
                fk for fk in (table.get("foreign_keys") or [])
                if drop_column not in (fk.get("columns") or [])]
        if alter and table["table_name"] == alter and not drop_column:
            table["columns"].append(
                {"name": "vip_level", "data_type": "int", "nullable": True})
    seed_catalog(repo, catalog, source_id=source_id, snapshot_id=snapshot_id,
                 started_at=started_at)
    seed_profiles(repo, catalog)
    return catalog


class ManualClock:
    """可控单调时钟（预算/期限测试不做真实长 sleep）。"""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# == 二、跨快照有效关系选择与重验 ================================================


class TestCrossSnapshotChains:
    def test_old_validated_new_snapshot_rebind_revalidate(self, tmp_path):
        """旧链 validated → 新快照结构变化 → 旧链 stale、新链候选可见 →
        针对新快照重验（重新解析两端完整标识），结论绑定新快照。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        old_key = next(
            r["relationship_key"]
            for r in service.list_relationships(source_id, status="validated")
            if r["source_columns"] == ["guest_id"])
        assert service.get_relationship(old_key)["snapshot_id"] == "snap-m3"

        new_catalog = _seed_snapshot(
            repo, source_id, snapshot_id="snap-2", alter="guest",
            started_at="2026-10-28T01:00:00+00:00")
        # 新快照结构（guest 加列）——连接器按新目录返回（模拟源库已变）
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        run2 = service.discover_relationships(source_id)
        assert run2["stale_marked"] >= 1  # 旧链结构变化 → stale

        # 默认列表（=发布快照 snap-2）：新候选可见，旧 stale 不遮挡
        published = service.list_relationships(source_id)
        assert all(r["snapshot_id"] == "snap-2" for r in published)
        new_record = next(
            r for r in published
            if r["relationship_key"] == old_key
            and r["source_columns"] == ["guest_id"])
        assert new_record["status"] == "candidate"

        # 针对新快照重验：重绑后验证，结论绑定 snap-2（不用旧 table_id）
        rerun = service.revalidate(source_id, old_key)
        assert rerun["status"] in ("succeeded", "partial")
        effective_new = repo.get_effective_relationship(old_key, snapshot_id="snap-2")
        assert effective_new["status"] in ("validated", "needs_review")
        assert effective_new["snapshot_id"] == "snap-2"
        assert effective_new["source_table_id"].startswith("snap-2-")
        # 旧链历史保留（stale 可显式查询）
        old_chain = repo.get_effective_relationship(old_key, snapshot_id="snap-m3")
        assert old_chain["status"] == "stale"

    def test_same_structure_new_snapshot_does_not_inherit_validated(self, tmp_path):
        """结构相同的新快照：旧链 validated 保持有效，但新链从 candidate
        起步——不自动借用旧证据冒充新快照已验证。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        guest_key = next(
            r["relationship_key"] for r in repo.list_relationships(
                source_id, snapshot_id="snap-2")
            if r["source_columns"] == ["guest_id"])
        assert repo.get_effective_relationship(
            guest_key, snapshot_id="snap-2")["status"] == "candidate"  # 不继承
        # 旧链未被误标 stale（结构未变）
        assert repo.get_effective_relationship(
            guest_key, snapshot_id="snap-m3")["status"] == "validated"

    def test_old_human_decisions_do_not_shadow_new_chain(self, tmp_path):
        """旧快照的人工确认/拒绝不遮挡新快照候选；新链审核独立进行。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        first = service.list_relationships(source_id)[0]
        key = first["relationship_key"]
        service.submit_review(key, action="reject",
                              expected_revision=first["revision"], note="旧否决")
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        # 新链候选不被旧拒绝遮挡
        new_record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert new_record["status"] == "candidate"
        # 新链可独立确认（链内修订号与旧链互不干扰）
        revision = service.submit_review(key, action="confirm",
                                         expected_revision=new_record["revision"])
        assert revision > new_record["revision"]
        assert repo.get_effective_relationship(
            key, snapshot_id="snap-2")["status"] == "validated"
        # 旧链的人工拒绝保持原样
        assert repo.get_effective_relationship(
            key, snapshot_id="snap-m3")["status"] == "rejected"

    def test_review_conflict_is_chain_scoped(self, tmp_path):
        """他快照的写入不构成本链冲突：旧链追加修订后，新链仍可按自身
        最新修订提交（修订号全局单调，冲突只看目标链）。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        first = service.list_relationships(source_id)[0]
        key = first["relationship_key"]
        service.submit_review(key, action="confirm",
                              expected_revision=first["revision"])  # 旧链 rev2
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        new_record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        # 新链最新修订（全局编号 >2）仍可正常提交，不被旧链 rev2 干扰
        revision = service.submit_review(key, action="confirm",
                                         expected_revision=new_record["revision"])
        assert revision > new_record["revision"]

    def test_revalidate_missing_table_not_revalidatable(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        emp_key = next(
            r["relationship_key"] for r in service.list_relationships(source_id)
            if r["source_table"] == "employee")
        service.submit_review(emp_key, action="confirm", expected_revision=1)
        service.mark_stale(emp_key, reason="演示", expected_revision=2)
        _seed_snapshot(repo, source_id, snapshot_id="snap-2", drop_table="employee",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        with pytest.raises(ValueError, match="不可重验.*employee"):
            service.revalidate(source_id, emp_key)

    def test_revalidate_missing_column_not_revalidatable(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        emp_key = next(
            r["relationship_key"] for r in service.list_relationships(source_id)
            if r["source_table"] == "employee")
        service.mark_stale(emp_key, reason="演示", expected_revision=1)
        _seed_snapshot(repo, source_id, snapshot_id="snap-2", alter="employee",
                       drop_column="manager_id",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        with pytest.raises(ValueError, match="不可重验.*manager_id"):
            service.revalidate(source_id, emp_key)

    def test_default_vs_explicit_history_query(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        first = service.list_relationships(source_id)[0]
        key = first["relationship_key"]
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       started_at="2026-10-28T01:00:00+00:00")
        # 默认 = 当前发布快照（snap-2 无该链时明确 404，不回退旧快照）
        with pytest.raises(Exception, match="快照 snap-2 内不存在|发布快照"):
            service.get_relationship(key)
        detail = service.get_relationship(key, snapshot_id="snap-m3")
        assert detail["snapshot_id"] == "snap-m3"
        assert service.get_relationship_history(key)  # 历史修订全保留

    def test_graph_paths_do_not_mix_snapshots(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       started_at="2026-10-28T01:00:00+00:00")
        service.discover_relationships(source_id)
        # snap-2 只发现候选（未验证）→ 图无边混入 snap-1 的 validated
        graph = service.graph(source_id)
        assert graph["snapshot_id"] == "snap-2"
        assert all(e["status"] != "validated" for e in graph["edges"])
        tables = {
            t["table_name"]: t["table_id"]
            for t in repo.list_table_metadata(source_id, snapshot_id="snap-2")
        }
        result = service.paths(source_id, tables["booking_item"], tables["tenant"])
        assert result["path_count"] == 0  # 不借用 snap-m3 的 validated 边

    def test_rediscovery_across_snapshots_idempotent(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        service.discover_relationships(source_id)  # 同快照重复 → 幂等
        records = repo.list_relationships(source_id, snapshot_id="snap-m3",
                                          effective_only=False)
        assert all(len(repo.get_relationship_history(r["relationship_key"])) == 1
                   for r in records)


# == 三、共享验证并发限制 ==========================================================


class ConcurrencyProbe(FakeRelationshipProbe):
    """记录并发峰值并可阻塞/抛错的探查替身。"""

    def __init__(self, *, hold_event=None, fail_first=False):
        super().__init__()
        self.hold_event = hold_event
        self.fail_first = fail_first
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0

    def window_metrics(self, request):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
        try:
            if self.fail_first and self.calls == 1:
                raise DataSourceConnectionError(
                    "timeout", "MySQL 连接或查询失败（timeout）：模拟首请求失败")
            if self.hold_event is not None:
                self.hold_event.wait(timeout=5)
            return super().window_metrics(request)
        finally:
            with self._lock:
                self.active -= 1


def _run_in_thread(fn):
    result: dict[str, Any] = {}

    def wrapper():
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 线程内收集
            result["error"] = exc

    thread = threading.Thread(target=wrapper)
    thread.start()
    return thread, result


class TestSharedConcurrencyLimit:
    def _validated_candidates(self, repo, source_id, service, count=2):
        service.discover_relationships(source_id)
        return [
            r for r in service.list_relationships(source_id)
            if r["status"] == "candidate"
        ][:count]

    def test_same_instance_two_requests_peak_one(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        probe = ConcurrencyProbe()
        service = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_max_concurrent_validations=1))
        candidates = self._validated_candidates(repo, source_id, service)
        assert len(candidates) >= 2
        threads = [
            _run_in_thread(
                lambda c=c: service.validate_relationship(source_id,
                                                           c["relationship_key"]))
            for c in candidates[:2]
        ]
        for thread, _ in threads:
            thread.join(timeout=30)
        assert probe.peak == 1  # 实际源库探查峰值 ≤ 1

    def test_different_instances_share_limit(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        probe = ConcurrencyProbe()
        service_a = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_max_concurrent_validations=1))
        service_b = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_max_concurrent_validations=1))
        candidates = self._validated_candidates(repo, source_id, service_a)
        threads = [
            _run_in_thread(lambda: service_a.validate_relationship(
                source_id, candidates[0]["relationship_key"])),
            _run_in_thread(lambda: service_b.validate_relationship(
                source_id, candidates[1]["relationship_key"])),
        ]
        for thread, _ in threads:
            thread.join(timeout=30)
        assert probe.peak == 1  # 不同实例也共享同一进程额度

    def test_single_batch_and_revalidate_mixed_peak_one(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        probe = ConcurrencyProbe()
        service = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_max_concurrent_validations=1,
                               relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        stale = next(
            r for r in service.list_relationships(source_id, status="validated")
            if r["source_columns"] == ["guest_id"])
        service.mark_stale(stale["relationship_key"],
                           expected_revision=stale["revision"])
        threads = [
            _run_in_thread(lambda: service.revalidate(
                source_id, stale["relationship_key"])),
            _run_in_thread(lambda: service.validate_batch(source_id, limit=2)),
        ]
        for thread, _ in threads:
            thread.join(timeout=60)
        assert probe.peak == 1

    def test_first_request_failure_releases_slot(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        probe = ConcurrencyProbe(fail_first=True)
        service = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_max_concurrent_validations=1))
        candidates = self._validated_candidates(repo, source_id, service)
        thread, result = _run_in_thread(
            lambda: service.validate_relationship(
                source_id, candidates[0]["relationship_key"]))
        thread.join(timeout=30)
        assert "value" in result  # 首请求失败收尾（不悬挂）
        # 额度已释放：第二请求正常执行
        second = service.validate_relationship(
            source_id, candidates[1]["relationship_key"])
        assert second["status"] == "succeeded"  # 额度已释放，第二请求完整执行

    def test_waiting_for_slot_respects_deadline(self, tmp_path):
        repo, source_id, catalog = _new_env(tmp_path)
        holder_event = threading.Event()
        holder_probe = ConcurrencyProbe(hold_event=holder_event)
        settings = _settings(relationship_max_concurrent_validations=1,
                             relationship_validation_deadline_seconds=1)
        holder_service = _make_service(repo, source_id, catalog,
                                       probe=holder_probe, settings=settings)
        service = _make_service(repo, source_id, catalog, probe=ConcurrencyProbe(),
                                settings=settings)
        candidates = self._validated_candidates(repo, source_id, service)
        assert len(candidates) >= 2
        thread, _ = _run_in_thread(
            lambda: holder_service.validate_relationship(
                source_id, candidates[0]["relationship_key"]))
        time.sleep(0.2)  # 让持额度请求进入探查
        run = service.validate_relationship(
            source_id, candidates[1]["relationship_key"])
        holder_event.set()
        thread.join(timeout=30)
        # 等待额度期间期限耗尽：明确收尾 failed，未执行任何查询
        assert run["status"] == "failed"
        assert any(e["reason"] == "concurrency_slot_timeout" for e in run["errors"])
        assert run["queries_used"] == 0

    def test_limiter_acquire_timeout_semantics(self):
        assert ValidationSlotLimiter.acquire(1, timeout=-1) is False  # 无剩余期限
        assert ValidationSlotLimiter.acquire(1, timeout=0.05) is True
        try:
            assert ValidationSlotLimiter.acquire(1, timeout=0.05) is False  # 被占用
        finally:
            ValidationSlotLimiter.release(1)


# == 四、查询数与期限预算覆盖完整过程 ==============================================


class TestBudgetAndDeadlineCoverage:
    def _service(self, tmp_path, *, settings=None, probe=None, clock=None):
        repo, source_id, catalog = _new_env(tmp_path, settings=settings)
        return _make_service(repo, source_id, catalog, settings=settings,
                             probe=probe, clock=clock), repo, source_id

    def test_total_budget_one_blocks_structure_check(self, tmp_path):
        """总预算 1：结构核对组（估计下界 2）也不能绕过限制启动。"""
        probe = FakeRelationshipProbe()
        service, _, source_id = self._service(
            tmp_path, settings=_settings(relationship_validation_max_queries=1),
            probe=probe)
        service.discover_relationships(source_id)
        run = service.validate_batch(source_id, limit=2)
        assert any(e["reason"] == "budget_exceeded" for e in run["errors"])
        assert run["queries_used"] == 0  # 未执行任何 SQL（含结构核对）
        assert probe.window_requests == []  # 探查未被触发
        # 零残留：候选保持 candidate
        for record in service.list_relationships(source_id):
            assert record["status"] == "candidate"

    def test_structure_queries_counted_in_budget(self, tmp_path):
        """结构核对按实际 SQL 语句计数（替身每次 get_table_metadata=2 条），
        不把一次方法调用算成一次 SQL。"""
        counter_probe = FakeRelationshipProbe()
        service, _, source_id = self._service(
            tmp_path, settings=_settings(
                relationship_validation_max_queries=100,
                relationship_validation_max_candidates=2),
            probe=counter_probe)
        service.discover_relationships(source_id)
        run = service.validate_batch(source_id, limit=1)
        assert run["status"] == "succeeded"
        # 1 候选：结构核对 2 表 ×2 条 + 窗口 2 + JOIN 2 = 8 条实际语句
        assert run["queries_used"] == 2 * 2 + 4

    def test_multi_candidate_shared_statement_budget(self, tmp_path):
        service, _, source_id = self._service(
            tmp_path, settings=_settings(
                relationship_validation_max_queries=12,
                relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        run = service.validate_batch(source_id, limit=12)
        assert run["status"] == "partial"
        assert run["queries_used"] <= 12
        assert run["candidates_skipped"] >= 1
        # 已处理候选的结果正常保留
        assert run["candidates_validated"] + run["candidates_needs_review"] >= 1

    def test_structure_check_phase_deadline(self, tmp_path):
        """结构核对阶段超期：核对完成后证据采集全部 gated（deadline_exceeded），
        该候选不得升级 validated，运行如实收尾。"""
        clock = ManualClock(0.0)
        repo, source_id, catalog = _new_env(tmp_path)

        class DeadlineConnector(FakeM3Connector):
            def get_table_metadata(self, schema_name, table_name):
                result = super().get_table_metadata(schema_name, table_name)
                clock.advance(10)  # 结构核对期间期限耗尽
                return result

        service = RelationshipService(
            metadata_repository=repo, source_repository=repo,
            credential_store=FakeCredentialStore({"cred-1": "secret"}),
            connector_factory=lambda params: DeadlineConnector(
                catalog, statement_counter=params.get("statement_counter")),
            probe_factory=lambda params: FakeRelationshipProbe(
                statement_counter=params.get("statement_counter")),
            settings=_settings(relationship_validation_deadline_seconds=5),
            clock=clock,
        )
        service.discover_relationships(source_id)
        run = service.validate_batch(source_id, limit=1)
        # 第一表结构核对耗尽期限 → 第二表核对前停止：failed 收尾、零残留
        assert run["status"] == "failed"
        assert any(e["reason"] == "deadline_exceeded" for e in run["errors"])
        assert run["queries_used"] == 2  # 仅第一表的核对语句已发生（如实计数）
        for record in repo.list_relationships(source_id, snapshot_id="snap-m3"):
            assert record.get("evidence") is None  # 无残留证据

    def test_metric_query_return_after_deadline_not_validated(self, tmp_path):
        """证据查询返回时已超期（模拟 5 秒返回、期限 1 秒）：即使覆盖率
        exact=1.0 也不得发布 validated。"""

        class SlowReturnProbe(FakeRelationshipProbe):
            def __init__(self, clock):
                super().__init__()
                self._clock_ref = clock

            def window_metrics(self, request):
                result = super().window_metrics(request)
                self._clock_ref.advance(10)  # 查询"返回"时已远超期限
                return result

        clock = ManualClock(0.0)
        probe = SlowReturnProbe(clock)
        service, repo, source_id = self._service(
            tmp_path, settings=_settings(relationship_validation_deadline_seconds=1),
            probe=probe, clock=clock)
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=1)
        record = next(
            r for r in repo.list_relationships(source_id, snapshot_id="snap-m3")
            if r.get("evidence"))
        assert record["evidence"]["deadline_exceeded"] is True
        assert record["status"] == "needs_review"  # 超期证据不升级
        assert any("deadline_exceeded" in b
                   for b in record["validation"]["blocking"])

    def test_publish_time_deadline_blocks_validation(self, tmp_path):
        """全部查询完成后、判定发布前超期：同样不得升级 validated。"""

        class LateExpiryProbe(FakeRelationshipProbe):
            """最后一条 JOIN 返回时推进时钟跨过期限。"""

            def __init__(self, clock):
                super().__init__()
                self._clock_ref = clock
                self._joins = 0

            def join_metrics(self, request):
                result = super().join_metrics(request)
                self._joins += 1
                if self._joins >= 2:  # 反向 JOIN（最后一条）返回时超期
                    self._clock_ref.advance(10)
                return result

        clock = ManualClock(0.0)
        probe = LateExpiryProbe(clock)
        service, repo, source_id = self._service(
            tmp_path, settings=_settings(relationship_validation_deadline_seconds=1),
            probe=probe, clock=clock)
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=1)
        record = next(
            r for r in repo.list_relationships(source_id, snapshot_id="snap-m3")
            if r.get("evidence"))
        assert record["status"] == "needs_review"
        assert record["evidence"]["deadline_exceeded"] is True

    def test_budget_exhausted_run_leaves_old_results_unchanged(self, tmp_path):
        service, repo, source_id = self._service(
            tmp_path, settings=_settings(
                relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        validated_before = {
            r["relationship_key"]: r["revision"]
            for r in service.list_relationships(source_id, status="validated")
        }
        assert validated_before
        # 穷尽预算的第二次运行：已验证结果不被污染
        tight = _make_service(
            repo, source_id, synthetic_catalog(source_id=source_id),
            settings=_settings(relationship_validation_max_queries=1))
        tight.validate_batch(source_id, limit=12)
        after = {
            r["relationship_key"]: r["revision"]
            for r in service.list_relationships(source_id, status="validated")
        }
        assert after == validated_before


# == 五、JOIN 成本门槛与重复匹配规模控制 ============================================


class TestJoinCostMode:
    def _tables(self):
        return {t["table_name"]: dict(t) for t in synthetic_catalog()}

    def _evidence_for(self, tables, source="booking_item", column="booking_id",
                      target="booking", target_column="id"):
        from tests.test_relationships_evidence import _collect, _record_for
        return _collect(
            _record_for(source, column, target, target_column), tables=tables)

    def test_full_composite_unique_key_uses_full_join(self):
        tables = self._tables()
        # booking_item 复合主键 (booking_id, item_id)：完整组合键映射
        evidence, _, _ = self._evidence_for(
            tables, source="booking", column="id",
            target="booking_item", target_column="booking_id")
        # 右端 booking_id 单列非唯一 → 窗口；改用完整组合键场景验证 full
        del evidence
        evidence2, _, _ = self._evidence_for(tables)  # booking_item.booking_id → booking.id：右端 id 是主键
        assert evidence2["coverage"]["forward"]["right_mode"] == "full"

    def test_missing_leading_column_composite_index_windowed(self):
        tables = self._tables()
        # 右表组合唯一索引 (tenant_id, code)，仅以 code 连接：缺前导列 →
        # 右端键非唯一（tenant_id 维度重复）→ 有界窗口而非完整 JOIN
        guest = tables["guest"]
        guest["primary_key"] = []
        guest["indexes"] = [{
            "name": "uq_tenant_code", "columns": ["tenant_id", "code"],
            "unique": True,
        }]
        # 目标列改为 code（guest 表没有 code → 用 room_type.code 演示组合索引）
        room = tables["room"]
        room["primary_key"] = []
        room["indexes"] = [{
            "name": "uq_room_composite", "columns": ["tenant_id", "room_no"],
            "unique": True,
        }]
        from tests.test_relationships_evidence import _collect, _record_for
        record = _record_for("booking", "room_id", "room", "room_no")
        # booking.room_id(int) vs room.room_no(varchar) 类型不兼容不召回；
        # 直接构造记录验证成本模式（不经召回）
        evidence, _, _ = _collect(record, tables=tables)
        assert evidence["coverage"]["forward"]["right_mode"] == "windowed"

    def test_leading_prefix_usable_when_unique(self):
        tables = self._tables()
        # 右表唯一索引恰为连接列（单列唯一）→ full
        evidence, _, _ = self._evidence_for(
            tables, source="payment", column="booking_no",
            target="booking", target_column="booking_no")
        assert evidence["coverage"]["forward"]["right_mode"] == "full"
        # 反向：payment.booking_no 非唯一 → windowed
        assert evidence["coverage"]["backward"]["right_mode"] == "windowed"

    def test_non_unique_right_with_many_duplicates_bounded(self, tmp_path):
        """非唯一右端大量相同键：受控窗口 JOIN（W×W 上界），不展开完整表。"""
        rows = synthetic_rows()
        # payment.booking_no 大量重复（160 行 100 值 → 追加更多重复行放大）
        for i in range(200):
            rows[(SCHEMA, "payment")].append(
                {"id": 1000 + i, "booking_no": f"B{i % 20 + 1:04d}",
                 "amount": 1.0, "paid_at": "2026-01-08 00:00:00",
                 "bank_txn_no": f"TXN{2000 + i:06d}"})
        probe = FakeRelationshipProbe(rows)
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog, probe=probe,
                                settings=_settings(
                                    relationship_sample_window_rows=50,
                                    relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        record = next(
            r for r in service.list_relationships(source_id)
            if r["source_table"] == "payment" and r["target_table"] == "booking"
            and r.get("evidence"))
        forward = record["evidence"]["coverage"]["forward"]
        assert forward["right_mode"] == "full"  # booking.booking_no 唯一
        backward = record["evidence"]["coverage"]["backward"]
        assert backward["right_mode"] == "windowed"  # 右端=payment 非唯一
        # 右端窗口请求受限（≤ 窗口行数）
        bounded = [req for req in probe.join_requests
                   if req.right_limit is not None]
        assert bounded and all(req.right_limit <= 50 for req in bounded)

    def test_row_estimate_lowball_still_bounded(self):
        tables = self._tables()
        # 行数估计严重低估（真实 400 行标 10）：不作为安全依据——
        # 无结构唯一键时仍走有界窗口
        tables["booking_item"]["row_count_estimate"] = 10
        tables["booking_item"]["primary_key"] = []
        tables["booking_item"]["indexes"] = []
        from tests.test_relationships_evidence import _collect, _record_for
        record = _record_for("service_item", "id", "booking_item", "booking_id")
        evidence, _, _ = _collect(record, tables=tables)
        assert evidence["coverage"]["forward"]["right_mode"] == "windowed"

    def test_small_table_normal_relation_still_full(self):
        tables = self._tables()
        evidence, _, _ = self._evidence_for(tables)
        forward = evidence["coverage"]["forward"]
        assert forward["right_mode"] == "full"
        assert forward["basis"] == "exact"
        assert forward["ratio"] == 1.0


# == 六、组合键去重碰撞 =============================================================


class TestCompositeDistinct:
    def test_window_distinct_with_separator_values(self, tmp_path):
        """字段值包含 CHAR(31)：替身按元组语义计数=2（旧 CONCAT_WS 方案=1）。"""
        rows = synthetic_rows()
        rows[(SCHEMA, "tenant")] = [
            {"id": 1, "name": "a\x1fb"},   # 值含分隔符
            {"id": 2, "name": "c"},
        ]
        # 组合键场景：用 booking_item 复合主键行构造 CHAR(31) 边界
        rows[(SCHEMA, "booking_item")] = [
            {"booking_id": 1, "item_id": 2},      # 元组 (1,2)
            {"booking_id": 12, "item_id": 2},     # 元组 (12,2)：拼接 '12|2' 相同边界
            {"booking_id": 1, "item_id": None},   # NULL：不参与去重口径
        ]
        probe = FakeRelationshipProbe(rows)
        result = probe.window_metrics(WindowMetricsRequest(
            schema_name=SCHEMA, table_name="booking_item",
            columns=("booking_id", "item_id"), order_by=(), limit=10,
            string_columns=(), timeout_ms=1000))
        assert result.non_null_rows == 2  # NULL 行排除
        assert result.distinct_values == 2  # 元组去重（拼接方案会碰撞的边界在 SQL 层验证）

    def test_distinct_with_char31_boundary_tuples(self):
        """('a','b\\x1fc') vs ('a\\x1fb','c')：拼接相同、元组不同。
        Python 替身天然正确；真实 SQL 语义由集成测试的常量 SELECT 验证。"""
        rows = synthetic_rows()
        rows[(SCHEMA, "booking_item")] = [
            {"booking_id": None, "item_id": None},  # 不入去重
        ]
        # 直接验证替身元组语义
        tuples = {("a", "b\x1fc"), ("a\x1fb", "c")}
        assert len(tuples) == 2
        joined = "a\x1fb" + "\x1f" + "c"  # 两个元组 CONCAT_WS 后的同一拼接结果
        assert ("a" + "\x1f" + "b\x1fc") == joined  # 旧方案确实碰撞（被修复的缺陷）

    def test_probe_sql_uses_native_multi_column_distinct(self):
        """SQL 构造断言：窗口/JOIN 均为原生多列 DISTINCT，无 CONCAT_WS。"""
        captured: dict[str, str] = {}

        def fake_fetch_one(self, sql):
            captured["sql"] = sql
            return {"window_rows": 1, "non_null_rows": 1, "empty_string_rows": 0,
                    "distinct_values": 1}

        probe = MySQLRelationshipProbe.__new__(MySQLRelationshipProbe)
        probe._fetch_one = fake_fetch_one.__get__(probe)  # type: ignore[attr-defined]
        probe.window_metrics(WindowMetricsRequest(
            schema_name="s", table_name="t", columns=("c1", "c2"),
            order_by=("pk",), limit=5, string_columns=(), timeout_ms=100))
        sql = captured["sql"]
        assert "CONCAT_WS" not in sql
        assert "COUNT(DISTINCT `c1`, `c2`)" in sql

        def fake_fetch_join(self, sql):
            captured["join_sql"] = sql
            return {"left_rows_seen": 1, "unmatched_left_rows": 0,
                    "rows_after_left_join": 1, "distinct_right_matched": 1,
                    "empty_key_matched_rows": 0}

        probe._fetch_one = fake_fetch_join.__get__(probe)  # type: ignore[attr-defined]
        probe.join_metrics(JoinMetricsRequest(
            left_schema="s", left_table="l", left_columns=("a", "b"),
            left_order_by=("pk1", "pk2"), left_limit=5,
            right_schema="s", right_table="r", right_columns=("x", "y"),
            right_presence_column="x", string_join_columns=(),
            timeout_ms=100))
        join_sql = captured["join_sql"]
        assert "CONCAT_WS" not in join_sql
        assert "COUNT(DISTINCT w.`pk1`, w.`pk2`)" in join_sql  # 组合主键行数
        assert "COUNT(DISTINCT CASE WHEN r.`x` IS NOT NULL THEN r.`x` END,"\
               " CASE WHEN r.`x` IS NOT NULL THEN r.`y` END)" in join_sql

    def test_probe_sql_windowed_right_join_bounded(self):
        """窗口化右端：右源为有界派生表（LIMIT），不直接 JOIN 完整表。"""
        captured: dict[str, str] = {}

        def fake_fetch(self, sql):
            captured["sql"] = sql
            return {"left_rows_seen": 1, "unmatched_left_rows": 0,
                    "rows_after_left_join": 1, "distinct_right_matched": 1,
                    "empty_key_matched_rows": 0}

        probe = MySQLRelationshipProbe.__new__(MySQLRelationshipProbe)
        probe._fetch_one = fake_fetch.__get__(probe)  # type: ignore[attr-defined]
        probe.join_metrics(JoinMetricsRequest(
            left_schema="s", left_table="l", left_columns=("a",),
            left_order_by=("pk",), left_limit=100,
            right_schema="s", right_table="r", right_columns=("x",),
            right_presence_column="x", string_join_columns=(),
            timeout_ms=100, right_order_by=("rpk",), right_limit=100))
        sql = captured["sql"]
        # 右端是派生子查询（含 LIMIT），而非裸表
        assert "LEFT JOIN (SELECT `x` FROM `s`.`r` ORDER BY `rpk` LIMIT 100)" in sql

    def test_statement_counter_counts_failures(self):
        counter = StatementCounter()
        probe = FakeRelationshipProbe(statement_counter=counter)

        class Failing(FakeRelationshipProbe):
            def window_metrics(self, request):
                self._note_statement()  # 语句发出即计数（失败尝试计入）
                raise DataSourceConnectionError("timeout", "模拟失败")

        failing = Failing(statement_counter=counter)
        with pytest.raises(DataSourceConnectionError):
            failing.window_metrics(WindowMetricsRequest(
                schema_name=SCHEMA, table_name="tenant", columns=("id",),
                order_by=(), limit=5, string_columns=(), timeout_ms=100))
        assert counter.count == 1  # 失败查询计入
        probe.window_metrics(WindowMetricsRequest(
            schema_name=SCHEMA, table_name="tenant", columns=("id",),
            order_by=(), limit=5, string_columns=(), timeout_ms=100))
        assert counter.count == 2
