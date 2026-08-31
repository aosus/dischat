FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin dischat \
    && chown -R dischat:dischat /app

USER dischat

CMD ["uv", "run", "--no-sync", "dischat"]
