"""DB models package — importing it registers every table on SQLModel.metadata.

The metadata deliberately carries **no `Tenant` model**: the `tenant` table
belongs to keep-api-gateway, which also owns the Alembic lineage for the `keep`
database these tables live in. The `tenant_id` FKs are declared as string
references (`foreign_key="tenant.id"`), which SQLAlchemy leaves unresolved until
something actually needs the target — nothing on the runtime path does, since
this service never emits DDL. Anything that *does* emit DDL (only the test
harness) must add a `tenant` stand-in to this metadata first; see tests/conftest.py.
"""
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
