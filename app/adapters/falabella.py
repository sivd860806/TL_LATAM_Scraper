"""Adapter de Falabella con Playwright.

Falabella (CL/CO/PE/AR) renderiza la pagina de producto con JS dinamico:
no podemos usar httpx directo. Levantamos un browser headless con
Playwright + stealth + locale/UA realistas.

Estrategia (P2.3.5):
  1. Navegar a la URL del producto (PDP).
  2. Esperar a que cargue el contenido principal (selector de title).
  3. Detectar CAPTCHA / login / out-of-stock con selectores especificos.
  4. Capturar DOM PDP + intentar extraer title/price con varios fallbacks.
  5. Best-effort: clic en "Agregar al carro" -> "Continuar" hasta llegar al
     selector de pagos del checkout. El DOM del checkout (con los 7+ medios
     de pago de Falabella) se concatena al DOM del PDP.
  6. Devolver AdapterResult(mode='browser', initial_dom=..., product=...)
     -> los agentes LLM (P2.4) toman este DOM combinado y extraen
     payment_methods + (opcionalmente) enriquecen producto.

Separacion de responsabilidades:
  - Adapter: se encarga del browser, navegacion deterministica y captura.
  - PaymentExtractor (LLM): interpreta el DOM y normaliza la lista de medios.
  - ProductEnricher (LLM, conditional): refina title/price si el adapter no
    los capturo del PDP.

Si la navegacion al checkout falla (modal cambio, sin stock, captcha al
agregar al carro), seguimos teniendo el DOM del PDP -- el response degrada
gracefully a la "preview" de medios visible en la PDP.
"""
from __future__ import annotations

import re
from typing import Any

from ..config import settings
from ..logging import get_logger
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PriceInfo, ProductInfo
from .base import AdapterResult

logger = get_logger(__name__)

# Selectores que probamos en orden hasta encontrar uno que matchee.
_TITLE_SELECTORS = [
    'h1[class*="product-name"]',
    'h1[data-name]',
    '[data-testid="pdp-title"]',
    'meta[property="og:title"]',
    "h1",
]

_PRICE_SELECTORS = [
    '[data-internet-price]',
    '[class*="copy10"]',
    '[class*="prices-0"]',
    'meta[property="product:price:amount"]',
    'span[class*="price"]',
]

_CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="cloudflare"]',
    '[class*="captcha"]',
    '[id*="cf-challenge"]',
    'div#challenge-running',
]

_LOGIN_SELECTORS = [
    'input[type="password"]',
    'a[href*="/login"]:visible',
]

_OOS_TEXTS = [
    "no disponible",
    "agotado",
    "sin stock",
    "out of stock",
]

_TLD_CURRENCY = {
    "cl": "CLP",
    "co": "COP",
    "pe": "PEN",
    "ar": "ARS",
}

# ---- Selectores para navegar al checkout (P2.3.5) ------------------------
_ADD_TO_CART_SELECTORS = [
    'button:has-text("Agregar al carro")',
    'button:has-text("Agregar al carrito")',
    'button:has-text("Agregar a la bolsa")',
    'button[data-action="add-to-cart"]',
    '[data-testid*="add-to-cart"]',
    'button[data-testid="add-product-to-cart"]',
    'button.add-to-cart',
]

_GO_TO_CART_SELECTORS = [
    'a:has-text("Ir al carro")',
    'a:has-text("Ir al carrito")',
    'button:has-text("Ir al carro")',
    'button:has-text("Ir al carrito")',
    'a:has-text("Ver carro")',
    'a[href*="/cart"]',
    'a[href*="/carro"]',
]

_CHECKOUT_CTA_SELECTORS = [
    'button:has-text("Continuar")',
    'button:has-text("Iniciar compra")',
    'button:has-text("Comprar ahora")',
    'a:has-text("Continuar")',
    'a:has-text("Iniciar compra")',
    'button[data-testid*="checkout"]',
    'a[href*="/checkout"]',
]

_PAYMENT_PAGE_HINTS = [
    "medios de pago",
    "metodos de pago",
    "métodos de pago",
    "forma de pago",
    "formas de pago",
    "selecciona tu forma de pago",
    "selecciona un medio de pago",
]

_CHECKOUT_URL_FRAGMENTS = ["/checkout", "/carro", "/cart"]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _infer_currency_from_url(url: str) -> str | None:
    m = re.search(r"(?:falabella[.-])(cl|co|pe|ar)(?:[/.])", url.lower())
    if m:
        return _TLD_CURRENCY.get(m.group(1))
    m = re.search(r"falabella\.com\.([a-z]{2})", url.lower())
    if m:
        return _TLD_CURRENCY.get(m.group(1))
    return None


def _parse_price_text(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
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
        navigate_to_checkout: bool = True,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms or settings.playwright_default_timeout_ms * 4
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.navigate_to_checkout = navigate_to_checkout

    async def fetch(self, url: str, country: str | None = None) -> AdapterResult:
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
                try:
                    from playwright_stealth import stealth_async
                    await stealth_async(page)
                except ImportError:
                    pass

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

                for sel in _CAPTCHA_SELECTORS:
                    if await page.query_selector(sel):
                        raise ScraperError(
                            code=ErrorCode.ANTI_BOT_DETECTED,
                            message=f"CAPTCHA / anti-bot challenge detected (selector: {sel})",
                            stage=Stage.ADAPTER,
                        )

                if "/login" in page.url.lower() or "iniciar-sesion" in page.url.lower():
                    raise ScraperError(
                        code=ErrorCode.LOGIN_REQUIRED,
                        message=f"Falabella redirected to login: {page.url}",
                        stage=Stage.ADAPTER,
                    )

                try:
                    await page.wait_for_selector(
                        "h1, [data-testid='pdp-title']",
                        timeout=10_000,
                    )
                except PWError:
                    pass

                title = await self._try_extract_title(page)
                price_amount = await self._try_extract_price(page)
                price_info = (
                    PriceInfo(amount=price_amount, currency=currency)
                    if price_amount is not None
                    else None
                )
                product_info = ProductInfo(title=title, price=price_info) if (title or price_info) else None

                pdp_dom = await page.content()
                checkout_dom: str | None = None
                checkout_reached = False
                network_calls = 1

                if self.navigate_to_checkout:
                    try:
                        checkout_dom = await self._navigate_to_checkout(page)
                        if checkout_dom is not None:
                            checkout_reached = True
                            network_calls += 1
                    except Exception as e:
                        logger.warning("falabella.nav.unhandled_exception", error=str(e))
                        checkout_dom = None
                        checkout_reached = False

                if checkout_dom:
                    combined_dom = (
                        "<!-- ====== PDP DOM ====== -->\n"
                        + pdp_dom
                        + "\n<!-- ====== CHECKOUT DOM (payment selector) ====== -->\n"
                        + checkout_dom
                    )
                else:
                    combined_dom = pdp_dom

                return AdapterResult(
                    mode="browser",
                    site_id=self.site_id,
                    product=product_info,
                    payment_methods=[],
                    initial_dom=combined_dom,
                    browser_context=None,
                    llm_calls_used=0,
                    network_calls=network_calls,
                    payment_methods_source=(
                        "captured_checkout_dom" if checkout_reached else "captured_dom"
                    ),
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

    # ---- checkout navigation (P2.3.5 + P2.3.6 instrumentation) ---------
    async def _navigate_to_checkout(self, page) -> str | None:
        """Best-effort: agrega el producto al carro y avanza hasta el selector
        de medios de pago. Devuelve el DOM en ese punto, o None si falla.

        Loguea cada paso con structlog para facilitar el debugging post-mortem.
        """
        from playwright.async_api import Error as PWError

        url_pdp = page.url

        clicked = await self._click_first_match(page, _ADD_TO_CART_SELECTORS, timeout_ms=5_000)
        logger.info("falabella.nav.add_to_cart", clicked=clicked, url=page.url)
        if not clicked:
            return None

        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PWError:
            pass
        logger.info("falabella.nav.after_add_networkidle", url=page.url)

        for sel in _CAPTCHA_SELECTORS:
            try:
                if await page.query_selector(sel):
                    logger.warning("falabella.nav.captcha_after_add", selector=sel)
                    return None
            except Exception:
                continue

        go_cart = await self._click_first_match(page, _GO_TO_CART_SELECTORS, timeout_ms=5_000)
        logger.info("falabella.nav.go_to_cart", clicked=go_cart, url=page.url)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PWError:
            pass

        if not any(frag in page.url.lower() for frag in _CHECKOUT_URL_FRAGMENTS):
            logger.info("falabella.nav.cart_url_missing_fragment", url=page.url)
            try:
                base = re.match(r"(https?://[^/]+)", page.url)
                if base:
                    fallback_url = f"{base.group(1)}/cart"
                    await page.goto(
                        fallback_url, timeout=10_000, wait_until="domcontentloaded"
                    )
                    logger.info("falabella.nav.cart_fallback_goto", url=page.url)
            except PWError as e:
                logger.warning("falabella.nav.cart_fallback_failed", error=str(e))
                return None

        for step_idx in range(3):
            cur_url = page.url

            if "/login" in cur_url.lower() or "iniciar-sesion" in cur_url.lower():
                logger.info("falabella.nav.hit_login", step=step_idx, url=cur_url)
                break

            if await self._is_payment_page(page):
                logger.info("falabella.nav.payment_page_reached", step=step_idx, url=cur_url)
                break

            advanced = await self._click_first_match(
                page, _CHECKOUT_CTA_SELECTORS, timeout_ms=4_000
            )
            logger.info(
                "falabella.nav.checkout_step",
                step=step_idx,
                clicked=advanced,
                url_before=cur_url,
            )
            if not advanced:
                break

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PWError:
                pass

        final_url = page.url
        in_checkout = any(frag in final_url.lower() for frag in _CHECKOUT_URL_FRAGMENTS)
        if not in_checkout:
            logger.warning("falabella.nav.final_url_not_checkout", url=final_url)
            return None

        try:
            dom = await page.content()
        except PWError as e:
            logger.warning("falabella.nav.content_failed", error=str(e))
            return None

        dom_lower = dom.lower()
        hint_hits = sum(1 for h in _PAYMENT_PAGE_HINTS if h in dom_lower)
        brand_keywords = ["visa", "mastercard", "amex", "pse", "daviplata", "nequi",
                          "efecty", "baloto", "webpay", "cmr"]
        brand_hits = sum(1 for b in brand_keywords if b in dom_lower)
        logger.info(
            "falabella.nav.captured_dom",
            url=final_url,
            dom_kb=round(len(dom) / 1024, 1),
            payment_hints_found=hint_hits,
            brand_keywords_found=brand_hits,
        )

        if settings.dump_falabella_dom:
            import time as _t
            try:
                ts = int(_t.time())
                path = f"/tmp/falabella_checkout_dom_{ts}.html"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"<!-- final_url: {final_url} -->\n")
                    f.write(f"<!-- pdp_url: {url_pdp} -->\n")
                    f.write(dom)
                logger.info("falabella.nav.dom_dumped", path=path)
            except OSError as e:
                logger.warning("falabella.nav.dom_dump_failed", error=str(e))

        return dom

    async def _click_first_match(
        self, page, selectors: list[str], timeout_ms: int = 5_000
    ) -> bool:
        from playwright.async_api import Error as PWError

        for sel in selectors:
            try:
                locator = page.locator(sel).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                await locator.scroll_into_view_if_needed(timeout=2_000)
                await locator.click(timeout=3_000)
                return True
            except PWError:
                continue
            except Exception:
                continue
        return False

    async def _is_payment_page(self, page) -> bool:
        url_low = page.url.lower()
        if not any(frag in url_low for frag in _CHECKOUT_URL_FRAGMENTS):
            return False
        if "/payment" in url_low or "/pago" in url_low or "/medios-pago" in url_low:
            return True
        try:
            body_text = (await page.locator("body").text_content(timeout=2_000)) or ""
        except Exception:
            return False
        body_low = body_text.lower()
        return any(hint in body_low for hint in _PAYMENT_PAGE_HINTS)
