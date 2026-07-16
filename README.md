# keep-automation-api

Control-plane API for the Keep **Automations** feature — a **dedicated service**
(spec §10.2). It owns authoring CRUD, the synchronous submit endpoint, the audit
tables, the CI webhook, SSE to the UI, git commits, and CAPP deploy/rollouts, and
custodies the single CAPP service identity.

> **Status: D12 skeleton.** This repo currently contains only the FastAPI
> application shell and the three exposure-tier routers with **stub routes and no
> business logic**. Endpoints are filled in by later stories (D13–D20). The DB
> schema + migrations land in **A1**.

## Exposure tiers (spec §8, §3.4)

| Tier | Router | Auth | Notes |
|---|---|---|---|
| **user** | `src/api/routes/user/` | existing identity/session (noauth shim for now) | UI-facing CRUD, `/events` SSE |
| **machine** | `src/api/routes/machine/` | GitLab secret token | CI webhook — network-restricted |
| **internal** | `src/api/routes/internal/` | service token | consumer + reconciler — network-restricted |

Auth is driven by the **existing identity provider** — never a second auth stack
(§10.2). A minimal noauth shim stands in until the shared identity manager is
vendored.

## Run locally

```bash
poetry install
poetry run uvicorn src.main:app --reload --port 8080
```

## Test

```bash
poetry run pytest
```

## Deferred (not in this skeleton)

- DB schema / models / Alembic migrations — **A1**.
- Business endpoints (authoring, submit, reconciler, runs, SSE payloads) — **D13–D20**.
- Deploy + NetworkPolicy manifests — handled out-of-repo (A0 infra).
- Real identity-provider integration + tier token verification.
