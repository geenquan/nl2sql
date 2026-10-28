from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectionTestError(BaseModel):
    reason: str
    message: str


class ConnectionTestResult(BaseModel):
    ok: bool
    dialect: str
    latency_ms: int | None = None
    source_id: str | None = None
    name: str | None = None
    error: ConnectionTestError | None = None


class SourceCreatedResponse(BaseModel):
    source_id: str
    name: str
    dialect: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    credential_ref: str | None = None
    allowed_schemas: list[str] = Field(default_factory=list)
    read_only: bool = True
    is_default: bool = False
    already_registered: bool = False


class LatestScanSummary(BaseModel):
    job_id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None


class CatalogInfo(BaseModel):
    """当前默认目录的实际来源：快照、状态与完整性，避免与最近扫描任务混淆。"""

    snapshot_id: str | None = None
    status: str | None = None
    complete: bool = True


class SourceCardResponse(SourceCreatedResponse):
    table_count: int = 0
    column_count: int = 0
    latest_scan: LatestScanSummary | None = None
    catalog: CatalogInfo | None = None


class ScanErrorItem(BaseModel):
    # 字段名避开 BaseModel.schema；对外的 JSON 键仍为 "schema"。
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str | None = Field(default=None, alias="schema")
    table: str | None = None
    reason: str
    message: str


class ScanJobResponse(BaseModel):
    job_id: str
    source_id: str
    snapshot_id: str | None = None
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    tables_planned: int | None = None
    tables_scanned: int | None = None
    tables_failed: int | None = None
    tables_skipped: int | None = None
    sample_rows: int | None = None
    max_tables: int | None = None
    errors: list[ScanErrorItem] = Field(default_factory=list)


class SchemaSummaryResponse(BaseModel):
    schema_name: str
    table_count: int


class ColumnItem(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    comment: str | None = None
    ordinal_position: int | None = None
    default_value: str | None = None


class IndexItem(BaseModel):
    name: str
    columns: list[str] = Field(default_factory=list)
    unique: bool = False


class TableCatalogItem(BaseModel):
    table_id: str
    source_id: str
    snapshot_id: str | None = None
    schema_name: str
    table_name: str
    table_comment: str | None = None
    column_count: int = 0
    row_count_estimate: int | None = None
    row_count_is_estimate: bool = True
    scanned_at: str | None = None


class TableDetailResponse(TableCatalogItem):
    snapshot_id: str | None = None
    columns: list[ColumnItem] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = Field(default_factory=list)
    indexes: list[IndexItem] = Field(default_factory=list)


class ColumnProfileResponse(BaseModel):
    table_id: str | None = None
    column_name: str
    null_ratio: float | None = None
    distinct_count_estimate: int | None = None
    uniqueness_ratio: float | None = None
    sample_values: list[str] = Field(default_factory=list)
    profile_version: str = "v1"
    metric_basis: str | None = None
    sample_row_count: int | None = None
    row_count_basis: int | None = None
    computed_at: str | None = None
    samples_redacted: bool = False
    empty_string_count: int | None = None
    unsupported_metrics: list[str] = Field(default_factory=list)
