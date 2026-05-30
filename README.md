# Dischat

Dischat bridges public Discourse activity into Matrix rooms and Matrix direct messages.

The service is designed to run continuously: it long-polls Matrix for inbound messages, polls Discourse for new posts, and drains a PostgreSQL-backed delivery queue.

The repository is built around:

- `uv` for environment and dependency management
- `ruff` for linting and formatting
- `ty` for type checking
- `pytest` for automated tests
- `MkDocs` for project documentation

See `docs/` for setup, architecture, operations, and testing guidance.
