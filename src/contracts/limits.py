"""Authoring validation limits (spec §4.1 / §7.6, automation-contracts.md §Validation errors)."""
from src.contracts.field_allowlist import MATCHABLE_FIELDS

# Structural guard behind the pinned M ≤ 3 fan-out assumption (spec §4.2).
TRIGGERS_MIN = 2

# One condition per matchable field (duplicates are rejected), so more than
# len(MATCHABLE_FIELDS) can never be valid. Checked BEFORE the accumulating
# per-entry validators — an unbounded list must not amplify into one error
# per entry (memory/CPU exhaustion on the authoring 400 path).
TRIGGERS_MAX = 9

# Pinned to keep-event-handler's AUTOMATION_INDEX_MAX_VALUE_BYTES default: the
# trigger index silently skips rows whose value exceeds it, so authoring
# refuses first. Bump both together (automation-contracts.md §Numeric limits).
TRIGGER_VALUE_MAX_BYTES = 512

# Same anti-amplification bound for cooldown_fields.
COOLDOWN_FIELDS_MAX = 9

# Re-fire suppression window cap: 1 day (spec §4.1).
COOLDOWN_SECONDS_MAX = 86400

# Hard run timeout — the API's HTTP-client deadline on /run (spec §4.1, §7.6).
TIMEOUT_SECONDS_MAX = 900
TIMEOUT_SECONDS_DEFAULT = 300

# Night-team escalation grace window (spec §4.1, §5.6).
GRACE_SECONDS_MIN = 60
GRACE_SECONDS_MAX = 3600
GRACE_SECONDS_DEFAULT = 300

# Script byte cap, enforced before ast.parse (CPU/memory guard on authoring).
SCRIPT_MAX_BYTES = 262144

# Spec §4.1: grace default must be ≥ the timeout default so the grace window
# never expires while the script is still legitimately running.
assert GRACE_SECONDS_DEFAULT >= TIMEOUT_SECONDS_DEFAULT

# The count caps are "one per matchable field" — they must track the allowlist.
assert TRIGGERS_MAX == len(MATCHABLE_FIELDS)
assert COOLDOWN_FIELDS_MAX == len(MATCHABLE_FIELDS)
