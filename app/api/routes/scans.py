from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_scan_service
from app.schemas.metadata import ScanJobResponse
from app.services.scan_service import ScanService

router = APIRouter()

ScanServiceDep = Annotated[ScanService, Depends(get_scan_service)]


@router.get("/{job_id}", response_model=ScanJobResponse)
def get_scan(job_id: str, service: ScanServiceDep) -> dict:
    return service.get_scan(job_id)
