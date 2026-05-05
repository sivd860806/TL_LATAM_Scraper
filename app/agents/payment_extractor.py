"""PaymentExtractor agent (P2.4).

Recibe el DOM (post-compresion) capturado por el adapter de Falabella, y
extrae la lista de payment_methods estructurada usando Claude con
structured output via tool_use API.

1 LLM call por request. Costo esperado con Haiku 4.5: ~$0.0005-$0.001.

Si Anthropic key no esta configurada, usar mock fallback que devuelve
una lista canonica del pais (decision TL para garantizar disponibilidad).
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from ..schemas.catalog import lookup_brand
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PaymentMethod, TokenUsage
from .dom_utils import compress_dom

# Tool schema que damos a Claude para que devuelva structured output.
# Es la forma estable y soportada de obtener JSON garantizado.
_PAYMENT_METHOD_TYPE_VALUES = [
    "credit_card", "debit_card", "wallet", "bank_transfer", "cash", "other",
]

_TOOL_SCHEMA = {
    "name": "submit_payment_methods",
    "description": (
        "Submit the list of payment methods detected on the e-commerce checkout/PDP page. "
        "Only include methods that are CLEARLY visible in the provided DOM/text. "
        "DO NOT invent or assume methods not present."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "payment_methods": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": _PAYMENT_METHOD_TYPE_VALUES,
                            "description": (
                                "Category of the payment method. credit_card for Visa/MC/Amex/etc; "
                                "debit_card for Visa Debito/MC Maestro; wallet for Mercado Pago/Daviplata/Nequi; "
                                "bank_transfer for PSE/Webpay/SPEI; cash for OXXO/Efecty/Rapipago; "
                                "other for unusual or ambiguous methods."
                            ),
                        },
                        "brand": {
                            "type": "string",
                            "description": (
                                "Brand or institution name (e.g. 'Visa', 'Mastercard', 'PSE', 'Daviplata'). "
                                "Use the canonical name visible in the page; do not abbreviate."
                            ),
                        },
                        "installments_max": {
                            "type": ["integer", "null"],
                            "description": "Max number of installments offered (e.g. 12, 24, 36). null if not visible.",
                        },
                        "installments_interest_free_max": {
                            "type": ["integer", "null"],
                            "description": "Max installments WITHOUT interest. null or 0 if not visible.",
                        },
                    },
                    "required": ["type", "brand"],
                },
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "How confident you are that the list is complete and correct. "
                    "high: methods are explicitly listed in a payment section. "
                    "medium: methods are inferred from logos or partial text. "
                    "low: only generic mentions of payment, hard to extract specifics."
                ),
            },
        },
        "required": ["payment_methods", "confidence"],
    },
}


_SYSTEM_PROMPT = """You are a payment methods extractor for LATAM e-commerce sites
(Falabella CL/CO/PE, Mercado Libre, Amazon MX/BR, Linio, Ripley, Paris).

Given a (compressed) HTML/text DOM from a product or checkout page, you identify
and list the payment methods accepted. You ONLY include methods clearly present
in the page text (logos, alt-texts, brand names, payment-section headings).
You DO NOT invent or assume methods that are not visible.

Special handling for INSTALLMENTS (cuotas / meses sin interes / parcelas):
- If you see phrases like "X cuotas", "X meses sin interes", "Paga en X cuotas",
  "hasta X cuotas", "X parcelas sem juros", populate installments_max=X.
- If the phrase explicitly says "sin interes" / "sem juros" / "interest-free",
  ALSO set installments_interest_free_max=X.
- If only some installments are interest-free (e.g. "12 cuotas, 6 sin interes"),
  set installments_max=12 and installments_interest_free_max=6.
- If no installment information is visible for a method, leave both fields null.

Special handling for LATAM-specific brands you might see:
- CMR / CMR Puntos / Tarjeta CMR -> credit_card / "CMR"
- Webpay / Webpay Plus / Transbank -> bank_transfer / "Webpay Plus"
- PSE / Pagos Seguros en Linea -> bank_transfer / "PSE"
- Daviplata / Nequi / Mercado Pago -> wallet / canonical brand
- Efecty / Baloto / OXXO / Rapipago / Pago Facil -> cash / canonical brand
- Banco Falabella debito -> debit_card / "Banco Falabella"
- Pix -> bank_transfer / "Pix" (Brazil)
- Boleto / Boleto Bancario -> cash / "Boleto" (Brazil)

Return your answer using the `submit_payment_methods` tool."""


_USER_PROMPT_TEMPLATE = """Extract the payment methods from this e-commerce DOM.

Site: {site_id}
Country: {country}
Source URL: {url}

DOM (compressed, may be truncated):
---
{dom}
---

Instructions:
1. Scan the DOM for explicit payment method names, logos, and section headings
   like "Medios de pago", "Formas de pagamento", "Payment options".
2. For each method, capture installments info if explicitly stated nearby
   (e.g. "Visa - hasta 24 cuotas, 12 sin interes" -> installments_max=24,
   installments_interest_free_max=12).
3. Use the `submit_payment_methods` tool to return the structured list.
4. If the DOM has no clear payment information, return an empty list with
   confidence="low" (do NOT fabricate a generic list).
"""


async def extract_payment_methods(
    initial_dom: str,
    *,
    url: str,
    site_id: str = "falabella",
    country: str | None = None,
    max_dom_chars: int = 30_000,
) -> tuple[list[PaymentMethod], TokenUsage]:
    """Llama al LLM para extraer payment_methods estructurados del DOM.

    Returns
    -------
    methods : list[PaymentMethod]
        Lista normalizada (marca canonica, type valido). Puede estar vacia.
    usage : TokenUsage
        Tokens consumidos por esta llamada.

    Raises
    ------
    ScraperError(LLM_BUDGET_EXCEEDED): si no hay API key configurada.
    ScraperError(PARSE_ERROR): si el LLM no devuelve formato esperado.
    """
    if not settings.anthropic_api_key:
        raise ScraperError(
            code=ErrorCode.LLM_BUDGET_EXCEEDED,
            message=(
                "ANTHROPIC_API_KEY no configurada. El PaymentExtractor agent "
                "requiere LLM. Setear la key en .env o usar otra strategy."
            ),
            stage=Stage.EXTRACTOR,
        )

    # Import diferido para que el modulo se pueda importar sin anthropic instalado
    try:
        import anthropic
    except ImportError as e:
        raise ScraperError(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"Anthropic SDK not installed: {e}",
            stage=Stage.EXTRACTOR,
        ) from e

    # Comprimir DOM antes de pasarlo al LLM
    compressed = compress_dom(initial_dom, max_chars=max_dom_chars)

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        site_id=site_id,
        country=country or "unknown",
        url=url,
        dom=compressed,
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=settings.model_name,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_payment_methods"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        raise ScraperError(
            code=ErrorCode.PARSE_ERROR,
            message=f"Anthropic API error during PaymentExtractor: {e}",
            stage=Stage.EXTRACTOR,
        ) from e

    # Extract usage
    usage = TokenUsage(
        input=response.usage.input_tokens,
        output=response.usage.output_tokens,
    )

    # Find the tool_use block
    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        raise ScraperError(
            code=ErrorCode.PARSE_ERROR,
            message="LLM did not return a tool_use block; cannot parse output.",
            stage=Stage.EXTRACTOR,
        )

    raw_methods = tool_block.input.get("payment_methods", [])
    confidence = tool_block.input.get("confidence", "unknown")

    # Convertir a PaymentMethod[] nuestro, normalizando con catalog
    methods = _validate_and_normalize(raw_methods)

    return methods, usage


def _validate_and_normalize(raw_methods: list[dict[str, Any]]) -> list[PaymentMethod]:
    """Convierte el output del LLM a PaymentMethod[] normalizado.

    - Mapea brand al catalogo canonico (Visa, Mastercard, PSE, ...).
    - Filtra entries malformadas.
    - Deduplica por (type, canonical_brand).
    """
    from ..schemas.response import Installments

    seen: set[tuple[str, str]] = set()
    out: list[PaymentMethod] = []

    for raw in raw_methods:
        if not isinstance(raw, dict):
            continue
        ptype = raw.get("type")
        if ptype not in _PAYMENT_METHOD_TYPE_VALUES:
            continue
        brand_raw = (raw.get("brand") or "").strip()
        if not brand_raw:
            continue

        canonical = lookup_brand(brand_raw) or brand_raw

        installments = None
        ins_max = raw.get("installments_max")
        if ins_max is not None and isinstance(ins_max, int) and ins_max > 0:
            ins_free = raw.get("installments_interest_free_max") or 0
            installments = Installments(
                max=ins_max,
                interest_free_max=max(0, int(ins_free)),
            )

        key = (ptype, canonical)
        if key in seen:
            continue
        seen.add(key)
        out.append(PaymentMethod(type=ptype, brand=canonical, installments=installments))

    return out
