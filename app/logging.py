"""Configuracion de structlog con correlation_id por request.

Decisiones:
  - structlog para logs estructurados sin pelearse con stdlib logging
  - JSON output en produccion, console en desarrollo
  - correlation_id se inyecta en el contexto al inicio de cada request
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from uuid import uuid4

import structlog

from .config import settings

# correlation_id por request — accesible desde cualquier lugar del codigo
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def _add_correlation_id(_logger, _method_name, event_dict):
    """Procesador que injecta correlation_id en cada log entry."""
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging() -> None:
    """Inicializa structlog. Llamar una vez al arranque del app."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _add_correlation_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    """Helper estandar para obtener un logger desde cualquier modulo."""
    return structlog.get_logger(name)


def new_correlation_id() -> str:
    """Genera un nuevo correlation_id (typicamente al inicio de un request)."""
    cid = uuid4().hex[:12]
    correlation_id_var.set(cid)
    return cid
