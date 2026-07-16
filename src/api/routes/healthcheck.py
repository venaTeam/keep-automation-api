"""Health / root routes."""
from fastapi import APIRouter

from src import config

router = APIRouter()


@router.get("/")
async def root():
    return {"service": "keep-automation-api", "version": config.KEEP_VERSION}


@router.get("/healthcheck")
async def healthcheck():
    return {"status": "ok"}
