import dataclasses

from app.domain.metadata import (
    ColumnMetadata,
    ColumnProfile,
    IndexMetadata,
    TableMetadata,
)


def test_table_metadata_carries_indexes_and_comment() -> None:
    table = TableMetadata(
        source_id="src-1",
        schema_name="sales",
        table_name="orders",
        columns=(ColumnMetadata(name="id", data_type="bigint", nullable=False),),
        primary_key=("id",),
        foreign_keys=(
            {
                "name": "fk_customer",
                "columns": ("customer_id",),
                "ref_schema": "sales",
                "ref_table": "customers",
                "ref_columns": ("id",),
            },
        ),
        indexes=(IndexMetadata(name="idx_customer", columns=("customer_id",), unique=False),),
        row_count_estimate=1234,
        table_comment="订单表",
    )
    assert table.indexes[0].name == "idx_customer"
    assert table.indexes[0].unique is False
    assert table.table_comment == "订单表"
    assert table.foreign_keys[0]["ref_table"] == "customers"


def test_column_metadata_optional_fields_default_to_none() -> None:
    column = ColumnMetadata(name="amount", data_type="decimal(10,2)")
    assert column.nullable is True
    assert column.comment is None
    assert column.ordinal_position is None
    assert column.default_value is None


def test_column_profile_records_method_basis_and_limits() -> None:
    profile = ColumnProfile(
        source_id="src-1",
        schema_name="sales",
        table_name="orders",
        column_name="amount",
        null_ratio=0.1,
        distinct_count_estimate=90,
        uniqueness_ratio=0.9,
        sample_values=["***"],
        metric_basis="sample",
        sample_row_count=100,
        row_count_basis=1234,
        computed_at="2026-10-27T00:00:00+00:00",
        samples_redacted=True,
        empty_string_count=0,
        unsupported_metrics=[],
    )
    assert profile.profile_version == "v1"
    # 兼容旧字段：未显式传新字段时保持 None/默认，不伪造统计。
    legacy = ColumnProfile(source_id="s", schema_name="t", table_name="t", column_name="c")
    assert legacy.null_ratio is None
    assert legacy.metric_basis is None
    assert legacy.sample_values == []
    assert dataclasses.is_dataclass(legacy)
