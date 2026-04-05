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

    assert namespace_response.status_code == 200
    assert namespace_response.json() == {"version": "v1", "status": "available"}
    assert openapi_response.status_code == 200
    assert "/api/v1/" in openapi_response.json()["paths"]
