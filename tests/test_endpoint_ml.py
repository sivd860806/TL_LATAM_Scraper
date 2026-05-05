"""Test E2E del endpoint /scrape con el ML adapter mockeado (P2.2)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Capturar la referencia original ANTES del patch para no caer en recursion
_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _make_mock_client_factory():
    item_payload = json.loads((FIXTURES / "ml_item.json").read_text())
    methods_payload = json.loads(
        (FIXTURES / "ml_payment_methods_mla.json").read_text()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/items/") and "/payment_methods" not in path:
            return httpx.Response(200, json=item_payload)
        if "/sites/" in path and path.endswith("/payment_methods"):
            return httpx.Response(200, json=methods_payload)
        return httpx.Response(404)

    def factory(*_args, **_kwargs):
        # Usar el AsyncClient ORIGINAL (no el patcheado) para evitar recursion
        return _ORIGINAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return factory


def test_scrape_ml_url_returns_payment_methods(client):
    """URL valida de ML con API mockeada -> 200 OK con payment_methods normalizados."""
    factory = _make_mock_client_factory()
    with patch("app.adapters.mercadolibre.httpx.AsyncClient", factory):
        response = client.post("/scrape", json={
            "url": "https://articulo.mercadolibre.com.ar/MLA-1234567890",
        })

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "ok"
    assert body["site"] == "mercadolibre"
    assert "payment_methods" in body
    assert len(body["payment_methods"]) > 0

    assert body["metadata"]["llm_calls"] == 0
    assert body["metadata"]["agent_steps"] >= 1
    assert body["metadata"]["duration_ms"] >= 0

    assert body["product"]["title"].startswith("Apple iPhone")
    assert body["product"]["price"]["currency"] == "ARS"

    brands = {pm["brand"] for pm in body["payment_methods"]}
    assert "Visa" in brands
    assert "Mastercard" in brands

    types = {pm["type"] for pm in body["payment_methods"]}
    assert "credit_card" in types
    assert "wallet" in types
    assert "cash" in types


def test_scrape_unsupported_site_returns_400(client):
    response = client.post("/scrape", json={
        "url": "https://www.linio.com.co/p/algun-producto-12345",
    })
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_SITE"
    assert body["error"]["stage"] == "dispatcher"


def test_scrape_falabella_returns_pending_p23(client):
    response = client.post("/scrape", json={
        "url": "https://www.falabella.com/falabella-cl/product/12345",
    })
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_SITE"
    assert body["error"]["stage"] == "adapter"
    assert "P2.3" in body["error"]["message"]
