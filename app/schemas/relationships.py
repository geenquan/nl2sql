from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DiscoverRequest(BaseModel):
    snapshot_id: str | None = None
    allow_partial: bool = False


class ValidateBatchRequest(BaseModel):
    limit: int | None = Field(default=None, gt=0, le=50)
    snapshot_id: str | None = None
    allow_partial: bool = False


class RelationshipRunResponse(BaseModel):
    # 运行记录含预算/计数字段，类型随运行类型不同，允许透传额外字段
    model_config = ConfigDict(extra="allow")

    run_id: str
    source_id: str
    run_type: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    snapshot_id: str | None = None
    input_complete: bool | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int | None = None


class RiskItem(BaseModel):
    type: str
    note: str | None = None


class RelationshipResponse(BaseModel):
    relationship_key: str
    source_id: str
    source_schema: str
    source_table: str
    source_table_id: str | None = None
    source_columns: list[str]
    target_schema: str
    target_table: str
    target_table_id: str | None = None
    target_columns: list[str]
    origin: list[str] = Field(default_factory=list)
    cardinality: str | None = None
    recall_reasons: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    support_evidence: list[dict[str, Any]] = Field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = Field(default_factory=list)
    scores: dict[str, Any] = Field(default_factory=dict)
    status: str
    evidence: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    snapshot_id: str | None = None
    type_verdict: str | None = None
    type_reason: str | None = None
    revision: int = 1
    inference_source: str = "rules"
    reviewed_by: str | None = None
    review_note: str | None = None
    run_id: str | None = None
    created_at: str | None = None


class ReviewRequest(BaseModel):
    action: str  # confirm | reject | revert
    reviewer: str = "local-dev"
    expected_revision: int
    note: str | None = None


class ReviewResponse(BaseModel):
    relationship_key: str
    revision: int
    status: str


class StaleRequest(BaseModel):
    reason: str | None = None
    reviewer: str = "local-dev"
    expected_revision: int | None = None


class GraphNode(BaseModel):
    table_id: str
    schema_name: str
    table_name: str
    table_full: str
    column_count: int | None = None


class GraphEdge(BaseModel):
    relationship_key: str
    source: dict[str, Any]
    target: dict[str, Any]
    status: str | None
    cardinality: str | None
    origin: list[str] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None
    evidence_summary: dict[str, Any] | None = None


class GraphResponse(BaseModel):
    source_id: str
    snapshot_id: str | None = None
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    mermaid: str | None = None
    status_note: str | None = None
    note: str | None = None


class PathsResponse(BaseModel):
    source_id: str
    snapshot_id: str | None = None
    from_table_id: str
    to_table_id: str
    paths: list[list[dict[str, Any]]] = Field(default_factory=list)
    path_count: int = 0
    stopped_reason: str | None = None
    searched_nodes: int = 0
    budget: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None
