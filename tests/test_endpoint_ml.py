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


def test_scrape_falabella_with_mocked_agents_returns_200(client):
    """P2.4 end-to-end: mockeamos el adapter (Playwright) Y los agentes LLM,
    verificamos que el flujo completo arme un response 200 OK con metodos."""
    from unittest.mock import patch, AsyncMock
    from app.adapters.base import AdapterResult
    from app.schemas.response import ProductInfo, PriceInfo, PaymentMethod, TokenUsage

    fake_adapter_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=ProductInfo(
            title="Smartwatch Test",
            price=PriceInfo(amount=99990.0, currency="CLP"),
        ),
        payment_methods=[],
        initial_dom="<html><body>" + "x" * 5000 + "</body></html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )
    fake_methods = [
        PaymentMethod(type="credit_card", brand="Visa"),
        PaymentMethod(type="credit_card", brand="Mastercard"),
        PaymentMethod(type="bank_transfer", brand="Webpay Plus"),
    ]
    fake_usage = TokenUsage(input=4200, output=380)

    with patch(
        "app.main.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_adapter_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(return_value=(fake_methods, fake_usage)),
    ):
        response = client.post("/scrape", json={
            "url": "https://www.falabella.com/falabella-cl/product/12345",
        })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["site"] == "falabella"
    assert len(body["payment_methods"]) == 3
    brands = {pm["brand"] for pm in body["payment_methods"]}
    assert {"Visa", "Mastercard", "Webpay Plus"} <= brands
    assert body["metadata"]["llm_calls"] == 1  # solo PaymentExtractor invocado (product completo)
    assert body["product"]["title"] == "Smartwatch Test"
    assert body["product"]["price"]["currency"] == "CLP"


def test_scrape_falabella_extractor_error_returns_502(client):
    """Si el PaymentExtractor falla (no key, API error), response es estructurado."""
    from unittest.mock import patch, AsyncMock
    from app.adapters.base import AdapterResult
    from app.schemas.response import ProductInfo, PriceInfo
    from app.schemas.error import ErrorCode, ScraperError, Stage

    fake_adapter_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=ProductInfo(title="X", price=PriceInfo(amount=1.0, currency="CLP")),
        payment_methods=[],
        initial_dom="<html></html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )

    extractor_error = ScraperError(
        code=ErrorCode.LLM_BUDGET_EXCEEDED,
        message="API key not configured",
        stage=Stage.EXTRACTOR,
    )

    with patch(
        "app.main.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_adapter_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(side_effect=extractor_error),
    ):
        response = client.post("/scrape", json={
            "url": "https://www.falabella.com/falabella-cl/product/12345",
        })

    assert response.status_code == 429  # LLM_BUDGET_EXCEEDED
    body = response.json()
    assert body["error"]["code"] == "LLM_BUDGET_EXCEEDED"
    assert body["error"]["stage"] == "extractor"


def test_scrape_falabella_returns_structured_error(client):
    """Test ambiente-agnostico: cualquiera sea la falla del adapter Falabella
    (sin Playwright, sin shared libs, sin red), el response debe ser estructurado
    con stage='adapter' y un error code conocido (no un 500 sin contexto).
    """
    response = client.post("/scrape", json={
        "url": "https://www.falabella.com/falabella-cl/product/12345",
    })
    # Codigos posibles segun el ambiente:
    # - 500 INTERNAL_ERROR: Playwright no instalado, o falta libnspr4
    # - 504 TIMEOUT: red lenta o sitio inaccesible
    # - 502 PARSE_ERROR: P2.4 no implementado, DOM si capturado
    # - 502 ANTI_BOT_DETECTED: Cloudflare challenge
    # En CUALQUIER escenario, queremos un response estructurado.
    assert response.status_code in {404, 500, 502, 504}
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["stage"] == "adapter"
    assert body["error"]["code"] in {
        "INTERNAL_ERROR",
        "TIMEOUT",
        "PARSE_ERROR",
        "ANTI_BOT_DETECTED",
        "CHECKOUT_UNREACHABLE",
        "OUT_OF_STOCK",  # Playwright navego OK pero el URL no existe (caso ficticio)
    }
    # Si es INTERNAL_ERROR, el mensaje debe ser accionable (mencionar Playwright)
    if body["error"]["code"] == "INTERNAL_ERROR":
        assert "Playwright" in body["error"]["message"] or "Chromium" in body["error"]["message"]
