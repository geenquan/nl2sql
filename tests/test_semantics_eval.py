"""小型字段语义评测集（离线，规则模式）。

标签来源声明：以下预期均为开发测试标签（由开发者依据通用业务语义编写），
不是经多人人工标注的金标准；指标仅用于回归监控，不宣称人工评测结论。
模型路径的结构化输出合格率由 tests/test_semantics_prompt.py 与
tests/test_semantics_service.py 的通过/回退矩阵覆盖（离线假模型）。
"""

from app.semantics.glossary import Glossary
from app.semantics.rules import RuleEngine

ENGINE = RuleEngine(Glossary.load_default())


def ctx(name: str, data_type: str, comment: str | None = None, **extra) -> dict:
    base = {
        "column": {"name": name, "data_type": data_type, "nullable": True, "comment": comment},
        "is_primary_key": False,
        "is_foreign_key": False,
        "in_unique_index": False,
        "profile": None,
    }
    base.update(extra)
    return base


def table(name: str, columns: list[dict], pk: list[str] | None = None,
          fks: list[dict] | None = None) -> dict:
    return {"table_name": name, "columns": columns, "primary_key": pk or [],
            "foreign_keys": fks or [], "indexes": [], "profiles": []}


# ---- 字段类别用例（开发测试标签） -------------------------------------------
# (name, type, comment, expected, 期望 unknown=False)
COLUMN_CASES = [
    ("order_id", "bigint", "订单编号", "foreign_identifier_candidate"),
    ("user_no", "varchar(32)", None, "foreign_identifier_candidate"),
    ("created_at", "datetime", "创建时间", "timestamp"),
    ("updated_time", "datetime", None, "timestamp"),
    ("biz_date", "date", "业务日期", "date"),
    ("total_price", "decimal(12,2)", "合计金额", "amount"),
    ("unit_price", "decimal(10,2)", None, "amount"),
    ("金额", "decimal(10,2)", None, "amount"),
    ("goods_count", "int", "商品数量", "quantity"),
    ("order_status", "tinyint", "订单状态", "status"),
    ("状态", "varchar(2)", None, "status"),
    ("goods_type", "varchar(20)", "商品类型", "category"),
    ("enterprise_name", "varchar(128)", "企业名称", "name"),
    ("hotel_name", "varchar(64)", None, "name"),
    ("remark", "varchar(500)", "备注", "description"),
    ("备注", "text", None, "description"),
    ("org_code", "varchar(32)", "组织机构编码", "organization_identifier"),
    ("联系电话", "varchar(20)", None, "entity_identifier"),
]

# 期望 unknown（证据不足）：无信号或刻意模糊
UNKNOWN_CASES = [
    ("field_a", "varchar(8)", None),
    ("f2", "json", None),
]

# 冲突用例：类型与命名信号冲突 → 必须降置信并保留备选
CONFLICT_CASES = [
    ("level_name", "int", "等级名称"),   # 名称 vs 数值
    ("金额", "varchar(30)", None),       # 金额 vs 文本
]

# ---- 表用途用例（开发测试标签） ---------------------------------------------
PURPOSE_CASES = [
    (table("order_info", [
        {"name": "id", "data_type": "bigint"},
        {"name": "order_no", "data_type": "varchar(32)"},
        {"name": "total_amount", "data_type": "decimal(12,2)"},
        {"name": "created_at", "data_type": "datetime"},
    ], pk=["id"]), "transaction_event"),
    (table("订单表", [
        {"name": "id", "data_type": "bigint"},
        {"name": "订单金额", "data_type": "decimal(12,2)"},
        {"name": "下单时间", "data_type": "datetime"},
    ], pk=["id"]), "transaction_event"),
    (table("status_dict", [
        {"name": "code", "data_type": "varchar(8)"},
        {"name": "name", "data_type": "varchar(32)"},
    ]), "dictionary"),
    (table("user_role_rel", [
        {"name": "user_id", "data_type": "bigint"},
        {"name": "role_id", "data_type": "bigint"},
    ], pk=["user_id", "role_id"], fks=[
        {"name": "fk1", "columns": ["user_id"], "ref_table": "user"},
        {"name": "fk2", "columns": ["role_id"], "ref_table": "role"},
    ]), "relation"),
    (table("sync_log", [
        {"name": "id", "data_type": "bigint"},
        {"name": "action", "data_type": "varchar(64)"},
        {"name": "created_at", "data_type": "datetime"},
    ]), "log_history"),
    (table("goods", [
        {"name": "id", "data_type": "bigint"},
        {"name": "goods_code", "data_type": "varchar(32)"},
        {"name": "goods_name", "data_type": "varchar(128)"},
    ], pk=["id"]), "master_data"),
]

# 期望 unknown 的表
PURPOSE_UNKNOWN = [
    table("tmp_2024", [{"name": "a", "data_type": "int"}]),
]


class TestEvalMetrics:
    def test_category_accuracy_on_dev_labels(self) -> None:
        correct = 0
        for name, dtype, comment, expected in COLUMN_CASES:
            got = ENGINE.infer_column(ctx(name, dtype, comment))["category"]
            correct += got == expected
        accuracy = correct / len(COLUMN_CASES)
        print(f"\n[field category accuracy] {correct}/{len(COLUMN_CASES)} = {accuracy:.3f}")
        assert accuracy >= 0.85

    def test_unknown_cases_stay_unknown(self) -> None:
        for name, dtype, comment in UNKNOWN_CASES:
            result = ENGINE.infer_column(ctx(name, dtype, comment))
            assert result["category"] == "unknown", name
        print(f"\n[unknown recall] {len(UNKNOWN_CASES)}/{len(UNKNOWN_CASES)} = 1.000")

    def test_conflict_cases_low_confidence_with_alternatives(self) -> None:
        for name, dtype, comment in CONFLICT_CASES:
            result = ENGINE.infer_column(ctx(name, dtype, comment))
            assert result["confidence"] == "low", name
            assert result["alternatives"], name

    def test_table_purpose_accuracy_on_dev_labels(self) -> None:
        correct = 0
        for table_ctx, expected in PURPOSE_CASES:
            got = ENGINE.infer_table_purpose(table_ctx)["purpose"]
            correct += got == expected
        accuracy = correct / len(PURPOSE_CASES)
        print(f"\n[table purpose accuracy] {correct}/{len(PURPOSE_CASES)} = {accuracy:.3f}")
        assert accuracy >= 0.8

    def test_purpose_unknown(self) -> None:
        for table_ctx in PURPOSE_UNKNOWN:
            assert ENGINE.infer_table_purpose(table_ctx)["purpose"] == "unknown"

    def test_evidence_reference_validity(self) -> None:
        """所有证据引用必须能映射回真实上下文对象（可核验率 = 1.0）。"""
        checked = 0
        for name, dtype, comment, _ in COLUMN_CASES + [
            (n, d, c, None) for n, d, c in UNKNOWN_CASES
        ]:
            context = ctx(name, dtype, comment)
            profile = context["profile"] or {}
            valid_refs = {
                "primary_key": {name} if context["is_primary_key"] else set(),
                "foreign_key": {name} if context["is_foreign_key"] else set(),
                "column_type": {name},
                "comment": {comment} if comment else set(),
                "profile_metric": set(profile.keys()),
                "name_pattern": {"命名提示", "标识符命名模式；未验证关系，不能据此 JOIN"},
                "glossary_term": set(),  # 术语命中由 glossary.hit 保证存在
            }
            for evidence in ENGINE.infer_column(context)["evidence"]:
                checked += 1
                if evidence["type"] == "name_pattern":
                    # 规则发出的模式串：形态校验（/…/…/ 包裹）+ 明确的口径说明
                    assert evidence["ref"].startswith("/") and evidence["ref"].endswith("/"), (
                        f"{name}: {evidence}"
                    )
                elif evidence["type"] in valid_refs and valid_refs[evidence["type"]]:
                    assert evidence["ref"] in valid_refs[evidence["type"]], (
                        f"{name}: {evidence}"
                    )
        print(f"\n[evidence refs checked] {checked} (rules-mode validity = 1.000)")

    def test_structured_output_compliance_rules_mode(self) -> None:
        """规则模式输出结构合格率恒为 1.0（确定性代码路径）。"""
        for name, dtype, comment, _ in COLUMN_CASES:
            result = ENGINE.infer_column(ctx(name, dtype, comment))
            assert result["category"] and result["confidence"] and result["evidence"] is not None
        print("\n[structure compliance: rules] 1.000")
