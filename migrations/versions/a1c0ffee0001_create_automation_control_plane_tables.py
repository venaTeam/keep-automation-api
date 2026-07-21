"""create automation control-plane tables

Revision ID: a1c0ffee0001
Revises:
Create Date: 2026-07-21

Spec §4.4 (rev 4 / CAPP): `automations`, `automation_runs`, `automation_revisions`.
Enum string values pinned in automation-contracts.md §"DB enums".
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "a1c0ffee0001"
down_revision = None
branch_labels = None
depends_on = None

matching_state = sa.Enum(
    "inactive", "active", "deleting", "deleted", name="automation_matching_state"
)
build_state = sa.Enum("idle", "building", "build_failed", name="automation_build_state")
run_state = sa.Enum(
    "pending",
    "submitted",
    "succeeded",
    "failed",
    "stalled",
    "suppressed",
    name="automation_run_state",
)
suppression_reason = sa.Enum(
    "duplicate", "cooldown", name="automation_suppression_reason"
)
failure_class = sa.Enum(
    "transient",
    "permanent",
    "deadline",
    "unclassified",
    "infra",
    "terminated_by_deletion",
    name="automation_failure_class",
)
revision_action = sa.Enum(
    "create", "edit", "enable", "disable", "delete", name="automation_revision_action"
)

ENUM_TYPES = (
    matching_state,
    build_state,
    run_state,
    suppression_reason,
    failure_class,
    revision_action,
)


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        # Holds a CAPP wallet_name; kept named `namespace` for continuity (§4.4).
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("capp_deployment_id", sa.Text(), nullable=True),
        sa.Column("deployment_url", sa.Text(), nullable=True),
        sa.Column("triggers", JSONB(), nullable=False),
        sa.Column("script_path", sa.Text(), nullable=False),
        sa.Column("secret_name", sa.Text(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("cooldown_fields", JSONB(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "grace_seconds",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("300"),
        ),
        sa.Column("logstash_url", sa.Text(), nullable=True),
        sa.Column(
            "matching_state",
            matching_state,
            nullable=False,
            server_default="inactive",
        ),
        sa.Column("build_state", build_state, nullable=False, server_default="idle"),
        sa.Column("active_digest", sa.Text(), nullable=True),
        sa.Column("building_sha", sa.Text(), nullable=True),
        sa.Column("contract_version", sa.Text(), nullable=True),
        sa.Column("build_lock_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_cascade_step", sa.SmallInteger(), nullable=True),
        sa.Column(
            "index_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_automations_matching_state", "automations", ["matching_state"])
    op.create_index(
        "ix_automations_build_state_lock",
        "automations",
        ["build_state", "build_lock_deadline"],
    )
    op.create_index("ix_automations_namespace", "automations", ["namespace"])

    op.create_table(
        "automation_runs",
        sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
        # No ON DELETE cascade: rows persist forever, delete is a state flip (§4.4).
        sa.Column(
            "automation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("automations.id"),
            nullable=False,
        ),
        sa.Column("history_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("state", run_state, nullable=False, server_default="pending"),
        sa.Column("suppression_reason", suppression_reason, nullable=True),
        sa.Column("gate_flags", JSONB(), nullable=True),
        sa.Column("automation_digest", sa.Text(), nullable=True),
        sa.Column("matched_m", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=True),
        sa.Column("outcome_status", sa.Text(), nullable=True),
        sa.Column("failure_class", failure_class, nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The idempotency authority: one row per (alert event, automation).
        sa.UniqueConstraint(
            "history_id", "automation_id", name="uq_automation_runs_history_automation"
        ),
    )
    op.create_index(
        "ix_automation_runs_state_created_at",
        "automation_runs",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_automation_runs_automation_id_created_at",
        "automation_runs",
        ["automation_id", "created_at"],
    )

    op.create_table(
        "automation_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "automation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("automations.id"),
            nullable=False,
        ),
        sa.Column("action", revision_action, nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("resulting_digest", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("automation_revisions")
    op.drop_table("automation_runs")
    op.drop_table("automations")
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=True)
