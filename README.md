# Dischat

Dischat bridges public Discourse activity into Matrix rooms and Matrix direct messages.

The service is designed to run continuously: it long-polls Matrix for inbound messages, polls Discourse for new posts, and drains a PostgreSQL-backed delivery queue.

## Deployment

For single-host deployments use the bundled Docker Compose stack: PostgreSQL data is persisted in the named volume `postgres-data`, services auto-restart (`restart: unless-stopped`), and the database has a healthcheck.

Before going to production:

1. set different strong `POSTGRES_ADMIN_PASSWORD` and `POSTGRES_PASSWORD`
   values in `.env` (git-ignored), and make the password in `DATABASE_URL`
   exactly match `POSTGRES_PASSWORD` (percent-encode URI-reserved characters);
2. take regular backups with `docker compose exec -T postgres pg_dump -U dischat -d dischat > backup.sql`.

See [Docker deployment](docs/docker.md) for the full production path (bundled vs external database) and [backup/restore commands](docs/docker.md#data-persistence-backups).

## Development

The repository is built around:

- `uv` for environment and dependency management
- `ruff` for linting and formatting
- `ty` for type checking
- `pytest` for automated tests
- `MkDocs` for project documentation

See `docs/` for setup, architecture, operations, and testing guidance.
