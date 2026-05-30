# Architecture

Dischat is a single long-running asyncio service.

Current dependency choices:

- `matrix-nio` for Matrix client access because it remains maintained, async-first, and supports sync/send flows.
- `httpx` for Discourse HTTP access.
- `asyncpg` for PostgreSQL access and concurrent-safe queue patterns.
- SQL migration files with a minimal internal runner to avoid unnecessary ORM weight.
- `pydantic-settings` for validated environment configuration.

The current codebase provides a tested domain baseline. Matrix sync, Discourse polling persistence, and production worker orchestration still need to be expanded.
