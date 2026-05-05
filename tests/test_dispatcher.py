"""Tests del dispatcher (P2.2)."""
from __future__ import annotations

import pytest

from app.dispatcher import (
    SITE_FALABELLA,
    SITE_MERCADOLIBRE,
    infer_country_from_url,
    resolve_site,
)


class TestResolveSite:
    @pytest.mark.parametrize("url, expected", [
        ("https://articulo.mercadolibre.com.ar/MLA-1234567890", SITE_MERCADOLIBRE),
        ("https://www.mercadolibre.com.ar/p/MLA1234567890", SITE_MERCADOLIBRE),
        ("https://articulo.mercadolibre.com.co/MCO-9876543210", SITE_MERCADOLIBRE),
        ("https://www.mercadolibre.com.mx/articulo/MLM-1111", SITE_MERCADOLIBRE),
        ("https://listado.mercadolibre.com.cl/categoria", SITE_MERCADOLIBRE),
        ("https://m.mercadolibre.com.uy/MLU-555", SITE_MERCADOLIBRE),
        ("https://www.falabella.com/falabella-cl/product/12345", SITE_FALABELLA),
        ("https://www.falabella.com.pe/falabella-pe/product/abc", SITE_FALABELLA),
    ])
    def test_supported_sites(self, url, expected):
        assert resolve_site(url) == expected

    @pytest.mark.parametrize("url", [
        "https://www.amazon.com.mx/dp/B07ZPC9QD4",
        "https://www.linio.com/p/abc",
        "https://www.magazineluiza.com.br/produto",
        "https://liverpool.com.mx/tienda/producto",
        "https://example.com",
        "https://localhost:8080",
    ])
    def test_unsupported_returns_none(self, url):
        assert resolve_site(url) is None

    def test_malformed_url_returns_none(self):
        assert resolve_site("not-a-url") is None
        assert resolve_site("") is None

    def test_case_insensitive_netloc(self):
        # netloc en mayusculas tambien debe matchear
        assert resolve_site("https://ARTICULO.MERCADOLIBRE.COM.AR/MLA-1") == SITE_MERCADOLIBRE


class TestInferCountry:
    @pytest.mark.parametrize("url, expected", [
        ("https://articulo.mercadolibre.com.ar/MLA-1", "AR"),
        ("https://articulo.mercadolibre.com.co/MCO-1", "CO"),
        ("https://articulo.mercadolibre.com.mx/MLM-1", "MX"),
        ("https://www.mercadolibre.com.cl/p/MLC-1", "CL"),
        ("https://www.mercadolibre.com.pe/articulo/MPE-1", "PE"),
    ])
    def test_infer_from_tld(self, url, expected):
        assert infer_country_from_url(url) == expected

    def test_falabella_dot_com_returns_none(self):
        # Falabella usa .com con path, el pais se resuelve en el adapter
        assert infer_country_from_url("https://www.falabella.com/falabella-cl/p/1") is None

    def test_unknown_tld_returns_none(self):
        assert infer_country_from_url("https://example.com") is None
