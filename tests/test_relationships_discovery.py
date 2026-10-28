"""M3 召回测试：外键/命名/语义/负例防线、Top-K、预算与类型兼容。"""

import time

from app.relationships.discovery import RelationshipDiscoveryService
from tests.relationship_fixtures import SCHEMA, synthetic_catalog


def _profiles_map(catalog):
    """模拟仓储画像（仅召回所需字段）。"""
    profiles = {}
    for table in catalog:
        plist = []
        for column in table["columns"]:
            name = column["name"]
            uniqueness = 0.1 if name == "tenant_seq" else (
                1.0 if name.endswith("_id") or name == "id" else 0.5)
            plist.append({
                "column_name": name, "uniqueness_ratio": uniqueness,
                "metric_basis": "exact", "sample_row_count": 100,
            })
        profiles[table["table_id"]] = plist
    return profiles


def _semantics_list(catalog):
    from tests.relationship_fixtures import seed_semantics  # 复用同一份语义映射
    recorded = []

    class _Capture:
        def save_field_semantics(self, record, expected_revision=None):
            recorded.append(record)
            return 1

    seed_semantics(_Capture(), catalog)  # type: ignore[arg-type]
    return recorded


def _run(catalog, semantics=None, **kwargs):
    service = RelationshipDiscoveryService()
    return service.recall_candidates(
        catalog, _profiles_map(catalog),
        semantics if semantics is not None else _semantics_list(catalog),
        **kwargs)


def _pairs(candidates):
    return {
        (c.source_table["table_name"], tuple(c.source_columns),
         c.target_table["table_name"], tuple(c.target_columns))
        for c in candidates
    }


class TestExplicitForeignKeyRecall:
    def test_all_declared_fks_recalled(self):
        candidates, notes = _run(synthetic_catalog())
        pairs = _pairs(candidates)
        for expected in [
            ("booking", ("guest_id",), "guest", ("id",)),
            ("booking", ("room_id",), "room", ("id",)),
            ("guest", ("tenant_id",), "tenant", ("id",)),
            ("room", ("tenant_id",), "tenant", ("id",)),
            ("room", ("type_id",), "room_type", ("id",)),
            ("booking_item", ("booking_id",), "booking", ("id",)),
            ("booking_item", ("item_id",), "service_item", ("id",)),
            ("employee", ("manager_id",), "employee", ("id",)),
        ]:
            assert expected in pairs, expected
        assert not notes

    def test_self_reference_flagged(self):
        candidates, _ = _run(synthetic_catalog())
        self_ref = [c for c in candidates
                    if c.source_table["table_name"] == "employee"]
        assert len(self_ref) == 1
        assert any(r["type"] == "self_referential" for r in self_ref[0].risks)

    def test_composite_fk_keeps_ordered_mapping(self):
        catalog = synthetic_catalog()
        # 人工构造组合外键：order_ext(a_id,b_id) → pk(a,b)
        catalog.append({
            "table_id": "snap-m3-order_ext", "source_id": "src-m3",
            "snapshot_id": "snap-m3", "schema_name": SCHEMA,
            "table_name": "order_ext", "table_comment": None,
            "columns": [{"name": "a_id", "data_type": "int"},
                        {"name": "b_id", "data_type": "int"}],
            "primary_key": ["a_id", "b_id"],
            "foreign_keys": [{
                "name": "fk_comp", "columns": ["a_id", "b_id"],
                "ref_schema": SCHEMA, "ref_table": "dim",
                "ref_columns": ["da", "db"]}],
            "indexes": [], "row_count_estimate": None, "row_count_is_estimate": True,
        })
        catalog.append({
            "table_id": "snap-m3-dim", "source_id": "src-m3",
            "snapshot_id": "snap-m3", "schema_name": SCHEMA,
            "table_name": "dim", "table_comment": None,
            "columns": [{"name": "da", "data_type": "int"},
                        {"name": "db", "data_type": "int"}],
            "primary_key": ["da", "db"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": None, "row_count_is_estimate": True,
        })
        candidates, _ = _run(catalog)
        composite = [c for c in candidates if c.source_table["table_name"] == "order_ext"]
        assert len(composite) == 1
        assert composite[0].source_columns == ["a_id", "b_id"]
        assert composite[0].target_columns == ["da", "db"]  # 有序映射，不拆单列

    def test_dangling_fk_reported_not_inferred(self):
        catalog = synthetic_catalog()
        catalog[4]["foreign_keys"].append({  # booking → 不存在的表
            "name": "fk_dangling", "columns": ["guest_id"],
            "ref_schema": SCHEMA, "ref_table": "missing_table",
            "ref_columns": ["id"]})
        candidates, notes = _run(catalog)
        pairs = _pairs(candidates)
        assert all(p[2] != "missing_table" for p in pairs)
        assert any(n["reason"] == "dangling_foreign_key" for n in notes)


class TestSignalRecall:
    def test_name_rule_recalls_no_fk_positive(self):
        # payment.booking_no → booking.booking_no（无外键，词法+类型兼容）
        candidates, _ = _run(synthetic_catalog())
        assert ("payment", ("booking_no",), "booking", ("booking_no",)) in _pairs(candidates)

    def test_risky_low_uniqueness_negative_recalled_with_risk(self):
        # booking.tenant_seq → tenant.id：召回但必须带低唯一率风险（共享编码/租户编号）
        candidates, _ = _run(synthetic_catalog())
        target = [c for c in candidates
                  if c.source_table["table_name"] == "booking"
                  and c.source_columns == ["tenant_seq"]]
        assert len(target) == 1
        assert any(r["type"] == "low_source_uniqueness" for r in target[0].risks)

    def test_generic_columns_not_paired(self):
        # id↔id、status_code↔status_code 不产生候选（共享状态码/通用词防线）
        candidates, _ = _run(synthetic_catalog())
        pairs = _pairs(candidates)
        for pair in pairs:
            cols = (pair[1][0], pair[3][0])
            assert cols != ("status_code", "status_code"), pair
            assert not (cols[0] == "id" and cols[1] == "id")

    def test_dictionary_code_pair_not_recalled(self):
        # room_type.code ↔ service_item.code：双端均为被引用方特征 → 不配对
        candidates, _ = _run(synthetic_catalog())
        pairs = _pairs(candidates)
        assert all(not (
            {pair[0], pair[2]} == {"room_type", "service_item"}
            and pair[1][0] == "code" and pair[3][0] == "code"
        ) for pair in pairs)

    def test_incompatible_types_excluded(self):
        # 同 token 但字符串 vs 整数 → 不召回（编码映射走人工登记）
        catalog = [{
            "table_id": "t1", "source_id": "s", "snapshot_id": "snap",
            "schema_name": SCHEMA, "table_name": "orders", "table_comment": None,
            "columns": [{"name": "guest_code", "data_type": "varchar(20)"}],
            "primary_key": ["guest_code"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }, {
            "table_id": "t2", "source_id": "s", "snapshot_id": "snap",
            "schema_name": SCHEMA, "table_name": "guests", "table_comment": None,
            "columns": [{"name": "guest_code", "data_type": "int"}],
            "primary_key": ["guest_code"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }]
        candidates, _ = _run(catalog)
        assert candidates == []

    def test_risky_types_recalled_with_risk(self):
        # date vs datetime / int 宽度：召回但带 type_risk
        catalog = [{
            "table_id": "t1", "source_id": "s", "snapshot_id": "snap",
            "schema_name": SCHEMA, "table_name": "events", "table_comment": None,
            "columns": [{"name": "event_day_id", "data_type": "date"}],
            "primary_key": [], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }, {
            "table_id": "t2", "source_id": "s", "snapshot_id": "snap",
            "schema_name": SCHEMA, "table_name": "event_days", "table_comment": None,
            "columns": [{"name": "event_day", "data_type": "datetime"}],
            "primary_key": ["event_day"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }]
        candidates, _ = _run(catalog)
        assert len(candidates) == 1
        assert candidates[0].type_verdict == "risky"
        assert any(r["type"] == "type_risk" for r in candidates[0].risks)

    def test_composite_target_risk_flagged(self):
        # 目标表为组合主键且匹配列非独立唯一 → 组合键风险
        catalog = synthetic_catalog()
        catalog.append({
            "table_id": "snap-m3-ext", "source_id": "src-m3",
            "snapshot_id": "snap-m3", "schema_name": SCHEMA,
            "table_name": "guest_ext", "table_comment": None,
            "columns": [{"name": "guest_ref_id", "data_type": "int"}],
            "primary_key": [], "foreign_keys": [],
            "indexes": [], "row_count_estimate": 5, "row_count_is_estimate": True,
        })
        # 把 guest 表主键改造成组合键（id + phone）
        guest = next(t for t in catalog if t["table_name"] == "guest")
        guest["primary_key"] = ["id", "phone"]
        candidates, _ = _run(catalog)
        target = [c for c in candidates
                  if c.source_table["table_name"] == "guest_ext"]
        assert len(target) == 1
        assert any(r["type"] == "composite_key_risk" for r in target[0].risks)

    def test_topk_per_field_limit(self):
        # 同一引用方列只保留 Top-K 目标
        catalog = [{
            "table_id": "t-main", "source_id": "s", "snapshot_id": "snap",
            "schema_name": SCHEMA, "table_name": "orders", "table_comment": None,
            "columns": [{"name": "user_id", "data_type": "int"}],
            "primary_key": ["id"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 10, "row_count_is_estimate": True,
        }]
        # 加一个 id 列（主键引用）——orders.columns 需含 id
        catalog[0]["columns"].insert(0, {"name": "id", "data_type": "int"})
        for i in range(1, 8):
            catalog.append({
                "table_id": f"t-user{i}", "source_id": "s", "snapshot_id": "snap",
                "schema_name": SCHEMA, "table_name": f"user_group{i}", "table_comment": None,
                "columns": [{"name": "id", "data_type": "int"},
                            {"name": "user_code", "data_type": "int"}],
                "primary_key": ["id"], "foreign_keys": [], "indexes": [],
                "row_count_estimate": 10, "row_count_is_estimate": True,
            })
            # 目标桶：user_groupN 表名 token 不匹配；改用与 user_id 同 token 的列
            catalog[-1]["columns"].append({"name": f"user{i}_id", "data_type": "int"})
            catalog[-1]["indexes"] = [{"name": f"uq{i}", "columns": [f"user{i}_id"],
                                       "unique": True}]
        candidates, _ = _run(catalog, topk_per_field=3)
        from_user = [c for c in candidates
                     if c.source_table["table_name"] == "orders"]
        assert len(from_user) <= 3

    def test_max_candidates_cap(self):
        candidates, notes = _run(synthetic_catalog(), max_candidates=3)
        assert len(candidates) == 3
        assert any(n["reason"] == "budget_truncated" for n in notes)

    def test_deadline_stops_recall(self):
        candidates, notes = _run(
            synthetic_catalog(), deadline=time.monotonic() - 1)
        assert candidates == []
        assert any(n["reason"] == "deadline_exceeded" for n in notes)


class TestSemanticsUsage:
    def test_rejected_semantics_not_positive(self):
        catalog = synthetic_catalog()
        semantics = _semantics_list(catalog)
        for record in semantics:
            if (record["table_id"].endswith("-payment")
                    and record["column_name"] == "booking_no"):
                record["review_status"] = "rejected"
        candidates, _ = _run(catalog, semantics=semantics)
        target = [c for c in candidates
                  if c.source_table["table_name"] == "payment"]
        assert len(target) == 1
        assert target[0].scores["semantic"] == 0.0  # rejected 不作为正向证据
        assert any(r["type"] == "semantics_rejected" for r in target[0].risks)

    def test_human_confirmed_semantics_boost(self):
        catalog = synthetic_catalog()
        semantics = _semantics_list(catalog)
        for record in semantics:
            if (record["table_id"].endswith("-payment")
                    and record["column_name"] == "booking_no"):
                record["review_status"] = "business_confirmed"
        base, _ = _run(synthetic_catalog())
        boosted, _ = _run(catalog, semantics=semantics)
        base_score = next(c for c in base
                          if c.source_table["table_name"] == "payment").scores["semantic"]
        boosted_score = next(c for c in boosted
                             if c.source_table["table_name"] == "payment").scores["semantic"]
        assert boosted_score > base_score

    def test_scores_are_not_probabilities_and_versioned(self):
        candidates, _ = _run(synthetic_catalog())
        scores = candidates[0].scores
        assert scores["naming_method"] == "lexical"  # 词法基线，不伪造向量分
        assert scores["version"] and scores["grade"] in ("low", "medium", "high")
        assert 0.0 <= scores["total_weighted"] <= 1.0

    def test_profile_uniqueness_carries_caveat(self):
        candidates, _ = _run(synthetic_catalog())
        tenant_seq = next(c for c in candidates
                          if c.source_columns == ["tenant_seq"])
        reasons_text = " ".join(str(r.get("detail", "")) for r in tenant_seq.reasons)
        assert "样本唯一不能断定全表唯一" in reasons_text


class TestIdempotency:
    def test_signature_stable_and_distinguishing(self):
        candidates, _ = _run(synthetic_catalog())
        signatures = {c.signature() for c in candidates}
        assert len(signatures) == len(candidates)  # 候选互不相同
        again, _ = _run(synthetic_catalog())
        assert {c.signature() for c in again} == signatures  # 重复召回签名一致
