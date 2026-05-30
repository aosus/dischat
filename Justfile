set shell := ["bash", "-cu"]

check:
    uv sync --locked
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check
    uv run pytest --cov=src --cov-report=term-missing
    uv run mkdocs build --strict
    docker build -t dischat:check .

test:
    uv run pytest --cov=src --cov-report=term-missing

docs:
    uv run mkdocs build --strict

live-e2e:
    docker compose -f docker-compose.live-e2e.yml --env-file .env.live-e2e up --build --abort-on-container-exit
