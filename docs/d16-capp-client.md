# D16 CAPP management adapter

`src/bl/capp/client.py` implements Capp and Keep-owned run-auth Secret
create/get/update/delete against the supplied `caap.yaml` contract. It uses
`httpx.AsyncClient`: await each resource operation after closing the DB transaction.
It never reads or writes the database, advances `active_digest`, polls readiness,
or invokes `/run`.

## Module responsibilities

- `client.py` checks target URL syntax and builds resource requests.
- `deployment.py` coordinates async D15 create/find, rollout and readiness.
- `transport.py` owns the HTTP pool, authentication, token refresh, deadlines,
  status handling, and rejection of responses that echo passwords or tokens.
- `responses.py` validates responses and produces typed resource results.
- `validation.py` validates nested request fields against the supplied CAPP
  contract without coercing values or filling omitted update fields. Invalid
  request serialization raises sanitized `invalid_request` before authentication.

The public async resource API and client lifetime are unchanged. Every deletion
cascade step now has an async interface; each step offloads its own blocking
DB, registry, Git, or reload calls. The runner awaits all steps uniformly.

## Configuration

Inject these settings into **keep-automation-api only**, through its deployment
secret store. Never send them from an authoring request or log configuration.

| Variable | Meaning |
| --- | --- |
| `CAPP_BASE_URL` | Required HTTPS backend URL, without embedded credentials |
| `CAPP_USERNAME` | Required OpenShift service identity username |
| `CAPP_PASSWORD` | Required OpenShift service identity password |
| `CAPP_REQUEST_TIMEOUT_SECONDS` | Positive finite timeout for every HTTP call; default 30 |
| `CAPP_READINESS_TIMEOUT_SECONDS` | Positive finite per-webhook readiness polling window; default 30 seconds |
| `CAPP_POLL_INTERVAL_SECONDS` | Positive finite interval between readiness reads; default 1 |
| `CAPP_CLUSTER` | Required default cluster for calls that supply a wallet name |

Wallet onboarding and tenant authorization belong to the calling business layer,
outside D16. The client has no wallet registry or allowlist. A wallet name uses
`CAPP_CLUSTER`; a `CappWalletTarget(cluster, wallet_name)` supplies both explicitly.
Both path segments are checked for valid URL syntax only.
CAPP settings are read once when `src.config` is imported during process startup,
like the other environment settings. Changing the default cluster, endpoint,
timeout, or credentials requires restarting workers.

The CI caller must retry a `503` webhook response with bounded exponential
backoff until the build lock deadline. The readiness window is per delivery,
so a cold image pull can legitimately require a later delivery.

## Callers

```python
from src.bl.capp.client import CappClient
from src.bl.capp.models import CappRequest
from src.config import load_capp_client_settings

request = CappRequest(
    name=f"automation-{automation_id}",
    image=verified_build_image,  # registry/path@sha256:<64 hex characters>
    config={
        "state": "enabled",
        "size": "small",
        "scaleSpec": {"minReplicas": 0, "metric": "concurrency"},
        "routeSpec": {"hostname": trusted_hostname, "tlsEnabled": True},
        "secretVolumes": [{"name": "automation-secrets",
                           "secretName": stored_team_secret_name,
                           "mountPath": "/etc/automation/secrets"}],
    },
)
async with CappClient(**load_capp_client_settings()) as client:
    resource = await client.create_capp(stored_wallet, request)
# For D15 use the deployment coordinator below: acceptance is not readiness.
# Keep resource.name (deterministic automation-UUID) for later GET/PUT/DELETE.
```

In API routes, reuse the app-scoped resource client supplied by
`get_capp_resource_client` for deletion and the deployment coordinator supplied
by `get_capp_deployment_client` for D15.
It is created at startup on that app's event loop and closed during FastAPI lifespan
shutdown. Standalone async callers own the client via `async with` or
`await client.aclose()`. Do not share a client across event loops or instantiate
one per call in a polling loop. This follows
[HTTPX's async client lifecycle guidance](https://www.python-httpx.org/async/).

For cutover call `await client.update_capp(wallet, name, CappRequest(name, new_image))`.
The adapter GETs the current object, preserves its managed configuration,
recursively overlays explicit dictionaries, replaces explicit arrays, and PUTs
the complete spec. Omitted fields survive; explicit nulls/empty arrays clear
fields. Response `resources` maps back to request `customResources` when there
is no t-shirt size. Selecting a size/custom resources removes the other choice.
The backend preserves metadata and fetches `resourceVersion` internally, as
documented in `caap.yaml`; the wire schema has no client-side version precondition.
Therefore the client cannot guarantee compare-and-swap against its earlier GET.
Existing Capps may be enabled or disabled. An omitted state preserves the current
state; explicitly requesting enabled re-enables a disabled Capp. Explicit disable
requests remain rejected by this client. Callers must serialize rollouts. A
concurrent update conflict is transient; identity mismatches remain permanent.

An accepted PUT is **not** readiness confirmation. D15/F25 must inspect the
requested image and Ready conditions, retain the build lock and old active
digest on failure, and let the reconciler inspect ambiguous mutations before
retrying. CAPP owns traffic switching and retaining the old serving revision.
The local mocked tests cannot prove that external traffic behavior.

Errors after CAPP accepts a create or update, including authorization failures
during readiness polling, remain retryable so reconciliation can restore the
durable row. Only a rejection before the mutation is accepted becomes a
permanent build failure.

Application startup validates CAPP settings and constructs one client per app.
Missing or invalid settings log a sanitized warning once at startup and select an
unconfigured deletion adapter, so deletion can stop matching and commit checkpoint 1.
Cleanup then stops at the CAPP step for a later reconciler retry. Correct deployment
settings and restart workers to load the corrected configuration. Requests reuse
the startup adapter; shutdown closes it. Initialization makes no network calls.
Deletes accept
both 204 and 404. `capp_name_for` supports legacy name-valued deployment IDs and
new UID-valued IDs, using the deterministic name for the latter. Secret methods
accept only `automation-<UUID>-run-auth`; team Secret references are never
fetched or deleted. Missing team Secrets remain CAPP runtime mount failures.

The D18 cascade, background attempt, resume, and wallet deboarding are async.
They await CAPP operations directly, while short database transactions and the
existing synchronous Git/registry/reload adapters run in worker threads. No DB
session crosses a CAPP await. Cancellation propagates and leaves the last durable
checkpoint for a later retry; it is not converted to success or retried inline.

All resource methods use the `wallet` argument, accepting a wallet name or an
explicit `CappWalletTarget`. Secret creation always uses type `Opaque`;
`SecretRequest` contains only `name` and `data`.

Secret updates use a single PUT to replace the entire `data` map; omitted keys are
removed. Secret results expose identity/version only, never data. Capp results
expose name, UID, URL, image, conditions and version, without raw response JSON,
environment values, annotations or freeform condition messages.

## Authentication and failures

This integration supports only CAPP's `openshift` username/password mode. The
platform must be configured for that mode. There is no mode selector, automatic
mode discovery, or cluster/token (`passthrough`) authentication implementation.

Login is lazy and token refresh is serialized by a per-client `asyncio.Lock`. Refresh
starts within 30 seconds of expiry; a failed refresh falls back to one login.
A failed login starts an exponential cooldown on that client, from five seconds
up to one minute. Calls needing authentication during the cooldown fail
immediately with the same sanitized error classification, without another login
attempt. Only error metadata is retained; each caller receives a fresh
exception. The cooldown uses a monotonic clock, resets on successful
authentication, and is local to each app/client (not shared across workers or
replicas). Cancellation does not start a cooldown. Per-HTTP-call timeouts are
unchanged; this does not add an overall operation deadline.
A 401 triggers one authentication recovery and one request replay. Timeouts,
connection failures, and conflicts never trigger automatic mutation replay.
Redirects are rejected. TLS verification remains enabled.
Each request has explicit HTTPX connect/read/write/pool timeouts and an outer
`asyncio.timeout` total deadline. Responses are streamed with a size limit and
closed on success, failure, timeout, or cancellation. Environment proxy and netrc
configuration is disabled (`trust_env=False`); the injected service identity is
the sole authentication source.

HTTP methods and response comparisons use Python's standard-library `HTTPMethod`
and `HTTPStatus`. `CappError` contains a `CappErrorCode` enum, `http_status`, and
`retryable`. Codes include
authentication, authorization, not_found, conflict, unavailable, timeout,
malformed_response, invalid_target, invalid_request, and configuration.
`CappAuthenticationError` and `CappConflictError` specialize the relevant codes.
`retryable` describes a transient failure, **not permission to blindly replay a
mutation**. Inspect a resource after ambiguous create/update outcomes.
Usernames are identifiers and may legitimately appear in resource paths or platform
annotations, so they are not used as response substring filters. Passwords and
tokens remain checked in response keys and values, including tokens retained
across concurrent refreshes.
Errors exclude upstream messages, request/response bodies and transport
exception chains. No client logging or tracing contains credentials or data.

Readiness status uses the strict `ConditionStatus` enum. Known sources use
`ConditionSource`; unknown source strings are preserved as informational text.
The enums, including `CappErrorCode`, preserve string comparisons and
JSON wire values. Response parsing validates nested objects, scalar types, and
enum values before constructing result models. HTTP failures share the same
classification for authentication and resource calls, including transient
backend outages and nonstandard proxy server-error statuses.

## Verification and remaining acceptance

`poetry run pytest tests/test_capp_client.py -q` runs mocked wire tests without
Docker. They cover every method/path, timeout propagation, auth refresh and
concurrency, 401 recovery, safe errors, replacement, conflict, ownership and
target syntax checks, caller-supplied routing, result mapping, and D18 dependency compatibility.
Async tests also verify overlapping requests, concurrent 401 recovery, cancellation,
stream cleanup, total deadlines, and per-app pool lifecycle. Run
`tests/test_cascade_async.py` for worker-thread and checkpoint guarantees without
Docker; the existing database-backed lifecycle tests exercise the same async runner.

`tests/test_capp_live.py` is an opt-in dev acceptance runner. It creates a unique
run-auth Secret and hello Capp, polls readiness within its configured deadline, invokes
`/run` once using only the disposable run key, and deletes both resources. It
never passes CAPP credentials to the hosted application. Failures report the stage
and sanitized error code, without raw exception messages or response bodies.
Cleanup errors are reported alongside the original failure. Capp deletion must
succeed before Secret deletion is attempted; otherwise diagnostics identify both
resources for inspection and explicitly report that Secret cleanup was skipped. It requires the
approved hello image to read `/etc/automation/run-auth/api-key` and implement
the golden wrapper's `X-API-Key` check. The supplied documents contain older
env-var-based run-auth text; this implementation follows the newer D16
spec/design and D18 Secret ownership contract.

In a provisioned dev environment, set `CAPP_LIVE_TEST=1`,
`CAPP_HELLO_WALLET`, `CAPP_HELLO_IMAGE` (digest-pinned), and a reserved
`CAPP_HELLO_HOSTNAME`, alongside the normal secret-backed CAPP settings. Run:

```text
poetry run pytest tests/test_capp_live.py -q
```

D15's webhook/cutover orchestration is implemented by the coordinator below.
F25 onboarding state and D17's invocation loop are intentionally outside this
adapter. No dev CAPP settings were
available during local implementation, so the live round-trip is unproven.

## D15 async deployment integration

`src/bl/capp/deployment.py` implements `CappDeploymentClient` with
`CappDeploymentCoordinator`, sharing the app-owned async resource client. Both
are initialized at startup. The webhook awaits build orchestration directly;
complete snapshot/commit transactions and blocking publishers run in worker
threads. Cleanup awaits the same async deletion client D18 uses; there is no
sync bridge or second event loop.

First creation uses `automation-{id}`. A conflict triggers GET/reconciliation in
the requested wallet, recovering a previous create whose database commit failed.
Matching image and managed configuration skip another PUT, so a readiness retry
does not restart the revision. Rollout receives the wallet and automation ID,
resolves legacy UID-valued deployment IDs to their deterministic resource name,
and rejects a changed resource name or a missing resource without recreating it.
The database stores the deterministic resource name and first deployment URL;
the UID is used internally to ensure readiness refers to the same resource.

Managed configuration sets `AUTOMATION_ID`, `AUTOMATION_SECRETS_PATH`, and the
optional `automation-secrets` volume mounted at `/etc/automation/secrets`.
Changing or removing a team Secret replaces or removes that mount. Existing
unrelated environment variables, system Secret mounts and sizing/route settings
survive. No team Secret values are fetched. Run-auth key provisioning/encryption
remains F24 work; existing run-auth configuration is preserved, not generated.
`CappResource.config` contains a validated configuration snapshot for these
updates and is excluded from repr; callers must never log its contents.

After mutation the coordinator GETs until the same resource name and requested image have
a route URL and serving readiness (Capp/Knative or legacy unsourced Ready), with
all exposed Ready conditions true. Route readiness alone is insufficient. The
API has no observed-generation field, so the available image/condition
correlation cannot establish stronger revision freshness than CAPP exposes.
The per-webhook deadline cancels stalled calls and polling. A readiness timeout
returns sanitized 503 with the build lock retained so a later webhook can observe
readiness. Permanent CAPP rejections record `build_failed` and release the lock;
transient failures retain it. Cancellation propagates. If deletion starts during
readiness, cleanup uses the original wallet and resource name even when readiness
fails.

The existing admission/commit checks fence database cutover; they do not make
CAPP and the database atomic or serialize different workers' external mutations.
The low-level GET/PUT API has no compare-and-swap precondition. Cross-wallet
orphan discovery and drift reconciliation remain E21 work: the coordinator does
not scan wallets or maintain an onboarding registry. E21 owns the cross-wallet
sweep for deployments stranded before their database wallet was persisted; D18's
normal cascade only deletes in the row's current wallet.

Coverage: `test_ci_webhook_capp.py` drives the HTTP webhook through the production
startup dependencies, real async coordinator/resource client, mocked CAPP and
PostgreSQL. `test_capp_deployment.py` tests readiness, secret replacement,
conflict recovery, missing/replaced resources, cancellation and error redaction.
The existing webhook race suite uses an async test double in
`tests/capp_deployment_fake.py`; no test double lives in production modules.
