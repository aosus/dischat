FROM python:3.11-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin dischat \
    && chown -R dischat:dischat /app

USER dischat

FROM app AS test

USER root
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
USER dischat

FROM app AS production

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-m", "dischat.healthcheck"]

CMD ["uv", "run", "--no-sync", "dischat"]
