# Docker

The repository includes a production `Dockerfile`, a standard `docker-compose.yml`, and a live-test `docker-compose.live-e2e.yml`.

Use `docker compose up --build -d` for local service startup. The container runs the long-lived bridge process rather than a one-shot bootstrap command.

The regular test suite also uses Docker for PostgreSQL integration tests through Testcontainers.

## Bundled PostgreSQL vs external database

**Bundled PostgreSQL (the `postgres` service in `docker-compose.yml`) is the supported production path for simple, single-host deployments.** With the `postgres-data` named volume it is safe to run long term:

- data survives container recreation, upgrades (`docker compose pull`, `docker compose up -d`), and daemon restarts;
- the service uses a `healthcheck` and `restart: unless-stopped`;
- migrations can be run before startup as described in [Operations](operations.md).

Choose an **external PostgreSQL instance** when you already operate one, need managed backups/high availability, or want to scale the bridge separately from the database. To use one:

1. Point `DATABASE_URL` in `.env` at the external instance.
2. Start the bridge without its Compose dependencies:

   ```bash
   POSTGRES_PASSWORD=external-database-unused \
     docker compose up -d --no-deps dischat
   ```

   `dischat` declares `depends_on` for the bundled `postgres` service, so without
   `--no-deps`, Compose would start the bundled database too. Compose interpolates
   the full file before selecting services, so its required bundled-database
   password must still receive a nonempty, unused placeholder as shown above.

## Development vs production database credentials

The example user/password pair `dischat/dischat` in `docker-compose.yml` is **for local development only**.

For production, set a strong password before the first startup (the password is baked into the data directory on initial volume creation):

```bash
# .env next to docker-compose.yml
POSTGRES_PASSWORD=<long-random-password>
```

and make sure `DATABASE_URL` inside the app's `.env` file matches it:

```text
DATABASE_URL=postgresql://dischat:<same-password>@postgres:5432/dischat
```

> [!NOTE]
> `DATABASE_URL` is parsed as a URI by asyncpg. Percent-encode URI-reserved characters (`@ : / # ? & = %`) in both the username and the password — e.g. Python's `urllib.parse.quote(password, safe="")` — or restrict yourself to URL-safe characters (letters, digits, `-`, `_`).

The password necessarily stays in `.env` even in production: the bundled `postgres` service reads it from the `POSTGRES_PASSWORD` environment variable, and the app reads the same secret inside `DATABASE_URL`, which has no file-based form. Compose interpolates each file *before* merging overrides, so the required `POSTGRES_PASSWORD` in `docker-compose.yml` cannot be swapped for the postgres image's `POSTGRES_PASSWORD_FILE` mechanism via an override file — the override fails interpolation with a "required variable POSTGRES_PASSWORD is missing a value" error unless the variable is set in the environment anyway, which defeats the purpose. For stronger secret hygiene, keep the value out of the repository (`.env` is git-ignored) and restrict file permissions on production hosts (`chmod 600 .env`).

Rotating the password later requires changing it inside PostgreSQL (`ALTER USER ... WITH PASSWORD`) plus updating `POSTGRES_PASSWORD` and `DATABASE_URL` in `.env`, since environment variables only apply on first initialization of the data directory.

## Data persistence & backups

PostgreSQL data lives in the named volume `postgres-data` (mounted at `/var/lib/postgresql/data`). Never remove this volume unless you intend to destroy all state — it contains account pairings, watches, room links, queued deliveries, delivery mappings, event history, and audit records.

Take regular backups of the running database:

```bash
# logical backup (plain SQL)
docker compose exec -T postgres pg_dump -U dischat -d dischat > backup_$(date +%F).sql
```

Restore into a fresh, initialized database:

```bash
docker compose exec -T postgres psql -U dischat -d dischat < backup.sql
```

Notes:

- restoring requires an empty database (drop/recreate it or start from a fresh volume);
- a live restore of the whole deployment from scratch looks like:

  ```bash
  docker compose down            # keeps the postgres-data volume
  docker volume rm <project>_postgres-data   # only if starting truly fresh
  docker compose up -d postgres
  # wait until healthy, then:
  docker compose exec -T postgres psql -U dischat -d dischat < backup.sql
  docker compose up -d
  ```

- store backup files off-host, encrypted if they contain private data;
- test your restore path periodically. A basic expectation: the deployment should be recoverable onto any host with Docker using only a backup file, `.env`, and `config.yaml`.
