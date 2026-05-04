"""Schemas del request al endpoint POST /scrape.

Contrato basado en la seccion 3 del PDF (tech_lead_assessment.pdf).
"""
from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class ScrapeOptions(BaseModel):
    """Opciones del request, todas opcionales con defaults razonables."""

    extract_title: bool = Field(
        default=True,
        description="Si el adapter debe extraer el titulo del producto.",
    )
    extract_price: bool = Field(
        default=True,
        description="Si el adapter debe extraer el precio del producto.",
    )
    timeout_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Timeout total del request. Despues se devuelve TIMEOUT.",
    )
    force_agents: bool = Field(
        default=False,
        description=(
            "Si True, bypassea el atajo deterministico de Mercado Libre "
            "(API publica) y fuerza el flujo Navigator + Extractor con LLM. "
            "Util para auditoria del sistema agentico end-to-end."
        ),
    )


class ScrapeRequest(BaseModel):
    """Body del POST /scrape."""

    url: HttpUrl = Field(
        ...,
        description="URL del producto en el e-commerce target.",
        examples=[
            "https://articulo.mercadolibre.com.ar/MLA-1234567890",
            "https://www.falabella.com/falabella-cl/product/1234567890",
        ],
    )
    country: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description="Codigo ISO de pais (AR, CL, CO, MX, BR, PE). Opcional; "
                    "si se omite se infiere del dominio.",
        examples=["AR", "CL"],
    )
    options: ScrapeOptions = Field(default_factory=ScrapeOptions)
