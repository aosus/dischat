from __future__ import annotations

from pathlib import Path

import asyncpg


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=10, command_timeout=30)


async def apply_sql_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise RuntimeError(f"No SQL migrations found in {migrations_dir}")

    async with pool.acquire() as connection:
        # A session-level advisory lock serializes the whole migration batch,
        # including the per-file transactions below. This prevents two
        # replicas starting together from both applying the same file.
        await connection.execute("SELECT pg_advisory_lock(hashtext('dischat-schema-migrations'))")
        try:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            applied = {
                row["version"]
                for row in await connection.fetch(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            }
            for migration_path in migration_paths:
                if migration_path.name in applied:
                    continue
                async with connection.transaction():
                    await connection.execute(migration_path.read_text(encoding="utf-8"))
                    await connection.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)",
                        migration_path.name,
                    )
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtext('dischat-schema-migrations'))"
            )
