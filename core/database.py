import logging
from pathlib import Path
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    """Wraps the asyncpg connection pool and exposes shared query helpers for model classes."""

    def __init__(self, dsn: str):
        """
        :param dsn: PostgreSQL connection string
        """
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Open the connection pool and ensure the core schema exists, logging on first initialization."""
        self.pool = await asyncpg.create_pool(dsn=self.dsn)
        async with self.pool.acquire() as conn:
            before = await self._table_names(conn)
            await conn.execute(_SCHEMA_PATH.read_text())
            after = await self._table_names(conn)

        created = sorted(after - before)
        if created:
            logger.info("Initialized database tables: %s", ", ".join(created))
        else:
            logger.info("Database schema already up to date")

    @staticmethod
    async def _table_names(conn: asyncpg.Connection) -> set[str]:
        """List the base table names currently in the public schema.
        :param conn: an open connection
        :return: set of table names
        """
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row["table_name"] for row in rows}

    async def close(self) -> None:
        """Close the connection pool."""
        if self.pool is not None:
            await self.pool.close()

    async def execute(self, query: str, *args: Any) -> str:
        """Run a query that doesn't return rows (INSERT/UPDATE/DELETE).
        :param query: SQL statement, with $1/$2/... placeholders
        :param args: positional values for the placeholders
        :return: the status string returned by the server
        """
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Run a query and return all matching rows.
        :param query: SQL statement, with $1/$2/... placeholders
        :param args: positional values for the placeholders
        :return: list of matching records
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Run a query and return the first matching row, if any.
        :param query: SQL statement, with $1/$2/... placeholders
        :param args: positional values for the placeholders
        :return: the first matching record, or None
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Run a query and return a single scalar value.
        :param query: SQL statement, with $1/$2/... placeholders
        :param args: positional values for the placeholders
        :return: the value of the first column of the first row
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)
