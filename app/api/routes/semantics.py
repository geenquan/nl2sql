from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_semantics_service
from app.schemas.semantics import (
    FieldSemanticsResponse,
    ReviewRequest,
    ReviewResponse,
    SearchResponse,
    SemanticAnalyzeRequest,
    SemanticRunResponse,
    TablePurposeResponse,
    TableSemanticsResponse,
)
from app.services.semantics_service import SemanticsService

router = APIRouter()

SemanticsServiceDep = Annotated[SemanticsService, Depends(get_semantics_service)]


@router.post("/sources/{source_id}/semantics/analyze",
             status_code=201, response_model=SemanticRunResponse)
def analyze_table(source_id: str, request: SemanticAnalyzeRequest,
                  service: SemanticsServiceDep) -> dict:
    """对指定表触发语义分析；默认读取发布快照（部分目录需显式 allow_partial）。"""
    return service.analyze_table(
        source_id,
        request.table_id,
        snapshot_id=request.snapshot_id,
        allow_partial=request.allow_partial,
        send_samples=request.send_samples,
        column_limit=request.column_limit,
    )


@router.get("/sources/{source_id}/semantics/runs", response_model=list[SemanticRunResponse])
def list_runs(source_id: str, service: SemanticsServiceDep) -> list[dict]:
    return service.list_runs(source_id)


@router.get("/semantics/runs/{run_id}", response_model=SemanticRunResponse)
def get_run(run_id: str, service: SemanticsServiceDep) -> dict:
    return service.get_run(run_id)


@router.get("/tables/{table_id}/semantics", response_model=TableSemanticsResponse)
def get_table_semantics(table_id: str, service: SemanticsServiceDep,
                        effective: bool = False) -> dict:
    return service.get_table_semantics(table_id, effective=effective)


@router.get("/tables/{table_id}/semantics/{column_name}/history",
            response_model=list[FieldSemanticsResponse])
def get_column_history(table_id: str, column_name: str,
                       service: SemanticsServiceDep) -> list[dict]:
    return service.get_column_history(table_id, column_name)


@router.get("/tables/{table_id}/purpose/history", response_model=list[TablePurposeResponse])
def get_purpose_history(table_id: str, service: SemanticsServiceDep) -> list[dict]:
    from app.core.exceptions import ResourceNotFoundError

    history = service._repo.get_table_purpose_history(table_id)
    if not history:
        raise ResourceNotFoundError("table_purpose", f"表用途不存在：{table_id}")
    return history


@router.post("/tables/{table_id}/semantics/{column_name}/review",
             response_model=ReviewResponse)
def review_field(table_id: str, column_name: str, request: ReviewRequest,
                 service: SemanticsServiceDep) -> dict:
    patch = {
        "business_name": request.business_name,
        "category": request.category,
        "candidate_entities": request.candidate_entities,
        "rationale": request.rationale,
    }
    revision = service.submit_review(
        table_id, column_name, action=request.action, patch=patch,
        reviewer=request.reviewer, expected_revision=request.expected_revision,
        note=request.note)
    return {"table_id": table_id, "column_name": column_name, "revision": revision,
            "review_status": "updated"}


@router.post("/tables/{table_id}/purpose/review", response_model=ReviewResponse)
def review_purpose(table_id: str, request: ReviewRequest,
                   service: SemanticsServiceDep) -> dict:
    patch = {"purpose": request.purpose, "rationale": request.rationale}
    revision = service.submit_purpose_review(
        table_id, action=request.action, patch=patch, reviewer=request.reviewer,
        expected_revision=request.expected_revision, note=request.note)
    return {"table_id": table_id, "column_name": None, "revision": revision,
            "review_status": "updated"}


@router.get("/sources/{source_id}/semantics/search", response_model=SearchResponse)
def search_fields(source_id: str, service: SemanticsServiceDep, q: str,
                  limit: int | None = None, snapshot_id: str | None = None) -> dict:
    """按业务概念搜索字段（本地词法基线，结果标记 method=lexical）。"""
    return service.search_fields(source_id, q, snapshot_id=snapshot_id, limit=limit)
