"""DSN resolution: both variable names, and the credential never being logged.

The fallback exists because the rest of the platform sets
`DATABASE_CONNECTION_STRING` for this same database (see src/config.py). If it
regresses, a deploy configures three services and silently misses this one, and
the miss only shows up as per-request 500s — hence a test rather than a comment.
"""
from src import config
from src.core import db as db_core

DEDICATED = "postgresql://a:a@dedicated:5432/keep"
PLATFORM = "postgresql://b:b@platform:5432/keep"


def test_dedicated_var_is_used_when_set():
    url, source = config.resolve_database_url({"DATABASE_URL": DEDICATED})
    assert url == DEDICATED
    assert source == "DATABASE_URL"


def test_falls_back_to_the_platform_wide_var():
    url, source = config.resolve_database_url({"DATABASE_CONNECTION_STRING": PLATFORM})
    assert url == PLATFORM
    assert source == "DATABASE_CONNECTION_STRING"


def test_dedicated_var_wins_when_both_are_set():
    url, source = config.resolve_database_url(
        {"DATABASE_URL": DEDICATED, "DATABASE_CONNECTION_STRING": PLATFORM}
    )
    assert url == DEDICATED
    assert source == "DATABASE_URL"


def test_blank_value_does_not_shadow_the_fallback():
    """An unset-but-declared env var is empty string, not missing."""
    url, source = config.resolve_database_url(
        {"DATABASE_URL": "", "DATABASE_CONNECTION_STRING": PLATFORM}
    )
    assert url == PLATFORM
    assert source == "DATABASE_CONNECTION_STRING"


def test_default_when_neither_is_set():
    url, source = config.resolve_database_url({})
    assert url == config.DEFAULT_DATABASE_URL
    assert source == "default"


def test_redacted_url_keeps_host_and_database_but_masks_the_password(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://keep:sup3rs3cret@db.internal:5432/keep"
    )
    rendered = db_core.redacted_database_url()
    assert "sup3rs3cret" not in rendered
    assert "db.internal:5432" in rendered
    assert rendered.endswith("/keep")


def test_redacted_url_survives_a_malformed_dsn(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "this is not a dsn")
    assert db_core.redacted_database_url() == "<unparseable DSN>"
