# D16 CAPP management adapter

`src/bl/capp_client.py` implements Capp and Keep-owned run-auth Secret
create/get/update/delete against the supplied `caap.yaml` contract. It is
synchronous: invoke it in a worker thread, after closing the DB transaction.
It never reads or writes the database, advances `active_digest`, polls readiness,
or invokes `/run`.

## Configuration

Inject these settings into **keep-automation-api only**, through its deployment
secret store. Never send them from an authoring request or log configuration.

| Variable | Meaning |
| --- | --- |
| `CAPP_BASE_URL` | Required HTTPS backend URL, without embedded credentials |
| `CAPP_USERNAME` | Required OpenShift service identity username |
| `CAPP_PASSWORD` | Required OpenShift service identity password |
| `CAPP_REQUEST_TIMEOUT_SECONDS` | Positive finite timeout for every HTTP call; default 30 |
| `CAPP_ONBOARDED_WALLETS` | Operator-managed JSON mapping of verified wallet names to clusters; default `{}` rejects all wallets |

Example non-secret mapping: `{"payments":"dev-cluster"}`. F25's persisted
wallet registry does not exist on this branch's dev base. Until it lands, this
mapping is the trusted registry, configured only after out-of-band verification.
Restart workers after registry or credential changes. Retain mappings during
deboarding until all deletion cascades finish. Passing a `CappWalletTarget`
does not bypass the mapping: both its namespace and cluster must match.
This adapter's allowlist protects CAPP calls; authoring dropdown/onboarding
validation remains F25's responsibility.

## Callers

```python
from src.bl.capp_client import CappClient
from src.bl.capp_models import CappRequest
from src.config import load_capp_client_settings

client = CappClient(**load_capp_client_settings())
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
resource = client.create_capp(stored_wallet, request)
# D15 persists resource.uid and resource.deployment_url in its own transaction.
# Keep resource.name (deterministic automation-UUID) for later GET/PUT/DELETE.
```

For cutover call `update_capp(wallet, name, CappRequest(name, new_image))`.
The adapter GETs the current object, preserves its managed configuration,
recursively overlays explicit dictionaries, replaces explicit arrays, and PUTs
the complete spec. Omitted fields survive; explicit nulls/empty arrays clear
fields. Response `resources` maps back to request `customResources` when there
is no t-shirt size. Selecting a size/custom resources removes the other choice.
The backend preserves metadata and fetches `resourceVersion` internally, as
documented in `caap.yaml`; the wire schema has no client-side version precondition.
Therefore the client cannot guarantee compare-and-swap against its earlier GET.
Callers must serialize rollouts. A returned 409 is a typed conflict, never retried.

An accepted PUT is **not** readiness confirmation. D15/F25 must inspect the
requested image and Ready conditions, retain the build lock and old active
digest on failure, and let the reconciler inspect ambiguous mutations before
retrying. CAPP owns traffic switching and retaining the old serving revision.
The local mocked tests cannot prove that external traffic behavior.

D18's default deletion dependency now constructs this client. Deletes accept
both 204 and 404. `capp_name_for` supports legacy name-valued deployment IDs and
new UID-valued IDs, using the deterministic name for the latter. Secret methods
accept only `automation-<UUID>-run-auth`; team Secret references are never
fetched or deleted. Missing team Secrets remain CAPP runtime mount failures.

Secret updates GET first, then replace the entire `data` map; omitted keys are
removed. Secret results expose identity/version only, never data. Capp results
expose name, UID, URL, image, conditions and version, without raw response JSON,
environment values, annotations or freeform condition messages.

## Authentication and failures

Login is lazy and token refresh is serialized by a per-client lock. Refresh
starts within 30 seconds of expiry; a failed refresh falls back to one login.
A 401 triggers one authentication recovery and one request replay. Timeouts,
connection failures, and conflicts never trigger automatic mutation replay.
Redirects are rejected. TLS verification remains enabled.

`CappError` contains `code`, `http_status`, and `retryable`. Codes include
authentication, authorization, not_found, conflict, unavailable, timeout,
malformed_response, invalid_target, invalid_request, and configuration.
`CappAuthenticationError` and `CappConflictError` specialize the relevant codes.
`retryable` describes a transient failure, **not permission to blindly replay a
mutation**. Inspect a resource after ambiguous create/update outcomes.
Errors exclude upstream messages, request/response bodies and transport
exception chains. No client logging or tracing contains credentials or data.

## Verification and remaining acceptance

`poetry run pytest tests/test_capp_client.py -q` runs mocked wire tests without
Docker. They cover every method/path, timeout propagation, auth refresh and
concurrency, 401 recovery, safe errors, replacement, conflict, ownership and
wallet validation, result mapping, and D18 dependency compatibility.

`tests/test_capp_live.py` is an opt-in dev acceptance runner. It creates a unique
run-auth Secret and hello Capp, polls readiness for up to 600 seconds, invokes
`/run` once using only the disposable run key, and deletes both resources. It
never passes CAPP credentials to the hosted application. It requires the
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

D15 webhook/cutover orchestration, F25 onboarding state, and D17's invocation
loop are intentionally outside this adapter. No dev CAPP settings were
available during local implementation, so the live round-trip is unproven.
