"""Database engine/session for the control-plane Postgres.

Lazy singleton engine — created on first use so importing models never requires
a reachable database (migrations and tests manage their own connections).
"""
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from src import config

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
    return _engine


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
