"""Alembic environment for the unified audit DB migrations.

The database path is passed in via the ``AUDIT_DB_PATH`` environment
variable, which ``AuditDB._run_migrations`` sets when it spawns the
Alembic subprocess.  This is an internal handoff — not a general-purpose
env-var read — so it does not go through ``load_config()``.  Using
``load_config()`` here would require the full app environment (Slack
tokens, inference config, etc.) to be present, which is not the case in
test or CLI contexts.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

from nemoclaw_escapades.audit.models import Base
from nemoclaw_escapades.config import DEFAULT_AUDIT_DB_PATH

# Alembic's global Config object — provides access to alembic.ini values
# and lets us override settings (like the SQLAlchemy URL) at runtime.
config = context.config

# Wire up Python's standard logging from alembic.ini's [loggers] section
# so migration output respects the configured log level.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The SQLAlchemy MetaData that Alembic compares against the DB to
# generate auto-migrations. All audit table definitions live in
# nemoclaw_escapades.audit.models and are registered on this Base.
target_metadata = Base.metadata

# Read the DB path from the env var that AuditDB._run_migrations injects
# into the subprocess.  Falls back to the config default for manual
# ``alembic`` CLI runs.  expanduser() handles the ``~`` prefix.
db_path = str(Path(os.environ.get("AUDIT_DB_PATH", DEFAULT_AUDIT_DB_PATH)).expanduser())
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits the generated SQL to stdout instead of executing it.  Useful
    for reviewing migration DDL before applying it.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live SQLite database.

    Creates a synchronous SQLAlchemy engine, opens a connection, and
    executes pending migrations inside a transaction.
    """
    connectable = create_engine(config.get_main_option("sqlalchemy.url", ""))

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


# Alembic calls this module at import time.  Choose the right runner
# based on whether ``--sql`` (offline) was passed on the CLI.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
