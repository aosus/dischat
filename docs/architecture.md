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
- the Matrix `/sync` continuation token is persisted in PostgreSQL
  (`matrix_sync_state`) so restarts resume from the last fully processed batch
  instead of performing a fresh initial sync
- inbound Matrix replies and side-effecting slash commands such as `/pair`
  are processed through a durable idempotency fence (`matrix_event_state`):
  an event marker with an exclusive processing lease is claimed before any
  Discourse write, replays short-circuit on the unique constraint, a crashed
  attempt's fence is taken over only after its lease lapses, the external
  write outcome is recorded on the marker as soon as the write returns, and
  the marker is confirmed only after the reply, its delivery mapping, and
  the room notice are durably recorded
- Discourse polling stores normalized events and enqueues delivery jobs
- delivery jobs are claimed atomically with `FOR UPDATE SKIP LOCKED`
- the main runtime continuously long-polls Matrix, polls Discourse, and drains queued deliveries
- runtime shutdown closes Matrix, Discourse, and PostgreSQL clients cleanly
