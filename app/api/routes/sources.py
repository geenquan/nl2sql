from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_scan_service, get_source_service
from app.schemas.metadata import (
    ConnectionTestResult,
    ScanJobResponse,
    SchemaSummaryResponse,
    SourceCardResponse,
    SourceCreatedResponse,
    TableCatalogItem,
)
from app.schemas.source import SourceConnectionTestRequest, SourceCreateRequest
from app.services.scan_service import ScanService
from app.services.source_service import SourceService

router = APIRouter()

SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
ScanServiceDep = Annotated[ScanService, Depends(get_scan_service)]


@router.post("", status_code=201, response_model=SourceCreatedResponse)
def create_source(request: SourceCreateRequest, service: SourceServiceDep) -> dict:
    # 持久化成功才返回 201；失败由异常处理器映射为 4xx，不伪造 created。
    return service.create_source(request.model_dump())


@router.post("/test", response_model=ConnectionTestResult)
def test_source_connection(
    request: SourceConnectionTestRequest, service: SourceServiceDep
) -> dict:
    """一次性连接测试：仅使用请求体参数，不注册、不持久化。"""
    return service.test_connection(request.model_dump())


@router.post("/default", response_model=SourceCreatedResponse)
def register_default_source(service: SourceServiceDep) -> dict:
    """显式把 env 中 `DATA_MYSQL_*` 配置注册为默认数据源（幂等）。"""
    return service.register_default_source()


def _card(source: dict, scan_service: ScanService) -> dict:
    overview = scan_service.source_overview(source["source_id"])
    return {**source, **overview}


@router.get("", response_model=list[SourceCardResponse])
def list_sources(service: SourceServiceDep, scan_service: ScanServiceDep) -> list[dict]:
    return [_card(source, scan_service) for source in service.list_sources()]


@router.get("/{source_id}", response_model=SourceCardResponse)
def get_source(source_id: str, service: SourceServiceDep, scan_service: ScanServiceDep) -> dict:
    return _card(service.get_source(source_id), scan_service)


@router.post("/{source_id}/test", response_model=ConnectionTestResult)
def test_persisted_source(source_id: str, service: SourceServiceDep) -> dict:
    return service.test_persisted_source(source_id)


@router.post("/{source_id}/scans", status_code=201, response_model=ScanJobResponse)
def run_scan(source_id: str, service: ScanServiceDep) -> dict:
    """同步执行扫描（受表数/采样预算约束）；后台任务队列在 M8 前不引入。"""
    return service.run_scan(source_id)


@router.get("/{source_id}/scans", response_model=list[ScanJobResponse])
def list_scans(source_id: str, service: ScanServiceDep) -> list[dict]:
    return service.list_scans(source_id)


@router.get("/{source_id}/schemas", response_model=list[SchemaSummaryResponse])
def list_schemas(source_id: str, service: ScanServiceDep) -> list[dict]:
    tables = service.list_tables(source_id)
    counts: dict[str, int] = {}
    for table in tables:
        counts[table["schema_name"]] = counts.get(table["schema_name"], 0) + 1
    return [
        {"schema_name": schema_name, "table_count": count}
        for schema_name, count in sorted(counts.items())
    ]


@router.get("/{source_id}/tables", response_model=list[TableCatalogItem])
def list_tables(
    source_id: str,
    service: ScanServiceDep,
    schema: str | None = None,
    snapshot_id: str | None = None,
) -> list[dict]:
    """默认返回发布目录；显式传 snapshot_id 可查看历史快照（诊断用）。"""
    return [
        {
            "table_id": table["table_id"],
            "source_id": table["source_id"],
            "snapshot_id": table["snapshot_id"],
            "schema_name": table["schema_name"],
            "table_name": table["table_name"],
            "table_comment": table["table_comment"],
            "column_count": len(table["columns"]),
            "row_count_estimate": table["row_count_estimate"],
            "row_count_is_estimate": table["row_count_is_estimate"],
            "scanned_at": table["scanned_at"],
        }
        for table in service.list_tables(source_id, schema_name=schema, snapshot_id=snapshot_id)
    ]
