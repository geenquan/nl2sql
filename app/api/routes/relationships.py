from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_relationship_service
from app.schemas.relationships import (
    DiscoverRequest,
    GraphResponse,
    PathsResponse,
    RelationshipResponse,
    RelationshipRunResponse,
    ReviewRequest,
    ReviewResponse,
    StaleRequest,
    ValidateBatchRequest,
)
from app.services.relationship_service import RelationshipService

router = APIRouter()

RelationshipServiceDep = Annotated[RelationshipService, Depends(get_relationship_service)]
RunTypeQuery = Annotated[str | None, Query(pattern="^(discovery|validation)$")]
StatusListQuery = Annotated[list[str] | None, Query()]
MaxHopsQuery = Annotated[int | None, Query(ge=1, le=6)]
PathLimitQuery = Annotated[int | None, Query(ge=1, le=50)]


@router.post(
    "/sources/{source_id}/relationships/discover",
    status_code=201, response_model=RelationshipRunResponse,
)
def discover_relationships(
    source_id: str, request: DiscoverRequest, service: RelationshipServiceDep,
) -> dict:
    """召回候选关系（纯本地 M1/M2 输入；默认发布快照，部分目录需 allow_partial）。"""
    return service.discover_relationships(
        source_id, snapshot_id=request.snapshot_id,
        allow_partial=request.allow_partial)


@router.post(
    "/sources/{source_id}/relationships/validate",
    status_code=201, response_model=RelationshipRunResponse,
)
def validate_batch(
    source_id: str, request: ValidateBatchRequest, service: RelationshipServiceDep,
) -> dict:
    """按优先级对发布快照内 Top-N 候选执行有界只读验证（显式触发才连源库）。"""
    return service.validate_batch(
        source_id, limit=request.limit, snapshot_id=request.snapshot_id,
        allow_partial=request.allow_partial)


@router.get(
    "/sources/{source_id}/relationships",
    response_model=list[RelationshipResponse],
)
def list_relationships(
    source_id: str, service: RelationshipServiceDep,
    status: str | None = None,
    snapshot_id: str | None = None,
    effective_only: bool = True,
) -> list[dict]:
    """列出关系（默认发布快照 + effective 结果；可按状态/快照过滤）。"""
    return service.list_relationships(
        source_id, status=status, snapshot_id=snapshot_id,
        effective_only=effective_only)


@router.get(
    "/sources/{source_id}/relationships/runs",
    response_model=list[RelationshipRunResponse],
)
def list_runs(
    source_id: str, service: RelationshipServiceDep,
    run_type: RunTypeQuery = None,
) -> list[dict]:
    return service.list_runs(source_id, run_type=run_type)


@router.get("/relationships/runs/{run_id}", response_model=RelationshipRunResponse)
def get_run(run_id: str, service: RelationshipServiceDep) -> dict:
    return service.get_run(run_id)


@router.get(
    "/relationships/{relationship_key}",
    response_model=RelationshipResponse,
)
def get_relationship(
    relationship_key: str, service: RelationshipServiceDep,
    effective: bool = True,
) -> dict:
    return service.get_relationship(relationship_key, effective=effective)


@router.get(
    "/relationships/{relationship_key}/history",
    response_model=list[RelationshipResponse],
)
def get_history(relationship_key: str, service: RelationshipServiceDep) -> list[dict]:
    return service.get_relationship_history(relationship_key)


@router.post(
    "/relationships/{relationship_key}/validate",
    status_code=201, response_model=RelationshipRunResponse,
)
def validate_relationship(
    relationship_key: str, source_id: str, service: RelationshipServiceDep,
) -> dict:
    """对单个关系触发有界只读验证（stale 关系即重验路径）。"""
    return service.validate_relationship(source_id, relationship_key)


@router.post(
    "/relationships/{relationship_key}/review",
    response_model=ReviewResponse,
)
def review_relationship(
    relationship_key: str, request: ReviewRequest, service: RelationshipServiceDep,
) -> dict:
    revision = service.submit_review(
        relationship_key, action=request.action, reviewer=request.reviewer,
        expected_revision=request.expected_revision, note=request.note)
    return {"relationship_key": relationship_key, "revision": revision,
            "status": "updated"}


@router.post(
    "/relationships/{relationship_key}/stale",
    response_model=ReviewResponse,
)
def mark_stale(
    relationship_key: str, request: StaleRequest, service: RelationshipServiceDep,
) -> dict:
    revision = service.mark_stale(
        relationship_key, reason=request.reason, reviewer=request.reviewer,
        expected_revision=request.expected_revision)
    return {"relationship_key": relationship_key, "revision": revision,
            "status": "stale"}


@router.post(
    "/relationships/{relationship_key}/revalidate",
    status_code=201, response_model=RelationshipRunResponse,
)
def revalidate(
    relationship_key: str, source_id: str, service: RelationshipServiceDep,
) -> dict:
    """对 stale 关系执行显式重验（重新采集证据并判定）。"""
    return service.revalidate(source_id, relationship_key)


@router.get(
    "/sources/{source_id}/relationships/graph",
    response_model=GraphResponse,
)
def graph(
    source_id: str, service: RelationshipServiceDep,
    snapshot_id: str | None = None,
    status: StatusListQuery = None,
    mermaid: bool = False,
) -> dict:
    """关系图数据（默认发布快照；候选/拒绝/过期边可见且状态区分）。"""
    return service.graph(
        source_id, snapshot_id=snapshot_id, statuses=status, mermaid=mermaid)


@router.get(
    "/sources/{source_id}/relationships/paths",
    response_model=PathsResponse,
)
def paths(
    source_id: str, service: RelationshipServiceDep,
    from_table_id: str, to_table_id: str,
    max_hops: MaxHopsQuery = None,
    limit: PathLimitQuery = None,
    snapshot_id: str | None = None,
) -> dict:
    """受限多跳路径搜索：仅沿有效 validated 非 stale 边（默认发布快照）。"""
    return service.paths(
        source_id, from_table_id, to_table_id, max_hops=max_hops,
        limit=limit, snapshot_id=snapshot_id)
