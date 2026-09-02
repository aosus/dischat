# Docker

The repository includes a production `Dockerfile`, a standard `docker-compose.yml`, and a live-test `docker-compose.live-e2e.yml`.

Use `docker compose up --build -d` for local service startup. The container runs the long-lived bridge process rather than a one-shot bootstrap command.
Compose requires a regular-file `config.yaml` next to `docker-compose.yml`; copy
`config.example.yaml` before the first startup. The bind mount deliberately
refuses to create a directory when that file is missing.

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
   POSTGRES_ADMIN_PASSWORD=external-admin-unused \
   POSTGRES_PASSWORD=external-runtime-unused \
     docker compose up -d --no-deps dischat
   ```

   `dischat` declares `depends_on` for the bundled `postgres` service, so without
   `--no-deps`, Compose would start the bundled database too. Compose interpolates
   the full file before selecting services, so its required bundled-database
   passwords must still receive nonempty, unused placeholders as shown above.

## Database roles and credentials

The bundled database creates two roles on the first initialization:

- `dischat_admin`, the PostgreSQL image's bootstrap administrator;
- `dischat`, a separate `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE` runtime
  role that owns only the `dischat` database/schema and runs application
  migrations.

Set different strong passwords for both roles before first startup (they are
written into the data directory when the volume is initialized):

```bash
# .env next to docker-compose.yml
POSTGRES_ADMIN_PASSWORD=<long-random-admin-password>
POSTGRES_PASSWORD=<different-long-random-runtime-password>
```

and make sure `DATABASE_URL` inside the app's `.env` file matches it:

```text
DATABASE_URL=postgresql://dischat:<runtime-password>@postgres:5432/dischat
```

> [!NOTE]
> `DATABASE_URL` is parsed as a URI by asyncpg. Percent-encode URI-reserved characters (`@ : / # ? & = %`) in both the username and the password — e.g. Python's `urllib.parse.quote(password, safe="")` — or restrict yourself to URL-safe characters (letters, digits, `-`, `_`).

Both passwords stay in `.env` for the bundled deployment. Keep that file out
of the repository and restrict it with `chmod 600 .env`. `.dockerignore`
explicitly excludes `.env*` and runtime `config*.yaml` files so credentials
and room mappings cannot enter image layers; Compose mounts `config.yaml`
read-only at runtime.

The one-shot `db-bootstrap` service verifies the role split and applies the
runtime-role password on every startup, including existing volumes. For a
volume created by the older single-role Compose setup, set
`POSTGRES_LEGACY_PASSWORD` to that role's previous password for the first
upgraded startup if it differs from the new `POSTGRES_PASSWORD`; remove it
after `db-bootstrap` succeeds.

The application image includes a heartbeat healthcheck. A stale heartbeat
marks the container unhealthy after five minutes; monitor that status in
production. Matrix network timeout retries are bounded, so persistent Matrix
failures terminate the process and `restart: unless-stopped` can recover it.

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
