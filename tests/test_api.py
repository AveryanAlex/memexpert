"""Tests for the FastAPI application factory and routes."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memexpert.api.app import create_app


def test_create_app_returns_fastapi_instance_with_expected_metadata() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "MemeXpert API"
    assert app.version == "0.1.0"


def test_health_endpoint_returns_ok_payload() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_v1_namespace_root_and_openapi_spec_are_available() -> None:
    with TestClient(create_app()) as client:
        namespace_response = client.get("/api/v1/")
        openapi_response = client.get("/openapi.json")

    paths = openapi_response.json()["paths"]
    expected_auth_paths = {
        "/api/v1/auth/guest": "post",
        "/api/v1/auth/email/signup": "post",
        "/api/v1/auth/email/login": "post",
        "/api/v1/auth/link/email/signup": "post",
        "/api/v1/auth/link/email/login": "post",
        "/api/v1/auth/link/google": "post",
        "/api/v1/auth/link/telegram": "post",
        "/api/v1/auth/linked-providers": "get",
        "/api/v1/auth/telegram": "post",
        "/api/v1/auth/telegram-miniapp": "post",
        "/api/v1/auth/google": "post",
        "/api/v1/auth/refresh": "post",
        "/api/v1/auth/me": "get",
    }

    assert namespace_response.status_code == 200
    assert namespace_response.json() == {"version": "v1", "status": "available"}
    assert openapi_response.status_code == 200
    assert "/api/v1/" in paths
    for path, method in expected_auth_paths.items():
        assert path in paths
        assert method in paths[path]
