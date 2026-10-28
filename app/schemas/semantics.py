from typing import Any

from pydantic import BaseModel, Field


class SemanticAnalyzeRequest(BaseModel):
    table_id: str
    snapshot_id: str | None = None
    allow_partial: bool = False
    send_samples: bool = False
    column_limit: int | None = Field(default=None, gt=0, le=200)


class RunErrorItem(BaseModel):
    model_config = {"populate_by_name": True}
    reason: str
    message: str


class SemanticRunResponse(BaseModel):
    run_id: str
    source_id: str
    snapshot_id: str | None = None
    mode: str
    table_ids: list[str] = Field(default_factory=list)
    input_complete: bool = True
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    columns_analyzed: int = 0
    rules_version: str | None = None
    prompt_version: str | None = None
    model_configured: str | None = None
    model_reported: str | None = None
    llm_calls: int = 0
    llm_prompt_tokens: int | None = None
    llm_completion_tokens: int | None = None
    llm_usage_complete: bool = True
    llm_prompt_usage_complete: bool = True
    llm_completion_usage_complete: bool = True
    elapsed_ms: int | None = None
    send_samples: bool = False
    errors: list[RunErrorItem] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    type: str
    ref: str
    detail: str = ""


class AlternativeItem(BaseModel):
    category: str | None = None
    purpose: str | None = None
    rationale: str | None = None
    confidence: str | None = None


class FieldSemanticsResponse(BaseModel):
    table_id: str | None = None
    column_name: str
    original_name: str | None = None
    original_comment: str | None = None
    suggested_business_name: str | None = None
    category: str
    candidate_entities: list[str] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: str
    inference_source: str
    model_name: str | None = None
    prompt_version: str | None = None
    rules_version: str | None = None
    run_id: str | None = None
    revision: int = 1
    review_status: str
    reviewed_by: str | None = None
    review_note: str | None = None
    created_at: str | None = None


class TablePurposeResponse(BaseModel):
    table_id: str
    table_name: str | None = None
    purpose: str
    rationale: str | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: str
    inference_source: str
    model_name: str | None = None
    prompt_version: str | None = None
    rules_version: str | None = None
    run_id: str | None = None
    revision: int = 1
    review_status: str
    reviewed_by: str | None = None
    created_at: str | None = None


class TableSemanticsResponse(BaseModel):
    table_id: str
    effective: bool = False
    purpose: TablePurposeResponse | None = None
    fields: list[FieldSemanticsResponse] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    action: str  # confirm | reject | revert
    business_name: str | None = None
    category: str | None = None
    candidate_entities: list[str] | None = None
    purpose: str | None = None
    rationale: str | None = None
    reviewer: str = "local-dev"
    expected_revision: int
    note: str | None = None


class ReviewResponse(BaseModel):
    table_id: str
    column_name: str | None = None
    revision: int
    review_status: str


class SearchItem(BaseModel):
    source_id: str
    snapshot_id: str | None
    table_id: str
    table_name: str | None
    column_name: str
    business_name: str | None
    category: str | None
    review_status: str | None
    inference_source: str | None
    score: float
    match_basis: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    method: str
    snapshot_id: str | None
    query: str
    items: list[SearchItem] = Field(default_factory=list)
    note: str | None = None
