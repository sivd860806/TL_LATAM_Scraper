"""Tests del MercadoLibreAdapter (P2.2) con graceful degrade."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.adapters.mercadolibre import (
    MercadoLibreAdapter,
    extract_id_from_url,
    extract_item_id,
    extract_site_id_from_item,
)
from app.schemas.error import ErrorCode, ScraperError

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# URL parsing
# -----------------------------------------------------------------------------
class TestExtractIdFromUrl:
    @pytest.mark.parametrize("url, expected", [
        ("https://articulo.mercadolibre.com.ar/MLA-1234567890-titulo-prod",
         ("item", "MLA-1234567890")),
        ("https://articulo.mercadolibre.com.ar/MLA-1234567890",
         ("item", "MLA-1234567890")),
        ("https://www.mercadolibre.com.co/articulo/MCO-9876543210",
         ("item", "MCO-9876543210")),
        ("https://articulo.mercadolibre.com.mx/MLM-555-titulo?vista=detail",
         ("item", "MLM-555")),
        ("https://www.mercadolibre.com.ar/p/MLA1234567890",
         ("product", "MLA1234567890")),
        ("https://www.mercadolibre.com.ar/apple-iphone-15-128-gb-negro/p/MLA1027172677",
         ("product", "MLA1027172677")),
        ("https://www.mercadolibre.com.ar/algo/p/MCO9876543210#polycard_client=x",
         ("product", "MCO9876543210")),
    ])
    def test_recognized_formats(self, url, expected):
        assert extract_id_from_url(url) == expected

    def test_no_match_returns_none(self):
        assert extract_id_from_url("https://www.mercadolibre.com.ar/categoria/cell") is None
        assert extract_id_from_url("https://example.com") is None


class TestExtractItemId:
    @pytest.mark.parametrize("url, expected", [
        ("https://articulo.mercadolibre.com.ar/MLA-1234567890", "MLA-1234567890"),
        ("https://www.mercadolibre.com.co/articulo/MCO-9876543210", "MCO-9876543210"),
    ])
    def test_item_urls(self, url, expected):
        assert extract_item_id(url) == expected

    def test_product_url_returns_none(self):
        assert extract_item_id("https://www.mercadolibre.com.ar/p/MLA1234567890") is None

    def test_extract_site_id(self):
        assert extract_site_id_from_item("MLA-1234567890") == "MLA"
        assert extract_site_id_from_item("MCO-555") == "MCO"
        assert extract_site_id_from_item("MLA1234567890") == "MLA"


# -----------------------------------------------------------------------------
# Mock helper
# -----------------------------------------------------------------------------
def _make_mock_client(*, item_payload=None, methods_payload=None,
                      item_status=200, methods_status=200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/items/"):
            if item_payload is None:
                return httpx.Response(404)
            return httpx.Response(item_status,
                json=item_payload if item_status == 200 else {"error": "x"})
        if "/sites/" in path and path.endswith("/payment_methods"):
            return httpx.Response(methods_status,
                json=methods_payload if methods_status == 200 else {"error": "x"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
class TestMercadoLibreAdapter:
    async def test_item_url_happy_path(self):
        item = _load("ml_item.json")
        methods = _load("ml_payment_methods_mla.json")
        async with _make_mock_client(item_payload=item, methods_payload=methods) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            result = await adapter.fetch(
                "https://articulo.mercadolibre.com.ar/MLA-1234567890")

        assert result.mode == "direct"
        assert result.llm_calls_used == 0
        assert result.network_calls == 2  # payment_methods + item
        assert result.product.title == "Apple iPhone 15 128GB Negro Original Sellado"
        assert result.product.price.amount == 4999000
        assert result.product.price.currency == "ARS"
        types = {pm.type for pm in result.payment_methods}
        assert {"credit_card", "wallet", "cash"} <= types

    async def test_catalog_url_skips_item_returns_methods_only(self):
        """Para /p/, no intentamos resolver al item. Devuelve methods + product=None."""
        methods = _load("ml_payment_methods_mla.json")
        async with _make_mock_client(methods_payload=methods) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            result = await adapter.fetch(
                "https://www.mercadolibre.com.ar/x/p/MLA1027172677#polycard_client=x")
        assert result.product is None
        assert result.network_calls == 1  # solo payment_methods
        assert len(result.payment_methods) > 0

    async def test_item_blocked_falls_back_to_methods_only(self):
        """Si /items/{id} devuelve 403, NO falla todo. Devuelve methods con product=None."""
        methods = _load("ml_payment_methods_mla.json")
        async with _make_mock_client(
            item_payload={}, item_status=403,
            methods_payload=methods,
        ) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            result = await adapter.fetch("https://articulo.mercadolibre.com.ar/MLA-1234567890")
        # Graceful degrade: product=None, pero seguimos teniendo methods
        assert result.product is None
        assert len(result.payment_methods) > 0

    async def test_payment_methods_blocked_raises_anti_bot(self):
        """Si /payment_methods falla, no podemos devolver nada -> error."""
        async with _make_mock_client(
            methods_payload={}, methods_status=403,
        ) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            with pytest.raises(ScraperError) as exc_info:
                await adapter.fetch("https://articulo.mercadolibre.com.ar/MLA-1234567890")
        assert exc_info.value.code == ErrorCode.ANTI_BOT_DETECTED

    async def test_invalid_url_raises_invalid_url(self):
        adapter = MercadoLibreAdapter()
        with pytest.raises(ScraperError) as exc_info:
            await adapter.fetch("https://www.mercadolibre.com.ar/categoria/no-id")
        assert exc_info.value.code == ErrorCode.INVALID_URL

    async def test_no_active_methods_raises_parse_error(self):
        methods = [
            {"id": "x", "name": "Y", "payment_type_id": "credit_card", "status": "deprecated"},
        ]
        async with _make_mock_client(methods_payload=methods) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            with pytest.raises(ScraperError) as exc_info:
                await adapter.fetch("https://articulo.mercadolibre.com.ar/MLA-1234567890")
        assert exc_info.value.code == ErrorCode.PARSE_ERROR

    async def test_dedupes_methods(self):
        methods = [
            {"id": "v1", "name": "Visa", "payment_type_id": "credit_card", "status": "active"},
            {"id": "v2", "name": "VISA", "payment_type_id": "credit_card", "status": "active"},
            {"id": "v3", "name": "Tarjeta Visa", "payment_type_id": "credit_card", "status": "active"},
        ]
        async with _make_mock_client(methods_payload=methods) as client:
            adapter = MercadoLibreAdapter(http_client=client)
            result = await adapter.fetch("https://www.mercadolibre.com.ar/x/p/MLA1234")
        visa = sum(1 for pm in result.payment_methods
                   if pm.brand == "Visa" and pm.type == "credit_card")
        assert visa == 1
