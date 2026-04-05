"""Shared pytest fixtures for API and future integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from memexpert.api.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh FastAPI app instance for each test."""

    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client bound directly to the ASGI app."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _build_redis_url(container: RedisContainer) -> str:
    """Construct a redis:// URL from a started Redis testcontainer."""

    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def postgres_container_url() -> Iterator[str]:
    """Yield a PostgreSQL connection URL for future integration tests."""

    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Yield a Redis connection URL for future integration tests."""

    with RedisContainer("redis:7") as redis:
        yield _build_redis_url(redis)
