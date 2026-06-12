# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14
ARG DEBIAN_VERSION=bookworm
ARG UV_VERSION=0.11.19

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv


FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS base


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app


FROM base AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY memexpert/ ./memexpert/
COPY scripts/ ./scripts/
COPY alembic.ini ./
COPY alembic/ ./alembic/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /app/memexpert ./memexpert
COPY --from=builder --chown=app:app /app/scripts ./scripts
COPY --from=builder --chown=app:app /app/alembic.ini ./alembic.ini
COPY --from=builder --chown=app:app /app/alembic ./alembic
RUN mkdir -p /app/.telegram-sessions \
    && chown app:app /app/.telegram-sessions

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["memexpert-api"]
