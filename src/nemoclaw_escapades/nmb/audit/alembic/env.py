"""Alembic environment for NMB audit DB migrations.

The database path is read from the ``NMB_AUDIT_DB_PATH`` environment
variable (set by ``AuditDB._run_migrations``).  Falls back to
``nmb_audit.db`` in the current directory for manual ``alembic`` runs.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from nemoclaw_escapades.nmb.audit.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

db_path = os.environ.get("NMB_AUDIT_DB_PATH", "nmb_audit.db")
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = create_engine(config.get_main_option("sqlalchemy.url", ""))

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
