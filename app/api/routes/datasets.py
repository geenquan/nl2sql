from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_dataset_service
from app.schemas.datasets import (
    DatasetCreateRequest,
    DatasetResponse,
    DatasetReviseRequest,
    PreviewResponse,
    PublishRequest,
    RollbackRequest,
)
from app.services.dataset_service import DatasetService

router = APIRouter()

DatasetServiceDep = Annotated[DatasetService, Depends(get_dataset_service)]


@router.post(
    "/sources/{source_id}/datasets", status_code=201,
    response_model=DatasetResponse,
)
def create_dataset(
    source_id: str, request: DatasetCreateRequest,
    service: DatasetServiceDep,
) -> dict:
    """创建数据集 v1 草稿（服务端核验 M4 门槛与结构化引用）。"""
    return service.create_dataset(
        source_id, request.model_dump(by_alias=False), created_by="local-dev")


@router.get(
    "/sources/{source_id}/datasets", response_model=list[DatasetResponse],
)
def list_datasets(source_id: str, service: DatasetServiceDep) -> list[dict]:
    return service.list_datasets(source_id)


@router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset(
    dataset_id: str, service: DatasetServiceDep, version: int | None = None,
) -> dict:
    return service.get_dataset(dataset_id, version=version)


@router.get(
    "/datasets/{dataset_id}/versions", response_model=list[DatasetResponse],
)
def list_versions(dataset_id: str, service: DatasetServiceDep) -> list[dict]:
    return service.list_versions(dataset_id)


@router.post("/datasets/{dataset_id}/revise", response_model=DatasetResponse)
def revise_dataset(
    dataset_id: str, request: DatasetReviseRequest,
    service: DatasetServiceDep,
) -> dict:
    """修改定义产生新版本（旧 SQL/质量报告不背书新定义）。"""
    changes = request.model_dump(exclude={"base_version", "reviewer", "note"})
    return service.revise_dataset(
        dataset_id, request.base_version, changes,
        created_by=request.reviewer, note=request.note)


@router.get("/datasets/{dataset_id}/preview", response_model=PreviewResponse)
def preview(
    dataset_id: str, service: DatasetServiceDep,
    version: int = Query(ge=1), max_rows: int | None = Query(default=None,
                                                             gt=0, le=1000),
) -> dict:
    """只读数据预览（有界行数；脱敏展示；质量计算用原始值）。"""
    return service.preview(dataset_id, version, max_rows=max_rows)


@router.post("/datasets/{dataset_id}/validate", response_model=DatasetResponse)
def validate(
    dataset_id: str, service: DatasetServiceDep,
    version: int = Query(ge=1),
) -> dict:
    """执行质量验证（独立基准对账；报告绑定版本与指纹）。"""
    return service.validate(dataset_id, version)


@router.post("/datasets/{dataset_id}/publish", response_model=DatasetResponse)
def publish(
    dataset_id: str, request: PublishRequest, service: DatasetServiceDep,
    version: int = Query(ge=1),
) -> dict:
    """发布（view/ES；质量通过才可发布；并发指针冲突 409）。"""
    return service.publish(
        dataset_id, version, operator=request.operator,
        expected_pointer=request.expected_pointer_version)


@router.post("/datasets/{dataset_id}/refresh", response_model=DatasetResponse)
def refresh(
    dataset_id: str, request: PublishRequest, service: DatasetServiceDep,
) -> dict:
    """显式刷新当前发布版本（重验质量后幂等发布）。"""
    return service.refresh(dataset_id, operator=request.operator)


@router.post("/datasets/{dataset_id}/rollback", response_model=DatasetResponse)
def rollback(
    dataset_id: str, request: RollbackRequest, service: DatasetServiceDep,
) -> dict:
    """回滚到仍处 published 状态的历史版本。"""
    return service.rollback(
        dataset_id, request.target_version, operator=request.operator,
        expected_pointer=request.expected_pointer_version)


@router.post(
    "/datasets/{dataset_id}/reconcile", response_model=DatasetResponse,
)
def reconcile(
    dataset_id: str, service: DatasetServiceDep, version: int = Query(ge=1),
) -> dict:
    """显式核对结果不明的发布尝试（外部成功本地失败恢复）。"""
    return service.reconcile(dataset_id, version)


@router.get("/datasets/{dataset_id}/runs")
def list_runs(dataset_id: str, service: DatasetServiceDep) -> list[dict]:
    """运行记录（验证/发布；安全错误消息）。"""
    return service._repo.list_dataset_runs(dataset_id)
