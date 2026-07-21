"""`automation_runs` — one row per (attempted or suppressed) run; the audit
trail (spec §4.4). Unique `(history_id, automation_id)` is the idempotency
authority. Enum string values pinned in automation-contracts.md §"DB enums".
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
        UniqueConstraint(
            "history_id", "automation_id", name="uq_automation_runs_history_automation"
        ),
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
