"""M3 服务编排测试：发现/验证运行、审核原子性、过期重验与失败零残留（离线）。"""

import threading
from typing import Any

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.core.exceptions import (
    DataSourceConnectionError,
    ResourceNotFoundError,
    RevisionConflictError,
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


class FakeCredentialStore:
    def __init__(self, secrets: dict[str, str] | None = None):
        self.secrets = secrets or {}

    def get(self, ref):
        return self.secrets.get(ref)

    def put(self, ref, secret):
        self.secrets[ref] = secret

    def delete(self, ref):
        return self.secrets.pop(ref, None) is not None


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture
def env(tmp_path):
    """真实 SQLite 仓储 + 合成目录/画像/语义 + 数据源记录。"""
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    catalog = synthetic_catalog(source_id=source_id)
    table_ids = seed_catalog(repo, catalog, source_id=source_id)
    seed_profiles(repo, catalog)
    seed_semantics(repo, catalog)
    return {
        "repo": repo, "catalog": catalog, "table_ids": table_ids,
        "credentials": FakeCredentialStore({"cred-1": "secret"}),
        "source_id": source_id,
    }


def _service(env, *, rows=None, catalog=None, alter_table=None,
             probe=None, settings=None, repo=None):
    catalog = catalog or env["catalog"]

    def connector_factory(params):
        return FakeM3Connector(catalog, alter_table=alter_table,
                               statement_counter=params.get("statement_counter"))

    def probe_factory(params):
        if probe is not None:
            probe._counter = params.get("statement_counter")
            return probe
        return FakeRelationshipProbe(rows,
                                     statement_counter=params.get("statement_counter"))

    return RelationshipService(
        metadata_repository=repo or env["repo"],
        source_repository=repo or env["repo"],
        credential_store=env["credentials"],
        connector_factory=connector_factory,
        probe_factory=probe_factory,
        settings=settings or _settings(),
    )


class TestDiscovery:
    def test_discovery_persists_candidates_and_run(self, env):
        service = _service(env)
        run = service.discover_relationships(env["source_id"])
        assert run["status"] == "succeeded"
        assert run["candidates_new"] >= 9
        assert run["stale_marked"] == 0
        listed = service.list_relationships(env["source_id"])
        assert len(listed) == run["candidates_new"]
        assert all(r["status"] == "candidate" for r in listed)
        # 运行记录可查
        assert service.get_run(run["run_id"])["run_type"] == "discovery"

    def test_discovery_requires_allow_partial_for_partial_catalog(self, env):
        env["repo"].save_scan_job({
            "job_id": "job-partial", "source_id": env["source_id"],
            "snapshot_id": "snap-partial", "status": "partial",
            "started_at": "2026-10-28T00:00:00+00:00",
            "finished_at": "2026-10-28T00:00:01+00:00",
            "tables_planned": 9, "tables_scanned": 3, "tables_failed": 1,
            "tables_skipped": 0, "errors": [],
        })
        # 使 partial 快照成为唯一“最新发布”目录：删除 succeeded 任务行
        with env["repo"]._engine.begin() as conn:
            from sqlalchemy import delete
            conn.execute(delete(env["repo"]._scan_jobs).where(
                env["repo"]._scan_jobs.c.status == "succeeded"))
        service = _service(env)
        with pytest.raises(ValueError, match="allow_partial"):
            service.discover_relationships(env["source_id"])
        run = service.discover_relationships(env["source_id"], allow_partial=True)
        assert run["input_complete"] is False

    def test_rediscovery_same_snapshot_idempotent(self, env):
        service = _service(env)
        first = service.discover_relationships(env["source_id"])
        second = service.discover_relationships(env["source_id"])
        assert second["candidates_new"] == 0
        assert second["candidates_idempotent_skipped"] == first["candidates_found"]
        # 修订数不变：同快照重复召回不追加修订
        for record in service.list_relationships(env["source_id"]):
            history = env["repo"].get_relationship_history(record["relationship_key"])
            assert len(history) == 1

    def test_new_snapshot_structure_change_marks_stale(self, env):
        service = _service(env, settings=_settings(
            relationship_validation_max_candidates=15))
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=12)  # 先产生已验证证据
        # 在新快照中改变 guest 表结构（加列）
        catalog2 = synthetic_catalog(source_id=env["source_id"], snapshot_id="snap-2")
        for table in catalog2:
            table["table_id"] = "snap-2-" + table["table_name"]
        guest2 = next(t for t in catalog2 if t["table_name"] == "guest")
        guest2["columns"].append({"name": "vip_level", "data_type": "int"})
        seed_catalog(env["repo"], catalog2, source_id=env["source_id"],
                     snapshot_id="snap-2",
                     started_at="2026-10-28T01:00:00+00:00")
        seed_profiles(env["repo"], catalog2)
        run2 = service.discover_relationships(env["source_id"])
        assert run2["stale_marked"] >= 1  # booking.guest_id 等旧证据在其旧链转 stale
        # 旧链 stale（快照先行：旧链按快照查询，不遮挡新快照候选）
        old_chain = service.list_relationships(env["source_id"], snapshot_id="snap-m3")
        stale = [r for r in old_chain if r["status"] == "stale"]
        assert any(r["source_table"] == "booking" and r["source_columns"] == ["guest_id"]
                   for r in stale)
        # 新快照链不受旧 stale 遮挡：默认列表（=发布快照 snap-2）可见新候选
        published = service.list_relationships(env["source_id"])
        assert all(r["status"] != "stale" for r in published)
        assert any(r["source_table"] == "booking" and r["source_columns"] == ["guest_id"]
                   for r in published)

    def test_missing_table_in_complete_new_snapshot_marks_stale(self, env):
        service = _service(env, settings=_settings(
            relationship_validation_max_candidates=15))
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=12)
        catalog2 = [dict(t) for t in synthetic_catalog(
            source_id=env["source_id"], snapshot_id="snap-2")]
        for table in catalog2:
            table["table_id"] = "snap-2-" + table["table_name"]
        catalog2 = [t for t in catalog2 if t["table_name"] != "employee"]
        seed_catalog(env["repo"], catalog2, source_id=env["source_id"],
                     snapshot_id="snap-2",
                     started_at="2026-10-28T01:00:00+00:00")
        run2 = service.discover_relationships(env["source_id"])
        assert run2["stale_marked"] >= 1

    def test_failed_discovery_zero_residue(self, env):
        real = env["repo"]

        class FailingBatch(type(real)):
            def save_relationship_batch(self, records, run_record):
                raise RuntimeError("持久化故障: 邮箱 a@b.com 泄漏检查")

        failing = FailingBatch.__new__(FailingBatch)
        failing.__dict__.update(real.__dict__)
        service = _service(env, repo=failing)
        run = service.discover_relationships(env["source_id"])
        assert run["status"] == "failed"
        assert "a@b.com" not in run["errors"][0]["message"]
        # 零残留：失败运行没有写入任何候选修订
        assert env["repo"].list_relationships(env["source_id"],
                                              effective_only=False) == []

    def test_internal_error_uses_safe_message(self, env):
        def boom(*args, **kwargs):
            raise RuntimeError("爆炸: 密码=pwd123 邮箱=x@y.com")

        env["repo"].list_field_semantics_by_source = boom  # type: ignore[method-assign]
        service = _service(env)
        run = service.discover_relationships(env["source_id"])
        assert run["status"] == "failed"
        message = run["errors"][0]["message"]
        assert "pwd123" not in message and "x@y.com" not in message
        assert "未预期错误[RuntimeError]" in message and "已抑制" in message


class TestValidation:
    def test_validate_batch_validates_fk_candidates(self, env):
        service = _service(env, settings=_settings(
            relationship_validation_max_candidates=15))
        service.discover_relationships(env["source_id"])
        run = service.validate_batch(env["source_id"], limit=12)
        assert run["status"] in ("succeeded", "partial")
        assert run["candidates_validated"] >= 7
        assert run["queries_used"] > 0 and run["queries_used"] <= run["budget"]["max_sql_statements"]
        validated = service.list_relationships(env["source_id"], status="validated")
        pairs = {(r["source_table"], tuple(r["source_columns"]),
                  r["target_table"]) for r in validated}
        assert ("booking", ("guest_id",), "guest") in pairs
        assert ("payment", ("booking_no",), "booking") in pairs  # 无外键，凭证据验证

    def test_high_overlap_negative_stays_needs_review(self, env):
        service = _service(env, settings=_settings(
            relationship_validation_max_candidates=15))
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=12)
        tenant_seq = next(
            r for r in service.list_relationships(env["source_id"])
            if r["source_columns"] == ["tenant_seq"])
        assert tenant_seq["status"] == "needs_review"
        assert any("low_source_uniqueness" in b
                   for b in tenant_seq["validation"]["blocking"])

    def test_validated_scope_records_snapshot_and_limits(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=10)
        record = next(
            r for r in service.list_relationships(env["source_id"], status="validated")
            if r["source_columns"] == ["guest_id"])
        scope = record["validation"]["scope"]
        assert scope["snapshot_id"] == "snap-m3"
        assert scope["cardinality"] == "many_to_one"
        assert "不代表无条件" in scope["limits"]

    def test_structure_mismatch_marks_stale_without_evidence(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        altered = _service(env, alter_table="guest")
        run = altered.validate_batch(env["source_id"], limit=10)
        record = next(
            r for r in altered.list_relationships(env["source_id"])
            if r["source_columns"] == ["guest_id"])
        assert record["status"] == "stale"
        assert record.get("evidence") is None  # 不绑定证据
        assert any(r["status"] == "stale" for r in run["results"])

    def test_budget_exhaustion_leaves_rest_unprocessed(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        tight = _settings(relationship_validation_max_queries=4)
        bounded = _service(env, settings=tight)
        run = bounded.validate_batch(env["source_id"], limit=10)
        assert run["status"] == "partial"
        assert run["candidates_skipped"] >= 1
        assert any(e["reason"] == "budget_exceeded" for e in run["errors"])

    def test_probe_connection_error_sanitized_and_skipped(self, env):
        class FailingProbe(FakeRelationshipProbe):
            def window_metrics(self, request):
                raise DataSourceConnectionError(
                    "timeout", "MySQL 连接或查询失败（timeout）：样本值 guest@x.com")

        service = _service(env, probe=FailingProbe())
        service.discover_relationships(env["source_id"])
        run = service.validate_batch(env["source_id"], limit=3)
        # 全部候选都因连接错误失败 → failed（零候选处理）
        assert run["status"] == "failed"
        assert any("source_query_failed:timeout" in e["reason"] for e in run["errors"])
        for record in service.list_relationships(env["source_id"]):
            assert record["status"] == "candidate"  # 无残留

    def test_partial_probe_failure_isolated_per_candidate(self, env):
        class GuestOnlyFailProbe(FakeRelationshipProbe):
            def join_metrics(self, request):
                if request.right_table == "guest":
                    raise DataSourceConnectionError(
                        "timeout", "MySQL 连接或查询失败（timeout）：右端探测超时")
                return super().join_metrics(request)

        service = _service(env, probe=GuestOnlyFailProbe())
        service.discover_relationships(env["source_id"])
        run = service.validate_batch(env["source_id"], limit=12)
        # 个别候选失败被隔离：其余照常完成 → partial
        assert run["status"] == "partial"
        assert any("source_query_failed:timeout" in e["reason"] for e in run["errors"])
        assert run["candidates_validated"] >= 1

    def test_unexpected_probe_error_uses_safe_message(self, env):
        class ExplodingProbe(FakeRelationshipProbe):
            def join_metrics(self, request):
                raise RuntimeError("原始值泄漏: 13800001234")

        service = _service(env, probe=ExplodingProbe())
        service.discover_relationships(env["source_id"])
        run = service.validate_batch(env["source_id"], limit=2)
        assert run["status"] == "failed"
        message = " ".join(e["message"] for e in run["errors"])
        assert "13800001234" not in message
        assert "未预期错误[RuntimeError]" in message

    def test_missing_credential_fails_run(self, env):
        env["credentials"].secrets.clear()
        service = _service(env)
        service.discover_relationships(env["source_id"])
        run = service.validate_batch(env["source_id"], limit=2)
        assert run["status"] == "failed"
        assert any(e["reason"] == "credential_store_error" for e in run["errors"])

    def test_failed_validation_run_no_residue(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        real = env["repo"]

        class FailingBatch(type(real)):
            def save_relationship_batch(self, records, run_record):
                raise RuntimeError("batch 故障")

        failing = FailingBatch.__new__(FailingBatch)
        failing.__dict__.update(real.__dict__)
        service._repo = failing
        run = service.validate_batch(env["source_id"], limit=3)
        assert run["status"] == "failed"
        # 零残留：没有任何 validated/needs_review 新修订
        for record in service.list_relationships(env["source_id"]):
            assert record["status"] == "candidate"


class TestReview:
    def _candidate(self, env, service):
        service.discover_relationships(env["source_id"])
        return service.list_relationships(env["source_id"])[0]

    def test_confirm_reject_revert_flow(self, env):
        service = _service(env)
        record = self._candidate(env, service)
        key = record["relationship_key"]
        rev = service.submit_review(key, action="confirm",
                                    expected_revision=record["revision"])
        assert rev == 2
        effective = service.get_relationship(key)
        assert effective["status"] == "validated"
        assert effective["inference_source"] == "human"
        assert effective["validation"]["decided_by"] == "human"
        rev = service.submit_review(key, action="reject", expected_revision=2,
                                    note="业务上不应关联")
        assert rev == 3
        assert service.get_relationship(key)["status"] == "rejected"
        rev = service.submit_review(key, action="revert", expected_revision=3)
        assert rev == 4
        assert service.get_relationship(key)["status"] == "candidate"

    def test_invalid_transition_rejected(self, env):
        service = _service(env)
        record = self._candidate(env, service)
        with pytest.raises(ValueError, match="不允许的转换"):
            service.submit_review(
                record["relationship_key"], action="revert",
                expected_revision=record["revision"])

    def test_stale_revision_conflict(self, env):
        service = _service(env)
        record = self._candidate(env, service)
        with pytest.raises(RevisionConflictError):
            service.submit_review(
                record["relationship_key"], action="confirm",
                expected_revision=record["revision"] + 5)

    def test_concurrent_review_exactly_one_wins(self, env):
        service = _service(env)
        record = self._candidate(env, service)
        key = record["relationship_key"]
        barrier = threading.Barrier(2)
        results: list[Any] = []

        def submit():
            barrier.wait()
            try:
                results.append(service.submit_review(
                    key, action="confirm", expected_revision=record["revision"]))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for r in results if isinstance(r, int)) == 1
        assert sum(1 for r in results if isinstance(r, RevisionConflictError)) == 1

    def test_machine_rediscovery_does_not_override_human(self, env):
        service = _service(env)
        record = self._candidate(env, service)
        key = record["relationship_key"]
        service.submit_review(key, action="confirm",
                              expected_revision=record["revision"])
        # 同快照重发现：签名一致 → 幂等跳过；人工确认保持有效
        service.discover_relationships(env["source_id"])
        assert service.get_relationship(key)["status"] == "validated"

    def test_not_found_raises_404(self, env):
        service = _service(env)
        with pytest.raises(ResourceNotFoundError):
            service.get_relationship("missing-key")


class TestStaleAndRevalidate:
    def test_mark_stale_then_revalidate(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=10)
        record = next(
            r for r in service.list_relationships(env["source_id"], status="validated")
            if r["source_columns"] == ["guest_id"])
        revision = service.mark_stale(record["relationship_key"],
                                      reason="业务反馈口径变化")
        assert revision > record["revision"]
        assert service.get_relationship(record["relationship_key"])["status"] == "stale"
        # 重验：回到 validated（证据重新采集）
        run = service.revalidate(env["source_id"], record["relationship_key"])
        assert run["status"] == "succeeded"
        effective = service.get_relationship(record["relationship_key"])
        assert effective["status"] == "validated"
        assert effective["evidence"] is not None

    def test_revalidate_requires_stale_or_validatable(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=10)
        # validated 链不可直接重验（需先 revert 或标记 stale）
        record = next(
            r for r in service.list_relationships(env["source_id"], status="validated")
            if r["source_columns"] == ["guest_id"])
        with pytest.raises(ValueError, match="不可重验"):
            service.revalidate(env["source_id"], record["relationship_key"])
        # candidate 链（目标快照内）可重验（等同于验证）
        candidate = next(
            r for r in service.list_relationships(env["source_id"])
            if r["status"] == "candidate")
        run = service.revalidate(env["source_id"], candidate["relationship_key"])
        assert run["run_type"] == "validation"

    def test_paths_exclude_stale_edges(self, env):
        service = _service(env)
        service.discover_relationships(env["source_id"])
        service.validate_batch(env["source_id"], limit=10)
        tables = {
            t["table_name"]: t["table_id"]
            for t in env["repo"].list_table_metadata(env["source_id"])
        }
        result = service.paths(
            env["source_id"], tables["booking_item"], tables["tenant"])
        assert result["path_count"] >= 1  # booking_item→booking→guest→tenant
        # 将 booking→guest 置为 stale 后，该路径消失（默认只走 validated 非 stale）
        booking_guest = next(
            r for r in service.list_relationships(env["source_id"], status="validated")
            if r["source_table"] == "booking" and r["target_table"] == "guest")
        service.mark_stale(booking_guest["relationship_key"], reason="演示")
        result2 = service.paths(
            env["source_id"], tables["booking_item"], tables["tenant"])
        paths_tables = [
            [hop["to_table_id"] for hop in path[:-1]]
            for path in result2["paths"]
        ]
        assert all(tables["guest"] not in p for p in paths_tables)
