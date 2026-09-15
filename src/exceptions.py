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
    """Edit/delete rejected while build_state=building (mid-build submission lock)."""


class AutomationLifecycleConflictError(Exception):
    """The requested transition is not allowed from the current matching state.

    Enable/disable/edit on a `deleting`/`deleted` automation, or a cascade resume
    on one that was never deleted (spec §5.3–5.4). A retry will not help — 409.
    """

    def __init__(self, matching_state: str):
        self.matching_state = matching_state
        super().__init__(f"transition not allowed from matching_state={matching_state}")


class RunNotFoundError(Exception):
    pass


class AutomationBusyError(Exception):
    """The automation row stayed locked past DB_LOCK_TIMEOUT_MS.

    Another admission (edit claim, toggle, delete, build cutover) holds it for a
    moment. Transient by definition — mapped to 503 + Retry-After (spec §8.4:
    5xx transient, callers back off and retry).
    """


class AutomationEditSupersededError(Exception):
    """An edit's build claim was released before the edit could finish.

    The script was committed to git, but in between the reconciler released the
    build lock past its deadline (E21) and the row moved on. The edit is not
    applied to the definition; the author reloads and retries.
    """
