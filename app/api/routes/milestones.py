from fastapi import APIRouter

from app.schemas.milestone import MilestoneResponse
from app.services.milestone_service import MilestoneService

router = APIRouter()
service = MilestoneService()


@router.get("", response_model=list[MilestoneResponse])
def list_milestones() -> list[MilestoneResponse]:
    # TODO(M8): 后续可以从项目状态存储读取真实进度；当前清单是开发基线。
    return service.list_milestones()
