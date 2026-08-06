"""Environment-driven configuration for keep-automation-api.

Skeleton (D12): only what the app shell needs. Concrete values (tokens, DB DSN,
CAPP identity) are provisioned by A0 and consumed by later stories.
"""
import os
from collections.abc import Mapping

# Service
KEEP_VERSION = os.environ.get("KEEP_VERSION", "0.1.0")
HOST = os.environ.get("KEEP_AUTOMATION_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("KEEP_AUTOMATION_API_PORT", "8080"))

# Auth — driven by the existing identity provider (never a second auth stack, §10.2).
# Only "noauth" is wired in the skeleton; the shared identity manager is vendored later.
AUTH_TYPE = os.environ.get("AUTH_TYPE", "noauth").lower()

# Tier tokens (placeholders — real values from A0; verification wired in D15/D17/D20).
CI_WEBHOOK_TOKEN = os.environ.get("CI_WEBHOOK_TOKEN", "")
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "")

# Database — the shared platform `keep` Postgres (spec §4.4). The automation
# tables live in its `public` schema alongside `alert`, `incident` and `tenant`;
# co-location is what makes `automations.tenant_id` an enforced FK. There is no
# separate `automations` database and no separate DSN.
#
# One database ⇒ one Alembic lineage: these tables' migrations live in
# keep-api-gateway. This service owns the ORM models, not the schema lineage,
# and this repo has no `alembic.ini`/`migrations/`.
#
# TWO ACCEPTED VARIABLE NAMES, ON PURPOSE — do not "clean this up".
# Every other service on this same database reads DATABASE_CONNECTION_STRING
# (keep-api-gateway src/config/consts.py, keep-event-handler
# src/config/consts.py, keep-workflows src/common/core/db_utils.py). While this
# service had a private database the divergent name was harmless; now that all
# four need the *same* DSN, a deploy that sets only the platform-wide name would
# configure three services and silently miss this one. That miss is invisible —
# the engine is lazy and nothing connects at startup, so the pod goes Ready and
# every request 500s instead. Accepting both names removes the trap.
# DATABASE_URL still wins so an existing per-service override keeps working.
DATABASE_URL_ENV_VARS = ("DATABASE_URL", "DATABASE_CONNECTION_STRING")

# Mirrors keep-event-handler's DB_CONNECTION_STRING default so a local run
# points at the same instance without extra config.
DEFAULT_DATABASE_URL = "postgresql://keep:keep@localhost:5432/keep"


def resolve_database_url(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return (dsn, source) — `source` is the variable it came from, or "default".

    Takes the environment as an argument so the precedence is testable without
    reimporting this module.
    """
    for name in DATABASE_URL_ENV_VARS:
        value = environ.get(name)
        if value:
            return value, name
    return DEFAULT_DATABASE_URL, "default"


DATABASE_URL, DATABASE_URL_SOURCE = resolve_database_url(os.environ)

# Bounds on the readiness probe's `SELECT 1` so a slow or black-holed database
# cannot turn the health endpoint into a hung request.
DB_CONNECT_TIMEOUT = int(os.environ.get("DATABASE_CONNECT_TIMEOUT", "3"))

# Hard per-statement ceiling on the application engine. One query hung on a
# lock (a gateway Alembic DDL on the shared `keep` database is the concrete
# case) must not pin a pooled connection indefinitely — with a 3+3 pool per
# worker that is a whole-service outage, and readiness shares the pool so it
# flaps with it. Applied per-connection via libpq options; `SET LOCAL` in a
# transaction (the healthcheck) still overrides it within that transaction.
DB_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("DATABASE_STATEMENT_TIMEOUT_MS", "5000")
)
DB_HEALTHCHECK_TIMEOUT_MS = int(
    os.environ.get("DATABASE_HEALTHCHECK_TIMEOUT_MS", "2000")
)

# Connection pool. Tunable because the connection budget is now SHARED: this
# service, keep-api-gateway, keep-event-handler and keep-workflows all draw from
# one Postgres. The container runs `gunicorn -w 4`, so the per-process numbers
# below multiply by four — 12 connections steady, 24 at peak. Variable names
# match the platform-wide ones (keep-api-gateway/keep-event-handler
# src/config/consts.py) so one deploy setting configures every service.
#
# Deliberately below SQLAlchemy's 5+10 default: today's traffic is authoring
# CRUD, a handful of concurrent users. The ~200 submits/s path (D17) is not
# built yet and will need these revisited — size it against the shared budget
# then, not by taking whatever is left.
DB_POOL_SIZE = int(os.environ.get("DATABASE_POOL_SIZE", "3"))
DB_MAX_OVERFLOW = int(os.environ.get("DATABASE_MAX_OVERFLOW", "3"))
# Wait for a free connection before giving up. Short on purpose: under pool
# exhaustion a fast 503 from readiness is better than requests queueing past the
# client's own timeout.
DB_POOL_TIMEOUT = int(os.environ.get("DATABASE_POOL_TIMEOUT", "10"))

# CORS — comma-separated trusted browser origins.
_cors_raw = os.environ.get("KEEP_CORS_TRUSTED_ORIGINS", "*")
CORS_TRUSTED_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
