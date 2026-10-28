from fastapi import APIRouter

from app.api.routes import milestones, scans, sources, tables

api_router = APIRouter()
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])
api_router.include_router(scans.router, prefix="/scans", tags=["scans"])
api_router.include_router(tables.router, prefix="/tables", tags=["tables"])
api_router.include_router(milestones.router, prefix="/milestones", tags=["milestones"])
