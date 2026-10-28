from pydantic import BaseModel


class MilestoneResponse(BaseModel):
    id: str
    name: str
    status: str
    description: str
