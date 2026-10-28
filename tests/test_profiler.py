from decimal import Decimal
from pathlib import Path

from app.domain.metadata import ColumnMetadata, TableMetadata
from app.profiling.profiler import DataProfiler


def make_table() -> TableMetadata:
    return TableMetadata(
        source_id="src-1",
        schema_name="sales",
        table_name="orders",
        columns=(
            ColumnMetadata(name="id", data_type="bigint", nullable=False),
            ColumnMetadata(name="amount", data_type="decimal"),
            ColumnMetadata(name="note", data_type="varchar(50)"),
            ColumnMetadata(name="user_password", data_type="varchar(50)"),
            ColumnMetadata(name="avatar", data_type="blob"),
            ColumnMetadata(name="contact", data_type="varchar(100)"),
            ColumnMetadata(name="id_number", data_type="char(18)"),
            ColumnMetadata(name="remark", data_type="varchar(200)"),
        ),
        primary_key=("id",),
    )


def sample_rows() -> list[dict]:
    return [
        {
            "id": 1, "amount": Decimal("10.00"), "note": "a", "user_password": "p1",
            "avatar": b"x", "contact": "zhang_san@example.com",
            "id_number": "330106199001011234", "remark": "13812345678",
        },
        {
            "id": 2, "amount": Decimal("10.00"), "note": "b", "user_password": "p2",
            "avatar": None, "contact": "li_si@example.com",
            "id_number": "330106199001015678", "remark": "备用电话 13912345678",
        },
    ]


def test_profiles_mixed_null_and_distinct_metrics() -> None:
    rows = [
        {"id": 1, "amount": Decimal("10.00"), "note": "a", "user_password": "p1", "avatar": b"x",
         "contact": None, "id_number": None, "remark": None},
        {"id": 2, "amount": Decimal("10.00"), "note": "b", "user_password": "p2", "avatar": None,
         "contact": None, "id_number": None, "remark": None},
        {"id": 3, "amount": None, "note": "b", "user_password": None, "avatar": None,
         "contact": None, "id_number": None, "remark": None},
    ]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=10)
    }
    amount = profiles["amount"]
    assert amount.null_ratio == 1 / 3
    assert amount.distinct_count_estimate == 1
    assert amount.uniqueness_ratio == 0.5
    assert amount.metric_basis == "exact"  # 返回行数 < LIMIT，说明覆盖整表
    assert amount.sample_row_count == 3
    assert amount.empty_string_count == 0
    note = profiles["note"]
    assert note.distinct_count_estimate == 2
    assert note.uniqueness_ratio == 2 / 3  # 3 个非空值去重后 2 个
    assert amount.computed_at is not None


def test_sample_basis_when_limit_reached() -> None:
    rows = [
        {"id": i, "amount": 1, "note": "x", "user_password": "p", "avatar": None}
        for i in range(5)
    ]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=5)
    }
    assert profiles["id"].metric_basis == "sample"  # 恰好等于 LIMIT，无法确认整表
    assert profiles["id"].distinct_count_estimate == 5
    assert profiles["id"].row_count_basis == 5


def test_empty_table_is_marked_insufficient_not_fabricated() -> None:
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), [], sample_limit=100)
    }
    for profile in profiles.values():
        assert profile.metric_basis == "insufficient_sample"
        assert profile.null_ratio is None
        assert profile.distinct_count_estimate is None
        assert profile.uniqueness_ratio is None
        assert profile.sample_values == []
        assert profile.sample_row_count == 0


def test_all_null_column_reports_null_ratio_one() -> None:
    rows = [
        {"id": 1, "amount": None, "note": "a", "user_password": "x", "avatar": None},
        {"id": 2, "amount": None, "note": "b", "user_password": "y", "avatar": None},
    ]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=10)
    }
    amount = profiles["amount"]
    assert amount.null_ratio == 1.0
    assert amount.distinct_count_estimate == 0
    assert amount.uniqueness_ratio is None  # 无非空值，唯一率无定义
    assert amount.sample_values == []


def test_empty_string_is_distinguished_from_null() -> None:
    rows = [
        {"id": 1, "amount": 1, "note": "", "user_password": "a", "avatar": None},
        {"id": 2, "amount": 1, "note": "", "user_password": "b", "avatar": None},
        {"id": 3, "amount": 1, "note": None, "user_password": "c", "avatar": None},
    ]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=10)
    }
    note = profiles["note"]
    assert note.null_ratio == 1 / 3
    assert note.empty_string_count == 2
    assert note.distinct_count_estimate == 1  # 两个空字符串视为同一个值
    assert note.samples_redacted is False


def test_sensitive_column_never_returns_raw_samples() -> None:
    rows = [
        {"id": 1, "amount": 1, "note": "n", "user_password": "hunter2", "avatar": b"z"},
    ]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=10)
    }
    pwd = profiles["user_password"]
    assert pwd.sample_values == ["***"]
    assert pwd.samples_redacted is True
    assert "hunter2" not in str(pwd)
    # 聚合指标仍计算（不泄露行级数据）
    assert pwd.distinct_count_estimate == 1


def test_value_format_detection_applies_in_profiles() -> None:
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(
            make_table(), sample_rows(), sample_limit=10
        )
    }
    contact = profiles["contact"]
    assert contact.sample_values == ["z***@example.com", "l***@example.com"]
    assert contact.samples_redacted is True
    assert "zhang_san@example.com" not in str(contact)

    id_number = profiles["id_number"]  # 强敏感列名：整列遮蔽
    assert id_number.sample_values == ["***", "***"]
    assert id_number.samples_redacted is True
    assert "330106199001011234" not in str(id_number)

    remark = profiles["remark"]  # 通用列：纯手机号部分遮蔽，嵌入文本整值遮蔽
    assert remark.sample_values[0] == "****5678"
    assert remark.sample_values[1] == "***"
    assert remark.samples_redacted is True

    # 聚合指标不受脱敏影响，仍按原始值计算
    assert contact.distinct_count_estimate == 2
    assert id_number.distinct_count_estimate == 2


def test_extra_sensitive_keywords_from_config() -> None:
    rows = [{"id": 1, "campus_code": "C001"}]
    table = TableMetadata(
        source_id="src-1", schema_name="s", table_name="t",
        columns=(ColumnMetadata(name="campus_code", data_type="varchar(8)"),),
    )
    default_profiles = DataProfiler().profile_table(table, rows, sample_limit=10)
    assert default_profiles[0].sample_values == ["C001"]
    configured = DataProfiler(sensitive_keywords=("campus",))
    custom_profiles = configured.profile_table(table, rows, sample_limit=10)
    assert custom_profiles[0].sample_values == ["***"]
    assert custom_profiles[0].samples_redacted is True


def test_binary_column_samples_show_length_only() -> None:
    rows = [{"id": 1, "amount": 1, "note": "n", "user_password": "p", "avatar": b"\x00\x01\x02"}]
    profiles = {
        p.column_name: p for p in DataProfiler().profile_table(make_table(), rows, sample_limit=10)
    }
    assert profiles["avatar"].sample_values == ["<binary:3B>"]
    assert profiles["avatar"].samples_redacted is True  # 长度信息也是一种脱敏


def test_columns_over_budget_are_skipped_not_fabricated() -> None:
    wide = TableMetadata(
        source_id="src-1",
        schema_name="s",
        table_name="t",
        columns=tuple(ColumnMetadata(name=f"c{i}", data_type="int") for i in range(5)),
    )
    rows = [{f"c{i}": i for i in range(5)}]
    profiles = DataProfiler().profile_table(wide, rows, sample_limit=10, max_columns=3)
    basis = [p.metric_basis for p in profiles]
    assert basis == ["exact", "exact", "exact", "skipped", "skipped"]
    skipped = profiles[3]
    assert skipped.null_ratio is None
    assert set(skipped.unsupported_metrics) == {
        "null_ratio",
        "distinct_count_estimate",
        "uniqueness_ratio",
        "sample_values",
    }


def test_unhashable_values_are_counted_and_conservatively_masked() -> None:
    table = TableMetadata(
        source_id="src-1",
        schema_name="s",
        table_name="t",
        columns=(ColumnMetadata(name="doc", data_type="json"),),
    )
    rows = [{"doc": {"a": 1}}, {"doc": {"a": 1}}, {"doc": [1, 2]}]
    profiles = DataProfiler().profile_table(table, rows, sample_limit=10)
    assert profiles[0].distinct_count_estimate == 2  # 统计按原始值口径
    assert profiles[0].sample_values == ["***", "***", "***"]  # JSON 样本保守遮蔽
    assert profiles[0].samples_redacted is True


def test_sample_values_are_limited_in_count(tmp_path: Path) -> None:
    table = TableMetadata(
        source_id="src-1",
        schema_name="s",
        table_name="t",
        columns=(ColumnMetadata(name="note", data_type="varchar(10)"),),
    )
    rows = [{"note": f"v{i}"} for i in range(50)]
    profiles = DataProfiler().profile_table(table, rows, sample_limit=50)
    assert len(profiles[0].sample_values) == 5
