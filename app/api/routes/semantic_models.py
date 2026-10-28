from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_semantic_model_service
from app.schemas.semantic_models import (
    BuildModelRequest,
    DiffResponse,
    ExportResponse,
    PublishRequest,
    PublishResponse,
    ReviseModelRequest,
    RollbackRequest,
    SemanticModelResponse,
    ValidationReport,
)
from app.services.semantic_model_service import SemanticModelService

router = APIRouter()

SemanticModelServiceDep = Annotated[
    SemanticModelService, Depends(get_semantic_model_service)]
VersionQuery = Annotated[int, Query(ge=1)]
ExportFormatQuery = Annotated[str, Query(pattern="^(json|yaml)$")]


@router.post(
    "/sources/{source_id}/semantic-models/build",
    status_code=201, response_model=SemanticModelResponse,
)
def build_model(
    source_id: str, request: BuildModelRequest, service: SemanticModelServiceDep,
) -> dict:
    """从 M1/M2/M3 当前结果构建模型草稿（新版本；不覆盖人工版本、不切指针）。"""
    return service.build_model(
        source_id, snapshot_id=request.snapshot_id,
        allow_partial=request.allow_partial)


@router.get(
    "/sources/{source_id}/semantic-models",
    response_model=list[SemanticModelResponse],
)
def list_models(source_id: str, service: SemanticModelServiceDep) -> list[dict]:
    """列出数据源下每个模型线的最新版本（含依赖可用性）。"""
    return service.list_models(source_id)


@router.get(
    "/sources/{source_id}/semantic-models/published",
    response_model=SemanticModelResponse,
)
def get_published(source_id: str, service: SemanticModelServiceDep) -> dict:
    """当前发布指针指向的模型版本（无发布返回 404）。"""
    published = service.get_published(source_id)
    if published is None:
        from app.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError(
            "semantic_model", f"数据源没有已发布的语义模型：{source_id}")
    return published["model"]


@router.get(
    "/sources/{source_id}/semantic-models/publication-log",
)
def publication_log(source_id: str, service: SemanticModelServiceDep) -> list[dict]:
    """发布/回滚审计记录（按时间倒序）。"""
    return service.publication_log(source_id)


@router.get(
    "/semantic-models/{model_id}",
    response_model=SemanticModelResponse,
)
def get_model(
    model_id: str, service: SemanticModelServiceDep, version: int | None = None,
) -> dict:
    """模型详情（默认最新版本；含依赖可用性与正式指标集合）。"""
    return service.get_model(model_id, version=version)


@router.get(
    "/semantic-models/{model_id}/versions",
    response_model=list[SemanticModelResponse],
)
def list_versions(model_id: str, service: SemanticModelServiceDep) -> list[dict]:
    """全部版本（按版本倒序；历史可追溯、内容不可变）。"""
    return service.list_versions(model_id)


@router.get(
    "/semantic-models/{model_id}/versions/{version}",
    response_model=SemanticModelResponse,
)
def get_version(model_id: str, version: int,
                service: SemanticModelServiceDep) -> dict:
    return service.get_model(model_id, version=version)


@router.post(
    "/semantic-models/{model_id}/revise",
    response_model=SemanticModelResponse,
)
def revise_model(
    model_id: str, request: ReviseModelRequest, service: SemanticModelServiceDep,
) -> dict:
    """人工修订/确认（产生新版本；引用经服务端核验；并发基线冲突 409）。"""
    changes: dict = {}
    if request.topic_names:
        changes["topic_names"] = request.topic_names
    if request.topic_grains:
        changes["topic_grains"] = {
            topic_id: grain.model_dump()
            for topic_id, grain in request.topic_grains.items()}
    if request.confirm_attributes:
        changes["confirm_attributes"] = request.confirm_attributes
    if request.metrics:
        changes["metrics"] = {}
        for metric_id, change in request.metrics.items():
            payload = change.model_dump()
            if payload.get("time_field") is not None:
                payload["time_field"] = {
                    "schema": payload["time_field"]["schema_name"],
                    "table": payload["time_field"]["table"],
                    "column": payload["time_field"]["column"],
                }
            if payload.get("measure_field") is not None:
                payload["measure_field"] = {
                    "schema": payload["measure_field"]["schema_name"],
                    "table": payload["measure_field"]["table"],
                    "column": payload["measure_field"]["column"],
                }
            for flt in payload.get("filters") or []:
                flt["field"] = {
                    "schema": flt["field"]["schema_name"],
                    "table": flt["field"]["table"],
                    "column": flt["field"]["column"],
                }
            changes["metrics"][metric_id] = payload
    if request.selected_paths:
        changes["selected_paths"] = request.selected_paths
    return service.revise_model(
        model_id, request.base_version, changes,
        reviewer=request.reviewer, note=request.note)


@router.post(
    "/semantic-models/{model_id}/validate",
    response_model=ValidationReport,
)
def validate_model(
    model_id: str, service: SemanticModelServiceDep, version: VersionQuery,
) -> dict:
    """发布前检查：grain/正式指标口径/引用/依赖/部分快照/阻断风险。"""
    return service.validate_model(model_id, version)


@router.post(
    "/semantic-models/{model_id}/publish",
    response_model=PublishResponse,
)
def publish_model(
    model_id: str, request: PublishRequest, service: SemanticModelServiceDep,
    version: VersionQuery,
) -> dict:
    """发布语义模型＝原子切换下游使用指针（不发布数据集、不执行 SQL）。"""
    expected = (
        (model_id, request.expected_pointer_version)
        if request.expected_pointer_version is not None else None)
    return service.publish_model(
        model_id, version, reviewer=request.reviewer, note=request.note,
        expected_pointer=expected)


@router.post(
    "/semantic-models/{model_id}/rollback",
    response_model=PublishResponse,
)
def rollback_model(
    model_id: str, request: RollbackRequest, service: SemanticModelServiceDep,
) -> dict:
    """回滚＝指针原子切回历史版本（目标必须当前仍可用；历史全保留）。"""
    expected = None
    if request.expected_pointer_version is not None:
        expected = (model_id, request.expected_pointer_version)
    return service.rollback_model(
        model_id, request.target_version, reviewer=request.reviewer,
        reason=request.reason, expected_pointer=expected)


@router.get(
    "/semantic-models/{model_id}/diff",
    response_model=DiffResponse,
)
def diff_models(
    model_id: str, service: SemanticModelServiceDep,
    version_a: int = Query(ge=1), version_b: int = Query(ge=1),
) -> dict:
    """两个版本的结构化差异（新增/移除/变更）。"""
    return service.diff_models(model_id, version_a, version_b)


@router.get(
    "/semantic-models/{model_id}/export",
    response_model=ExportResponse,
)
def export_model(
    model_id: str, service: SemanticModelServiceDep,
    version: VersionQuery, format: ExportFormatQuery = "json",
) -> dict:
    """导出 JSON/YAML（同一份模型语义；顺序稳定；不含凭证/样本值）。"""
    return service.export_model(model_id, version, export_format=format)

