# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14
ARG DEBIAN_VERSION=bookworm
ARG UV_VERSION=0.11.19

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv


FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
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
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/.cache /app/.telegram-sessions \
    && chown app:app /app/.cache /app/.telegram-sessions

COPY pyproject.toml uv.lock ./

RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project


FROM runtime-base AS main-app

COPY memexpert/ ./memexpert/
COPY scripts/ ./scripts/
COPY alembic.ini ./
COPY alembic/ ./alembic/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-editable


FROM main-app AS main

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["memexpert-api"]


FROM main AS api

CMD ["memexpert-api"]


FROM main AS bot

CMD ["memexpert-bot"]


FROM main AS scheduler

CMD ["memexpert-scheduler"]


FROM runtime-base AS worker-system

ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/.paddleocr /app/.paddlex \
    && chown app:app /app/.paddleocr /app/.paddlex

COPY docker/paddleocr-requirements.txt ./docker/paddleocr-requirements.txt

RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_PYTHON_DOWNLOADS=automatic uv venv --python 3.13 /opt/paddleocr-venv \
    && UV_PYTHON_DOWNLOADS=automatic uv pip install --python /opt/paddleocr-venv/bin/python -r docker/paddleocr-requirements.txt


FROM worker-system AS worker-deps

RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --group worker --no-install-project


FROM worker-deps AS worker-app

COPY memexpert/ ./memexpert/
COPY scripts/ ./scripts/
COPY alembic.ini ./
COPY alembic/ ./alembic/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --group worker --no-editable


FROM worker-app AS worker

ENV MEMEXPERT_WORKER_IMAGE=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PIPELINE_OCR_PADDLE_COMMAND="/opt/paddleocr-venv/bin/python /app/scripts/paddleocr_json.py --input {input}"

USER app

CMD ["memexpert-workers"]


FROM main AS runtime
