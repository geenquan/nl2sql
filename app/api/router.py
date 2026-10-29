from fastapi import APIRouter

from app.api.routes import (
    datasets,
    milestones,
    relationships,
    scans,
    semantic_models,
    semantics,
    sources,
    tables,
)

api_router = APIRouter()
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])
api_router.include_router(scans.router, prefix="/scans", tags=["scans"])
api_router.include_router(tables.router, prefix="/tables", tags=["tables"])
api_router.include_router(semantics.router, tags=["semantics"])
api_router.include_router(relationships.router, tags=["relationships"])
api_router.include_router(semantic_models.router, tags=["semantic-models"])
api_router.include_router(datasets.router, tags=["datasets"])
api_router.include_router(milestones.router, prefix="/milestones", tags=["milestones"])
