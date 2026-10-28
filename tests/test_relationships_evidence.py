"""M3 证据采集测试：口径/分母、覆盖率、膨胀、空值与占位、成本门槛与策略。"""

import time

from app.relationships.evidence import (
    EvidenceCollector,
    ValidationBudget,
    decide_status,
    structure_changed,
)
from app.relationships.type_compat import type_compatibility
from tests.relationship_fixtures import SCHEMA, synthetic_catalog, synthetic_rows


def _budget(**overrides):
    defaults = {
        "max_queries": 20, "per_query_timeout_ms": 5000, "deadline_monotonic": None,
        "sample_window_rows": 500, "exact_max_rows": 50000,
    }
    defaults.update(overrides)
    return ValidationBudget(**defaults)


def _catalog_by_name():
    return {t["table_name"]: t for t in synthetic_catalog()}


def _record_for(source_table, source_col, target_table, target_col, **overrides):
    tables = _catalog_by_name()
    base = {
        "relationship_key": "rel-test",
        "source_id": "src-m3",
        "source_schema": SCHEMA, "source_table": source_table,
        "source_columns": [source_col],
        "source_table_id": tables[source_table]["table_id"],
        "target_schema": SCHEMA, "target_table": target_table,
        "target_columns": [target_col],
        "target_table_id": tables[target_table]["table_id"],
        "snapshot_id": "snap-m3",
        "type_verdict": "compatible", "type_reason": "",
        "scores": {"total_weighted": 0.8},
        "risks": [],
    }
    base.update(overrides)
    return base


def _collect(record, probe=None, budget=None, tables=None):
    probe = probe or FakeProbe()
    tables = tables or _catalog_by_name()
    collector = EvidenceCollector(probe)
    evidence = collector.collect(
        record, tables[record["source_table"]], tables[record["target_table"]],
        budget or _budget())
    return evidence, collector, probe


class FakeProbe:
    """轻量替身：直接复用共享 FakeRelationshipProbe 的数据与实现。"""

    def __init__(self, rows=None):
        from tests.relationship_fixtures import FakeRelationshipProbe
        self._inner = FakeRelationshipProbe(rows)

    def window_metrics(self, request):
        return self._inner.window_metrics(request)

    def join_metrics(self, request):
        return self._inner.join_metrics(request)


class TestWindowMetricsBasis:
    def test_small_table_exact_basis(self):
        evidence, _, _ = _collect(_record_for("payment", "booking_no", "booking", "booking_no"))
        # payment 160 行 < 窗口 500 → exact（窗口覆盖整表）
        assert evidence["source_side"]["basis"] == "exact"
        assert evidence["source_side"]["window_rows"] == 160
        assert evidence["source_side"]["truncated"] is False

    def test_truncated_window_is_sample_not_exact(self):
        evidence, _, _ = _collect(
            _record_for("booking", "guest_id", "guest", "id"),
            budget=_budget(sample_window_rows=50))
        assert evidence["source_side"]["basis"] == "sample"
        assert evidence["source_side"]["truncated"] is True

    def test_null_manager_excluded_from_uniqueness_not_denominator(self):
        evidence, _, _ = _collect(_record_for("employee", "manager_id", "employee", "id"))
        # 10 行、1 行 NULL：非空 9、去重 5
        assert evidence["source_side"]["window_rows"] == 10
        assert evidence["source_side"]["non_null_rows"] == 9
        assert evidence["source_side"]["distinct_values"] == 5
        assert evidence["uniqueness"]["source"]["non_null"] == 9


class TestCoverageMath:
    def test_forward_and_backward_direction_not_confused(self):
        evidence, _, _ = _collect(_record_for("payment", "booking_no", "booking", "booking_no"))
        forward = evidence["coverage"]["forward"]
        backward = evidence["coverage"]["backward"]
        # payment(160) → booking：全部命中（分母 160）；
        # booking(200) → payment：仅 B0001..B0100 存在于 payment → 100/200
        assert forward["ratio"] == 1.0
        assert forward["denominator"] == 160
        assert backward["ratio"] == 0.5
        assert backward["denominator"] == 200

    def test_null_keys_excluded_from_denominator(self):
        evidence, _, _ = _collect(_record_for("employee", "manager_id", "employee", "id"))
        forward = evidence["coverage"]["forward"]
        assert forward["denominator"] == 9  # NULL 管理者不入覆盖率分母
        assert forward["null_key_rows"] == 1
        assert forward["ratio"] == 1.0

    def test_join_fanout_detected_for_duplicated_target(self):
        # 右端重复：booking_item.booking_id 每值对 2 行 item —— 用左窗=booking 反向演示
        evidence, _, _ = _collect(_record_for("booking", "id", "booking_item", "booking_id"))
        forward = evidence["coverage"]["forward"]
        # booking.id(200) → booking_item.booking_id：全部命中，但每个 id 命中 2 行
        assert forward["ratio"] == 1.0
        assert forward["fanout_ratio"] == 2.0
        assert forward["inner_join_rows"] == 400

    def test_both_sampled_unmatched_not_disproof(self):
        evidence, _, _ = _collect(
            _record_for("booking", "guest_id", "guest", "id"),
            budget=_budget(sample_window_rows=30))
        forward = evidence["coverage"]["forward"]
        assert forward["basis"] == "sample"
        assert "样本缺失" in evidence["null_impact"]["note"]


class TestPlaceholderAndEmpty:
    def test_empty_string_matches_not_meaningful(self):
        rows = synthetic_rows()
        # 制造空串匹配：payment 增加一行 booking_no=''（booking 无此值，不匹配）
        rows[(SCHEMA, "payment")].append(
            {"id": 999, "booking_no": "", "amount": 1.0,
             "paid_at": "2026-01-07 00:00:00", "bank_txn_no": "TXN999999"})
        evidence, _, _ = _collect(
            _record_for("payment", "booking_no", "booking", "booking_no"),
            probe=FakeProbe(rows))
        source = evidence["source_side"]
        assert source["empty_string_rows"] == 1
        forward = evidence["coverage"]["forward"]
        # 空串不匹配右端 → unmatched（不是有意义匹配）
        assert forward["matched"] == 160

    def test_constant_column_flagged(self):
        rows = synthetic_rows()
        for row in rows[(SCHEMA, "tenant")]:
            row["name"] = "占位"  # 常量列（可能占位码）
        evidence, _, _ = _collect(
            _record_for("tenant", "name", "tenant", "id"), probe=FakeProbe(rows))
        assert evidence["target_side"]["constant_column"] is False
        assert evidence["source_side"]["constant_column"] is True
        assert any("占位" in note for note in evidence["notes"])


class TestCostGateAndBudget:
    def test_large_unindexed_right_table_gated(self):
        tables = _catalog_by_name()
        # 目标表无主键/无索引且行数估计超门槛 → JOIN 降级 unknown
        tables["guest"]["row_count_estimate"] = 100000
        tables["guest"]["primary_key"] = []
        tables["guest"]["indexes"] = []
        record = _record_for("booking", "guest_id", "guest", "id")
        evidence, _, _ = _collect(record, tables=tables)
        assert evidence["coverage"]["forward"]["basis"] == "unknown"
        assert any("cost_gate" in g["reason"] for g in evidence["gated"])

    def test_query_budget_exhaustion_gates_metrics(self):
        evidence, collector, _ = _collect(
            _record_for("booking", "guest_id", "guest", "id"),
            budget=_budget(max_queries=2))
        assert collector.queries_used == 2  # 只有双侧窗口执行，JOIN 被预算挡住
        assert any(g["metric"] == "join.forward" for g in evidence["gated"])
        assert evidence["coverage"]["forward"]["basis"] == "unknown"

    def test_deadline_expires_gates_all(self):
        evidence, _, _ = _collect(
            _record_for("booking", "guest_id", "guest", "id"),
            budget=_budget(deadline_monotonic=time.monotonic() - 1))
        assert evidence["source_side"]["basis"] == "unknown"
        assert evidence["coverage"]["forward"]["basis"] == "unknown"

    def test_row_count_unknown_without_index_gated(self):
        tables = _catalog_by_name()
        tables["guest"]["row_count_estimate"] = None
        tables["guest"]["primary_key"] = []
        tables["guest"]["indexes"] = []
        evidence, _, _ = _collect(
            _record_for("booking", "guest_id", "guest", "id"), tables=tables)
        assert any("row_count_unknown" in g["reason"] for g in evidence["gated"])


class TestStructureCheck:
    def test_structure_change_detected(self):
        old = _catalog_by_name()["guest"]
        new = dict(old)
        new["columns"] = old["columns"] + [{"name": "extra", "data_type": "int"}]
        assert structure_changed(old, new)
        assert not structure_changed(old, dict(old))

    def test_live_structure_matches(self):
        from tests.relationship_fixtures import FakeM3Connector
        connector = FakeM3Connector(synthetic_catalog())
        tables = _catalog_by_name()
        live = connector.get_table_metadata(SCHEMA, "guest")
        from app.services.relationship_service import RelationshipService
        live_dict = RelationshipService._live_table_dict(live)
        assert EvidenceCollector.structure_matches(tables["guest"], live_dict)


class TestValidationPolicy:
    def _validated_case(self, **overrides):
        record = _record_for("booking", "guest_id", "guest", "id", **overrides)
        evidence, _, _ = _collect(record)
        return decide_status(record, evidence, coverage_threshold=0.995)

    def test_full_evidence_auto_validates(self):
        validation = self._validated_case()
        assert validation["decision"] == "validated"
        assert validation["scope"]["cardinality"] == "many_to_one"
        assert validation["scope"]["snapshot_id"] == "snap-m3"
        assert "不代表无条件" in validation["scope"]["limits"]

    def test_sample_basis_blocks_auto_validation(self):
        record = _record_for("booking", "guest_id", "guest", "id")
        evidence, _, _ = _collect(record, budget=_budget(sample_window_rows=30))
        validation = decide_status(record, evidence, coverage_threshold=0.995)
        assert validation["decision"] == "needs_review"
        assert any("coverage_basis:sample" in b for b in validation["blocking"])

    def test_low_support_blocks_auto_validation(self):
        validation = self._validated_case(scores={"total_weighted": 0.3})
        assert validation["decision"] == "needs_review"
        assert any("support_score" in b for b in validation["blocking"])

    def test_type_incompatible_blocks(self):
        validation = self._validated_case(
            type_verdict="incompatible", type_reason="类型族不同")
        assert validation["decision"] == "needs_review"
        assert any(b.startswith("type:") for b in validation["blocking"])

    def test_fanout_blocks(self):
        record = _record_for("booking", "id", "booking_item", "booking_id")
        evidence, _, _ = _collect(record)
        validation = decide_status(record, evidence, coverage_threshold=0.995)
        assert validation["decision"] == "needs_review"
        assert any("join_fanout" in b for b in validation["blocking"])

    def test_low_source_uniqueness_risk_blocks(self):
        validation = self._validated_case(
            risks=[{"type": "low_source_uniqueness", "note": "共享编码风险"}])
        assert validation["decision"] == "needs_review"
        assert any("low_source_uniqueness" in b for b in validation["blocking"])

    def test_no_target_uniqueness_blocks(self):
        # 目标端不唯一（tenant_seq 200 行仅 20 个值）→ 阻断自动验证
        record = _record_for("booking", "id", "booking", "tenant_seq")
        evidence, _, _ = _collect(record)
        validation = decide_status(record, evidence, coverage_threshold=0.995)
        assert validation["decision"] == "needs_review"
        assert any("target_not_unique" in b for b in validation["blocking"])


class TestTypeCompatibility:
    def test_matrix(self):
        assert type_compatibility("int", "bigint")[0] == "compatible"
        assert type_compatibility("bigint", "int")[0] == "risky"  # 源更宽
        assert type_compatibility("int unsigned", "int")[0] == "risky"
        assert type_compatibility("varchar(20)", "varchar(50)")[0] == "compatible"
        assert type_compatibility("varchar(80)", "varchar(50)")[0] == "risky"
        assert type_compatibility("varchar(20)", "int")[0] == "incompatible"
        assert type_compatibility("date", "datetime")[0] == "risky"
        assert type_compatibility("datetime", "datetime")[0] == "compatible"
        assert type_compatibility("decimal(10,2)", "decimal(12,2)")[0] == "compatible"
        assert type_compatibility("decimal(10,3)", "decimal(12,2)")[0] == "risky"
        assert type_compatibility("text", "varchar(50)")[0] == "risky"
