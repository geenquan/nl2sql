from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BuildModelRequest(BaseModel):
    snapshot_id: str | None = None
    allow_partial: bool = False


class GrainChange(BaseModel):
    statement: str
    keys: list[str] | None = None
    confirm: bool = True


class MetricFieldRef(BaseModel):
    schema_name: str  # 请求体键 schema_name（避免遮蔽 BaseModel.schema）
    table: str
    column: str

    def to_ref(self) -> dict:
        return {"schema": self.schema_name, "table": self.table,
                "column": self.column}


class MetricFilterIn(BaseModel):
    field: MetricFieldRef
    operator: str
    value: Any = None


class MetricChange(BaseModel):
    action: str  # confirm | reject | update
    name: str | None = None  # 非口径字段（改名不撤销确认）
    description: str | None = None  # 非口径字段
    unit: str | None = None
    time_basis: str | None = None
    time_field: MetricFieldRef | None = None
    measure_field: MetricFieldRef | None = None  # 聚合变更时可同步提供度量字段
    filters: list[MetricFilterIn] | None = None
    aggregation: str | None = None
    skip_status_filter: bool = False
    note: str | None = None


class ReviseModelRequest(BaseModel):
    base_version: int = Field(ge=1)
    reviewer: str = "local-dev"
    note: str | None = None
    topic_names: dict[str, str] | None = None
    topic_grains: dict[str, GrainChange] | None = None
    confirm_attributes: list[str] | None = None
    metrics: dict[str, MetricChange] | None = None
    selected_paths: dict[str, str] | None = None


class PublishRequest(BaseModel):
    reviewer: str = "local-dev"
    note: str | None = None
    expected_pointer_version: int | None = None


class RollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    reviewer: str = "local-dev"
    reason: str | None = None
    expected_pointer_version: int | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    valid: bool
    issues: list[str] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)


class SemanticModelResponse(BaseModel):
    model_config = ConfigDict(extra="allow")  # 模型含丰富的结构化内容
    model_id: str
    source_id: str
    snapshot_id: str | None = None
    version: int
    created_at: str | None = None
    created_by: str | None = None


class ExportResponse(BaseModel):
    format: str
    model_id: str
    version: int
    content: str
    note: str | None = None


class PublishResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_id: str
    version: int
    operator: str
    report: ValidationReport | None = None


class DiffResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_id: str
    version_a: int
    version_b: int
