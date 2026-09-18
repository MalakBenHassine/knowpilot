"""One engine and one session factory for the whole process."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str, echo: bool = False) -> AsyncEngine:
    """Create the connection pool.

    Built once at startup: opening a connection costs a round trip, and doing
    it per request would dominate the response time.
    """
    return create_async_engine(
        url,
        echo=echo,
        # Small on purpose: this application runs beside a 2.2 GB model on a
        # free VM, and every idle connection costs memory on the server too.
        pool_size=5,
        max_overflow=5,
        # Checks that a pooled connection is still alive before handing it out.
        # Without it, a connection dropped by the network or by a database
        # restart surfaces as a random failure on an unrelated request.
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build sessions.

    expire_on_commit=False matters in async code: by default SQLAlchemy expires
    every attribute after a commit, so reading one afterwards triggers a lazy
    reload - an implicit database call in the middle of building a response,
    which raises in an async context.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
