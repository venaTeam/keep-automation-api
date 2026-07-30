"""Health / root routes.

Two endpoints, two different questions — the distinction is load-bearing:

- **`/healthcheck` is the readiness probe** and actually connects to the
  database. It is the endpoint deployment manifests already point at, so it is
  the one that must not lie: a pod pointed at an unreachable DSN answers 503 and
  never receives traffic, instead of going Ready and 500ing every request. This
  service does no DB work at startup (lazy engine, empty lifespan), so a
  misconfigured DSN has no other way to surface.
- **`/livez` is the liveness probe** and never touches the database. A database
  outage must not restart pods: restarts do not fix a database, and a restart
  loop would destroy an otherwise-healthy process the moment the DB recovers.

The probe is bounded in `src/core/db.check_database` and runs in the threadpool
because the DB driver is blocking (ADR-008).
"""
import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from src import config
from src.core import db as db_core

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def root():
    return {"service": "keep-automation-api", "version": config.KEEP_VERSION}


@router.get("/healthcheck")
async def healthcheck():
    """Readiness: is this pod able to serve a request right now?"""
    try:
        await run_in_threadpool(db_core.check_database)
    except Exception as exc:
        logger.error(
            "Readiness probe failed against %s (DSN from %s): %s",
            db_core.redacted_database_url(),
            config.DATABASE_URL_SOURCE,
            exc,
        )
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable"},
        )
    return {"status": "ok", "database": "ok"}


@router.get("/livez")
async def livez():
    """Liveness: is the process itself up? Deliberately no database call."""
    return {"status": "ok"}
