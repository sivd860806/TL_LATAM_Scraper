"""Tests del FalabellaAdapter (P2.3) con mocks de Playwright.

No requieren Playwright real instalado: usamos AsyncMock para simular
el comportamiento del browser y verificar que el adapter procesa el DOM
y los selectores correctamente.

Para tests E2E reales contra falabella.com hay un test marcado @pytest.mark.live
que se desactiva por default y se opt-in con `pytest -m live`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.falabella import (
    FalabellaAdapter,
    _infer_currency_from_url,
    _parse_price_text,
)

# Skip tests del adapter si Playwright no esta instalado en el ambiente
try:
    import playwright.async_api  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

requires_playwright = pytest.mark.skipif(
    not HAS_PLAYWRIGHT,
    reason="Playwright not installed; install with: pip install playwright && playwright install chromium",
)
from app.schemas.error import ErrorCode, ScraperError

FIXTURES = Path(__file__).parent / "fixtures"


# -----------------------------------------------------------------------------
# Helpers (puros)
# -----------------------------------------------------------------------------
class TestInferCurrency:
    @pytest.mark.parametrize("url, expected", [
        ("https://www.falabella.com/falabella-cl/product/123", "CLP"),
        ("https://www.falabella.com.co/falabella-co/product/abc", "COP"),
        ("https://www.falabella.com.pe/falabella-pe/product/xyz", "PEN"),
        ("https://www.falabella.com/falabella-ar/product/123", "ARS"),
    ])
    def test_known_tlds(self, url, expected):
        assert _infer_currency_from_url(url) == expected

    def test_unknown_url_returns_none(self):
        assert _infer_currency_from_url("https://example.com") is None


class TestParsePriceText:
    @pytest.mark.parametrize("text, expected", [
        ("$ 99.990", 99990.0),
        ("$1.599.999", 1599999.0),
        ("CLP 250.000", 250000.0),
        ("99990", 99990.0),
        ("1.599,99", 1599.99),  # decimal con coma (formato LATAM)
    ])
    def test_known_formats(self, text, expected):
        assert _parse_price_text(text) == expected

    @pytest.mark.parametrize("text", ["", "    ", "no hay precio", None])
    def test_empty_or_garbage_returns_none(self, text):
        assert _parse_price_text(text or "") is None


# -----------------------------------------------------------------------------
# Adapter (mock de Playwright)
# -----------------------------------------------------------------------------
def _make_mock_page(
    *,
    title_text: str | None = "Smartwatch Test",
    price_text: str | None = "$ 99.990",
    url: str = "https://www.falabella.com/falabella-cl/product/123",
    captcha: bool = False,
    login_redirect: bool = False,
    dom: str | None = None,
):
    """Crea un mock de page Playwright para usar dentro del adapter."""
    page = MagicMock()
    page.url = url if not login_redirect else url + "/login"
    page.goto = AsyncMock(return_value=MagicMock(status=200))

    async def fake_query_selector(sel: str):
        # Simular CAPTCHA presente
        if captcha and ("captcha" in sel or "cloudflare" in sel or "challenge" in sel):
            el = MagicMock()
            return el
        # Title selectors
        if "h1" in sel or "product-name" in sel or "pdp-title" in sel:
            if title_text is None:
                return None
            el = MagicMock()
            el.text_content = AsyncMock(return_value=title_text)
            el.get_attribute = AsyncMock(return_value=title_text)
            return el
        # Price selectors
        if "price" in sel.lower() or "data-internet-price" in sel or "copy10" in sel:
            if price_text is None:
                return None
            el = MagicMock()
            el.text_content = AsyncMock(return_value=price_text)
            el.get_attribute = AsyncMock(return_value="99990")
            return el
        if "meta" in sel and "og:title" in sel:
            if title_text is None:
                return None
            el = MagicMock()
            el.get_attribute = AsyncMock(return_value=title_text)
            return el
        return None

    page.query_selector = fake_query_selector
    page.wait_for_selector = AsyncMock()
    page.content = AsyncMock(
        return_value=dom or (FIXTURES / "falabella_pdp.html").read_text()
    )
    return page


def _make_mock_playwright(page):
    """Mock del context manager `async_playwright()` que devuelve un browser."""
    browser = MagicMock()
    browser.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    browser.new_context = AsyncMock(return_value=context)

    pw = MagicMock()
    pw.chromium.launch = AsyncMock(return_value=browser)

    mock_pw_cm = MagicMock()
    mock_pw_cm.__aenter__ = AsyncMock(return_value=pw)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_pw_cm


@pytest.mark.asyncio
@requires_playwright
class TestFalabellaAdapter:
    async def test_happy_path_extracts_title_price_and_dom(self):
        page = _make_mock_page()
        with patch(
            "playwright.async_api.async_playwright",
            return_value=_make_mock_playwright(page),
        ):
            adapter = FalabellaAdapter()
            result = await adapter.fetch(
                "https://www.falabella.com/falabella-cl/product/123"
            )
        assert result.mode == "browser"
        assert result.site_id == "falabella"
        assert result.product is not None
        assert result.product.title == "Smartwatch Test"
        assert result.product.price.amount == 99990.0
        assert result.product.price.currency == "CLP"
        assert result.payment_methods == []  # se llenan en P2.4
        assert result.initial_dom is not None
        assert len(result.initial_dom) > 0
        assert result.payment_methods_source == "captured_dom"

    async def test_captcha_detected_raises_anti_bot(self):
        page = _make_mock_page(captcha=True)
        with patch(
            "playwright.async_api.async_playwright",
            return_value=_make_mock_playwright(page),
        ):
            adapter = FalabellaAdapter()
            with pytest.raises(ScraperError) as exc_info:
                await adapter.fetch(
                    "https://www.falabella.com/falabella-cl/product/123"
                )
        assert exc_info.value.code == ErrorCode.ANTI_BOT_DETECTED

    async def test_login_redirect_raises_login_required(self):
        page = _make_mock_page(login_redirect=True)
        with patch(
            "playwright.async_api.async_playwright",
            return_value=_make_mock_playwright(page),
        ):
            adapter = FalabellaAdapter()
            with pytest.raises(ScraperError) as exc_info:
                await adapter.fetch(
                    "https://www.falabella.com/falabella-cl/product/123"
                )
        assert exc_info.value.code == ErrorCode.LOGIN_REQUIRED

    async def test_404_raises_out_of_stock(self):
        page = _make_mock_page()
        page.goto = AsyncMock(return_value=MagicMock(status=404))
        with patch(
            "playwright.async_api.async_playwright",
            return_value=_make_mock_playwright(page),
        ):
            adapter = FalabellaAdapter()
            with pytest.raises(ScraperError) as exc_info:
                await adapter.fetch(
                    "https://www.falabella.com/falabella-cl/product/123"
                )
        assert exc_info.value.code == ErrorCode.OUT_OF_STOCK

    async def test_no_title_no_price_returns_no_product(self):
        page = _make_mock_page(title_text=None, price_text=None)
        with patch(
            "playwright.async_api.async_playwright",
            return_value=_make_mock_playwright(page),
        ):
            adapter = FalabellaAdapter()
            result = await adapter.fetch(
                "https://www.falabella.com/falabella-cl/product/123"
            )
        # Sin title ni price, product=None pero el DOM se captura igual
        assert result.product is None
        assert result.initial_dom is not None
        assert result.mode == "browser"


# -----------------------------------------------------------------------------
# Test live (real browser, opt-in con `pytest -m live`)
# -----------------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.asyncio
async def test_falabella_real_navigation():
    """Test E2E real contra falabella.com. Requiere internet + chromium instalado."""
    adapter = FalabellaAdapter(headless=True, timeout_ms=30_000)
    # URL generica de Falabella CL — actualizar si el producto cambia
    url = "https://www.falabella.com/falabella-cl/category/cat6970373/Smartwatches"
    result = await adapter.fetch(url)
    assert result.mode == "browser"
    assert result.initial_dom is not None
    assert len(result.initial_dom) > 1000
