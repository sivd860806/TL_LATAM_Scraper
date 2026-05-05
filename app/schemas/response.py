"""Schemas del response al endpoint POST /scrape (caso exitoso).

Contrato basado en la seccion 3 del PDF -- "Successful response (200)".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

PaymentType = Literal[
    "credit_card",
    "debit_card",
    "wallet",
    "bank_transfer",
    "cash",
    "other",
]


class Installments(BaseModel):
    max: int = Field(..., ge=1, description="Numero maximo de cuotas.")
    interest_free_max: int = Field(default=0, ge=0,
        description="Maximo de cuotas SIN interes. 0 si todas tienen interes.")


class PaymentMethod(BaseModel):
    type: PaymentType
    brand: str = Field(..., description="Marca canonica (Visa, Mastercard, PSE, ...).")
    installments: Installments | None = None


class PriceInfo(BaseModel):
    amount: float = Field(..., ge=0)
    currency: str = Field(..., pattern=r"^[A-Z]{3}$",
        description="Codigo ISO 4217 (ARS, CLP, MXN, COP, BRL, PEN, USD).")


class ProductInfo(BaseModel):
    title: str | None = None
    price: PriceInfo | None = None


class TokenUsage(BaseModel):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input + self.output


class ResponseMetadata(BaseModel):
    """Metricas operacionales del request -- el evaluador las lee."""
    duration_ms: int = Field(..., ge=0)
    agent_steps: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0,
        description="Para Mercado Libre via API publica esto es 0.")
    llm_tokens: TokenUsage = Field(default_factory=TokenUsage)
    payment_methods_source: str = Field(
        default="site_catalog",
        description=(
            "Granularidad de los payment_methods devueltos: "
            "'site_catalog' (catalogo del site, no filtrado por item), "
            "'item_specific' (filtrado por seller/item, mas preciso), "
            "'captured_dom' (extraido del DOM del PDP via LLM, preview pre-login), "
            "'captured_checkout_dom' (extraido del DOM del checkout via LLM, "
            "lista completa post add-to-cart)."
        ),
    )


class ScrapeResponseSuccess(BaseModel):
    """Response cuando el scrape fue exitoso."""
    status: str = Field(default="ok", frozen=True)
    source_url: HttpUrl
    site: str = Field(..., examples=["mercadolibre", "falabella"])
    product: ProductInfo | None = None
    payment_methods: list[PaymentMethod] = Field(..., min_length=1)
    metadata: ResponseMetadata
