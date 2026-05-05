"""Configuracion de structlog con correlation_id por request."""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from uuid import uuid4

import structlog

from .config import settings

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def _add_correlation_id(_logger, _method_name, event_dict):
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging() -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
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
    return structlog.get_logger(name)


def new_correlation_id() -> str:
    cid = uuid4().hex[:12]
    correlation_id_var.set(cid)
    return cid
