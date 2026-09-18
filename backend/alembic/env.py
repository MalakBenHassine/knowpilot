"""Alembic environment.

The database URL comes from the application settings, never from alembic.ini.
A password written in a configuration file is the classic way secrets end up
committed, and it would also allow the migrations to run against a different
database from the one the application uses.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context
from app.core.config import get_settings
from app.db import models  # noqa: F401  imported so the tables register themselves
from app.db.base import Base
from app.db.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What autogenerate compares the live database against.
target_metadata = Base.metadata


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        # Without this, a column whose type changed is not detected and the
        # generated migration silently does nothing.
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Print the SQL instead of running it.

    This is how a migration is reviewed, or handed to someone who is allowed to
    connect to production when we are not.
    """
    _configure(
        url=get_settings().database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(get_settings().database_url)
    async with engine.connect() as connection:
        # Alembic itself is synchronous; run_sync drives it on the async
        # connection so we keep one database driver for the whole project.
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
