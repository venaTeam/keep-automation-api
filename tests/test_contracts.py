"""Drift guards: src/contracts constants vs automation-contracts.md (the source of truth).

The contracts doc lives at the keepHQ workspace root (one level above this
repo). When run standalone (repo checked out alone, e.g. CI), the doc isn't
present — those tests skip rather than fail; the in-repo invariants still run.
"""
import re
from pathlib import Path

import pytest

from src.contracts import (
    COOLDOWN_SECONDS_MAX,
    GRACE_SECONDS_DEFAULT,
    GRACE_SECONDS_MAX,
    GRACE_SECONDS_MIN,
    MATCHABLE_FIELDS,
    MATCHABLE_OPTIONAL,
    MATCHABLE_REQUIRED,
    SCRIPT_MAX_BYTES,
    TIMEOUT_SECONDS_DEFAULT,
    TIMEOUT_SECONDS_MAX,
    TRIGGERS_MIN,
)

CONTRACTS_DOC = Path(__file__).resolve().parents[2] / "automation-contracts.md"

requires_doc = pytest.mark.skipif(
    not CONTRACTS_DOC.exists(), reason="automation-contracts.md not in workspace"
)


def _doc_allowlist_block(doc: str, header: str) -> set[str]:
    """Extract a fenced code block of comma-separated fields following `header`."""
    section = doc.split(header, 1)[1]
    block = re.search(r"```\n(.*?)\n```", section, re.DOTALL).group(1)
    return {f.strip() for f in block.split(",") if f.strip()}


@requires_doc
def test_allowlist_matches_contracts_doc():
    doc = CONTRACTS_DOC.read_text(encoding="utf-8")
    doc_required = _doc_allowlist_block(doc, "**Matchable — required")
    doc_optional = _doc_allowlist_block(doc, "**Matchable — optional")
    assert doc_required == set(MATCHABLE_REQUIRED)
    assert doc_optional == set(MATCHABLE_OPTIONAL)
    assert MATCHABLE_FIELDS == doc_required | doc_optional


@requires_doc
def test_numeric_limits_recorded_in_contracts_doc():
    doc = CONTRACTS_DOC.read_text(encoding="utf-8")
    limits_section = doc.split("**Numeric limits**", 1)[1]
    for constant, value in [
        ("TRIGGERS_MIN", TRIGGERS_MIN),
        ("COOLDOWN_SECONDS_MAX", COOLDOWN_SECONDS_MAX),
        ("TIMEOUT_SECONDS_MAX", TIMEOUT_SECONDS_MAX),
        ("TIMEOUT_SECONDS_DEFAULT", TIMEOUT_SECONDS_DEFAULT),
        ("GRACE_SECONDS_MIN", GRACE_SECONDS_MIN),
        ("GRACE_SECONDS_MAX", GRACE_SECONDS_MAX),
        ("GRACE_SECONDS_DEFAULT", GRACE_SECONDS_DEFAULT),
        ("SCRIPT_MAX_BYTES", SCRIPT_MAX_BYTES),
    ]:
        assert constant in limits_section, f"{constant} missing from doc limits table"
        assert str(value) in limits_section, f"{constant}={value} not in doc"


def test_grace_default_covers_timeout_default():
    # Spec §4.1: the grace window must not expire while a script can still run.
    assert GRACE_SECONDS_DEFAULT >= TIMEOUT_SECONDS_DEFAULT


def test_required_and_optional_disjoint():
    assert not set(MATCHABLE_REQUIRED) & set(MATCHABLE_OPTIONAL)
    assert len(MATCHABLE_FIELDS) == len(MATCHABLE_REQUIRED) + len(MATCHABLE_OPTIONAL)
