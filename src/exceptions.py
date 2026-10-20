"""Domain exceptions raised by the BL and mapped to HTTP responses in routes."""
from src.contracts.validation_errors import FieldError


class AutomationValidationError(Exception):
    """Carries the accumulated field-level errors for a 400 response."""

    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


class AutomationNotFoundError(Exception):
    pass


class AutomationBuildingError(Exception):
    """Edit rejected while build_state=building (mid-build submission lock)."""


class ScriptRepoUnavailableError(Exception):
    """The script repository (GitLab) could not be reached or returned an error
    — a timeout, a connection failure, or a non-404 API error. Mapped to a 503
    so the caller can retry; the DB transaction never committed, so there is no
    orphan automation row to reconcile."""


class CappUnavailableError(Exception):
    """A CAPP control-API call (deploy / rollout) failed, timed out, or no CAPP
    client is configured yet (D16). Mapped to a 503: the build cutover was not
    applied — the row is still `building` under its lock and the live
    deployment keeps serving the old digest (spec §6.3), so a webhook replay or
    the reconciler can retry it. The message must never carry the CAPP
    identity (spec §3.4)."""
