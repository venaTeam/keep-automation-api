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
