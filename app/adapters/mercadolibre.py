"""Adapter de Mercado Libre con graceful degradation.

Problema conocido (verificado 2026-05-05):
  ML rate-limita los endpoints /items/{id}, /sites/{site}, /sites/{site}/search
  desde IPs de datacenters (incluyendo WSL2 / cloud providers). Devuelve 403
  consistentemente. SOLO /sites/{site}/payment_methods sigue siendo publica.

Estrategia v1:
  1. Extraer site_id del URL (e.g. MLA, MCO).
  2. Llamar a /sites/{site}/payment_methods (siempre funciona) para los metodos.
  3. INTENTAR /items/{id} para title+price; si falla (403/404/timeout),
     continuamos con product=None y un flag en metadata.
  4. Para URLs de catalogo (/p/), saltamos el item lookup directamente.

Resultado:
  - Para CUALQUIER URL valida de ML, devolvemos 200 OK con payment_methods reales.
  - title/price puede venir o no segun rate-limit del momento.
  - 0 LLM calls.

Future Work:
  - Proxies residenciales rotativos para evitar el rate-limit.
  - Cache local de items (TTL 24h) para amortizar el rate-limit.
  - HTML scrape liviano del URL del producto si la API esta totalmente bloqueada.
"""
from __future__ import annotations

import re
from typing import Any, Literal

import httpx

from ..schemas.catalog import lookup_brand
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PaymentMethod, PriceInfo, ProductInfo
from .base import AdapterResult

ML_API_BASE = "https://api.mercadolibre.com"

_PRODUCT_PATTERN = re.compile(r"/p/(M[A-Z]{2}\d+)")
_ITEM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/articulo/(M[A-Z]{2}-?\d+)"),
    re.compile(r"/(M[A-Z]{2}-?\d+)(?:-|/|$|\?|#)"),
]

_ML_TYPE_TO_OUR_TYPE: dict[str, str] = {
    "credit_card": "credit_card",
    "debit_card": "debit_card",
    "prepaid_card": "debit_card",
    "account_money": "wallet",
    "bank_transfer": "bank_transfer",
    "atm": "bank_transfer",
    "ticket": "cash",
    "digital_currency": "other",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}


# -----------------------------------------------------------------------------
# URL parsing
# -----------------------------------------------------------------------------
def extract_id_from_url(url: str) -> tuple[Literal["item", "product"], str] | None:
    m = _PRODUCT_PATTERN.search(url)
    if m:
        return ("product", m.group(1))
    for pattern in _ITEM_PATTERNS:
        m = pattern.search(url)
        if m:
            raw = m.group(1)
            if "-" not in raw:
                raw = f"{raw[:3]}-{raw[3:]}"
            return ("item", raw)
    return None


def extract_item_id(url: str) -> str | None:
    result = extract_id_from_url(url)
    if result and result[0] == "item":
        return result[1]
    return None


def extract_site_id_from_item(ml_id: str) -> str:
    return ml_id.split("-")[0][:3]




def title_from_url_slug(url: str) -> str | None:
    """Extrae un title aproximado del slug de la URL.

    Ejemplo:
      https://www.mercadolibre.com.ar/apple-iphone-15-128-gb-negro-distribuidor/p/MLA1027172677
      -> 'Apple Iphone 15 128 Gb Negro Distribuidor'

    Es un fallback util cuando /items/{id} esta rate-limited.
    """
    from urllib.parse import urlparse
    try:
        path = urlparse(url).path
    except (ValueError, TypeError):
        return None
    if not path:
        return None
    # Eliminar el sufijo de id: /p/MLA1234 o /MLA-1234... o /articulo/MLA-1234
    import re as _re
    path = _re.sub(r"/p/M[A-Z]{2}\d+.*$", "", path)
    path = _re.sub(r"/M[A-Z]{2}-?\d+.*$", "", path)
    path = _re.sub(r"/articulo/?$", "", path)
    # Limpiar barras y dejar solo el ultimo segmento (el slug del producto)
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    slug = segments[-1]
    # Convertir slug-con-guiones a "Title Case"
    if "-" not in slug:
        return None
    words = [w for w in slug.split("-") if w]
    if len(words) < 2:
        return None
    return " ".join(w.capitalize() for w in words)


# -----------------------------------------------------------------------------
# Normalizacion de payment methods
# -----------------------------------------------------------------------------
def _normalize_payment_method(raw: dict[str, Any]) -> PaymentMethod | None:
    """Normaliza un payment method del JSON de la API de ML.

    - Si hay campo status, excluir solo si es 'deactive'/'deprecated'/'pending'.
      Si no hay campo status, asumir activo.
    - payment_type_id desconocido -> caer a 'other' en lugar de descartar.
    """
    status = (raw.get("status") or "active").lower()
    if status in {"deactive", "deprecated", "pending"}:
        return None

    ml_type = (raw.get("payment_type_id") or "").lower()
    our_type = _ML_TYPE_TO_OUR_TYPE.get(ml_type, "other")

    raw_name = (raw.get("name") or raw.get("id") or "").strip()
    if not raw_name:
        return None
    canonical = lookup_brand(raw_name) or raw_name
    return PaymentMethod(type=our_type, brand=canonical)


def _dedupe(methods: list[PaymentMethod]) -> list[PaymentMethod]:
    seen: set[tuple[str, str]] = set()
    out: list[PaymentMethod] = []
    for m in methods:
        key = (m.type, m.brand)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# -----------------------------------------------------------------------------
# Adapter
# -----------------------------------------------------------------------------
class MercadoLibreAdapter:
    site_id: str = "mercadolibre"
    requires_browser: bool = False

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client
        self._owns_client = http_client is None

    async def fetch(self, url: str, country: str | None = None) -> AdapterResult:
        extracted = extract_id_from_url(url)
        if not extracted:
            raise ScraperError(
                code=ErrorCode.INVALID_URL,
                message=f"Could not extract Mercado Libre id from URL: {url}",
                stage=Stage.ADAPTER,
            )
        kind, ml_id = extracted
        ml_site_id = extract_site_id_from_item(ml_id)

        client = self._client or httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        )
        n_calls = 0
        try:
            # 1) Payment methods (endpoint que sabemos que funciona)
            try:
                pm_data = await self._fetch_payment_methods(client, ml_site_id)
                n_calls += 1
            except ScraperError:
                # Si esto falla, no hay nada que devolver. Re-raise.
                raise

            methods = _dedupe([m for m in (
                _normalize_payment_method(pm) for pm in pm_data
            ) if m is not None])

            if not methods:
                raise ScraperError(
                    code=ErrorCode.PARSE_ERROR,
                    message=f"No active payment methods for site {ml_site_id}",
                    stage=Stage.ADAPTER,
                )

            # 2) Intentar enriquecer con product info (best-effort)
            #    Solo intentamos para listings, no para catalog (que requiere
            #    resolver via search, que esta bloqueado).
            product_info: ProductInfo | None = None
            if kind == "item":
                try:
                    item_data = await self._fetch_item(client, ml_id)
                    product_info = self._build_product_from_item(item_data)
                    n_calls += 1
                except ScraperError:
                    # Item endpoint puede estar rate-limited; lo dejamos pasar.
                    # El response sigue siendo util porque tiene payment_methods.
                    product_info = None

            # Fallback: si no logramos product_info pero el URL tiene slug,
            # extraer al menos un title aproximado.
            if product_info is None:
                slug_title = title_from_url_slug(url)
                if slug_title:
                    product_info = ProductInfo(title=slug_title, price=None)

            return AdapterResult(
                mode="direct",
                site_id=self.site_id,
                product=product_info,
                payment_methods=methods,
                llm_calls_used=0,
                network_calls=n_calls,
                payment_methods_source="site_catalog",
            )
        finally:
            if self._owns_client:
                await client.aclose()

    # ---- HTTP calls ------------------------------------------------------
    async def _fetch_item(self, client, item_id: str) -> dict[str, Any]:
        return await self._get_or_raise(
            client, f"{ML_API_BASE}/items/{item_id}", item_id, "item"
        )

    async def _fetch_payment_methods(self, client, ml_site_id: str) -> list[dict[str, Any]]:
        try:
            r = await client.get(f"{ML_API_BASE}/sites/{ml_site_id}/payment_methods")
        except httpx.TimeoutException as e:
            raise ScraperError(
                code=ErrorCode.TIMEOUT,
                message=f"ML API timed out fetching payment methods for {ml_site_id}",
                stage=Stage.ADAPTER,
            ) from e

        if r.status_code in (401, 403):
            raise ScraperError(
                code=ErrorCode.ANTI_BOT_DETECTED,
                message=(
                    f"ML payment_methods API blocked us ({r.status_code}) for site "
                    f"{ml_site_id}. This usually means the IP is rate-limited."
                ),
                stage=Stage.ADAPTER,
            )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ScraperError(
                code=ErrorCode.PARSE_ERROR,
                message="Unexpected response shape from ML payment_methods API",
                stage=Stage.ADAPTER,
            )
        return data

    async def _get_or_raise(self, client, url: str, ml_id: str, kind: str) -> dict[str, Any]:
        try:
            r = await client.get(url)
        except httpx.TimeoutException as e:
            raise ScraperError(
                code=ErrorCode.TIMEOUT,
                message=f"ML API timed out fetching {kind} {ml_id}",
                stage=Stage.ADAPTER,
            ) from e
        except httpx.RequestError as e:
            raise ScraperError(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Network error fetching {kind} {ml_id}: {e}",
                stage=Stage.ADAPTER,
            ) from e

        if r.status_code == 404:
            raise ScraperError(
                code=ErrorCode.OUT_OF_STOCK,
                message=f"{kind.capitalize()} {ml_id} not found",
                stage=Stage.ADAPTER,
            )
        if r.status_code in (401, 403):
            raise ScraperError(
                code=ErrorCode.ANTI_BOT_DETECTED,
                message=f"ML API rate-limited ({r.status_code}) for {kind} {ml_id}",
                stage=Stage.ADAPTER,
            )
        r.raise_for_status()
        return r.json()

    def _build_product_from_item(self, item_data: dict[str, Any]) -> ProductInfo | None:
        title = item_data.get("title")
        price = item_data.get("price")
        currency_id = item_data.get("currency_id")
        if not title and price is None:
            return None
        price_info = None
        if price is not None and currency_id:
            try:
                price_info = PriceInfo(amount=float(price), currency=currency_id)
            except (ValueError, TypeError):
                price_info = None
        return ProductInfo(title=title, price=price_info)
