"""Fixtures shared by the tests that need a real database.

Collected automatically by pytest, so nothing imports this file. Keeping the
session fixture here means one definition to fix the day the setup changes.
"""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import create_engine

# Tests needing PostgreSQL opt in, so the normal suite stays offline and fast.
# A suite that takes minutes stops being run before every commit, and a suite
# nobody runs protects nothing.
requires_database = pytest.mark.skipif(
    os.getenv("KP_RUN_DB_TESTS") != "1",
    reason="set KP_RUN_DB_TESTS=1 and run docker compose up -d postgres",
)


@pytest.fixture
def anyio_backend() -> str:
    """anyio can drive asyncio or trio; the application only uses asyncio."""
    return "asyncio"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    The repository functions flush but never commit, so everything written by a
    test disappears at the end. That is why these tests can run against the
    development database without leaving a single row behind.
    """
    engine = create_engine(get_settings().database_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False)

    async with factory() as session:
        yield session

    await transaction.rollback()
    await connection.close()
    await engine.dispose()
