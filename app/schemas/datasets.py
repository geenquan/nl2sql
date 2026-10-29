from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FieldRefIn(BaseModel):
    schema_name: str
    table: str
    column: str


class FilterIn(BaseModel):
    field: FieldRefIn
    operator: str
    value: Any = None


class DimensionIn(BaseModel):
    field: FieldRefIn
    output_alias: str
    relationship_key: str
    join_type: str = "left"


class MetricSelectionIn(BaseModel):
    metric_id: str
    output_alias: str


class PreaggregationIn(BaseModel):
    metric_id: str
    output_alias: str
    relationship_key: str
    scope_note: str = ""


class DatasetCreateRequest(BaseModel):
    name: str
    model_id: str
    model_version: int = Field(ge=1)
    topic_id: str
    kind: str  # detail_view | aggregate
    central_entity_id: str
    snapshot_id: str | None = None
    grain_keys: list[str] = Field(min_length=1)
    grain_statement: str = ""
    fields: list[dict[str, Any]] = Field(default_factory=list)
    dimensions: list[DimensionIn] = Field(default_factory=list)
    metrics: list[MetricSelectionIn] = Field(default_factory=list)
    preaggregations: list[PreaggregationIn] = Field(default_factory=list)
    filters: list[FilterIn] = Field(default_factory=list)
    join_type: str = "left"
    target: str = "view"


class DatasetReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    reviewer: str = "local-dev"
    note: str | None = None
    grain_keys: list[str] | None = None
    grain_statement: str | None = None
    fields: list[dict[str, Any]] | None = None
    dimensions: list[DimensionIn] | None = None
    metrics: list[MetricSelectionIn] | None = None
    preaggregations: list[PreaggregationIn] | None = None
    filters: list[FilterIn] | None = None
    join_type: str | None = None
    kind: str | None = None
    target: str | None = None


class DatasetResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    dataset_id: str
    version: int
    status: str
    name: str


class PreviewResponse(BaseModel):
    dataset_id: str
    version: int
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    note: str | None = None


class PublishRequest(BaseModel):
    operator: str = "local-dev"
    expected_pointer_version: int | None = None


class RollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    operator: str = "local-dev"
    expected_pointer_version: int | None = None
