from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnMetadata:
    """与数据库方言无关的字段元数据。"""

    name: str
    data_type: str
    nullable: bool = True
    comment: str | None = None
    ordinal_position: int | None = None
    default_value: str | None = None


@dataclass(frozen=True)
class IndexMetadata:
    """索引元数据；columns 为按索引内顺序排列的字段名。"""

    name: str
    columns: tuple[str, ...]
    unique: bool = False


@dataclass(frozen=True)
class TableMetadata:
    """统一表结构；具体 Connector 负责把原生元数据映射到这里。"""

    source_id: str
    schema_name: str
    table_name: str
    columns: tuple[ColumnMetadata, ...] = ()
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[dict[str, Any], ...] = ()
    indexes: tuple[IndexMetadata, ...] = ()
    row_count_estimate: int | None = None
    row_count_is_estimate: bool = True
    table_comment: str | None = None


@dataclass
class ColumnProfile:
    """字段画像摘要；必须记录统计方法、采样范围和版本。

    metric_basis 说明每项指标的计算口径：
    - "exact": 采样覆盖了整表（返回行数小于 LIMIT 且行数估计不超过采样上限）；
    - "sample": 有界采样估计，不代表全表精确值；
    - "insufficient_sample": 样本不足（如空表），指标保持 None，不虚构结果；
    - "skipped": 超出扫描预算（列数/表数上限），本列未计算画像。
    """

    source_id: str
    schema_name: str
    table_name: str
    column_name: str
    null_ratio: float | None = None
    distinct_count_estimate: int | None = None
    uniqueness_ratio: float | None = None
    sample_values: list[str] = field(default_factory=list)
    profile_version: str = "v1"
    metric_basis: str | None = None
    sample_row_count: int | None = None
    row_count_basis: int | None = None
    computed_at: str | None = None
    samples_redacted: bool = False
    empty_string_count: int | None = None
    unsupported_metrics: list[str] = field(default_factory=list)
