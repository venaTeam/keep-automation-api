"""Cross-service automation contracts (canonical copy).

Seed of the A2 shared package. Canonical path: `keep-automation-api/src/contracts/`
(recorded in automation-contracts.md §Vendor-sync); A2 vendors it byte-identical
into keep-automation-consumer and keep-event-handler with a checksum CI guard.
Values here mirror automation-contracts.md — where they disagree, the doc wins
and this module is a bug.
"""
from src.contracts.field_allowlist import (
    MATCHABLE_FIELDS,
    MATCHABLE_OPTIONAL,
    MATCHABLE_REQUIRED,
)
from src.contracts.limits import (
    COOLDOWN_SECONDS_MAX,
    GRACE_SECONDS_DEFAULT,
    GRACE_SECONDS_MAX,
    GRACE_SECONDS_MIN,
    SCRIPT_MAX_BYTES,
    TIMEOUT_SECONDS_DEFAULT,
    TIMEOUT_SECONDS_MAX,
    TRIGGERS_MIN,
)

__all__ = [
    "MATCHABLE_FIELDS",
    "MATCHABLE_OPTIONAL",
    "MATCHABLE_REQUIRED",
    "COOLDOWN_SECONDS_MAX",
    "GRACE_SECONDS_DEFAULT",
    "GRACE_SECONDS_MAX",
    "GRACE_SECONDS_MIN",
    "SCRIPT_MAX_BYTES",
    "TIMEOUT_SECONDS_DEFAULT",
    "TIMEOUT_SECONDS_MAX",
    "TRIGGERS_MIN",
]
