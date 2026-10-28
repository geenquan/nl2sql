"""规则初筛测试：类别、冲突、unknown、口径 caveat 与表用途。"""

from app.domain.semantics import EVIDENCE_TYPES, FIELD_CATEGORIES, TABLE_PURPOSES
from app.semantics.glossary import Glossary
from app.semantics.rules import RULES_VERSION, RuleEngine


def make_engine() -> RuleEngine:
    return RuleEngine(Glossary.load_default())


def column_ctx(**overrides):
    base = {
        "column": {"name": "x", "data_type": "varchar(64)", "nullable": True, "comment": None},
        "is_primary_key": False,
        "is_foreign_key": False,
        "in_unique_index": False,
        "profile": None,
    }
    base.update(overrides)
    return base


def table_ctx(**overrides):
    base = {
        "table_name": "t",
        "columns": [{"name": "id", "data_type": "bigint"}],
        "primary_key": ["id"],
        "foreign_keys": [],
        "indexes": [],
        "profiles": [],
    }
    base.update(overrides)
    return base


def assert_valid_semantics(result: dict) -> None:
    assert result["category"] in FIELD_CATEGORIES
    assert result["confidence"] in ("low", "medium", "high")
    assert result["inference_source"] == "rules"
    assert result["rules_version"] == RULES_VERSION
    for evidence in result["evidence"]:
        assert evidence["type"] in EVIDENCE_TYPES
        assert evidence["ref"]
    for alternative in result["alternatives"]:
        assert alternative["category"] in FIELD_CATEGORIES


class TestColumnRules:
    def test_primary_key_is_entity_identifier_with_basis_caveat(self) -> None:
        result = make_engine().infer_column(
            column_ctx(
                column={"name": "id", "data_type": "bigint", "nullable": False, "comment": "主键"},
                is_primary_key=True,
                profile={
                    "distinct_count_estimate": 500,
                    "uniqueness_ratio": 1.0,
                    "metric_basis": "sample",
                    "sample_row_count": 500,
                    "row_count_basis": 500,
                },
            )
        )
        assert result["category"] == "entity_identifier"
        assert result["confidence"] == "high"
        types = {e["type"] for e in result["evidence"]}
        assert "primary_key" in types
        metric = next(e for e in result["evidence"] if e["type"] == "profile_metric")
        assert "样本" in metric["detail"]  # 口径 caveat：样本唯一≠全表唯一

    def test_non_pk_id_column_is_foreign_candidate_not_verified(self) -> None:
        result = make_engine().infer_column(
            column_ctx(column={"name": "customer_id", "data_type": "bigint", "nullable": True, "comment": None})
        )
        assert result["category"] == "foreign_identifier_candidate"
        assert "未验证" in (result.get("rationale") or "") or any(
            "未验证" in alt.get("rationale", "") for alt in result["alternatives"]
        ) or any("未验证" in e["detail"] for e in result["evidence"])

    def test_fk_column_foreign_candidate_high_confidence(self) -> None:
        result = make_engine().infer_column(
            column_ctx(
                column={"name": "cust_no", "data_type": "bigint", "nullable": True, "comment": None},
                is_foreign_key=True,
            )
        )
        assert result["category"] == "foreign_identifier_candidate"
        assert result["confidence"] == "high"
        assert any(e["type"] == "foreign_key" for e in result["evidence"])

    def test_datetime_types(self) -> None:
        engine = make_engine()
        ts = engine.infer_column(
            column_ctx(column={"name": "created_at", "data_type": "datetime", "nullable": True, "comment": None})
        )
        assert ts["category"] == "timestamp"
        date = engine.infer_column(
            column_ctx(column={"name": "biz_day", "data_type": "date", "nullable": True, "comment": None})
        )
        assert date["category"] == "date"

    def test_amount_quantity_status_category_name_description(self) -> None:
        engine = make_engine()
        cases = [
            ({"name": "total_amount", "data_type": "decimal(10,2)", "nullable": True, "comment": None}, "amount"),
            ({"name": "数量", "data_type": "int", "nullable": True, "comment": None}, "quantity"),
            ({"name": "order_status", "data_type": "varchar(20)", "nullable": True, "comment": None}, "status"),
            ({"name": "goods_type", "data_type": "varchar(20)", "nullable": True, "comment": None}, "category"),
            ({"name": "hotel_name", "data_type": "varchar(128)", "nullable": True, "comment": None}, "name"),
            ({"name": "remark", "data_type": "varchar(500)", "nullable": True, "comment": "备注"}, "description"),
        ]
        for column, expected in cases:
            result = engine.infer_column(column_ctx(column=column))
            assert result["category"] == expected, column["name"]
            assert_valid_semantics(result)

    def test_comment_hint_used_with_glossary(self) -> None:
        result = make_engine().infer_column(
            column_ctx(column={"name": "field_a", "data_type": "decimal(12,2)", "nullable": True, "comment": "订单金额"})
        )
        assert result["category"] == "amount"
        assert any(e["type"] == "comment" for e in result["evidence"])

    def test_conflicting_signals_keep_alternatives_low_confidence(self) -> None:
        result = make_engine().infer_column(
            column_ctx(column={"name": "level", "data_type": "int", "nullable": True, "comment": "客户名称"})
        )
        # 名称/注释提示 name，类型提示数值度量 → 冲突保留双候选并降置信
        assert result["confidence"] == "low"
        categories = {result["category"]} | {a["category"] for a in result["alternatives"]}
        assert "name" in categories
        assert "measurement" in categories or "unknown" in categories

    def test_no_signals_is_unknown_not_fabricated(self) -> None:
        result = make_engine().infer_column(
            column_ctx(column={"name": "field_x", "data_type": "varchar(10)", "nullable": True, "comment": None})
        )
        assert result["category"] == "unknown"
        assert result["confidence"] == "low"

    def test_empty_profile_does_not_fabricate_metrics(self) -> None:
        result = make_engine().infer_column(
            column_ctx(
                column={"name": "code", "data_type": "varchar(32)", "nullable": True, "comment": None},
                profile={"metric_basis": "insufficient_sample", "sample_row_count": 0,
                         "distinct_count_estimate": None, "uniqueness_ratio": None},
            )
        )
        assert not [e for e in result["evidence"] if e["type"] == "profile_metric"]

    def test_sample_basis_uniqueness_not_treated_as_pk(self) -> None:
        # 非主键列样本完全唯一：仍是 foreign/普通标识候选，不得断定主键成立
        result = make_engine().infer_column(
            column_ctx(
                column={"name": "order_no", "data_type": "varchar(32)", "nullable": True, "comment": None},
                in_unique_index=True,
                profile={"distinct_count_estimate": 500, "uniqueness_ratio": 1.0,
                         "metric_basis": "sample", "sample_row_count": 500, "row_count_basis": 500},
            )
        )
        assert result["category"] != "entity_identifier" or result["confidence"] != "high"
        metric = next((e for e in result["evidence"] if e["type"] == "profile_metric"), None)
        if metric:
            assert "样本" in metric["detail"]

    def test_glossary_organization_identifier(self) -> None:
        result = make_engine().infer_column(
            column_ctx(column={"name": "org_code", "data_type": "varchar(32)", "nullable": True, "comment": "组织机构编码"})
        )
        assert result["category"] == "organization_identifier"
        assert "organization" in result["candidate_entities"]
        assert any(e["type"] == "glossary_term" for e in result["evidence"])

    def test_status_code_meaning_not_inferred_from_industry_knowledge(self) -> None:
        # 状态字段不得根据“行业常识”猜测具体取值含义
        result = make_engine().infer_column(
            column_ctx(column={"name": "status", "data_type": "tinyint", "nullable": True, "comment": "状态"})
        )
        assert result["category"] == "status"
        assert not result.get("value_meanings")


class TestTablePurposeRules:
    def test_dictionary_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="goods_type_dict",
                columns=[
                    {"name": "id", "data_type": "int"},
                    {"name": "code", "data_type": "varchar(32)"},
                    {"name": "name", "data_type": "varchar(64)"},
                ],
            )
        )
        assert result["purpose"] == "dictionary"
        assert result["purpose"] in TABLE_PURPOSES

    def test_relation_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="goods_hotel_relation",
                columns=[
                    {"name": "goods_id", "data_type": "bigint"},
                    {"name": "hotel_id", "data_type": "bigint"},
                ],
                primary_key=["goods_id", "hotel_id"],
                foreign_keys=[
                    {"name": "fk1", "columns": ["goods_id"], "ref_table": "goods"},
                    {"name": "fk2", "columns": ["hotel_id"], "ref_table": "hotel"},
                ],
            )
        )
        assert result["purpose"] == "relation"

    def test_detail_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="order_detail",
                columns=[
                    {"name": "id", "data_type": "bigint"},
                    {"name": "order_id", "data_type": "bigint"},
                    {"name": "amount", "data_type": "decimal(10,2)"},
                ],
            )
        )
        assert result["purpose"] == "detail"

    def test_log_history_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="operation_log",
                columns=[
                    {"name": "id", "data_type": "bigint"},
                    {"name": "action", "data_type": "varchar(64)"},
                    {"name": "created_at", "data_type": "datetime"},
                    {"name": "operator", "data_type": "varchar(64)"},
                ],
            )
        )
        assert result["purpose"] == "log_history"

    def test_transaction_event_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="订单表",
                columns=[
                    {"name": "id", "data_type": "bigint"},
                    {"name": "order_no", "data_type": "varchar(32)"},
                    {"name": "total_amount", "data_type": "decimal(12,2)"},
                    {"name": "created_at", "data_type": "datetime"},
                ],
            )
        )
        assert result["purpose"] == "transaction_event"

    def test_master_data_table(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(
                table_name="enterprise",
                columns=[
                    {"name": "id", "data_type": "bigint"},
                    {"name": "org_code", "data_type": "varchar(32)"},
                    {"name": "enterprise_name", "data_type": "varchar(128)"},
                ],
            )
        )
        assert result["purpose"] == "master_data"

    def test_ambiguous_table_keeps_alternatives_or_unknown(self) -> None:
        result = make_engine().infer_table_purpose(
            table_ctx(table_name="temp_x", columns=[{"name": "a", "data_type": "int"}], primary_key=[])
        )
        assert result["purpose"] == "unknown"
        assert result["confidence"] == "low"
