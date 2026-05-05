"""Adapter de Falabella con Playwright.

Falabella (CL/CO/PE/AR) renderiza la pagina de producto con JS dinamico:
no podemos usar httpx directo. Levantamos un browser headless con
Playwright + stealth + locale/UA realistas.

Estrategia:
  1. Navegar a la URL del producto (PDP).
  2. Esperar a que cargue el contenido principal (selector de title).
  3. Detectar CAPTCHA / login / out-of-stock con selectores especificos.
  4. Capturar DOM completo + intentar extraer title/price con varios fallbacks.
  5. Devolver AdapterResult(mode='browser', initial_dom=..., product=...)
     -> los agentes LLM en P2.4 toman este DOM y extraen payment_methods.

Por que NO clicamos en "Comprar" / "Ver opciones de pago" en este adapter:
  Esa decision la toma el PaymentNavigator agent (P2.4) usando el DOM como
  input. El adapter solo se encarga de "llegar a la pagina del producto y
  capturar el estado inicial". Es la separacion de responsabilidades del
  enunciado: 2 agentes con responsabilidades distintas.
"""
from __future__ import annotations

import re
from typing import Any

from ..config import settings
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PriceInfo, ProductInfo
from .base import AdapterResult

# Selectores que probamos en orden hasta encontrar uno que matchee.
# Son aproximados — Falabella cambia su layout periodicamente. Si la captura
# falla, el LLM Extractor (P2.4) sigue trabajando con el DOM crudo igual.
_TITLE_SELECTORS = [
    'h1[class*="product-name"]',
    'h1[data-name]',
    '[data-testid="pdp-title"]',
    'meta[property="og:title"]',
    "h1",
]

_PRICE_SELECTORS = [
    '[data-internet-price]',
    '[class*="copy10"]',  # Falabella prefix de classnames
    '[class*="prices-0"]',
    'meta[property="product:price:amount"]',
    'span[class*="price"]',
]

# Senales de bot detection / CAPTCHA. Si alguna matchea -> ANTI_BOT_DETECTED.
_CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="cloudflare"]',
    '[class*="captcha"]',
    '[id*="cf-challenge"]',
    'div#challenge-running',
]

# Senales de login wall (Falabella a veces requiere login para ciertos productos).
_LOGIN_SELECTORS = [
    'input[type="password"]',
    'a[href*="/login"]:visible',
]

# Selectores que indican "producto no disponible / out of stock".
_OOS_TEXTS = [
    "no disponible",
    "agotado",
    "sin stock",
    "out of stock",
]

# Map TLD -> currency code (Falabella opera en multiples paises)
_TLD_CURRENCY = {
    "cl": "CLP",
    "co": "COP",
    "pe": "PEN",
    "ar": "ARS",
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _infer_currency_from_url(url: str) -> str | None:
    """Falabella usa un patron tipo /falabella-cl/... o falabella.com.co/..."""
    m = re.search(r"(?:falabella[.-])(cl|co|pe|ar)(?:[/.])", url.lower())
    if m:
        return _TLD_CURRENCY.get(m.group(1))
    # Fallback: sufijo del netloc
    m = re.search(r"falabella\.com\.([a-z]{2})", url.lower())
    if m:
        return _TLD_CURRENCY.get(m.group(1))
    return None


def _parse_price_text(text: str) -> float | None:
    """Intenta extraer un numero de un string tipo '$ 1.599.999' o '1599999'."""
    if not text:
        return None
    # Quitar simbolos no numericos excepto . , -
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    # Heuristica simple: si tiene ',' como decimal (LATAM), reemplazar; sino
    # solo quitar separadores de miles.
    if "," in cleaned and "." in cleaned:
        # 1.599.999,00 -> remover '.' y poner '.' en lugar de ','
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        # 1.599.999 -> remover '.'
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Adapter
# -----------------------------------------------------------------------------
class FalabellaAdapter:
    """Adapter browser-based: levanta Playwright, captura DOM del PDP."""

    site_id: str = "falabella"
    requires_browser: bool = True

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms or settings.playwright_default_timeout_ms * 4  # ~60s
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    async def fetch(self, url: str, country: str | None = None) -> AdapterResult:
        # Import diferido para que el modulo se pueda importar sin Playwright instalado
        # (util en CI sin dependencias browser).
        try:
            from playwright.async_api import async_playwright, Error as PWError
        except ImportError as e:
            raise ScraperError(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Playwright no instalado: {e}. Run `playwright install chromium`.",
                stage=Stage.ADAPTER,
            ) from e

        currency = _infer_currency_from_url(url) or "CLP"

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
            except PWError as e:
                msg_lower = str(e).lower()
                if "libnspr" in msg_lower or "shared libraries" in msg_lower or "cannot open shared object" in msg_lower:
                    raise ScraperError(
                        code=ErrorCode.INTERNAL_ERROR,
                        message=(
                            "Playwright no puede levantar Chromium: faltan shared libraries "
                            "del sistema. Run: sudo playwright install-deps chromium "
                            "(o apt-get install libnspr4 libnss3 libdbus-1-3 libatk1.0-0 "
                            "libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 "
                            "libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 "
                            "libasound2 libatspi2.0-0)"
                        ),
                        stage=Stage.ADAPTER,
                    ) from e
                raise ScraperError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"Playwright failed to launch Chromium: {e}",
                    stage=Stage.ADAPTER,
                ) from e
            try:
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    locale="es-CL",
                    viewport={"width": 1366, "height": 768},
                    extra_http_headers={
                        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                    },
                )
                page = await context.new_page()
                # Aplicar stealth si esta disponible
                try:
                    from playwright_stealth import stealth_async
                    await stealth_async(page)
                except ImportError:
                    pass  # stealth es opcional

                # 1) Navegar
                try:
                    response = await page.goto(
                        url,
                        timeout=self.timeout_ms,
                        wait_until="domcontentloaded",
                    )
                except PWError as e:
                    raise ScraperError(
                        code=ErrorCode.TIMEOUT,
                        message=f"Falabella navigation failed: {e}",
                        stage=Stage.ADAPTER,
                    ) from e

                if response is None:
                    raise ScraperError(
                        code=ErrorCode.CHECKOUT_UNREACHABLE,
                        message="Page.goto returned None response",
                        stage=Stage.ADAPTER,
                    )

                status = response.status
                if status == 404:
                    raise ScraperError(
                        code=ErrorCode.OUT_OF_STOCK,
                        message=f"Falabella product not found (HTTP 404): {url}",
                        stage=Stage.ADAPTER,
                    )
                if status >= 500:
                    raise ScraperError(
                        code=ErrorCode.CHECKOUT_UNREACHABLE,
                        message=f"Falabella server error (HTTP {status}): {url}",
                        stage=Stage.ADAPTER,
                    )

                # 2) Detectar CAPTCHA
                for sel in _CAPTCHA_SELECTORS:
                    if await page.query_selector(sel):
                        raise ScraperError(
                            code=ErrorCode.ANTI_BOT_DETECTED,
                            message=f"CAPTCHA / anti-bot challenge detected (selector: {sel})",
                            stage=Stage.ADAPTER,
                        )

                # 3) Detectar redirect a login
                if "/login" in page.url.lower() or "iniciar-sesion" in page.url.lower():
                    raise ScraperError(
                        code=ErrorCode.LOGIN_REQUIRED,
                        message=f"Falabella redirected to login: {page.url}",
                        stage=Stage.ADAPTER,
                    )

                # 4) Esperar a que el contenido cargue (best-effort)
                try:
                    await page.wait_for_selector(
                        "h1, [data-testid='pdp-title']",
                        timeout=10_000,
                    )
                except PWError:
                    # No es fatal — el extractor LLM puede igual procesar el DOM
                    pass

                # 5) Extraer title/price con selectores
                title = await self._try_extract_title(page)
                price_amount = await self._try_extract_price(page)
                price_info = (
                    PriceInfo(amount=price_amount, currency=currency)
                    if price_amount is not None
                    else None
                )
                product_info = ProductInfo(title=title, price=price_info) if (title or price_info) else None

                # 6) Capturar DOM
                dom = await page.content()
                dom_size_kb = len(dom) / 1024

                return AdapterResult(
                    mode="browser",
                    site_id=self.site_id,
                    product=product_info,
                    payment_methods=[],  # se llenan en P2.4 con el LLM Extractor
                    initial_dom=dom,
                    browser_context=None,  # cerramos el browser; P2.4 va a re-fetchear si necesita
                    llm_calls_used=0,
                    network_calls=1,
                    payment_methods_source="captured_dom",
                )
            finally:
                await browser.close()

    # ---- selector helpers ------------------------------------------------
    async def _try_extract_title(self, page) -> str | None:
        for sel in _TITLE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                if sel.startswith("meta"):
                    val = await el.get_attribute("content")
                else:
                    val = await el.text_content()
                if val and val.strip():
                    return val.strip()
            except Exception:
                continue
        return None

    async def _try_extract_price(self, page) -> float | None:
        for sel in _PRICE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                if sel.startswith("meta"):
                    val = await el.get_attribute("content")
                elif sel.startswith("[data-internet-price]"):
                    val = await el.get_attribute("data-internet-price")
                else:
                    val = await el.text_content()
                amount = _parse_price_text(val) if val else None
                if amount is not None and amount > 0:
                    return amount
            except Exception:
                continue
        return None
