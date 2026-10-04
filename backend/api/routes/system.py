from fastapi import APIRouter

from backend.config import settings
from backend.api.schemas import HealthResponse, RootResponse

router = APIRouter()


@router.get("/", response_model=RootResponse, tags=["System"], summary="Get API status")
async def root():
    return {
        "status": "ok",
        "message": "Weather Trading Bot API v4.0",
        "simulation_mode": settings.SIMULATION_MODE,
    }


@router.get("/api/health", response_model=HealthResponse, tags=["System"], summary="Check service health")
async def health():
    return {"status": "healthy"}
