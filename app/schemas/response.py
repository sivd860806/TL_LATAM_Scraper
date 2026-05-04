"""Schemas del response al endpoint POST /scrape (caso exitoso).

Contrato basado en la seccion 3 del PDF — "Successful response (200)".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

# -----------------------------------------------------------------------------
# Tipos de payment method (taxonomia cerrada)
# -----------------------------------------------------------------------------
PaymentType = Literal[
    "credit_card",   # Visa, Mastercard, Amex, Diners, etc. con cuotas
    "debit_card",    # debito de cualquier marca
    "wallet",        # Mercado Pago, Daviplata, Nequi, RappiPay, MACH, etc.
    "bank_transfer", # PSE (CO), Webpay (CL), SPEI (MX), etc.
    "cash",          # Efecty (CO), OXXO (MX), Servipag (CL), Rapipago (AR), etc.
    "other",         # fallback explicito si el LLM duda
]


class Installments(BaseModel):
    """Plan de cuotas (o 'meses sin intereses' / 'parcelado').

    Los campos pueden faltar si el site no muestra esta info.
    """

    max: int = Field(
        ...,
        ge=1,
        description="Numero maximo de cuotas en las que se puede dividir el pago.",
    )
    interest_free_max: int = Field(
        default=0,
        ge=0,
        description="Maximo de cuotas SIN interes. 0 si todas las cuotas tienen interes.",
    )


class PaymentMethod(BaseModel):
    """Un metodo de pago detectado en el checkout."""

    type: PaymentType
    brand: str = Field(
        ...,
        description="Marca canonica (Visa, Mastercard, PSE, Mercado Pago, OXXO, etc.).",
        examples=["Visa", "Mastercard", "PSE", "Mercado Pago"],
    )
    installments: Installments | None = Field(
        default=None,
        description="Solo se completa para credit_card cuando el site lo muestra.",
    )


class PriceInfo(BaseModel):
    """Precio del producto."""

    amount: float = Field(..., ge=0, description="Monto en la divisa local.")
    currency: str = Field(
        ...,
        pattern=r"^[A-Z]{3}$",
        description="Codigo ISO 4217 (ARS, CLP, MXN, COP, BRL, PEN, USD).",
        examples=["ARS", "CLP", "COP"],
    )


class ProductInfo(BaseModel):
    """Info opcional del producto extraida durante la navegacion."""

    title: str | None = None
    price: PriceInfo | None = None


class TokenUsage(BaseModel):
    """Tokens consumidos por el LLM (in/out)."""

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input + self.output


class ResponseMetadata(BaseModel):
    """Metricas operacionales del request — el evaluador las lee."""

    duration_ms: int = Field(..., ge=0, description="Latencia total del request.")
    agent_steps: int = Field(
        default=0,
        ge=0,
        description="Numero total de steps del LangGraph (incluye nodos no-LLM).",
    )
    llm_calls: int = Field(
        default=0,
        ge=0,
        description="Numero total de llamadas al LLM. "
                    "Para Mercado Libre via API publica esto es 0.",
    )
    llm_tokens: TokenUsage = Field(default_factory=TokenUsage)


class ScrapeResponseSuccess(BaseModel):
    """Response cuando el scrape fue exitoso."""

    status: str = Field(default="ok", frozen=True)
    source_url: HttpUrl
    site: str = Field(
        ...,
        description="Site identificado por el dispatcher.",
        examples=["mercadolibre", "falabella"],
    )
    product: ProductInfo | None = None
    payment_methods: list[PaymentMethod] = Field(
        ...,
        min_length=1,
        description="Metodos de pago detectados, normalizados por el Validator.",
    )
    metadata: ResponseMetadata
