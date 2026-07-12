"""Deterministic no-I/O tests for the E2E seed HTTP retry budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from scripts import seed_e2e

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class FakeClock:
    now: float = 100.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def build_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: FakeClock,
    timeout_seconds: float = 20.0,
) -> tuple[seed_e2e.PipelineApiClient, seed_e2e.MonotonicDeadline]:
    deadline = seed_e2e.MonotonicDeadline.after(
        timeout_seconds,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    http_client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    client = seed_e2e.PipelineApiClient(
        base_url="https://api.test",
        operator_token="operator-token",
        timeout_seconds=10.0,
        client=http_client,
    )
    return client, deadline


def test_healthcheck_retries_transport_error_with_capped_budget() -> None:
    clock = FakeClock()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    client, deadline = build_client(handler, clock=clock)
    with client:
        client.healthcheck(deadline=deadline)

    assert len(requests) == 2
    assert clock.sleeps == [seed_e2e.HTTP_RETRY_INITIAL_BACKOFF_SECONDS]


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503, 599])
def test_healthcheck_retries_only_transient_http_statuses(status_code: int) -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, text="transient")
        return httpx.Response(200, json={"status": "ok"})

    client, deadline = build_client(handler, clock=clock)
    with client:
        client.healthcheck(deadline=deadline)

    assert attempts == 2
    assert clock.sleeps == [seed_e2e.HTTP_RETRY_INITIAL_BACKOFF_SECONDS]


def test_retry_after_is_honored_but_capped() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "600"}, text="rate limited")
        return httpx.Response(200, json={"status": "ok"})

    client, deadline = build_client(handler, clock=clock)
    with client:
        client.healthcheck(deadline=deadline)

    assert attempts == 2
    assert clock.sleeps == [seed_e2e.HTTP_RETRY_MAX_BACKOFF_SECONDS]


def test_retry_after_below_cap_controls_delay() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="rate limited")
        return httpx.Response(200, json={"status": "ok"})

    client, deadline = build_client(handler, clock=clock)
    with client:
        client.healthcheck(deadline=deadline)

    assert attempts == 2
    assert clock.sleeps == [2.0]


def test_non_transient_response_is_not_retried() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, text="missing")

    client, deadline = build_client(handler, clock=clock)
    with client, pytest.raises(seed_e2e.E2ESeedError, match="unexpected status 404"):
        client.healthcheck(deadline=deadline)

    assert attempts == 1
    assert clock.sleeps == []


def test_unsafe_post_transient_response_is_not_retried() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="write status is ambiguous")

    client, deadline = build_client(handler, clock=clock)
    with client:
        response = client._request("POST", "/unsafe-write", deadline=deadline)

    assert response.status_code == 503
    assert attempts == 1
    assert clock.sleeps == []


def test_unsafe_post_transport_ambiguity_is_never_retried() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("response was ambiguous", request=request)

    client, deadline = build_client(handler, clock=clock)
    with client, pytest.raises(seed_e2e.E2ESeedError, match="not retry-safe"):
        client._request("POST", "/unsafe-write", deadline=deadline)

    assert attempts == 1
    assert clock.sleeps == []


def test_durably_idempotent_post_can_retry_ambiguous_transport_failure() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("response was ambiguous", request=request)
        return httpx.Response(202, json={"accepted": True})

    client, deadline = build_client(handler, clock=clock)
    with client:
        response = client._request(
            "POST",
            "/durably-idempotent-write",
            deadline=deadline,
            retry_safe=True,
        )

    assert response.status_code == 202
    assert attempts == 2
    assert clock.sleeps == [seed_e2e.HTTP_RETRY_INITIAL_BACKOFF_SECONDS]


def test_retry_chain_exhaustion_uses_caller_phase_deadline() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="still unavailable")

    client, deadline = build_client(handler, clock=clock, timeout_seconds=0.6)
    with client, pytest.raises(seed_e2e.E2ESeedError, match="overall HTTP deadline"):
        client.healthcheck(deadline=deadline)

    assert attempts == 2
    assert clock.sleeps == pytest.approx([0.25, 0.35])
    assert clock.now == pytest.approx(100.6)


def test_public_search_poller_survives_transport_error_without_real_sleep() -> None:
    clock = FakeClock()
    meme_id = uuid.uuid4()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("warming up", request=request)
        if attempts == 2:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [{"meme": {"id": str(meme_id)}}]})

    client, deadline = build_client(handler, clock=clock)
    with client:
        payload = seed_e2e.wait_for_public_search_contains(
            client,
            query="cat",
            meme_id=meme_id,
            deadline=deadline,
        )

    assert payload == {"items": [{"meme": {"id": str(meme_id)}}]}
    assert attempts == 3
    assert clock.sleeps == [seed_e2e.HTTP_RETRY_INITIAL_BACKOFF_SECONDS, seed_e2e.POLL_INTERVAL_SECONDS]


def test_sequential_phases_each_receive_a_fresh_configured_budget() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts in {1, 3}:
            return httpx.Response(503, headers={"Retry-After": "0.8"}, text="warming")
        return httpx.Response(200, json={"status": "ok"})

    client, first_phase = build_client(handler, clock=clock, timeout_seconds=1.0)
    with client:
        client.healthcheck(deadline=first_phase)
        second_phase = seed_e2e.MonotonicDeadline.after(
            1.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        client.healthcheck(deadline=second_phase)

    assert attempts == 4
    assert clock.sleeps == [0.8, 0.8]
    assert first_phase.expires_at == pytest.approx(101.0)
    assert second_phase.expires_at == pytest.approx(101.8)
    assert clock.now == pytest.approx(101.6)


def test_poller_request_timeout_never_exceeds_remaining_phase_budget() -> None:
    clock = FakeClock()
    meme_id = uuid.uuid4()
    request_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = cast("dict[str, float]", request.extensions["timeout"])
        request_timeouts.append(timeout["read"])
        if len(request_timeouts) == 1:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [{"meme": {"id": str(meme_id)}}]})

    client, deadline = build_client(handler, clock=clock, timeout_seconds=1.5)
    with client:
        payload = seed_e2e.wait_for_public_search_contains(
            client,
            query="cat",
            meme_id=meme_id,
            deadline=deadline,
        )

    assert payload == {"items": [{"meme": {"id": str(meme_id)}}]}
    assert request_timeouts == pytest.approx([1.5, 0.5])
