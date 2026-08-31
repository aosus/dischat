# Security

Security rules implemented in this baseline:

- pairing codes are hashed
- pairing codes expire
- pairing codes are single-use
- raw pairing codes are not stored in the database model
- posting identity must come from pairing or relay configuration
- live test category checks fail closed for unexpected category IDs

Still to complete in runtime code:

- persistent pairing attempt rate limits
- audit persistence for every live write path
- production poller filtering of non-public categories before enqueue

Deployment secrets hygiene:

- the example `dischat/dischat` database credentials in `docker-compose.yml` are development-only; set a strong `POSTGRES_PASSWORD` in `.env` (git-ignored) before any real deployment
- the bundled PostgreSQL data volume (`postgres-data`) is the authoritative store for pairings, watches, room links, queued deliveries, and audit records — include it in your backup plan (see [Docker: Data persistence & backups](docker.md#data-persistence-backups))
