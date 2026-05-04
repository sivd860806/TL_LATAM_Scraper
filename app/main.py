"""FastAPI entry point.

P2.0: bootstrap minimo — solo /health y placeholder de /scrape.
P2.1: schemas reales + endpoint /scrape valida request y responde con
      shape correcto (todavia con datos placeholder; la logica del
      dispatcher/adapters/agentes llega en P2.2+).
P2.2+: dispatcher + adapters + agentes.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import is_llm_configured, settings
from .logging import configure_logging, get_logger, new_correlation_id
from .schemas.error import (
    ErrorCode,
    ErrorDetail,
    ScrapeResponseError,
    ScraperError,
    Stage,
)
from .schemas.request import ScrapeRequest
from .schemas.response import ResponseMetadata, ScrapeResponseSuccess

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Setup y teardown del app."""
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
    description=(
        "Multi-Agent Scraping System for LATAM E-commerce. "
        "Extracts payment methods at checkout from product URLs. "
        "Supports Mercado Libre (via public API) and Falabella (via Playwright + LLM agents)."
    ),
    version=__version__,
    lifespan=lifespan,
)


# -----------------------------------------------------------------------------
# Middleware: correlation_id por request
# -----------------------------------------------------------------------------
@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    """Inyecta correlation_id en cada request para tracing."""
    cid = new_correlation_id()
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


# -----------------------------------------------------------------------------
# Manejo global de errores — convertir a ScrapeResponseError
# -----------------------------------------------------------------------------
@app.exception_handler(ScraperError)
async def handle_scraper_error(request: Request, exc: ScraperError) -> JSONResponse:
    """Errores levantados por nodos del graph se convierten a 4xx/5xx limpio."""
    body = request.scope.get("body_url") or "unknown"
    payload = ScrapeResponseError(
        source_url=body if body.startswith("http") else "https://unknown.local",
        error=exc.to_detail(),
    ).model_dump(mode="json")
    status_code = _http_status_for(exc.code)
    logger.warning(
        "scraper.error",
        code=exc.code.value,
        stage=exc.stage.value,
        message=exc.message,
    )
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 de Pydantic se convierte a INVALID_URL con shape consistente."""
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
    """Mapea ErrorCode a HTTP status code."""
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


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/health", tags=["health"])
async def health() -> dict:
    """Health check rapido. Devuelve estado del servicio."""
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
    """Extrae los metodos de pago del checkout para una URL de producto.

    En P2.1 el endpoint solo valida el request y devuelve un placeholder
    consistente con el contrato. La implementacion real (dispatcher +
    adapters + agentes) llega en P2.2+.
    """
    t0 = time.perf_counter()
    logger.info(
        "scrape.received",
        url=str(payload.url),
        country=payload.country,
        force_agents=payload.options.force_agents,
    )

    # NOTA P2.1: aun no tenemos dispatcher/adapters; emitimos UNSUPPORTED_SITE
    # con un mensaje claro para que el reviewer/test sepa que estamos en bootstrap.
    raise ScraperError(
        code=ErrorCode.UNSUPPORTED_SITE,
        message=(
            "Endpoint validates the request shape correctly, but the dispatcher "
            "and site adapters are not implemented yet. This will be fixed in P2.2."
        ),
        stage=Stage.DISPATCHER,
    )
