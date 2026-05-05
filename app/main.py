"""FastAPI entry point.

P2.0: bootstrap minimo solo /health y placeholder de /scrape.
P2.1: schemas reales + endpoint /scrape valida request.
P2.2: dispatcher + ML adapter via API publica funcional (0 LLM calls).
P2.3+: Falabella adapter + agentes LLM.
P2.5: refactor a LangGraph state machine (orquestacion declarativa).
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import is_llm_configured, settings
from .graph import get_graph
from .logging import configure_logging, get_logger, new_correlation_id
from .schemas.error import ErrorCode, ScrapeResponseError, ScraperError, Stage
from .schemas.request import ScrapeRequest
from .schemas.response import ResponseMetadata, ScrapeResponseSuccess, TokenUsage

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    # Compilar el grafo al startup (en lugar de lazy en el primer request)
    # — error temprano si hay un problema de import.
    try:
        get_graph()
        graph_ok = True
    except Exception as e:
        logger.error("app.startup.graph_compile_failed", error=str(e))
        graph_ok = False

    logger.info(
        "app.startup",
        version=__version__,
        model=settings.model_name,
        llm_configured=is_llm_configured(),
        max_navigator_steps=settings.max_navigator_steps,
        graph_compiled=graph_ok,
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
    logger.warning(
        "scraper.error",
        code=exc.code.value,
        stage=exc.stage.value,
        message=exc.message,
    )
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
    """Endpoint principal: orquesta el pipeline via LangGraph.

    Flujo:
      1. Construye el initial_state con url + country.
      2. Invoca el grafo (singleton compilado).
      3. Si el grafo termino con state['error'], re-raise el ScraperError
         para que el exception_handler lo convierta a JSON estructurado.
      4. Sino, arma el ScrapeResponseSuccess con final_state.
    """
    t0 = time.perf_counter()
    url_str = str(payload.url)
    logger.info(
        "scrape.received",
        url=url_str,
        country=payload.country,
        force_agents=payload.options.force_agents,
    )

    graph = get_graph()
    initial_state = {
        "url": url_str,
        "country": payload.country,
        "llm_calls": 0,
        "llm_tokens": TokenUsage(input=0, output=0),
        "agent_steps": 0,
        "payment_methods": [],
    }

    final_state = await graph.ainvoke(initial_state)

    # Si el grafo termino con error, propagarlo al exception_handler.
    err = final_state.get("error")
    if err is not None:
        if isinstance(err, ScraperError):
            raise err
        # Defensa: si llego algo raro, lo envolvemos.
        raise ScraperError(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"Unexpected error in graph: {err!r}",
            stage=Stage.DISPATCHER,
        )

    duration_ms = int((time.perf_counter() - t0) * 1000)
    response = ScrapeResponseSuccess(
        source_url=payload.url,
        site=final_state.get("site_id", "unknown"),
        product=final_state.get("product"),
        payment_methods=final_state.get("payment_methods", []),
        metadata=ResponseMetadata(
            duration_ms=duration_ms,
            agent_steps=final_state.get("agent_steps", 0),
            llm_calls=final_state.get("llm_calls", 0),
            llm_tokens=final_state.get("llm_tokens", TokenUsage()),
            payment_methods_source=final_state.get(
                "payment_methods_source", "site_catalog"
            ),
        ),
    )
    logger.info(
        "scrape.success",
        site=response.site,
        n_payment_methods=len(response.payment_methods),
        duration_ms=duration_ms,
        llm_calls=response.metadata.llm_calls,
        agent_steps=response.metadata.agent_steps,
        payment_methods_source=response.metadata.payment_methods_source,
    )
    return response
