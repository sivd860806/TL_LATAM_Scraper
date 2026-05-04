"""Smoke tests basicos del app (health + correlation tracing).

Tests del endpoint /scrape viven en test_schemas.py::TestEndpointShape.
"""
from __future__ import annotations


def test_health_endpoint(client):
    """GET /health debe responder 200 con status=ok y version."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "llm_configured" in body


def test_correlation_id_header(client):
    """Cada response debe incluir X-Correlation-ID en headers."""
    response = client.get("/health")
    assert "X-Correlation-ID" in response.headers
    assert len(response.headers["X-Correlation-ID"]) == 12


def test_correlation_id_unique_per_request(client):
    """Cada request debe tener correlation_id distinto."""
    r1 = client.get("/health")
    r2 = client.get("/health")
    cid_1 = r1.headers["X-Correlation-ID"]
    cid_2 = r2.headers["X-Correlation-ID"]
    assert cid_1 != cid_2
