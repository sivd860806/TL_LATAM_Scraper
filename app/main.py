"""FastAPI entry point.

P2.0: bootstrap minimo solo /health y placeholder de /scrape.
P2.1: schemas reales + endpoint /scrape valida request.
P2.2: dispatcher + ML adapter via API publica funcional (0 LLM calls).
P2.3+: Falabella adapter + agentes LLM.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .adapters.mercadolibre import MercadoLibreAdapter
from .config import is_llm_configured, settings
from .dispatcher import SITE_FALABELLA, SITE_MERCADOLIBRE, resolve_site
from .logging import configure_logging, get_logger, new_correlation_id
from .schemas.error import ErrorCode, ScrapeResponseError, ScraperError, Stage
from .schemas.request import ScrapeRequest
from .schemas.response import ResponseMetadata, ScrapeResponseSuccess

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    logger.info(
        "app.startup",
        version=__version__,
        model=settings.model_name,
        llm_configured=is_llm_configured(),
        max_navigator_steps=settings.max_navigator_steps,
    )
    yield
    logger.info("app.shutdown")


app = FastAPI(
    title="TL LATAM Scraper",
    description="Multi-Agent Scraping System for LATAM E-commerce.",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = new_correlation_id()
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.exception_handler(ScraperError)
async def handle_scraper_error(request: Request, exc: ScraperError) -> JSONResponse:
    payload = ScrapeResponseError(
        source_url="https://unknown.local",
        error=exc.to_detail(),
    ).model_dump(mode="json")
    status_code = _http_status_for(exc.code)
    logger.warning("scraper.error", code=exc.code.value, stage=exc.stage.value, message=exc.message)
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "source_url": "https://invalid.local",
            "error": {
                "code": ErrorCode.INVALID_URL.value,
                "message": f"Request validation failed: {exc.errors()}",
                "stage": Stage.DISPATCHER.value,
            },
        },
    )


def _http_status_for(code: ErrorCode) -> int:
    return {
        ErrorCode.INVALID_URL: 400,
        ErrorCode.UNSUPPORTED_SITE: 400,
        ErrorCode.OUT_OF_STOCK: 404,
        ErrorCode.LOGIN_REQUIRED: 422,
        ErrorCode.GEO_BLOCKED: 451,
        ErrorCode.CHECKOUT_UNREACHABLE: 502,
        ErrorCode.ANTI_BOT_DETECTED: 502,
        ErrorCode.PARSE_ERROR: 502,
        ErrorCode.TIMEOUT: 504,
        ErrorCode.LLM_BUDGET_EXCEEDED: 429,
        ErrorCode.INTERNAL_ERROR: 500,
    }.get(code, 500)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "llm_configured": is_llm_configured(),
        "model": settings.model_name if is_llm_configured() else None,
    }


@app.post(
    "/scrape",
    tags=["scrape"],
    response_model=ScrapeResponseSuccess,
    responses={
        400: {"model": ScrapeResponseError},
        404: {"model": ScrapeResponseError},
        422: {"model": ScrapeResponseError},
        451: {"model": ScrapeResponseError},
        500: {"model": ScrapeResponseError},
        502: {"model": ScrapeResponseError},
        504: {"model": ScrapeResponseError},
    },
)
async def scrape(payload: ScrapeRequest) -> ScrapeResponseSuccess:
    t0 = time.perf_counter()
    url_str = str(payload.url)
    logger.info("scrape.received", url=url_str, country=payload.country,
                force_agents=payload.options.force_agents)

    site_id = resolve_site(url_str)
    if site_id is None:
        logger.warning("dispatcher.unsupported", url=url_str)
        raise ScraperError(
            code=ErrorCode.UNSUPPORTED_SITE,
            message="No SiteAdapter registered for URL. Supported: Mercado Libre and Falabella.",
            stage=Stage.DISPATCHER,
        )
    logger.info("dispatcher.resolved", site_id=site_id)

    if site_id == SITE_MERCADOLIBRE:
        adapter = MercadoLibreAdapter()
        result = await adapter.fetch(url_str, country=payload.country)
    elif site_id == SITE_FALABELLA:
        raise ScraperError(
            code=ErrorCode.UNSUPPORTED_SITE,
            message="Falabella adapter scaffolded; will be activated in P2.3 with Playwright.",
            stage=Stage.ADAPTER,
        )
    else:
        raise ScraperError(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"Unhandled site_id: {site_id}",
            stage=Stage.DISPATCHER,
        )

    duration_ms = int((time.perf_counter() - t0) * 1000)
    response = ScrapeResponseSuccess(
        source_url=payload.url,
        site=result.site_id,
        product=result.product,
        payment_methods=result.payment_methods,
        metadata=ResponseMetadata(
            duration_ms=duration_ms,
            agent_steps=2,
            llm_calls=result.llm_calls_used,
            payment_methods_source=result.payment_methods_source,
        ),
    )
    logger.info(
        "scrape.success",
        site=result.site_id,
        n_payment_methods=len(result.payment_methods),
        duration_ms=duration_ms,
        llm_calls=result.llm_calls_used,
        payment_methods_source=result.payment_methods_source,
    )
    return response
