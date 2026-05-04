"""Codigos de error y stages.

Cada error que el sistema puede emitir tiene un codigo y una etapa donde
ocurrio. Esta taxonomia es lo que el evaluador va a inspeccionar primero
si el sistema falla en sus URLs de prueba.

Importante: la lista esta CERRADA — agregar un nuevo codigo requiere
modificar este archivo y bumpar la version del schema.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class ErrorCode(str, Enum):
    """Taxonomia completa de codigos de error que el sistema puede emitir."""

    # ---- Dispatcher ------------------------------------------------------
    INVALID_URL = "INVALID_URL"
    """URL malformada o no parseable."""

    UNSUPPORTED_SITE = "UNSUPPORTED_SITE"
    """Dominio no matchea ningun adapter registrado."""

    # ---- Network / Adapter ----------------------------------------------
    GEO_BLOCKED = "GEO_BLOCKED"
    """Site responde con bloqueo geografico (HTTP 403, mensaje especifico)."""

    OUT_OF_STOCK = "OUT_OF_STOCK"
    """Producto no disponible para compra."""

    # ---- Navigator ------------------------------------------------------
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    """El navigator detecta que el modal de payment requiere login."""

    CHECKOUT_UNREACHABLE = "CHECKOUT_UNREACHABLE"
    """Tras N steps no se llego al modal de payment methods."""

    ANTI_BOT_DETECTED = "ANTI_BOT_DETECTED"
    """Cloudflare challenge, hCaptcha, recaptcha, PerimeterX visible."""

    LLM_BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
    """Mas de MAX_NAVIGATOR_STEPS calls al LLM."""

    # ---- Extractor -------------------------------------------------------
    PARSE_ERROR = "PARSE_ERROR"
    """LLM no produce JSON valido o el output no es de la forma esperada."""

    # ---- Generic --------------------------------------------------------
    TIMEOUT = "TIMEOUT"
    """Excede REQUEST_TIMEOUT_S total."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Excepcion no manejada (fallback). Solo deberia aparecer en bugs."""


class Stage(str, Enum):
    """Etapa del pipeline donde ocurrio el error."""

    DISPATCHER = "dispatcher"
    ADAPTER = "adapter"
    NAVIGATOR = "navigator"
    EXTRACTOR = "extractor"
    VALIDATOR = "validator"


class ErrorDetail(BaseModel):
    """Detalle estructurado de un error para el response."""

    code: ErrorCode = Field(
        ...,
        description="Codigo enumerado que identifica el tipo de error.",
    )
    message: str = Field(
        ...,
        description="Mensaje legible (no para mostrar al usuario final, "
                    "sino para que el reviewer entienda el contexto).",
    )
    stage: Stage = Field(
        ...,
        description="Etapa del pipeline donde ocurrio.",
    )


class ScrapeResponseError(BaseModel):
    """Response cuando el scrape falla."""

    status: str = Field(default="error", frozen=True)
    source_url: HttpUrl
    error: ErrorDetail


class ScraperError(Exception):
    """Excepcion interna que cualquier nodo del graph puede lanzar.

    Lleva el ErrorDetail completo para que el orchestrator lo serialice.
    """

    def __init__(self, code: ErrorCode, message: str, stage: Stage) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        super().__init__(f"[{stage.value}] {code.value}: {message}")

    def to_detail(self) -> ErrorDetail:
        return ErrorDetail(code=self.code, message=self.message, stage=self.stage)
