"""role grants: API writer + event-handler SELECT-only

Revision ID: a1c0ffee0002
Revises: a1c0ffee0001
Create Date: 2026-07-21

Grants (spec §3.2): the event-handler role hydrates its trigger index via
read-only SELECT on `automations` and must never write; the API role (when it
is not the table owner running these migrations) gets full write on all three
tables. Role names come from the environment (provisioned by A0):
  DATABASE_EVENT_HANDLER_ROLE  (default: keep_event_handler_ro)
  DATABASE_API_ROLE            (default: empty — migration role owns the tables)
A grant is skipped with a notice when the role does not exist, so the schema
migration still applies on environments where A0 has not run (e.g. local dev).
"""
import logging
import os

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c0ffee0002"
down_revision = "a1c0ffee0001"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

TABLES = ("automations", "automation_runs", "automation_revisions")


def _role_exists(bind, role: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).scalar()
    )


def _roles(bind):
    eh_role = os.environ.get("DATABASE_EVENT_HANDLER_ROLE", "keep_event_handler_ro")
    api_role = os.environ.get("DATABASE_API_ROLE", "")
    for label, role in (("event-handler", eh_role), ("api", api_role)):
        if not role:
            continue
        if not _role_exists(bind, role):
            logger.warning(
                "role %r (%s) does not exist — skipping its grants", role, label
            )
            continue
        yield label, role


def upgrade() -> None:
    bind = op.get_bind()
    for label, role in _roles(bind):
        if label == "event-handler":
            op.execute(f'GRANT SELECT ON automations TO "{role}"')
        else:
            for table in TABLES:
                op.execute(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"'
                )


def downgrade() -> None:
    bind = op.get_bind()
    for label, role in _roles(bind):
        if label == "event-handler":
            op.execute(f'REVOKE SELECT ON automations FROM "{role}"')
        else:
            for table in TABLES:
                op.execute(
                    f'REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM "{role}"'
                )
