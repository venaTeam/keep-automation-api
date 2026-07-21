"""DB models package — importing it registers every table on SQLModel.metadata
(migrations/env.py relies on this)."""
from src.models.db.automation import Automation, BuildState, MatchingState
from src.models.db.automation_revision import AutomationRevision, RevisionAction
from src.models.db.automation_run import (
    AutomationRun,
    FailureClass,
    RunState,
    SuppressionReason,
)

__all__ = [
    "Automation",
    "AutomationRevision",
    "AutomationRun",
    "BuildState",
    "FailureClass",
    "MatchingState",
    "RevisionAction",
    "RunState",
    "SuppressionReason",
]
