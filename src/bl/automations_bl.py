"""Authoring business logic: validate → claim → git-commit → persist.

Synchronous by design — routes run these via `run_in_threadpool` (the pinned
blocking-I/O pattern, see src/bl/ssrf.py).

**Tenant scoping (spec §8.1).** `tenant_id` leads every signature here because
it is not optional context: create stamps it, list/get/update filter on it, and
an id belonging to another tenant must read as *absent* — `AutomationNotFoundError`
→ 404, never a 403, so existence never leaks across tenants. It is always
server-derived from the authenticated entity; `AutomationIn` deliberately has no
`tenant_id` field (§4.1), so a client cannot supply one. Note that a primary-key
`session.get()` cannot express this filter — hence the explicit selects below.

**Git I/O never runs inside a DB transaction or under a row lock** (ADR-008,
D14-ready). A real GitLab commit takes hundreds of ms to seconds; holding the
automation's row lock across it would stall every other admission on that row
(toggle, DELETE, build cutover) into the statement timeout, and holding a pooled
connection across it drains the small pool.

- **Create** commits the script first, then inserts the row + revision in one
  short transaction. Nothing can reference the row before it exists, so no claim
  is needed. A git failure leaves no DB state. A DB failure after the commit
  leaves an unreferenced `{id}/script.py` in git (logged; no row points at it).
- **Edit** uses `build_state=building` itself as the lock: (1) a short row-locked
  transaction checks lifecycle/build state and claims `building` with a fresh
  `build_lock_deadline`; (2) the git commit runs with no session open; (3) a
  short row-locked transaction verifies the claim is still ours and writes the
  definition, `building_sha` and the revision. A git failure releases the claim
  (compare-and-set on the deadline, so it never clobbers a newer claim). While
  claimed, DELETE sees `building` → 409 at once, never a lock wait. A crash
  between (1) and (3) leaves `building` with a deadline — E21's stuck-build
  branch releases it (spec §6.2).

Coupling note: create/edit set `build_state=building` (spec §5.1). Nothing in
D13 clears it — the D15 CI webhook flips it to idle/build_failed on build
completion. Until D15 lands, a freshly created automation stays `building`
and PUT correctly returns the mid-build 409.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from src import config
from src.bl.git_client import GitClient
from src.bl.validation import validate_automation
from src.core.db import get_session, lock_first
from src.exceptions import (
    AutomationBuildingError,
    AutomationEditSupersededError,
    AutomationLifecycleConflictError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from src.contracts.limits import TIMEOUT_SECONDS_DEFAULT, GRACE_SECONDS_DEFAULT
from src.models.api.automation import AutomationIn
from src.models.db.automation import (
    DELETION_STATES,
    Automation,
    BuildState,
    MatchingState,
)
from src.models.db.automation_revision import AutomationRevision, RevisionAction

logger = logging.getLogger(__name__)

# Bounded list query — ~500 automations expected; pagination is a follow-up.
LIST_LIMIT = 1000


def _build_lock_deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(
        seconds=config.BUILD_LOCK_TIMEOUT_SECONDS
    )


def _validated(data: AutomationIn) -> None:
    errors = validate_automation(data)
    if errors:
        raise AutomationValidationError(errors)


def scoped_get(
    session, tenant_id: str, automation_id: UUID, for_update: bool = False
) -> Automation:
    """Fetch one automation *within* a tenant, or raise not-found.

    Never `session.get(Automation, automation_id)`: a bare primary-key lookup
    cannot carry the tenant predicate, so it would return another tenant's row.

    `for_update=True` takes the row lock that serializes admission between
    edit/build, enable/disable and delete (D18): whichever transaction locks
    first decides, the other re-reads the committed state. The wait is bounded
    (`lock_first`): a row held too long is a 503, never a statement-timeout 500.
    Callers hold it only for a short DB-only transaction — never across I/O.
    """
    query = select(Automation).where(
        Automation.id == automation_id,
        Automation.tenant_id == tenant_id,
    )
    if for_update:
        automation = lock_first(session, query)
    else:
        automation = session.scalars(query).first()
    if automation is None:
        raise AutomationNotFoundError()
    return automation


def create_automation(
    tenant_id: str, data: AutomationIn, actor: str, git: GitClient
) -> Automation:
    _validated(data)
    automation_id = uuid4()
    script_path = f"{automation_id}/script.py"
    # Git first, with no session open: the row does not exist yet, so nothing
    # can race it and no lock or pooled connection is held across git I/O.
    git_sha = git.commit_script(
        script_path, data.script, f"create {data.name} ({automation_id})"
    )
    automation = Automation(
        id=automation_id,
        tenant_id=tenant_id,
        name=data.name,
        namespace=data.namespace,
        triggers=data.triggers,
        script_path=script_path,
        secret_name=data.secret_name,
        cooldown_seconds=data.cooldown_seconds,
        cooldown_fields=data.cooldown_fields,
        timeout_seconds=data.timeout_seconds or TIMEOUT_SECONDS_DEFAULT,
        grace_seconds=data.grace_seconds or GRACE_SECONDS_DEFAULT,
        logstash_url=data.logstash_url,
        matching_state=MatchingState.INACTIVE,
        build_state=BuildState.BUILDING,
        building_sha=git_sha,
        build_lock_deadline=_build_lock_deadline(),
        created_by=actor,
        updated_by=actor,
    )
    try:
        with get_session() as session:
            session.add(automation)
            session.flush()
            session.add(
                AutomationRevision(
                    automation_id=automation_id,
                    action=RevisionAction.CREATE,
                    git_sha=git_sha,
                    actor=actor,
                )
            )
            session.commit()
            session.refresh(automation)
            session.expunge(automation)
    except Exception as exc:
        logger.warning(
            "automations: create of %s failed after git commit %s (%s); the "
            "script path is unreferenced",
            automation_id,
            git_sha,
            type(exc).__name__,
        )
        raise
    return automation


@dataclass(frozen=True)
class _BuildClaim:
    """What an edit needs to finish or release its `building` claim.

    `deadline` doubles as the claim's fencing token: finish/release act only
    while the row still carries exactly this deadline, so a claim released by
    E21 and re-taken by a newer edit is never overwritten by this one.
    """

    automation_id: UUID
    script_path: str
    deadline: datetime
    previous_build_state: BuildState
    previous_building_sha: str | None


def _claim_build(tenant_id: str, automation_id: UUID) -> _BuildClaim:
    """Transaction 1: admission under the row lock, DB work only."""
    deadline = _build_lock_deadline()
    with get_session() as session:
        automation = scoped_get(session, tenant_id, automation_id, for_update=True)
        # Lifecycle before build: a deleting/deleted row is never editable, and
        # an edit would re-arm a build that recreates CAPP resources the
        # cascade is tearing down (D18).
        if automation.matching_state in DELETION_STATES:
            raise AutomationLifecycleConflictError(automation.matching_state.value)
        if automation.build_state == BuildState.BUILDING:
            raise AutomationBuildingError()
        claim = _BuildClaim(
            automation_id=automation.id,
            script_path=automation.script_path,
            deadline=deadline,
            previous_build_state=automation.build_state,
            previous_building_sha=automation.building_sha,
        )
        automation.build_state = BuildState.BUILDING
        automation.build_lock_deadline = deadline
        # No SHA until the commit exists: a late webhook for the previous build
        # must not match this claim.
        automation.building_sha = None
        session.add(automation)
        session.commit()
    return claim


def _release_build_claim(claim: _BuildClaim) -> None:
    """Undo a claim whose git commit failed. Best effort; never raises.

    If this fails too, the row stays `building` until its deadline and E21's
    stuck-build branch releases it — the original git error is what surfaces.
    """
    try:
        with get_session() as session:
            session.execute(
                update(Automation)
                .where(
                    Automation.id == claim.automation_id,
                    Automation.build_state == BuildState.BUILDING,
                    Automation.build_lock_deadline == claim.deadline,
                )
                .values(
                    build_state=claim.previous_build_state,
                    building_sha=claim.previous_building_sha,
                    build_lock_deadline=None,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "automations: could not release build claim on %s (%s); it expires "
            "at its build-lock deadline",
            claim.automation_id,
            type(exc).__name__,
        )


def update_automation(
    tenant_id: str,
    automation_id: UUID,
    data: AutomationIn,
    actor: str,
    git: GitClient,
) -> Automation:
    # Validate BEFORE any session: ssrf.validate_logstash_url can block up to 3s
    # on DNS, and holding one of the few pooled connections across it exhausts
    # the pool under concurrent PUTs. A wasted validation on a 409 costs nothing.
    _validated(data)
    claim = _claim_build(tenant_id, automation_id)

    # No row lock and no pooled connection held across git I/O.
    try:
        git_sha = git.commit_script(
            claim.script_path, data.script, f"edit {data.name} ({automation_id})"
        )
    except Exception:
        _release_build_claim(claim)
        raise

    # Transaction 2: finish only if the claim is still ours.
    with get_session() as session:
        automation = scoped_get(session, tenant_id, automation_id, for_update=True)
        if (
            automation.build_state != BuildState.BUILDING
            or automation.build_lock_deadline != claim.deadline
        ):
            logger.warning(
                "automations: edit of %s superseded after git commit %s; the "
                "definition was not updated",
                automation_id,
                git_sha,
            )
            raise AutomationEditSupersededError()

        automation.name = data.name
        automation.namespace = data.namespace
        automation.triggers = data.triggers
        automation.secret_name = data.secret_name
        automation.cooldown_seconds = data.cooldown_seconds
        automation.cooldown_fields = data.cooldown_fields
        automation.timeout_seconds = data.timeout_seconds or TIMEOUT_SECONDS_DEFAULT
        automation.grace_seconds = data.grace_seconds or GRACE_SECONDS_DEFAULT
        automation.logstash_url = data.logstash_url
        automation.building_sha = git_sha
        automation.updated_by = actor
        automation.updated_at = func.now()
        session.add(automation)
        session.add(
            AutomationRevision(
                automation_id=automation_id,
                action=RevisionAction.EDIT,
                git_sha=git_sha,
                actor=actor,
            )
        )
        session.commit()
        session.refresh(automation)
        session.expunge(automation)
    return automation


def get_automation(
    tenant_id: str, automation_id: UUID, git: GitClient
) -> tuple[Automation, str | None]:
    with get_session() as session:
        automation = scoped_get(session, tenant_id, automation_id)
        session.expunge(automation)
    script = git.read_script(automation.script_path)
    return automation, script


def list_automations(
    tenant_id: str,
    namespace: str | None = None,
    matching_state: MatchingState | None = None,
    build_state: BuildState | None = None,
) -> list[Automation]:
    query = select(Automation).where(Automation.tenant_id == tenant_id)
    if namespace is not None:
        query = query.where(Automation.namespace == namespace)
    if matching_state is not None:
        query = query.where(Automation.matching_state == matching_state)
    if build_state is not None:
        query = query.where(Automation.build_state == build_state)
    query = query.order_by(Automation.created_at.desc()).limit(LIST_LIMIT)
    with get_session() as session:
        automations = list(session.scalars(query).all())
        for automation in automations:
            session.expunge(automation)
    return automations
