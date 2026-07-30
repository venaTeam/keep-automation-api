"""`automation_revisions` — human-attributed change log (spec §4.4): one row
per definition-affecting action, since git commits are made by the single
backend identity. Enum values pinned in automation-contracts.md §"DB enums".

**No `tenant_id` here, deliberately** (unlike `automation_runs`): a revision is
only ever reached through its parent automation, so `automation_id` →
`automations.tenant_id` already scopes every read. A denormalized copy would be
a second value to keep true for no query it enables (spec §4.4).
"""
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, text
from sqlmodel import Field, SQLModel

from src.models.db.helpers import enum_column


class RevisionAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    ENABLE = "enable"
    DISABLE = "disable"
    DELETE = "delete"


class AutomationRevision(SQLModel, table=True):
    __tablename__ = "automation_revisions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    automation_id: UUID = Field(
        sa_column=Column(ForeignKey("automations.id"), nullable=False)
    )
    action: RevisionAction = Field(
        sa_column=enum_column(
            RevisionAction, "automation_revision_action", nullable=False
        )
    )
    git_sha: str | None = None
    resulting_digest: str | None = None
    actor: str
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
