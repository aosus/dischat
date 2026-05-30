# Docker

The repository includes a production `Dockerfile`, a standard `docker-compose.yml`, and a live-test `docker-compose.live-e2e.yml`.

Use `docker compose up --build` for local service startup.

The regular test suite also uses Docker for PostgreSQL integration tests through Testcontainers.
