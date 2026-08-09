"""User-tier SSE stub (spec §8.1 `GET /events`). Real event stream lands in D19."""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.deps import get_authenticated_entity

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


@router.get("/events", status_code=200)
async def events():
    async def event_stream():
        # Skeleton: emit a single SSE keep-alive comment, then close.
        yield ": keep-alive\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
