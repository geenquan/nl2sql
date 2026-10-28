"""M3 修复轮 2 回归：跨快照重绑依据重算 + SQL 逐语句硬预算。

一、重绑依据重算（P1）：稳定逻辑身份 vs 快照相关候选依据明确区分——
重绑后 origin/type_verdict/支持证据/风险/评分全部来自目标快照重算，
断言依据字段本身而不只断言最终状态。
二、逐语句硬预算（P1）：真实 SQLAlchemy 执行事件级验证（临时 SQLite 引擎），
证明被拒语句未到达数据库、已发出语句（含失败）如实计数、并发不超发。
"""

import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.exceptions import ValidationStatementBudgetExceeded
from app.ports.connectors import WindowMetricsRequest
from app.relationships.execution import (
    StatementBudget,
    StatementCounter,
    ValidationSlotLimiter,
)
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


def _new_env(tmp_path):
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


def _make_service(repo, source_id, catalog, *, settings=None, probe=None):
    def connector_factory(params):
        return FakeM3Connector(catalog,
                               statement_counter=params.get("statement_counter"))

    def probe_factory(params):
        target = probe if probe is not None else FakeRelationshipProbe()
        target._counter = params.get("statement_counter")
        return target

    return RelationshipService(
        metadata_repository=repo, source_repository=repo,
        credential_store=FakeCredentialStore({"cred-1": "secret"}),
        connector_factory=connector_factory, probe_factory=probe_factory,
        settings=settings or _settings(),
    )


def _seed_snapshot(repo, source_id, *, snapshot_id, mutate=None, with_profiles=True,
                   with_semantics=True):
    """构造目标快照；mutate(catalog) 就地改结构（加列/改类型/删约束等）。"""
    catalog = synthetic_catalog(source_id=source_id, snapshot_id=snapshot_id)
    for table in catalog:
        table["table_id"] = f"{snapshot_id}-{table['table_name']}"
    if mutate is not None:
        mutate(catalog)
    seed_catalog(repo, catalog, source_id=source_id, snapshot_id=snapshot_id,
                 started_at="2026-10-28T02:00:00+00:00")
    if with_profiles:
        seed_profiles(repo, catalog)
    if with_semantics:
        seed_semantics(repo, catalog)
    return catalog


def _validated_guest_key(service, source_id):
    service.discover_relationships(source_id)
    service.validate_batch(source_id, limit=12)
    return next(
        r["relationship_key"]
        for r in service.list_relationships(source_id, status="validated")
        if r["source_columns"] == ["guest_id"] and r["target_table"] == "guest")


# == 一、重绑依据重算 ==============================================================


class TestRebindRecomputesBasis:
    def test_int_to_varchar_with_fk_deleted(self, tmp_path):
        """int→varchar 且外键删除：新链 type_verdict=incompatible、origin 不含
        explicit_fk、不执行 JOIN（证据 queries 为空）、阻断原因明确。"""

        def mutate(catalog):
            guest = next(t for t in catalog if t["table_name"] == "guest")
            for column in guest["columns"]:
                if column["name"] == "id":
                    column["data_type"] = "varchar(20)"
            booking = next(t for t in catalog if t["table_name"] == "booking")
            booking["foreign_keys"] = [
                fk for fk in booking["foreign_keys"]
                if fk.get("name") != "fk_booking_guest"]

        repo, source_id, catalog = _new_env(tmp_path)
        probe = FakeRelationshipProbe()
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15),
            probe=probe)
        key = _validated_guest_key(service, source_id)
        old = repo.get_effective_relationship(key, snapshot_id="snap-m3")
        assert old["origin"] == ["explicit_fk"] and old["type_verdict"] == "compatible"

        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15),
            probe=probe)
        service.discover_relationships(source_id)  # 旧链 stale；snap-2 无该链（类型跨族不召回）
        run = service.revalidate(source_id, key)
        assert run["status"] in ("succeeded", "partial")

        new_record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        # 依据来自目标快照重算：类型跨族如实判定，不继承旧 compatible
        assert new_record["type_verdict"] == "incompatible"
        assert "explicit_fk" not in new_record["origin"]
        assert new_record["status"] == "needs_review"
        # 不执行可能产生隐式转换语义混淆的 JOIN：无任何查询
        assert new_record["evidence"]["queries"] == []
        assert any("type_incompatible" in g["reason"]
                   for g in new_record["evidence"]["gated"])
        assert any(b.startswith("type:incompatible")
                   for b in new_record["validation"]["blocking"])
        # 新链不继承旧审核/验证内容；旧链历史保留
        assert new_record.get("reviewed_by") is None
        assert new_record["validation"]["decided_by"] == "policy"
        assert repo.get_effective_relationship(
            key, snapshot_id="snap-m3")["status"] == "stale"

    def test_fk_deleted_types_compatible_no_explicit_fk(self, tmp_path):
        """外键删除但类型兼容：新链 origin 来自词法/语义信号（无 explicit_fk），
        仍可凭证据验证。"""

        def mutate(catalog):
            booking = next(t for t in catalog if t["table_name"] == "booking")
            booking["foreign_keys"] = [
                fk for fk in booking["foreign_keys"]
                if fk.get("name") != "fk_booking_guest"]

        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert record is not None  # 信号召回重建（类型兼容）
        assert "explicit_fk" not in record["origin"]  # 目标快照无外键不标记
        assert record["type_verdict"] == "compatible"
        run = service.revalidate(source_id, key)
        assert run["status"] in ("succeeded", "partial")
        assert repo.get_effective_relationship(
            key, snapshot_id="snap-2")["status"] == "validated"

    def test_unique_constraint_removed_recomputes_structure(self, tmp_path):
        """目标端主键删除且外键删除（id 不再唯一、无结构权威）：重绑后
        结构分/基数/阻断按目标快照重算，不借用旧 explicit_fk 结构分。"""

        def mutate(catalog):
            guest = next(t for t in catalog if t["table_name"] == "guest")
            guest["primary_key"] = []
            booking = next(t for t in catalog if t["table_name"] == "booking")
            booking["foreign_keys"] = [
                fk for fk in booking["foreign_keys"]
                if fk.get("name") != "fk_booking_guest"]

        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        run = service.revalidate(source_id, key)
        assert run["status"] in ("succeeded", "partial")
        record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        # 结构依据重算：不继承旧 explicit_fk 的结构满分
        assert record["scores"]["structural"] <= 0.4
        assert "explicit_fk" not in record["origin"]
        # 基数也按目标快照重算：目标端唯一性不再来自结构（主键已删），
        # 而是来自 exact 窗口观察（数据仍唯一）——口径如实注明
        observed = record["evidence"]["cardinality_observed"]
        assert observed["target_uniqueness"]["value"] == "unique"
        assert "exact" in observed["target_uniqueness"]["note"]
        assert "结构唯一" not in observed["target_uniqueness"]["note"]

    def test_target_becomes_composite_unique_flags_risk(self, tmp_path):
        """目标端主键变为组合键：组合键风险按目标快照标记。"""

        def mutate(catalog):
            guest = next(t for t in catalog if t["table_name"] == "guest")
            guest["primary_key"] = ["id", "phone"]

        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert record is not None
        assert any(r["type"] == "composite_key_risk" for r in record["risks"])
        # 组合键风险阻断自动验证
        assert record["status"] in ("candidate", "needs_review")
        if record["status"] == "needs_review":
            assert any("composite_key_risk" in b
                       for b in record["validation"]["blocking"])

    def test_missing_profiles_and_semantics_not_borrowed(self, tmp_path):
        """目标快照缺画像/语义：对应分项按缺失处理（记 0），不借用旧快照。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        _validated_guest_key(service, source_id)
        _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                       with_profiles=False, with_semantics=False)
        service.discover_relationships(source_id)
        record = next(
            r for r in repo.list_relationships(source_id, snapshot_id="snap-2")
            if r["source_columns"] == ["guest_id"])
        # 评分来自目标快照输入：缺画像/语义 → 分项 0（旧快照曾有画像/语义）
        assert record["scores"]["profile"] == 0.0
        assert record["scores"]["semantic"] == 0.0

    def test_added_column_only_still_revalidates(self, tmp_path):
        """仅加无关字段、关系未变：仍可重验（外键依据保留，重新验证通过）。"""

        def mutate(catalog):
            guest = next(t for t in catalog if t["table_name"] == "guest")
            guest["columns"].append(
                {"name": "vip_level", "data_type": "int", "nullable": True})

        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        run = service.revalidate(source_id, key)
        record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert record["origin"] == ["explicit_fk"]  # 外键仍在 → 结构依据真实
        assert record["status"] == "validated"
        assert run["status"] in ("succeeded", "partial")

    def test_new_chain_and_old_history_not_polluted(self, tmp_path):
        """新链与旧历史互不污染：旧链人工字段保持；新链无任何旧内容。"""

        def mutate(catalog):
            guest = next(t for t in catalog if t["table_name"] == "guest")
            for column in guest["columns"]:
                if column["name"] == "id":
                    column["data_type"] = "varchar(20)"
            booking = next(t for t in catalog if t["table_name"] == "booking")
            booking["foreign_keys"] = [
                fk for fk in booking["foreign_keys"]
                if fk.get("name") != "fk_booking_guest"]

        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        service.submit_review(key, action="confirm", expected_revision=2,
                              reviewer="alice", note="旧确认")
        new_catalog = _seed_snapshot(repo, source_id, snapshot_id="snap-2",
                                     mutate=mutate)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.revalidate(source_id, key)

        old_chain = repo.get_relationship_history(key)
        # 旧链的人工确认与 stale 修订保持原样（不被新链写覆盖）
        assert any(r["snapshot_id"] == "snap-m3" and r["status"] == "validated"
                   and r.get("reviewed_by") == "alice" for r in old_chain)
        assert any(r["snapshot_id"] == "snap-m3" and r["status"] == "stale"
                   for r in old_chain)
        new_record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert new_record["snapshot_id"] == "snap-2"
        assert new_record.get("reviewed_by") is None  # 不继承旧审核人
        assert "revalidate_rebind" in " ".join(
            str(r.get("origin", "")) for r in new_record["recall_reasons"])


# == 二、SQL 逐语句硬预算 ===========================================================

_DUMMY_CONFIG = {"host": "unused", "port": 3306, "database": "unused",
                 "username": "unused", "password": "secret"}


def _sqlite_engine(tmp_path: Path, name: str = "events.db") -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (pk INTEGER PRIMARY KEY, a INTEGER, b TEXT)"))
        conn.execute(text("INSERT INTO t (pk, a, b) VALUES (1, 1, 'x'), (2, 2, 'y')"))
    return engine


def _connector_with(engine: Engine, counter) -> MySQLConnector:
    return MySQLConnector(
        MySQLConnectionConfig(**_DUMMY_CONFIG),
        engine_factory=lambda config: engine,
        statement_counter=counter,
    )


class TestStatementBudgetEventLevel:
    def test_budget_two_database_receives_only_two(self, tmp_path):
        """预算 2、尝试 3 条 SELECT：数据库实际只收到 2 条（第 3 条发出前被拒）。"""
        engine = _sqlite_engine(tmp_path)
        budget = StatementBudget(2)
        connector = _connector_with(engine, budget)
        assert connector.engine is engine  # 首次访问即挂载守卫（先于接收计数器）
        received: list[str] = []
        event.listen(engine, "before_cursor_execute",
                     lambda conn, cursor, statement, *a, **k: received.append(statement))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
            assert conn.execute(text("SELECT 2")).scalar() == 2
            with pytest.raises(ValidationStatementBudgetExceeded):
                conn.execute(text("SELECT 3"))
        assert len(received) == 2  # 第 3 条未到达数据库（接收监听器在守卫之后）
        assert budget.count == 2   # 已发出 2（被拒绝不计入已执行）
        assert budget.rejected == 1
        connector.close()

    def test_structure_read_with_one_quota_left(self, tmp_path):
        """剩余 1 条额度进入多语句结构读取（Inspector）：第 2 条被拒，
        异常为领域预算错误（非 unknown 数据库故障、无 SQL 文本）。"""
        engine = _sqlite_engine(tmp_path)
        budget = StatementBudget(1)
        connector = _connector_with(engine, budget)
        assert connector.engine is engine  # 挂载守卫
        with pytest.raises(ValidationStatementBudgetExceeded) as excinfo:
            connector.get_table_metadata("main", "t")
        assert "预算耗尽" in str(excinfo.value)
        assert "SELECT" not in str(excinfo.value) and "information_schema" not in str(
            excinfo.value)
        assert budget.count == 1  # 仅第一条 Inspector 语句实际发出
        assert budget.rejected >= 1
        connector.close()

    def test_connector_and_probe_share_budget(self, tmp_path):
        """连接器与探查各持独立引擎（真实部署形态），共享同一预算对象：
        两端发出的语句从同一额度池扣减，耗尽后双方都被拒。"""
        engine_a = _sqlite_engine(tmp_path, "a.db")
        engine_b = _sqlite_engine(tmp_path, "b.db")
        budget = StatementBudget(3)
        connector = _connector_with(engine_a, budget)
        assert connector.engine is engine_a
        probe = MySQLRelationshipProbe(
            MySQLConnectionConfig(**_DUMMY_CONFIG), engine=engine_b,
            statement_counter=budget)
        # 连接器 1 条 + 探查 2 条 = 3；连接器再执行 → 被拒（共享额度耗尽）
        with engine_a.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
        probe.window_metrics(WindowMetricsRequest(
            schema_name="main", table_name="t", columns=("a",),
            order_by=("pk",), limit=5, string_columns=(), timeout_ms=1000))
        probe.window_metrics(WindowMetricsRequest(
            schema_name="main", table_name="t", columns=("b",),
            order_by=("pk",), limit=5, string_columns=("b",), timeout_ms=1000))
        with (
            engine_a.connect() as conn,
            pytest.raises(ValidationStatementBudgetExceeded),
        ):
            conn.execute(text("SELECT 2")).scalar()
        assert budget.count == 3 and budget.rejected == 1
        probe.close()
        connector.close()

    def test_failed_sql_consumes_quota_no_refund(self, tmp_path):
        """已发出的失败 SQL 消耗一次额度（不退回、不重复计数）。"""
        engine = _sqlite_engine(tmp_path)
        budget = StatementBudget(2)
        connector = _connector_with(engine, budget)
        assert connector.engine is engine
        with engine.connect() as conn:
            with pytest.raises(Exception):  # noqa: B017 - sqlite 报表不存在的执行错误
                conn.execute(text("SELECT * FROM `nope_table`"))
            assert budget.count == 1  # 失败语句已消耗额度（不退回）
            assert conn.execute(text("SELECT 1")).scalar() == 1
            with pytest.raises(ValidationStatementBudgetExceeded):
                conn.execute(text("SELECT 2")).scalar()
        assert budget.count == 2  # 失败 1 + 成功 1；被拒不计
        assert budget.rejected == 1
        connector.close()

    def test_concurrent_last_quota_never_overissues(self, tmp_path):
        """并发争抢最后 1 条额度：恰一成功一拒绝，已发出数不超过预算。"""
        engine = _sqlite_engine(tmp_path)
        budget = StatementBudget(1)
        connector = _connector_with(engine, budget)
        assert connector.engine is engine
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def run_select():
            barrier.wait()
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1")).scalar()
                outcomes.append("ok")
            except ValidationStatementBudgetExceeded:
                outcomes.append("rejected")

        threads = [threading.Thread(target=run_select) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(outcomes) == ["ok", "rejected"]
        assert budget.count == 1  # 无超发
        assert budget.rejected == 1
        connector.close()

    def test_plain_counter_keeps_legacy_counting_behavior(self, tmp_path):
        """普通计数器（非预算）只计数不阻止：M1/M2 历史行为不变。"""
        engine = _sqlite_engine(tmp_path)
        counter = StatementCounter()
        connector = _connector_with(engine, counter)
        assert connector.engine is engine
        with engine.connect() as conn:
            for i in range(3):
                assert conn.execute(text(f"SELECT {i}")).scalar() == i
        assert counter.count == 3  # 全部执行，无拒绝
        connector.close()


class VerboseWindowProbe(FakeRelationshipProbe):
    """窗口聚合每次执行 2 条语句（模拟真实适配器多语句操作组：
    组估计 1 < 实际 2，验证逐语句硬限制在组内生效）。"""

    def window_metrics(self, request):
        self._note_statements(2)
        return super().window_metrics(request)


class TestBudgetExhaustedRunFinalization:
    def test_exhausted_mid_run_finalizes_and_old_results_unchanged(self, tmp_path):
        """预算中途耗尽：候选优雅降级（gated 证据不冒充完整验证）、剩余跳过、
        旧有效结果不变、共享并发额度随后可再次取得。

        预算 5：候选1 结构核对 4 条（2 表 ×2）+ 第 1 条窗口聚合发出后第 2 条
        被逐语句拒绝（组估计 1 < 实际 2）；后续指标 gated，候选降级
        needs_review；剩余候选不再执行。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        key = _validated_guest_key(service, source_id)
        validated_before = {
            (r["relationship_key"], r["revision"])
            for r in service.list_relationships(source_id, status="validated")
        }
        tight = _make_service(
            repo, source_id, catalog, probe=VerboseWindowProbe(),
            settings=_settings(relationship_validation_max_queries=5,
                               relationship_validation_max_candidates=15))
        run = tight.validate_batch(source_id, limit=12)
        assert run["status"] == "partial"
        assert run["queries_used"] == 5  # 已发出 = 预算（无超发）
        assert run["sql_statements_rejected"] >= 1  # 被拒语句如实单列（不计入已执行）
        assert any(e["reason"] == "budget_exceeded" for e in run["errors"])
        # 优雅降级的候选：gated 证据 + needs_review（不冒充完整验证）
        degraded = next(
            r for r in repo.list_relationships(source_id, snapshot_id="snap-m3")
            if r.get("evidence") and any(
                "statement_rejected" in g["reason"]
                for g in r["evidence"].get("gated", [])))
        assert degraded["status"] == "needs_review"
        # 旧有效结果不变
        assert {
            (r["relationship_key"], r["revision"])
            for r in service.list_relationships(source_id, status="validated")
        } == validated_before
        assert key in {k for k, _ in validated_before}
        # 共享并发额度已释放：可再次取得
        assert ValidationSlotLimiter.acquire(1, timeout=0.5) is True
        ValidationSlotLimiter.release(1)

    def test_structure_check_rejection_skips_candidate_cleanly(self, tmp_path):
        """结构核对中途被拒（多语句 Inspector）：该候选无残留修订，运行收尾。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(repo, source_id, catalog)
        service.discover_relationships(source_id)
        revisions_before = {
            (r["relationship_key"], r["revision"])
            for r in repo.list_relationships(source_id, effective_only=False)
        }
        # 预算 1：结构核对组估计 2 > 1 → 组准入直接拦截（0 语句发出）
        tight = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_queries=1))
        run = tight.validate_batch(source_id, limit=3)
        assert any(e["reason"] == "budget_exceeded" for e in run["errors"])
        assert run["queries_used"] == 0
        assert run["sql_statements_rejected"] == 0  # 组准入拦截（未到逐语句层）
        assert {
            (r["relationship_key"], r["revision"])
            for r in repo.list_relationships(source_id, effective_only=False)
        } == revisions_before  # 零残留
