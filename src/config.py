"""Environment-driven configuration for keep-automation-api.

Skeleton (D12): only what the app shell needs. Concrete values (tokens, DB DSN,
CAPP identity) are provisioned by A0 and consumed by later stories.
"""
import os

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
# Default mirrors keep-event-handler's DB_CONNECTION_STRING default so a local
# run points at the same instance without extra config.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://keep:keep@localhost:5432/keep",
)

# CORS — comma-separated trusted browser origins.
_cors_raw = os.environ.get("KEEP_CORS_TRUSTED_ORIGINS", "*")
CORS_TRUSTED_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
