# Testing

Run the standard checks with `just check`.

Current test layers:

- unit tests for command, pairing, i18n, permissions, and bridge logic
- mocked E2E tests for pairing and watch flows
- PostgreSQL integration tests using Docker-backed temporary Postgres containers
- live E2E scaffold, skipped by default

The PostgreSQL integration tests require Docker to be available locally and in CI.
