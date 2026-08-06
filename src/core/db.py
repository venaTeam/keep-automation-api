"""Database engine/session for the shared platform `keep` Postgres.

The automation tables live in the same database and schema as the rest of the
platform (spec §4.4); `config.DATABASE_URL` is that database's DSN. This service
is the tables' sole writer by convention only — the database enforces nothing.

Lazy singleton engine — created on first use so importing models never requires
a reachable database (tests manage their own connections).
"""
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, create_engine

from src import config

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.DATABASE_URL,
            pool_pre_ping=True,
            # Explicit, tunable pool: the connection budget is shared with
            # gateway / event-handler / workflows, and `gunicorn -w 4` multiplies
            # whatever is set here by four. See src/config.py for the sizing
            # rationale and the D17 caveat.
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_timeout=config.DB_POOL_TIMEOUT,
            connect_args={
                # Bounds the TCP/auth handshake. Without it a black-holed host
                # hangs until the OS gives up, far longer than any probe budget.
                "connect_timeout": config.DB_CONNECT_TIMEOUT,
                # Per-statement ceiling on every query this engine runs — a
                # lock-hung query must not pin one of the few pooled
                # connections. See src/config.py for the rationale.
                "options": f"-c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}",
            },
        )
    return _engine


def redacted_database_url() -> str:
    """The configured DSN with the password masked — safe to log.

    Logged once at startup (src/main.py) so "which database did this pod
    actually get" is greppable instead of being guessed from 500s.
    """
    try:
        return make_url(config.DATABASE_URL).render_as_string(hide_password=True)
    except Exception:
        # Never let a malformed DSN break startup logging — the failure will
        # surface loudly on the first connection attempt anyway.
        return "<unparseable DSN>"


def check_database() -> None:
    """Readiness probe: `SELECT 1` against the configured database.

    Raises on any failure; the caller turns that into a 503. Fully bounded so a
    slow database degrades readiness rather than hanging the endpoint:
    `pool_timeout` caps the wait for a pooled connection, `connect_timeout` caps
    the handshake, and `statement_timeout` caps the query itself.

    Deliberately uses the application engine, not a private one — a probe on a
    separate pool would report healthy while the pool every real request draws
    from is exhausted.
    """
    with get_engine().connect() as conn:
        with conn.begin():
            # SET LOCAL scopes the timeout to this transaction, so the setting
            # cannot leak back into the pool and shorten unrelated queries.
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = {config.DB_HEALTHCHECK_TIMEOUT_MS}"
            )
            conn.exec_driver_sql("SELECT 1")


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
