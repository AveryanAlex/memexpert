# syntax=docker/dockerfile:1.7

ARG PLAYWRIGHT_VERSION=1.60.0
ARG PNPM_VERSION=10.28.0

FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble

ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:${PATH}" \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

RUN corepack enable \
    && corepack prepare "pnpm@${PNPM_VERSION}" --activate

COPY e2e/package.json e2e/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm-e2e-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

COPY e2e/ ./

CMD ["pnpm", "exec", "playwright", "test"]
