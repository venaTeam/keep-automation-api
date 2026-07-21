import os

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import src.models.db  # noqa: F401 — registers all tables on SQLModel.metadata

config = context.config

database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = SQLModel.metadata


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    # TEXT and unbounded VARCHAR are interchangeable in Postgres; SQLModel maps
    # plain `str` to VARCHAR (AutoString) while the schema uses TEXT — not a
    # real diff. Compare by affinity to also cover TypeDecorator wrappers.
    import sqlalchemy as sa

    if (
        inspected_type._type_affinity is sa.String
        and metadata_type._type_affinity is sa.String
    ):
        return False
    return None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
