"""字段画像算法（M1）。

输入为统一元数据和有界采样行，输出为统一画像对象。纯计算模块：
不访问数据库、不写日志，样本必须经 app/security/masking 脱敏后才会出现在结果中。
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.domain.metadata import ColumnProfile, TableMetadata
from app.security.masking import MASKING_VERSION, mask_sample_values

_ALL_METRICS = ("null_ratio", "distinct_count_estimate", "uniqueness_ratio", "sample_values")


def _hashable(value: Any) -> Any:
    # JSON/SET 等不可哈希类型以 repr 参与去重，避免崩溃；口径为“按值文本去重”。
    if isinstance(value, (dict, list, set)):
        return repr(value)
    return value


class DataProfiler:
    """基于有界采样的字段画像。

    口径约定（逐列记录在 ColumnProfile 中，不虚构结果）：
    - metric_basis="exact"：采样返回行数小于 LIMIT，说明已覆盖整表，指标为精确值；
    - "sample"：返回行数等于 LIMIT，指标仅为样本估计；
    - "insufficient_sample"：无任何行（如空表），指标保持 None；
    - "skipped"：超出列数预算，本列未计算。
    - distinct/uniqueness 始终基于样本计算；空字符串是非 NULL 值，单独计数。

    样本脱敏发生在返回之前（持久化与 API 响应拿到的都是脱敏值）；
    samples_redacted 如实反映包括值格式检测在内的任何脱敏。
    统计指标按原始值计算，不受脱敏影响。
    """

    def __init__(self, sensitive_keywords: Sequence[str] = ()) -> None:
        self._sensitive_keywords = tuple(sensitive_keywords)

    def profile_table(
        self,
        table: TableMetadata,
        rows: list[dict[str, Any]],
        sample_limit: int,
        computed_at: str | None = None,
        max_columns: int | None = None,
    ) -> list[ColumnProfile]:
        timestamp = computed_at or datetime.now(UTC).isoformat()
        basis = (
            "insufficient_sample"
            if not rows
            else "exact"
            if len(rows) < sample_limit
            else "sample"
        )
        profiles: list[ColumnProfile] = []
        for index, column in enumerate(table.columns):
            if max_columns is not None and index >= max_columns:
                profiles.append(
                    ColumnProfile(
                        source_id=table.source_id,
                        schema_name=table.schema_name,
                        table_name=table.table_name,
                        column_name=column.name,
                        metric_basis="skipped",
                        sample_row_count=len(rows),
                        row_count_basis=len(rows),
                        computed_at=timestamp,
                        samples_redacted=True,
                        empty_string_count=None,
                        unsupported_metrics=list(_ALL_METRICS),
                        masking_version=MASKING_VERSION,
                    )
                )
                continue
            values = [row.get(column.name) for row in rows]
            non_null = [value for value in values if value is not None]
            null_count = len(values) - len(non_null)
            empty_string_count = sum(
                1 for value in non_null if isinstance(value, str) and value == ""
            )
            distinct_count = len({_hashable(value) for value in non_null})
            masked = mask_sample_values(
                non_null, column.name, extra_keywords=self._sensitive_keywords
            )
            profiles.append(
                ColumnProfile(
                    source_id=table.source_id,
                    schema_name=table.schema_name,
                    table_name=table.table_name,
                    column_name=column.name,
                    null_ratio=(null_count / len(values)) if values else None,
                    distinct_count_estimate=distinct_count if values else None,
                    uniqueness_ratio=(distinct_count / len(non_null)) if non_null else None,
                    sample_values=masked.values,
                    metric_basis=basis,
                    sample_row_count=len(rows),
                    row_count_basis=len(rows),
                    computed_at=timestamp,
                    samples_redacted=masked.redacted,
                    empty_string_count=empty_string_count if values else None,
                    unsupported_metrics=[],
                    masking_version=MASKING_VERSION,
                )
            )
        return profiles
