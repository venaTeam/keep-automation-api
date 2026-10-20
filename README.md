# keep-automation-api

Control-plane API for the Keep **Automations** feature — a **dedicated service**
(spec §10.2). It owns authoring CRUD, the synchronous submit endpoint, the audit
tables, the CI webhook, SSE to the UI, git commits, and CAPP deploy/rollouts, and
custodies the single CAPP service identity.

> **Status: authoring CRUD + CI webhook.** The FastAPI shell, the three
> exposure-tier routers, the authoring CRUD endpoints and the CI webhook (D15)
> are in place; submit, the reconciler, the SSE stream and the real CAPP client
> (D16 — until then the webhook fails closed with 503) are still stubs.

## Database

The automation tables (`automations`, `automation_runs`, `automation_revisions`)
live in the **shared platform `keep` database, `public` schema** — the same
database as `alert`, `incident` and `tenant`, **not** a separate `automations`
database. That co-location is what lets `automations.tenant_id` be an **enforced
FK to `tenant.id`** (Postgres has no cross-database foreign keys).

Consequences:

- **Migrations live in keep-api-gateway's Alembic**, the single lineage for that
  database. This repo owns the ORM models (`src/models/db/`) and has **no**
  `alembic.ini` / `migrations/` of its own.
- This service is the tables' **sole writer as a code-level convention**, not a
  database guarantee — there are no grants and no read-only role. keep-event-handler
  reads them over the shared engine inside a per-transaction read-only transaction.
- Every automation query is **tenant-scoped**. `tenant_id` is server-derived from
  the authenticated entity and never accepted from a request body; an id
  belonging to another tenant is a `404`, never a `403`.

Set `DATABASE_URL` to the `keep` DSN (default
`postgresql://keep:keep@localhost:5432/keep`, matching keep-event-handler).
**`DATABASE_CONNECTION_STRING` is accepted as a fallback** — that is the name the
gateway, event-handler and workflows read, and all four services now need the
same DSN, so a deploy that sets only the platform-wide name still configures this
one. `DATABASE_URL` wins when both are set.

## Health probes

| Path | Probe | Touches DB |
|---|---|---|
| `/healthcheck` | **readiness** | yes — bounded `SELECT 1` |
| `/livez` | **liveness** | no |

Readiness connects on purpose: nothing else in this service touches the database
at startup (the engine is lazy, the lifespan does no DB work), so a pod pointed
at the wrong DSN would otherwise report healthy and 500 every request. Liveness
stays DB-free so a database outage degrades readiness instead of restarting pods.
The probe is bounded by `DATABASE_CONNECT_TIMEOUT`, `DATABASE_POOL_TIMEOUT` and
`DATABASE_HEALTHCHECK_TIMEOUT_MS`.

### Connection pool

`DATABASE_POOL_SIZE` (3) and `DATABASE_MAX_OVERFLOW` (3) are **per gunicorn
worker**, and the container runs `-w 4` — so 12 connections steady, 24 at peak.
They are below SQLAlchemy's 5+10 defaults because the Postgres connection budget
is now shared with the gateway, event-handler and workflows, and today's traffic
is authoring CRUD. The ~200 submits/s path (D17) will need these revisited.

## Exposure tiers (spec §8, §3.4)

| Tier | Router | Auth | Notes |
|---|---|---|---|
| **user** | `src/api/routes/user/` | existing identity/session (noauth shim for now) | UI-facing CRUD, `/events` SSE |
| **machine** | `src/api/routes/machine/` | GitLab secret token | CI webhook — network-restricted |
| **internal** | `src/api/routes/internal/` | service token | consumer + reconciler — network-restricted |

Auth is driven by the **existing identity provider** — never a second auth stack
(§10.2). A minimal noauth shim stands in until the shared identity manager is
vendored.

## CI webhook (D15)

`POST /internal/ci-webhook` — called by the script repo's pipeline when one
automation's build finishes. Payload, responses and the SSE event shape are in
`automation-contracts.md` §"CI webhook"; the cutover logic is
`src/bl/builds.py`.

| Env var | Default | Purpose |
|---|---|---|
| `CI_WEBHOOK_TOKEN` | _(empty)_ | Shared secret CI sends as `X-Gitlab-Token`. **Empty = every webhook is rejected (401)** — fail closed. |
| `AUTOMATION_IMAGE_REPOSITORY` | _(empty)_ | Docker repository prefix CI pushes to; a success webhook's `image` must be `{prefix}/{automation_id}@sha256:…`. **Empty = every success webhook is refused (503).** |
| `CI_WEBHOOK_MAX_BODY_BYTES` | `16384` | Checked from `Content-Length` with the token, before the body is read (`411` without one, `413` above it). |
| `LOG_LEVEL` | `INFO` | Root log level (cutovers and build failures log at INFO, refusals at WARNING). |
| `AUTOMATION_BUILD_LOCK_TIMEOUT_SECONDS` | `1800` | `build_lock_deadline` armed on create/edit (E21 releases past it). |
| `REDIS_URL` | _(empty)_ | `reload` signal + automation events. Empty = both skipped (periodic reload still converges the index). |
| `AUTOMATION_RELOAD_CHANNEL` | `reload` | Must match keep-event-handler's subscriber. |
| `AUTOMATION_EVENTS_CHANNEL` | `automation-events` | Build events for the `GET /events` SSE stream (D19 subscribes). |
| `REDIS_PUBLISH_TIMEOUT_SECONDS` | `1.0` | Bounds each publish; failures are logged and swallowed. |

For local poking without CI, send the webhook yourself (the row's
`building_sha` is the `commit_sha` to use). Until D16 lands a success webhook
answers 503 — the build stays `building`; a `failed` one works.

## Run locally

```bash
poetry install
poetry run uvicorn src.main:app --reload --port 8083   # 8080 is keep-api-gateway's
```

## Test

```bash
poetry run pytest
```

DB-backed tests stand up a disposable Postgres (`tests/docker-compose.test.yml`)
and build the schema from this repo's ORM metadata plus a minimal `tenant`
stand-in — see the note at the top of `tests/conftest.py`.

## Deferred

- Business endpoints (submit, reconciler, runs, SSE stream) — **D17–D20**; the real CAPP client — **D16**.
- Deploy + NetworkPolicy manifests — handled out-of-repo (A0 infra).
- Real identity-provider integration + internal-tier token verification (the machine tier's is live since D15).
