# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14
ARG DEBIAN_VERSION=bookworm
ARG UV_VERSION=0.11.19
ARG FFMPEG_VERSION=8.1.2
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:8.1.2@sha256:33f770f812cbfc3de96c547157fc9faf8bd95a36481753439ffa761045167585

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM ${FFMPEG_IMAGE} AS ffmpeg-bin


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
    && mkdir -p /app/.cache \
    && chown app:app /app/.cache

COPY pyproject.toml uv.lock ./

RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project


FROM runtime-base AS main-app

COPY memexpert/ ./memexpert/
COPY scripts/ ./scripts/
COPY docs/research/ ./docs/research/
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

ARG FFMPEG_VERSION

ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

COPY --from=ffmpeg-bin --chmod=0755 /ffmpeg /ffprobe /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version 2>&1 | grep -F "ffmpeg version ${FFMPEG_VERSION} " \
    && ffprobe -version 2>&1 | grep -F "ffprobe version ${FFMPEG_VERSION} " \
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

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD memexpert-runtime-health

CMD ["memexpert-workers"]


FROM main AS runtime
