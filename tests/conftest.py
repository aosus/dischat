from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from dischat.storage.db import apply_sql_migrations, create_pool


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:17")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    raw_url = postgres_container.get_connection_url()
    return raw_url.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture()
async def pg_pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await create_pool(database_url)
    await apply_sql_migrations(
        pool,
        Path(__file__).resolve().parents[1] / "src" / "dischat" / "storage" / "migrations",
    )
    yield pool
    async with pool.acquire() as connection:
        for table in [
            "audit_logs",
            "delivery_messages",
            "delivery_jobs",
            "discourse_events",
            "room_link_categories",
            "room_links",
            "user_watches",
            "categories",
            "pairing_sessions",
            "chat_accounts",
        ]:
            await connection.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
    await pool.close()
