"""Configuracion global cargada desde el .env via pydantic-settings.

Una sola fuente de verdad para todas las variables de entorno. Si algo
cambia en el .env, cambia aca y se propaga.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings cargados desde .env y/o variables de entorno."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM provider ----------------------------------------------------
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key. Si esta vacio, los agentes LLM se desactivan."
    )
    model_name: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Modelo de Anthropic a usar."
    )

    # ---- Caps operacionales ---------------------------------------------
    max_navigator_steps: int = Field(default=6, ge=1, le=20)
    request_timeout_s: int = Field(default=60, ge=10, le=300)
    playwright_default_timeout_ms: int = Field(default=15_000, ge=1000)

    # ---- Debug ----------------------------------------------------------
    dump_falabella_dom: bool = Field(
        default=False,
        description="Si True, dumpear el DOM capturado del checkout a /tmp/ para inspeccion."
    )

    # ---- Logging ---------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # ---- Server ----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- Ollama (opcional, swap local) ----------------------------------
    ollama_base_url: str | None = None
    ollama_model: str | None = None


# Singleton — instanciado una sola vez al importar el modulo
settings = Settings()


def is_llm_configured() -> bool:
    """True si tenemos credenciales para usar Anthropic o Ollama."""
    return bool(settings.anthropic_api_key) or bool(settings.ollama_base_url)
