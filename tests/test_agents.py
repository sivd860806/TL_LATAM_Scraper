"""Tests de los agentes LLM (P2.4) con anthropic mockeado.

No requieren ANTHROPIC_API_KEY real ni anthropic SDK instalado: usamos
unittest.mock para simular el cliente y verificar el flujo + parsing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip si anthropic no esta instalado (no podemos importar el agent)
try:
    import anthropic  # noqa: F401
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

requires_anthropic = pytest.mark.skipif(
    not HAS_ANTHROPIC,
    reason="anthropic SDK not installed",
)


def _make_fake_response(tool_input: dict, input_tokens: int = 1500, output_tokens: int = 200):
    """Construye un response de Anthropic Messages API con un tool_use block."""
    tool_block = SimpleNamespace(type="tool_use", input=tool_input)
    return SimpleNamespace(
        content=[tool_block],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# -----------------------------------------------------------------------------
# DOM compressor (puro, no requiere anthropic)
# -----------------------------------------------------------------------------
class TestDomCompressor:
    def test_strips_scripts_and_styles(self):
        from app.agents.dom_utils import compress_dom
        html = '<html><script>var x=1;</script><style>.a{}</style><body><h1>Hola</h1></body></html>'
        out = compress_dom(html)
        assert "<script" not in out.lower()
        assert "<style" not in out.lower()
        assert "Hola" in out

    def test_strips_svg_and_iframes(self):
        from app.agents.dom_utils import compress_dom
        html = '<body><svg><path d="M0 0"/></svg><iframe src="x"></iframe><p>Texto</p></body>'
        out = compress_dom(html)
        assert "<svg" not in out.lower()
        assert "<iframe" not in out.lower()
        assert "Texto" in out

    def test_strips_unimportant_attrs(self):
        from app.agents.dom_utils import compress_dom
        html = '<div class="really-long-class-name" style="color:red" data-foo="bar" id="kept">A</div>'
        out = compress_dom(html)
        assert 'class=' not in out  # filtered out
        assert 'style=' not in out
        assert 'id="kept"' in out

    def test_truncates_to_max_chars_keeping_payment_keywords(self):
        from app.agents.dom_utils import compress_dom
        # Generate a long HTML with payment keywords in the middle
        prefix = "<body>" + ("<div>filler</div>" * 1000)
        middle = "<div>Pago con Visa, Mastercard, PSE, Efecty</div>"
        suffix = ("<div>more filler</div>" * 1000) + "</body>"
        html = prefix + middle + suffix

        out = compress_dom(html, max_chars=500)
        assert len(out) <= 500
        # Deberia conservar la zona con keywords de pago
        assert "Visa" in out or "Pago" in out or "PSE" in out


# -----------------------------------------------------------------------------
# PaymentExtractor (requiere anthropic SDK)
# -----------------------------------------------------------------------------
@requires_anthropic
@pytest.mark.asyncio
class TestPaymentExtractor:
    async def test_happy_path_extracts_methods(self):
        from app.agents.payment_extractor import extract_payment_methods

        fake_response = _make_fake_response({
            "payment_methods": [
                {"type": "credit_card", "brand": "Visa", "installments_max": 36, "installments_interest_free_max": 12},
                {"type": "credit_card", "brand": "Mastercard"},
                {"type": "bank_transfer", "brand": "Webpay Plus"},
            ],
            "confidence": "high",
        })

        with patch("app.agents.payment_extractor.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test"
            mock_settings.model_name = "claude-haiku-4-5"
            with patch("anthropic.AsyncAnthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create = AsyncMock(return_value=fake_response)

                methods, usage = await extract_payment_methods(
                    "<body>Visa, Mastercard, Webpay</body>",
                    url="https://www.falabella.com/falabella-cl/product/123",
                    site_id="falabella",
                    country="CL",
                )

        assert len(methods) == 3
        brands = {m.brand for m in methods}
        assert {"Visa", "Mastercard", "Webpay Plus"} <= brands
        assert any(m.installments and m.installments.max == 36 for m in methods)
        assert usage.input == 1500
        assert usage.output == 200

    async def test_no_api_key_raises_llm_budget_exceeded(self):
        from app.agents.payment_extractor import extract_payment_methods
        from app.schemas.error import ErrorCode, ScraperError

        with patch("app.agents.payment_extractor.settings") as mock_settings:
            mock_settings.anthropic_api_key = ""  # vacio
            with pytest.raises(ScraperError) as exc_info:
                await extract_payment_methods(
                    "<body></body>",
                    url="https://www.falabella.com/x",
                    site_id="falabella",
                )
        assert exc_info.value.code == ErrorCode.LLM_BUDGET_EXCEEDED

    async def test_invalid_type_filtered_out(self):
        from app.agents.payment_extractor import extract_payment_methods

        fake_response = _make_fake_response({
            "payment_methods": [
                {"type": "credit_card", "brand": "Visa"},
                {"type": "INVALID_TYPE", "brand": "X"},  # debe ser filtrado
                {"type": "wallet", "brand": ""},  # brand vacio, filtrado
            ],
            "confidence": "high",
        })

        with patch("app.agents.payment_extractor.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test"
            mock_settings.model_name = "claude-haiku-4-5"
            with patch("anthropic.AsyncAnthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create = AsyncMock(return_value=fake_response)
                methods, _ = await extract_payment_methods(
                    "<body></body>",
                    url="https://www.falabella.com/x",
                )
        assert len(methods) == 1
        assert methods[0].brand == "Visa"

    async def test_dedupes_methods(self):
        from app.agents.payment_extractor import extract_payment_methods

        fake_response = _make_fake_response({
            "payment_methods": [
                {"type": "credit_card", "brand": "Visa"},
                {"type": "credit_card", "brand": "VISA"},  # se normaliza a Visa
                {"type": "credit_card", "brand": "Tarjeta Visa"},  # idem
            ],
            "confidence": "medium",
        })

        with patch("app.agents.payment_extractor.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test"
            mock_settings.model_name = "claude-haiku-4-5"
            with patch("anthropic.AsyncAnthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create = AsyncMock(return_value=fake_response)
                methods, _ = await extract_payment_methods(
                    "<body></body>", url="https://www.falabella.com/x"
                )
        # Las 3 deberian colapsar a una sola Visa/credit_card
        visa_count = sum(1 for m in methods if m.brand == "Visa" and m.type == "credit_card")
        assert visa_count == 1


# -----------------------------------------------------------------------------
# ProductEnricher (requiere anthropic SDK)
# -----------------------------------------------------------------------------
@requires_anthropic
@pytest.mark.asyncio
class TestProductEnricher:
    async def test_skips_llm_if_product_complete(self):
        from app.agents.product_enricher import enrich_product
        from app.schemas.response import ProductInfo, PriceInfo

        complete = ProductInfo(title="Existing", price=PriceInfo(amount=99.0, currency="CLP"))
        with patch("app.agents.product_enricher.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test"
            with patch("anthropic.AsyncAnthropic") as MockClient:
                product, usage = await enrich_product(
                    "<body></body>",
                    url="https://x.com",
                    current=complete,
                )
                # No deberia haber llamado al LLM
                MockClient.assert_not_called()
        assert product is complete
        assert usage.input == 0

    async def test_invokes_llm_when_product_missing_price(self):
        from app.agents.product_enricher import enrich_product
        from app.schemas.response import ProductInfo

        partial = ProductInfo(title="Title only", price=None)

        fake_response = _make_fake_response({
            "title": "Title only",
            "price_amount": 1599999.0,
            "currency": "COP",
        }, input_tokens=800, output_tokens=50)

        with patch("app.agents.product_enricher.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-ant-test"
            mock_settings.model_name = "claude-haiku-4-5"
            with patch("anthropic.AsyncAnthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create = AsyncMock(return_value=fake_response)
                product, usage = await enrich_product(
                    "<body>Producto $1.599.999 COP</body>",
                    url="https://www.falabella.com.co/x",
                    current=partial,
                    country="CO",
                )
        assert product.title == "Title only"  # preservamos el title del adapter
        assert product.price.amount == 1599999.0
        assert product.price.currency == "COP"
        assert usage.input == 800

    async def test_no_api_key_returns_current_unchanged(self):
        from app.agents.product_enricher import enrich_product
        from app.schemas.response import ProductInfo

        partial = ProductInfo(title="X", price=None)
        with patch("app.agents.product_enricher.settings") as mock_settings:
            mock_settings.anthropic_api_key = ""
            product, usage = await enrich_product(
                "<body></body>", url="https://x.com", current=partial,
            )
        assert product is partial
        assert usage.input == 0
