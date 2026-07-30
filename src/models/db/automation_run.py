"""`automation_runs` — one row per (attempted or suppressed) run; the audit
trail (spec §4.4). Unique `(history_id, automation_id)` is the idempotency
authority. Enum string values pinned in automation-contracts.md §"DB enums".

Carries its own `tenant_id` (unlike `automation_revisions`): run history is a
tenant-scoped read, and denormalizing the tenant onto the row keeps every
history read filtered at the row it returns rather than trusting a join back to
`automations`. Always equals the parent automation's `tenant_id`.
"""
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.models.db.helpers import enum_column


class RunState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALLED = "stalled"
    SUPPRESSED = "suppressed"


class SuppressionReason(str, Enum):
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    DEADLINE = "deadline"
    UNCLASSIFIED = "unclassified"
    INFRA = "infra"
    TERMINATED_BY_DELETION = "terminated_by_deletion"


class AutomationRun(SQLModel, table=True):
    __tablename__ = "automation_runs"
    __table_args__ = (
        # The idempotency authority (§4.4): one run row per (alert event, automation).
        # Stays tenant-less on purpose — `history_id` is already unique per alert
        # event platform-wide, so tenant adds no discriminating power, and a
        # three-column key would let a mis-stamped tenant open a SECOND run row
        # for an event that already ran (automation-contracts.md §submit).
        UniqueConstraint(
            "history_id", "automation_id", name="uq_automation_runs_history_automation"
        ),
        # Both scans are deliberately tenant-less too: `automation_id` already
        # implies the tenant, and the reconciler's state scan is an infra repair
        # loop that must see every tenant.
        Index("ix_automation_runs_state_created_at", "state", "created_at"),
        Index(
            "ix_automation_runs_automation_id_created_at",
            "automation_id",
            "created_at",
        ),
    )

    run_id: UUID = Field(default_factory=uuid4, primary_key=True)
    automation_id: UUID = Field(
        sa_column=Column(ForeignKey("automations.id"), nullable=False)
    )
    # Denormalized from the parent automation; see the module docstring.
    tenant_id: str = Field(foreign_key="tenant.id", nullable=False)
    history_id: str
    fingerprint: str
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    state: RunState = Field(
        default=RunState.PENDING,
        sa_column=enum_column(
            RunState,
            "automation_run_state",
            nullable=False,
            server_default=RunState.PENDING.value,
        ),
    )
    suppression_reason: SuppressionReason | None = Field(
        default=None,
        sa_column=enum_column(SuppressionReason, "automation_suppression_reason"),
    )
    gate_flags: dict | None = Field(default=None, sa_column=Column(JSONB))
    automation_digest: str | None = None
    matched_m: int = Field(sa_column=Column(SmallInteger, nullable=False))
    attempts: int | None = Field(default=None, sa_column=Column(SmallInteger))
    outcome_status: str | None = None
    failure_class: FailureClass | None = Field(
        default=None,
        sa_column=enum_column(FailureClass, "automation_failure_class"),
    )
    result: dict | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        )
    )
    submitted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
