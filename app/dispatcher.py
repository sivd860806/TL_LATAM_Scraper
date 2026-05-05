"""Dispatcher: resuelve URL -> site_id deterministicamente.

Funcion pura, sin LLM, sin red. Usa regex sobre el netloc.

Decision TL: NO uso LLM aca por dos razones:
  1. Para los 2 sites soportados (ML, Falabella), regex resuelve el 100%.
  2. Costo cero, latencia cero, deterministico.

Si el dominio no matchea, devolvemos None y el caller decide que hacer
(tipicamente: error UNSUPPORTED_SITE).

Si en el futuro queremos soportar sites sin adapter, la extension natural
es un GenericLLMAdapter que use un Navigator agentic. Eso queda como
Future Work documentado en el README.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Site IDs canonicos
SITE_MERCADOLIBRE = "mercadolibre"
SITE_FALABELLA = "falabella"

# Mapeo dominio -> site_id. El primer match gana.
# Patrones se validan sobre netloc.lower() — case-insensitive y sin scheme/path.
_SITE_PATTERNS: dict[str, re.Pattern[str]] = {
    SITE_MERCADOLIBRE: re.compile(
        # mercadolibre.com.{ar,co,mx,cl,pe,uy,ec} con subdominios opcionales
        # (articulo., produto., www., m., listado., etc.)
        r"(?:^|\.)mercadolibre\.com(?:\.[a-z]{2,3})?$"
    ),
    SITE_FALABELLA: re.compile(
        # falabella.com{,.cl,.pe,.co,.ar} con www/m opcional
        r"(?:^|\.)falabella\.com(?:\.[a-z]{2})?$"
    ),
}


def resolve_site(url: str) -> str | None:
    """Resuelve la URL a un site_id soportado.

    Parameters
    ----------
    url : str
        URL completa, p.ej. 'https://articulo.mercadolibre.com.ar/MLA-1234'.

    Returns
    -------
    str | None
        site_id (e.g. 'mercadolibre', 'falabella') o None si no es soportado.
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None

    netloc = (parsed.netloc or "").lower().strip()
    if not netloc:
        return None

    for site_id, pattern in _SITE_PATTERNS.items():
        if pattern.search(netloc):
            return site_id
    return None


# Map site_id -> ISO country code, inferido del TLD si la URL lo trae.
# Si no se puede inferir, devuelve None y el caller usa el `country` del request.
_TLD_COUNTRY_MAP: dict[str, str] = {
    "ar": "AR",
    "cl": "CL",
    "co": "CO",
    "mx": "MX",
    "pe": "PE",
    "uy": "UY",
    "ec": "EC",
    "br": "BR",
}


def infer_country_from_url(url: str) -> str | None:
    """Intenta inferir el pais ISO-2 del TLD del netloc.

    Examples
    --------
    >>> infer_country_from_url("https://articulo.mercadolibre.com.ar/MLA-1")
    'AR'
    >>> infer_country_from_url("https://www.falabella.com/falabella-cl/...")
    None  # el dominio termina en .com, el pais esta en el path (caso especial)
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None

    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return None

    # Tomar la ultima parte del netloc despues del ultimo punto
    parts = netloc.split(".")
    if len(parts) >= 2:
        last = parts[-1]
        if last in _TLD_COUNTRY_MAP:
            return _TLD_COUNTRY_MAP[last]

    # Caso especial Falabella: www.falabella.com/falabella-cl/... -> CL
    # Lo manejamos en el adapter de Falabella, no aqui.
    return None
