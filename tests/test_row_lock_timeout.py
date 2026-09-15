"""A row held past DB_LOCK_TIMEOUT_MS is a clean 503 + Retry-After, not a 500."""
import threading
import time
from uuid import UUID

import pytest
from sqlalchemy import text

from src import config
from src.bl import run_finalization
from src.core.db import get_session
from src.exceptions import AutomationBusyError
from tests.helpers import VALID, create_built, row


@pytest.fixture()
def short_lock_timeout(monkeypatch):
    monkeypatch.setattr(config, "DB_LOCK_TIMEOUT_MS", 200)


@pytest.fixture()
def held_row_lock(test_engine):
    """Hold FOR UPDATE on every automation row from another connection."""
    held = threading.Event()
    release = threading.Event()

    def holder():
        with test_engine.connect() as conn:
            with conn.begin():
                conn.execute(text("SELECT id FROM automations FOR UPDATE"))
                held.set()
                release.wait(timeout=15)

    thread = threading.Thread(target=holder)

    def start():
        thread.start()
        assert held.wait(timeout=10)

    yield start
    release.set()
    if thread.is_alive():
        thread.join(timeout=15)


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("post", "/enable", None),
        ("post", "/disable", None),
        ("delete", "", None),
        ("put", "", VALID),
    ],
    ids=["enable", "disable", "delete", "edit"],
)
def test_locked_row_returns_503_with_retry_after(
    client, test_engine, short_lock_timeout, held_row_lock, method, suffix, body
):
    automation_id = create_built(client, test_engine)
    before = row(test_engine, automation_id)
    held_row_lock()

    started = time.monotonic()
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(client, method)(f"/automations/{automation_id}{suffix}", **kwargs)
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    assert elapsed < 3  # bounded by lock_timeout, not the 5s statement timeout
    after = row(test_engine, automation_id)
    assert (after.matching_state, after.build_state, after.index_generation) == (
        before.matching_state,
        before.build_state,
        before.index_generation,
    )


def test_build_mutation_lock_is_bounded_too(
    client, test_engine, short_lock_timeout, held_row_lock
):
    automation_id = UUID(create_built(client, test_engine))
    held_row_lock()
    with pytest.raises(AutomationBusyError):
        with get_session() as session:
            run_finalization.lock_for_build_mutation(session, automation_id)


def test_lock_timeout_does_not_leak_into_the_pool(client, test_engine, short_lock_timeout):
    """`SET LOCAL` ends with the transaction; the next checkout has the default."""
    automation_id = create_built(client, test_engine)
    client.post(f"/automations/{automation_id}/disable")
    with get_session() as session:
        value = session.execute(text("SHOW lock_timeout")).scalar()
    assert value == "0"
