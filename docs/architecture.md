# Architecture

Dischat is a single long-running asyncio service.

Current dependency choices:

- `matrix-nio` for Matrix client access because it remains maintained, async-first, and supports sync/send flows.
- `httpx` for Discourse HTTP access.
- `asyncpg` for PostgreSQL access and concurrent-safe queue patterns.
- SQL migration files with a minimal internal runner to avoid unnecessary ORM weight.
- `pydantic-settings` for validated environment configuration.

The current codebase provides a tested domain baseline and now runs as a continuous asyncio daemon.

Current implemented runtime slices:

- startup applies SQL migrations with `asyncpg`
- category metadata is synced from Discourse into PostgreSQL
- YAML room links are loaded into PostgreSQL
- Matrix sync is used to accept invites and inspect incoming text messages
- slash commands are persisted through PostgreSQL-backed pairing and watch state
- Matrix replies to bridged messages can post back to Discourse
- Discourse polling stores normalized events and enqueues delivery jobs
- delivery jobs are claimed atomically with `FOR UPDATE SKIP LOCKED`
- the main runtime continuously long-polls Matrix, polls Discourse, and drains queued deliveries
- runtime shutdown closes Matrix, Discourse, and PostgreSQL clients cleanly
