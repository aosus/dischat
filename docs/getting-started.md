# Getting Started

1. Copy `.env.example` to `.env`.
2. Copy `config.example.yaml` to `config.yaml`.
3. Replace both example PostgreSQL passwords and fill every Matrix/Discourse
   credential in `.env`.
4. Start the bundled deployment with `docker compose up --build -d`.

For development, run `uv sync` and `uv run pytest`. To run `uv run dischat`
directly on the host, point `DATABASE_URL` at a reachable PostgreSQL hostname
(normally `localhost`, not Compose's internal `postgres` service name).
