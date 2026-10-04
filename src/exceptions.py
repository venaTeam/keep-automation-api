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


class AutomationStateConflictError(Exception):
    """A lifecycle action (enable/disable/delete) is not valid from the current
    state — e.g. enabling an automation that has not built, or disabling one
    that is not active. Carries a human-readable `detail` for the 409 body."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)
