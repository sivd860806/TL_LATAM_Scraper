"""Tests de los schemas Pydantic (P2.1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.catalog import (
    CANONICAL_BRANDS,
    lookup_brand,
    normalize_brand_key,
)
from app.schemas.error import ErrorCode, ErrorDetail, ScraperError, Stage
from app.schemas.request import ScrapeOptions, ScrapeRequest
from app.schemas.response import (
    Installments,
    PaymentMethod,
    PriceInfo,
    ResponseMetadata,
    ScrapeResponseSuccess,
    TokenUsage,
)

# -----------------------------------------------------------------------------
# Request validation
# -----------------------------------------------------------------------------
class TestScrapeRequest:
    def test_minimal_valid_request(self):
        req = ScrapeRequest(url="https://articulo.mercadolibre.com.ar/MLA-1234")
        assert str(req.url).startswith("https://")
        assert req.country is None
        assert req.options.timeout_seconds == 60
        assert req.options.force_agents is False

    def test_full_request_with_options(self):
        req = ScrapeRequest.model_validate({
            "url": "https://www.falabella.com/falabella-cl/product/123",
            "country": "CL",
            "options": {
                "extract_title": True,
                "extract_price": True,
                "timeout_seconds": 90,
                "force_agents": True,
            },
        })
        assert req.country == "CL"
        assert req.options.timeout_seconds == 90
        assert req.options.force_agents is True

    def test_invalid_url_rejected(self):
        with pytest.raises(ValidationError):
            ScrapeRequest(url="not-a-url")

    def test_invalid_country_pattern_rejected(self):
        with pytest.raises(ValidationError):
            ScrapeRequest(url="https://x.com", country="argentina")

    def test_country_lowercase_rejected(self):
        with pytest.raises(ValidationError):
            ScrapeRequest(url="https://x.com", country="ar")

    def test_timeout_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            ScrapeRequest(url="https://x.com",
                          options={"timeout_seconds": 500})


# -----------------------------------------------------------------------------
# Response shape
# -----------------------------------------------------------------------------
class TestScrapeResponseSuccess:
    def test_minimal_response(self):
        resp = ScrapeResponseSuccess(
            source_url="https://articulo.mercadolibre.com.ar/MLA-1234",
            site="mercadolibre",
            payment_methods=[
                PaymentMethod(type="credit_card", brand="Visa"),
            ],
            metadata=ResponseMetadata(duration_ms=200, agent_steps=1, llm_calls=0),
        )
        assert resp.status == "ok"
        assert len(resp.payment_methods) == 1

    def test_response_with_installments(self):
        pm = PaymentMethod(
            type="credit_card",
            brand="Visa",
            installments=Installments(max=36, interest_free_max=12),
        )
        assert pm.installments.max == 36
        assert pm.installments.interest_free_max == 12

    def test_response_with_product_info(self):
        resp = ScrapeResponseSuccess(
            source_url="https://x.com/p/1",
            site="mercadolibre",
            product={
                "title": "Apple iPhone 15 128GB",
                "price": {"amount": 999.99, "currency": "USD"},
            },
            payment_methods=[PaymentMethod(type="wallet", brand="Mercado Pago")],
            metadata=ResponseMetadata(duration_ms=100),
        )
        assert resp.product.title.startswith("Apple")
        assert resp.product.price.currency == "USD"

    def test_payment_methods_min_length(self):
        with pytest.raises(ValidationError):
            ScrapeResponseSuccess(
                source_url="https://x.com",
                site="mercadolibre",
                payment_methods=[],  # vacio rechazado
                metadata=ResponseMetadata(duration_ms=100),
            )

    def test_currency_must_be_uppercase_3_letter(self):
        with pytest.raises(ValidationError):
            PriceInfo(amount=100.0, currency="ars")  # lowercase rechazado

    def test_token_usage_total(self):
        usage = TokenUsage(input=4200, output=380)
        assert usage.total == 4580


# -----------------------------------------------------------------------------
# Error taxonomy
# -----------------------------------------------------------------------------
class TestErrorTaxonomy:
    def test_all_codes_have_unique_values(self):
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values)), "ErrorCode values must be unique"

    def test_error_detail_serializes(self):
        detail = ErrorDetail(
            code=ErrorCode.ANTI_BOT_DETECTED,
            message="Cloudflare challenge",
            stage=Stage.NAVIGATOR,
        )
        d = detail.model_dump(mode="json")
        assert d["code"] == "ANTI_BOT_DETECTED"
        assert d["stage"] == "navigator"

    def test_scraper_error_carries_detail(self):
        err = ScraperError(
            code=ErrorCode.PARSE_ERROR,
            message="LLM returned malformed JSON after 3 retries",
            stage=Stage.EXTRACTOR,
        )
        detail = err.to_detail()
        assert detail.code == ErrorCode.PARSE_ERROR
        assert detail.stage == Stage.EXTRACTOR
        assert "malformed" in detail.message


# -----------------------------------------------------------------------------
# Catalog (normalizacion de marcas)
# -----------------------------------------------------------------------------
class TestCatalog:
    def test_normalize_strips_accents_and_lowercases(self):
        assert normalize_brand_key("Mércadó Págo") == "mercado pago"

    def test_normalize_collapses_spaces(self):
        assert normalize_brand_key("  visa   credito  ") == "visa credito"

    def test_normalize_strips_common_prefixes(self):
        assert normalize_brand_key("Tarjeta de credito Visa") == "visa"

    def test_lookup_canonical_visa(self):
        assert lookup_brand("Visa") == "Visa"
        assert lookup_brand("VISA CREDITO") == "Visa"
        assert lookup_brand("Tarjeta Visa") == "Visa"

    def test_lookup_mercadopago_aliases(self):
        assert lookup_brand("Mercado Pago") == "Mercado Pago"
        assert lookup_brand("mercadopago") == "Mercado Pago"
        assert lookup_brand("MP") == "Mercado Pago"

    def test_lookup_unknown_returns_none(self):
        assert lookup_brand("KryptoCoinBank") is None

    def test_catalog_covers_main_latam_methods(self):
        """Smoke test: las marcas que aparecen en el ejemplo del PDF deben estar."""
        for raw in ["Visa", "Mastercard", "Visa Debit", "Mercado Pago", "PSE", "Efecty"]:
            assert lookup_brand(raw) is not None, f"Brand {raw} missing from catalog"


# -----------------------------------------------------------------------------
# Endpoint integration (con TestClient)
# -----------------------------------------------------------------------------
class TestEndpointShape:
    def test_health_returns_expected_shape(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"status", "version", "llm_configured", "model"}
        assert body["status"] == "ok"

    def test_scrape_with_invalid_url_returns_422(self, client):
        r = client.post("/scrape", json={"url": "not-a-url"})
        assert r.status_code == 422
        assert r.json()["status"] == "error"
        assert r.json()["error"]["code"] == "INVALID_URL"

    def test_scrape_with_valid_url_returns_dispatcher_error_in_p21(self, client):
        """En P2.1 todavia no hay dispatcher real; devolvemos UNSUPPORTED_SITE."""
        r = client.post("/scrape", json={
            "url": "https://articulo.mercadolibre.com.ar/MLA-1234",
        })
        assert r.status_code == 400
        body = r.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "UNSUPPORTED_SITE"
        assert body["error"]["stage"] == "dispatcher"
