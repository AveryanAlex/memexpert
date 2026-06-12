# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=22
ARG DEBIAN_VERSION=bookworm
ARG PNPM_VERSION=10.28.0

FROM node:${NODE_VERSION}-${DEBIAN_VERSION}-slim AS base

ARG PNPM_VERSION
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:${PATH}"

WORKDIR /app

RUN corepack enable \
    && corepack prepare "pnpm@${PNPM_VERSION}" --activate


FROM base AS prod-deps

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --prod --frozen-lockfile --store-dir /pnpm/store


FROM base AS build-deps

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

COPY frontend/ ./
RUN pnpm build


FROM node:${NODE_VERSION}-${DEBIAN_VERSION}-slim AS runtime

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

WORKDIR /app

COPY --from=prod-deps --chown=node:node /app/node_modules ./node_modules
COPY --from=prod-deps --chown=node:node /app/package.json ./package.json
COPY --from=build-deps --chown=node:node /app/build ./build

USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then((r) => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

CMD ["node", "build"]
