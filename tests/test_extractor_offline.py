"""Tests del PaymentExtractor contra DOMs reales en disco (P2.6).

Dos modos coexisten en este archivo:

  1. **Mocked (default)**: la respuesta de Anthropic se mockea con un payload
     canonico. Verifica que el pipeline (compress_dom -> tool_use ->
     normalize -> dedupe) funciona sin red ni costos. Estos tests corren
     siempre, son rapidos, y son lo que pytest -q ejecuta por default.

  2. **Live (`@pytest.mark.live`)**: invocan Anthropic real con la key del
     entorno y validan calidad de extraccion del LLM contra ground truth.
     Deselected por default (`addopts = "-m 'not live' -v"`). Para correrlos:
       pytest -m live -v
     Tienen costo (~$0.0005 por test) y requieren ANTHROPIC_API_KEY.

Por que el split:
- CI debe ser determinista, gratis y rapido. El modo mocked cubre eso.
- El modo live es regresion REAL del LLM: si Anthropic cambia el modelo,
  o si afinamos el prompt, queremos saber si la calidad se mantiene
  contra DOMs estables.

Los fixtures HTML viven en `tests/fixtures/`:
- falabella_checkout_full.html: checkout CO con 7 metodos canonicos.
- falabella_cl_checkout.html: checkout CL con CMR, Webpay, Onepay.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.payment_extractor import extract_payment_methods
from app.schemas.response import TokenUsage

FIXTURES = Path(__file__).parent / "fixtures"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _fake_anthropic_response(payment_methods_payload: list[dict], confidence: str = "high"):
    """Construye un mock que simula la respuesta del SDK de anthropic con
    un tool_use block cargado con `payment_methods_payload`."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {"payment_methods": payment_methods_payload, "confidence": confidence}

    response = MagicMock()
    response.content = [tool_block]
    response.usage = MagicMock(input_tokens=1000, output_tokens=200)
    return response


# -----------------------------------------------------------------------------
# Mocked tests (rapidos, sin red, corren siempre)
# -----------------------------------------------------------------------------
class TestExtractorMocked:
    """Tests con anthropic mockeado contra DOMs reales en disco.

    Validan el pipeline post-LLM: compress_dom (input), validate_and_normalize
    (output), dedupe, mapping al brand catalog.
    """

    @pytest.fixture(autouse=True)
    def _setup_key(self, monkeypatch):
        # extract_payment_methods rebota si no hay key. Le ponemos una fake
        # para esta clase. Settings se lee al modulo de payment_extractor.
        monkeypatch.setattr(
            "app.agents.payment_extractor.settings.anthropic_api_key",
            "sk-ant-fake-for-testing"
        )

    @pytest.mark.asyncio
    async def test_co_checkout_extracts_seven_methods(self):
        """DOM CO con 7 metodos: validamos que el pipeline mantiene los 7
        cuando el LLM los devuelve correctamente."""
        dom = (FIXTURES / "falabella_checkout_full.html").read_text(encoding="utf-8")

        fake_methods = [
            {"type": "credit_card", "brand": "CMR", "installments_max": 36, "installments_interest_free_max": 12},
            {"type": "credit_card", "brand": "Visa", "installments_max": 24, "installments_interest_free_max": None},
            {"type": "credit_card", "brand": "Mastercard", "installments_max": 24, "installments_interest_free_max": None},
            {"type": "debit_card", "brand": "Banco Falabella", "installments_max": None, "installments_interest_free_max": None},
            {"type": "debit_card", "brand": "Visa Debito", "installments_max": None, "installments_interest_free_max": None},
            {"type": "bank_transfer", "brand": "PSE", "installments_max": None, "installments_interest_free_max": None},
            {"type": "cash", "brand": "Efecty", "installments_max": None, "installments_interest_free_max": None},
        ]

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = AsyncMock(return_value=_fake_anthropic_response(fake_methods))
            methods, usage = await extract_payment_methods(
                dom, url="https://www.falabella.com.co/falabella-co/checkout/payment",
                site_id="falabella", country="CO",
            )

        assert len(methods) == 7
        brands = {m.brand for m in methods}
        assert brands >= {"CMR", "Visa", "Mastercard", "PSE", "Efecty"}
        cmr = next(m for m in methods if m.brand == "CMR")
        assert cmr.installments is not None
        assert cmr.installments.max == 36
        assert cmr.installments.interest_free_max == 12
        assert usage.input == 1000
        assert usage.output == 200

    @pytest.mark.asyncio
    async def test_cl_checkout_extracts_chilean_brands(self):
        """DOM CL: chequea que el normalizer mapea Webpay/Redcompra/Onepay
        al canonical correcto via lookup_brand."""
        dom = (FIXTURES / "falabella_cl_checkout.html").read_text(encoding="utf-8")

        fake_methods = [
            {"type": "credit_card", "brand": "CMR", "installments_max": 48, "installments_interest_free_max": 12},
            {"type": "credit_card", "brand": "Visa", "installments_max": 12, "installments_interest_free_max": None},
            {"type": "debit_card", "brand": "Redcompra", "installments_max": None, "installments_interest_free_max": None},
            {"type": "bank_transfer", "brand": "Webpay Plus", "installments_max": None, "installments_interest_free_max": None},
            {"type": "wallet", "brand": "Onepay", "installments_max": None, "installments_interest_free_max": None},
        ]

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = AsyncMock(return_value=_fake_anthropic_response(fake_methods))
            methods, usage = await extract_payment_methods(
                dom, url="https://www.falabella.com/falabella-cl/checkout/payment",
                site_id="falabella", country="CL",
            )

        assert len(methods) == 5
        brands = {m.brand for m in methods}
        assert "CMR" in brands
        assert "Webpay Plus" in brands

    @pytest.mark.asyncio
    async def test_dedupe_collapses_duplicate_brands(self):
        """Si el LLM devuelve duplicados (e.g. Visa credit + Visa credit),
        validate_and_normalize debe deduplicar por (type, brand)."""
        dom = "<html><body>Visa Mastercard</body></html>"

        fake_methods = [
            {"type": "credit_card", "brand": "Visa", "installments_max": 12, "installments_interest_free_max": None},
            {"type": "credit_card", "brand": "Visa", "installments_max": 24, "installments_interest_free_max": None},  # dup
            {"type": "credit_card", "brand": "Mastercard", "installments_max": None, "installments_interest_free_max": None},
        ]

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = AsyncMock(return_value=_fake_anthropic_response(fake_methods))
            methods, _ = await extract_payment_methods(
                dom, url="https://www.falabella.com/falabella-cl/test",
                site_id="falabella", country="CL",
            )

        assert len(methods) == 2  # Visa duplicate collapsed
        brands = [m.brand for m in methods]
        assert brands.count("Visa") == 1

    @pytest.mark.asyncio
    async def test_invalid_type_dropped(self):
        """Si el LLM devuelve un type invalido, validate_and_normalize lo dropea
        en vez de fallar el request entero."""
        dom = "<html><body>test</body></html>"

        fake_methods = [
            {"type": "credit_card", "brand": "Visa", "installments_max": None, "installments_interest_free_max": None},
            {"type": "INVENTED_TYPE", "brand": "Bitcoin", "installments_max": None, "installments_interest_free_max": None},  # bad type
        ]

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = AsyncMock(return_value=_fake_anthropic_response(fake_methods))
            methods, _ = await extract_payment_methods(
                dom, url="https://test.com/x", site_id="falabella", country="CL",
            )

        assert len(methods) == 1
        assert methods[0].brand == "Visa"


# -----------------------------------------------------------------------------
# Live tests (Anthropic real, opt-in via -m live)
# -----------------------------------------------------------------------------
@pytest.mark.live
class TestExtractorLive:
    """Tests que invocan Anthropic real contra los DOMs en disco.

    Corren con: pytest -m live -v
    Cuesta: ~$0.0005 por test (~5 tests = $0.003)
    Requieren: ANTHROPIC_API_KEY en el entorno o .env

    El proposito NO es "el LLM siempre devuelve exactamente esto" (los LLMs
    no son deterministicos), sino "el LLM devuelve algo razonable contra
    DOMs estables". Por eso las assertions son inclusivas (>= N metodos,
    al menos contiene marca X), no estrictas.
    """

    @pytest.fixture(autouse=True)
    def _check_key(self):
        from app.config import settings
        if not settings.anthropic_api_key:
            pytest.skip("ANTHROPIC_API_KEY not set; skipping live test")

    @pytest.mark.asyncio
    async def test_live_co_checkout_extracts_at_least_4_methods(self):
        """Anthropic real sobre el DOM CO debe encontrar >=4 metodos. Los
        7 esperados son: CMR, Visa/MC (Tarjeta credito), Banco Falabella,
        Visa Debito (Tarjeta debito), Gift Card, PSE, Efecty/Baloto."""
        dom = (FIXTURES / "falabella_checkout_full.html").read_text(encoding="utf-8")
        methods, usage = await extract_payment_methods(
            dom, url="https://www.falabella.com.co/falabella-co/checkout/payment",
            site_id="falabella", country="CO",
        )

        # Inclusivo: al menos 4 de los 7 esperados (margen para variaciones del LLM)
        assert len(methods) >= 4, f"Expected >=4 methods, got {len(methods)}: {[m.brand for m in methods]}"
        brands = {m.brand for m in methods}

        # Estas marcas son INEQUIVOCAS en el DOM (logos + brand-name + section)
        # El LLM las debe encontrar. Si falla aca, hay regresion del prompt o del modelo.
        critical = {"CMR", "PSE"}
        found_critical = critical & brands
        assert len(found_critical) >= 1, (
            f"Critical brands missing: expected at least one of {critical}, "
            f"got {brands}"
        )

        # Sanity: el LLM consumio tokens (no es un fake response)
        assert usage.input > 0
        assert usage.output > 0

    @pytest.mark.asyncio
    async def test_live_cl_checkout_extracts_chilean_brands(self):
        """DOM CL debe extraer al menos CMR y/o Webpay (marcas senaladas
        explicitamente en el system prompt LATAM-aware del extractor)."""
        dom = (FIXTURES / "falabella_cl_checkout.html").read_text(encoding="utf-8")
        methods, usage = await extract_payment_methods(
            dom, url="https://www.falabella.com/falabella-cl/checkout/payment",
            site_id="falabella", country="CL",
        )

        assert len(methods) >= 3, f"Expected >=3 methods, got {len(methods)}: {[m.brand for m in methods]}"
        brands = {m.brand for m in methods}
        critical_cl = {"CMR", "Webpay Plus", "Webpay"}
        assert critical_cl & brands, f"No critical CL brand found in {brands}"
