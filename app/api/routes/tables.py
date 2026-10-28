from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_scan_service
from app.schemas.metadata import ColumnProfileResponse, TableDetailResponse
from app.services.scan_service import ScanService

router = APIRouter()

ScanServiceDep = Annotated[ScanService, Depends(get_scan_service)]


@router.get("/{table_id}", response_model=TableDetailResponse)
def get_table(table_id: str, service: ScanServiceDep) -> dict:
    table = service.get_table(table_id)
    return {
        **{
            key: table[key]
            for key in (
                "table_id",
                "source_id",
                "snapshot_id",
                "schema_name",
                "table_name",
                "table_comment",
                "primary_key",
                "foreign_keys",
                "row_count_estimate",
                "row_count_is_estimate",
                "scanned_at",
            )
        },
        "columns": table["columns"],
        "indexes": table["indexes"],
        "column_count": len(table["columns"]),
    }


@router.get("/{table_id}/profile", response_model=list[ColumnProfileResponse])
def get_table_profile(table_id: str, service: ScanServiceDep) -> list[dict]:
    return service.get_table_profile(table_id)
