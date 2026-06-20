"""Ambiente do Alembic — usa a MESMA engine/URL do app (settings.database_url)
e o metadata dos modelos, para autogenerate enxergar o schema real."""
from logging.config import fileConfig

from alembic import context

# A engine já trata postgres:// -> postgresql:// e SQLite local.
from app.db import engine, Base
from app import models  # noqa: F401 — registra todas as tabelas no Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
