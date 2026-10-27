"""Curated matchable alert fields (spec §4.2, automation-contracts.md §Field allowlist).

Serves both trigger `field` and `cooldown_fields`. Copied exactly from the
contracts doc — the UI (G28) renders from `GET /alert-schema/fields`, never
hardcodes; the server (D13) validates against this set.
"""

# Always present at runtime.
MATCHABLE_REQUIRED: tuple[str, ...] = (
    "application",
    "component",
    "status",
    "site",
    "severity",
    "environment",
    "operator",
)

# Only these two may be absent at runtime (drives the cooldown missing-field rule).
MATCHABLE_OPTIONAL: tuple[str, ...] = (
    "node_name",
    "network",
)

MATCHABLE_FIELDS: frozenset[str] = frozenset(MATCHABLE_REQUIRED) | frozenset(
    MATCHABLE_OPTIONAL
)
