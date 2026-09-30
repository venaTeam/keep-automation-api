# keep-automation-api

Control-plane API for the Keep **Automations** feature — a **dedicated service**
(spec §10.2). It owns authoring CRUD, the synchronous submit endpoint, the audit
tables, the CI webhook, SSE to the UI, git commits, and CAPP deploy/rollouts, and
custodies the single CAPP service identity.

> **Status: authoring CRUD + lifecycle orchestration; integration pending.**
> The FastAPI shell, the three exposure-tier routers, authoring CRUD,
> enable/disable and the delete cascade
> are in place; submit, the CI webhook, the reconciler and SSE payloads are still
> stubs (D14–D17, D19–D20). The cascade's CAPP adapter remains a D16
> dependency; the D18 Artifactory adapter uses A0 configuration and fails closed
> when that configuration is absent — see "Lifecycle" below.

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

## Authoring writes and git (D14-ready)

Git I/O never runs inside a DB transaction or under a row lock:

- **Create** commits the script to git first, then inserts the row + revision
  in one short transaction (`building`, `building_sha`, `build_lock_deadline`).
- **Edit** claims `build_state=building` in a short row-locked transaction,
  commits to git with no session open, then writes the definition,
  `building_sha` and revision in a second short transaction — only if the claim
  (fenced by its deadline) is still its own; otherwise `409`. A git failure
  restores the previous build state.
- Every row-lock acquisition waits at most `DATABASE_LOCK_TIMEOUT_MS` (2000);
  past that the request is `503` + `Retry-After: 1`, never a statement-timeout
  500. Code that later adds CAPP/git calls (D15/D16) must keep this shape:
  claim, call with no session, finish.

## Lifecycle (D18)

| Endpoint | Behaviour |
|---|---|
| `POST /automations/{id}/enable` | `matching_state=active`. `400 active_digest_required` if never built. |
| `POST /automations/{id}/disable` | `matching_state=inactive`. Matching only — CAPP is not touched, in-flight runs finish. |
| `DELETE /automations/{id}` | `409` while `building`. Otherwise `202` + progress and the cascade runs in the background; `200` once `deleted`. |

Enable/disable bump `index_generation`, write an `automation_revisions` row and
publish the Redis `reload` signal **after commit**. Toggles and edits on a
`deleting`/`deleted` automation return `409`.

**Delete cascade** (`src/bl/cascade.py`) — `delete_cascade_step` is the last
completed step:

1. DB: `deleting`, generation bump, delete revision, publish `reload`.
2. CAPP: delete Capp `capp_deployment_id` (or `automation-{id}`) → delete the
   Keep-owned run-auth Secret `automation-{id}-run-auth` → clear the encrypted
   DB key in the checkpoint-2 transaction (column pending D16/F24).
3. Registry: delete every image under the automation's own path.
4. Git: archive-mark `{id}/.archived` (script bytes kept).
5. DB: `deleted`.

CAPP `404` is success. Any other failure parks the automation in `deleting` at
its last checkpoint. Only the DELETE that starts the deletion launches the
cascade; repeated DELETEs just report progress, so polling cannot pile up
attempts. `cascade.resume_delete` (D20's internal route, driven by E21's
CronJob) continues a stopped cascade. Checkpoints are compare-and-set,
so concurrent runners are safe. The team Secret named by `secret_name`, the row,
revisions, runs and script bytes are never deleted.

**Adapters.** `CappDeletionClient` is the narrow cascade interface that D16's
general CAPP client must implement. `ArtifactoryRegistryClient` enumerates and
deletes all artifacts below the automation's configured image path; it uses A0's
Artifactory URL, repository, prefix, and credentials. Missing configuration
fails closed, so a DELETE never reports resources removed when they were not.
Git archive uses the in-memory `GitClient` until D14.

Registry deployment settings (supplied by A0, path agreed with F23):

- `ARTIFACTORY_URL`: HTTPS service URL including its context path, e.g.
  `https://registry.example/artifactory`.
- `ARTIFACTORY_REPOSITORY`: local Docker repository; do not use a virtual repository.
- `ARTIFACTORY_IMAGE_PREFIX`: explicit path used by CI, e.g. `automations`.
  All versions/build artifacts must be under `{prefix}/{automation_id}`; the
  shared golden base must be outside that directory. There is no assumed prefix.
- Inject `ARTIFACTORY_TOKEN` OR both `ARTIFACTORY_USERNAME` and
  `ARTIFACTORY_PASSWORD` from deployment secrets. The identity needs read/delete
  permissions on the automation paths.
- `ARTIFACTORY_TIMEOUT_SECONDS`: positive per-socket timeout (default 10 seconds).

The adapter uses [Folder Info](https://docs.jfrog.com/artifactory/reference/getstorageitem)
to enumerate immediate entries, then [Delete Item](https://docs.jfrog.com/artifactory/reference/deleteitem)
to remove each version directory or partial artifact. Responses and HTTP errors
are closed, redirects are refused, and malformed/oversized inventories stop
cleanup instead of counting as empty. A final inventory check gates checkpoint 3.
Tests cover the HTTP contract with simulated responses; a dev Artifactory
round-trip with the provisioned credentials remains an integration acceptance check.

**For other stories:** `run_finalization.finalize_terminated_by_deletion`
(D17/D20 — deletion-killed runs, no team notice), `deletion_started` (D17 retry
and E21 re-drive gate), `lock_for_build_mutation` (D15 cutover fencing),
`cascade.deboard_wallet(tenant_id, wallet, actor, deps)` (F25/ops — no route).

Never-invoked runs of a deleting/deleted automation are finalized `suppressed`
with `suppression_reason=inactive`. That enum value needs keep-migrations revision
`automation_suppression_reason_inactive` applied **before** this service deploys.

### Runbook: stuck deletion

- Row `matching_state=deleting`, `delete_cascade_step` not advancing → log line
  `delete cascade for <id> stopped at step N: <code> (<ExceptionType>)`.
- `capp_delete_failed` / `run_auth_secret_delete_failed` with `Forbidden`: our
  CAPP identity lost Secret/Capp delete permission on the wallet — restore
  access; do not mark deleted by hand.
- `registry_inventory_not_empty`: an image was pushed after deletion began
  (late build, C2/C3) — resume once the push is done.
- Resume: `cascade.resume_delete(id, deps)` — D20's
  `POST /internal/automations/{id}/resume-delete`, run by E21 every tick. A
  repeated user DELETE does **not** resume.

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

- Business endpoints (submit, reconciler, CI webhook, runs, SSE payloads) — **D14–D17, D19–D20**.
- Real CAPP deletion client + run-auth key column (**D16/F24**), A0 Artifactory deployment configuration, and git archive adapter (**D14**).
- Deploy + NetworkPolicy manifests — handled out-of-repo (A0 infra).
- Real identity-provider integration + tier token verification.
