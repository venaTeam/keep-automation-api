"""Readiness actually reaches the database; liveness deliberately does not.

The DB-down case is simulated by making `check_database` raise. Stopping the
real container mid-suite would poison every later test — and the assertion here
is about how the route reacts to a failed probe, not about how psycopg2 fails.
"""
import pytest
from sqlalchemy.exc import OperationalError

from src.core import db as db_core


def test_healthcheck_reports_a_reachable_database(client):
    resp = client.get("/healthcheck")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_probe_runs_a_real_query_against_the_test_engine(test_engine):
    """No mock here on purpose: proves the probe's SQL is valid on Postgres."""
    db_core.check_database()


def test_healthcheck_is_503_when_the_database_is_unreachable(client, monkeypatch):
    def _refused():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db_core, "check_database", _refused)

    resp = client.get("/healthcheck")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "database": "unreachable"}


def test_healthcheck_is_503_on_any_probe_failure(client, monkeypatch):
    """A misconfigured DSN can fail long before OperationalError (bad driver,
    unparseable URL); readiness must fail closed for all of them."""

    def _boom():
        raise ValueError("could not parse DSN")

    monkeypatch.setattr(db_core, "check_database", _boom)
    assert client.get("/healthcheck").status_code == 503


def test_liveness_ignores_the_database(client, monkeypatch):
    """A DB outage must not restart the pod — /livez never calls the probe."""

    def _fail_the_test():
        pytest.fail("liveness must not touch the database")

    monkeypatch.setattr(db_core, "check_database", _fail_the_test)

    resp = client.get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
