"""Authoring business logic: validate → persist → git-commit, one transaction.

Synchronous by design — routes run these via `run_in_threadpool` (the pinned
blocking-I/O pattern, see src/bl/ssrf.py).

**Tenant scoping (spec §8.1).** `tenant_id` leads every signature here because
it is not optional context: create stamps it, list/get/update filter on it, and
an id belonging to another tenant must read as *absent* — `AutomationNotFoundError`
→ 404, never a 403, so existence never leaks across tenants. It is always
server-derived from the authenticated entity; `AutomationIn` deliberately has no
`tenant_id` field (§4.1), so a client cannot supply one. Note that a primary-key
`session.get()` cannot express this filter — hence the explicit selects below.

Transactional shape (create and update): the `automations` row and its
`automation_revisions` row are written in ONE session; the git commit happens
inside that transaction (after flush, before commit) so a git failure rolls
back both rows — no orphan DB state, retry is a clean resubmit.

Coupling note: create/edit set `build_state=building` (spec §5.1). Nothing in
D13 clears it — the D15 CI webhook flips it to idle/build_failed on build
completion. Until D15 lands, a freshly created automation stays `building`
and PUT correctly returns the mid-build 409.
"""
from uuid import UUID, uuid4

from sqlalchemy import select

from src.bl.git_client import GitClient
from src.bl.validation import validate_automation
from src.core.db import get_session
from src.exceptions import (
    AutomationBuildingError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from src.contracts.limits import TIMEOUT_SECONDS_DEFAULT, GRACE_SECONDS_DEFAULT
from src.models.api.automation import AutomationIn
from src.models.db.automation import Automation, BuildState, MatchingState
from src.models.db.automation_revision import AutomationRevision, RevisionAction

# Bounded list query — ~500 automations expected; pagination is a follow-up.
LIST_LIMIT = 1000


def _validated(data: AutomationIn) -> None:
    errors = validate_automation(data)
    if errors:
        raise AutomationValidationError(errors)


def _scoped_get(session, tenant_id: str, automation_id: UUID) -> Automation:
    """Fetch one automation *within* a tenant, or raise not-found.

    Never `session.get(Automation, automation_id)`: a bare primary-key lookup
    cannot carry the tenant predicate, so it would return another tenant's row.
    """
    automation = session.scalars(
        select(Automation).where(
            Automation.id == automation_id,
            Automation.tenant_id == tenant_id,
        )
    ).first()
    if automation is None:
        raise AutomationNotFoundError()
    return automation


def create_automation(
    tenant_id: str, data: AutomationIn, actor: str, git: GitClient
) -> Automation:
    _validated(data)
    automation_id = uuid4()
    script_path = f"{automation_id}/script.py"
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
        created_by=actor,
        updated_by=actor,
    )
    with get_session() as session:
        session.add(automation)
        session.flush()
        git_sha = git.commit_script(
            script_path, data.script, f"create {data.name} ({automation_id})"
        )
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
    return automation


def update_automation(
    tenant_id: str,
    automation_id: UUID,
    data: AutomationIn,
    actor: str,
    git: GitClient,
) -> Automation:
    with get_session() as session:
        automation = _scoped_get(session, tenant_id, automation_id)
        if automation.build_state == BuildState.BUILDING:
            raise AutomationBuildingError()
        _validated(data)

        automation.name = data.name
        automation.namespace = data.namespace
        automation.triggers = data.triggers
        automation.secret_name = data.secret_name
        automation.cooldown_seconds = data.cooldown_seconds
        automation.cooldown_fields = data.cooldown_fields
        automation.timeout_seconds = data.timeout_seconds or TIMEOUT_SECONDS_DEFAULT
        automation.grace_seconds = data.grace_seconds or GRACE_SECONDS_DEFAULT
        automation.logstash_url = data.logstash_url
        automation.build_state = BuildState.BUILDING
        automation.updated_by = actor

        session.add(automation)
        session.flush()
        git_sha = git.commit_script(
            automation.script_path, data.script, f"edit {data.name} ({automation_id})"
        )
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
        automation = _scoped_get(session, tenant_id, automation_id)
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
