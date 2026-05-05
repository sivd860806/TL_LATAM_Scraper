"""Interface comun para SiteAdapters.

Cada adapter (Mercado Libre, Falabella, ...) implementa este Protocol.

Hay dos categorias de adapter:
  1. Direct (ML): el adapter resuelve completo el request -- extrae producto y
     payment_methods sin necesidad de browser ni LLM (API publica).
  2. Browser-based (Falabella): el adapter solo navega a la pagina y devuelve
     DOM crudo. Los pasos posteriores (Navigator + Extractor) viven afuera del
     adapter, en el grafo principal con LLM.

El campo `mode` del AdapterResult diferencia los dos casos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ..schemas.response import PaymentMethod, ProductInfo


@dataclass
class AdapterResult:
    """Resultado de invocar un SiteAdapter."""
    mode: Literal["direct", "browser"]
    site_id: str
    product: ProductInfo | None = None
    payment_methods: list[PaymentMethod] = field(default_factory=list)
    browser_context: object | None = None
    initial_dom: str | None = None
    llm_calls_used: int = 0
    network_calls: int = 0
    # Granularidad del catalogo de payment_methods devuelto:
    # - "site_catalog": metodos disponibles en el SITE (e.g. todos los de MLA)
    # - "item_specific": filtrados por el seller/item (mas preciso)
    # - "captured_dom": extraidos del DOM del PDP (Falabella+LLM, preview)
    # - "captured_checkout_dom": extraidos tras navegar add-to-cart -> checkout
    payment_methods_source: str = "site_catalog"


@runtime_checkable
class SiteAdapter(Protocol):
    """Interface que cualquier adapter de site debe implementar."""

    site_id: str
    requires_browser: bool

    async def fetch(self, url: str, country: str | None = None) -> AdapterResult:
        """Procesa la URL y devuelve un AdapterResult.

        Raises ScraperError con stage='adapter' si algo falla recuperablemente.
        """
        ...
