"""Smoke tests del LangGraph state machine (P2.5).

Verifica que:
  1. El grafo compila correctamente.
  2. Routing condicional funciona (dispatch -> ml | falabella | error).
  3. Mode='direct' (ML) skipea LLM nodes.
  4. Mode='browser' (Falabella) invoca payment_extractor.
  5. Errores en cualquier nodo terminan el grafo limpiamente con state['error'].
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.base import AdapterResult
from app.graph import build_scraper_graph, get_graph
from app.schemas.error import ErrorCode, ScraperError, Stage
from app.schemas.response import (
    PaymentMethod,
    PriceInfo,
    ProductInfo,
    TokenUsage,
)


def test_graph_compiles():
    """Verifica que el StateGraph se compila sin errores."""
    g = build_scraper_graph()
    assert g is not None


def test_graph_singleton():
    """get_graph() debe devolver el mismo grafo cacheado."""
    g1 = get_graph()
    g2 = get_graph()
    assert g1 is g2


def test_graph_render_mermaid():
    """El grafo expone un diagrama Mermaid valido (para README)."""
    from app.graph import render_mermaid
    diagram = render_mermaid()
    # Debe contener al menos los nombres de nodos clave.
    assert "dispatcher" in diagram
    assert "validator" in diagram


@pytest.mark.asyncio
async def test_graph_unsupported_url_terminates_with_error():
    """URL no soportada: dispatcher setea error y el grafo termina."""
    g = build_scraper_graph()
    result = await g.ainvoke({
        "url": "https://www.linio.com.co/p/algun-producto",
        "country": None,
        "llm_calls": 0,
        "llm_tokens": TokenUsage(),
        "agent_steps": 0,
        "payment_methods": [],
    })
    assert result.get("error") is not None
    err = result["error"]
    assert isinstance(err, ScraperError)
    assert err.code == ErrorCode.UNSUPPORTED_SITE
    assert err.stage == Stage.DISPATCHER


@pytest.mark.asyncio
async def test_graph_falabella_path_invokes_extractor_skips_enricher():
    """Mode='browser' + product completo: extractor SI, enricher NO."""
    fake_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=ProductInfo(title="X", price=PriceInfo(amount=1.0, currency="CLP")),
        payment_methods=[],
        initial_dom="<html>" + "x" * 1000 + "</html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )
    fake_methods = [PaymentMethod(type="credit_card", brand="Visa")]
    fake_usage = TokenUsage(input=1000, output=50)

    g = build_scraper_graph()
    with patch(
        "app.adapters.falabella.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(return_value=(fake_methods, fake_usage)),
    ) as mock_extract, patch(
        "app.agents.product_enricher.enrich_product",
        new=AsyncMock(return_value=(None, TokenUsage())),
    ) as mock_enrich:
        result = await g.ainvoke({
            "url": "https://www.falabella.com/falabella-cl/product/123",
            "country": None,
            "llm_calls": 0,
            "llm_tokens": TokenUsage(),
            "agent_steps": 0,
            "payment_methods": [],
        })

    assert result.get("error") is None
    assert len(result["payment_methods"]) == 1
    assert result["payment_methods"][0].brand == "Visa"
    assert result["llm_calls"] == 1  # solo extractor
    mock_extract.assert_awaited_once()
    mock_enrich.assert_not_awaited()  # product completo, skip


@pytest.mark.asyncio
async def test_graph_falabella_path_invokes_enricher_when_product_incomplete():
    """Mode='browser' + product incompleto: extractor SI, enricher SI."""
    fake_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=None,  # adapter no capturo title/price
        payment_methods=[],
        initial_dom="<html>" + "x" * 1000 + "</html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )
    fake_methods = [PaymentMethod(type="credit_card", brand="Visa")]
    fake_extract_usage = TokenUsage(input=1000, output=50)
    fake_enrich = ProductInfo(title="LLM-enriched", price=PriceInfo(amount=99.0, currency="CLP"))
    fake_enrich_usage = TokenUsage(input=500, output=30)

    g = build_scraper_graph()
    with patch(
        "app.adapters.falabella.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(return_value=(fake_methods, fake_extract_usage)),
    ), patch(
        "app.agents.product_enricher.enrich_product",
        new=AsyncMock(return_value=(fake_enrich, fake_enrich_usage)),
    ) as mock_enrich:
        result = await g.ainvoke({
            "url": "https://www.falabella.com/falabella-cl/product/123",
            "country": None,
            "llm_calls": 0,
            "llm_tokens": TokenUsage(),
            "agent_steps": 0,
            "payment_methods": [],
        })

    assert result.get("error") is None
    assert result["product"].title == "LLM-enriched"
    assert result["llm_calls"] == 2  # extractor + enricher
    mock_enrich.assert_awaited_once()


@pytest.mark.asyncio
async def test_graph_payment_extractor_error_terminates():
    """Si el PaymentExtractor falla (LLM_BUDGET_EXCEEDED), el grafo termina con error."""
    fake_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=ProductInfo(title="X", price=PriceInfo(amount=1.0, currency="CLP")),
        payment_methods=[],
        initial_dom="<html></html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )
    extractor_err = ScraperError(
        code=ErrorCode.LLM_BUDGET_EXCEEDED,
        message="No API key",
        stage=Stage.EXTRACTOR,
    )

    g = build_scraper_graph()
    with patch(
        "app.adapters.falabella.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(side_effect=extractor_err),
    ):
        result = await g.ainvoke({
            "url": "https://www.falabella.com/falabella-cl/product/123",
            "country": None,
            "llm_calls": 0,
            "llm_tokens": TokenUsage(),
            "agent_steps": 0,
            "payment_methods": [],
        })

    assert result.get("error") is not None
    err = result["error"]
    assert isinstance(err, ScraperError)
    assert err.code == ErrorCode.LLM_BUDGET_EXCEEDED
    assert err.stage == Stage.EXTRACTOR


@pytest.mark.asyncio
async def test_graph_validator_rejects_empty_methods():
    """Si despues de todo el flujo no hay payment_methods, validator
    inserta error PARSE_ERROR + stage=validator."""
    fake_result = AdapterResult(
        mode="browser",
        site_id="falabella",
        product=ProductInfo(title="X", price=PriceInfo(amount=1.0, currency="CLP")),
        payment_methods=[],
        initial_dom="<html></html>",
        llm_calls_used=0,
        network_calls=1,
        payment_methods_source="captured_dom",
    )

    g = build_scraper_graph()
    with patch(
        "app.adapters.falabella.FalabellaAdapter.fetch",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.payment_extractor.extract_payment_methods",
        new=AsyncMock(return_value=([], TokenUsage(input=100, output=20))),
    ):
        result = await g.ainvoke({
            "url": "https://www.falabella.com/falabella-cl/product/123",
            "country": None,
            "llm_calls": 0,
            "llm_tokens": TokenUsage(),
            "agent_steps": 0,
            "payment_methods": [],
        })

    assert result.get("error") is not None
    err = result["error"]
    assert err.code == ErrorCode.PARSE_ERROR
    assert err.stage == Stage.VALIDATOR
