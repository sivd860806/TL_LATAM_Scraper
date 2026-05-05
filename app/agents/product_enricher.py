"""ProductEnricher agent (P2.4).

Solo se invoca cuando el adapter de Falabella NO consiguio extraer title o
price con sus selectores estaticos (porque Falabella cambio el layout). En
ese caso, le pasamos el DOM crudo al LLM para que extraiga lo que falte.

Responsabilidad distinta a la del PaymentExtractor:
  - PaymentExtractor: mira metodos de pago.
  - ProductEnricher: mira info del producto (titulo, precio, moneda).

Cumple "2 agents with distinct, well-defined responsibilities" del enunciado.

Costo esperado: 1 LLM call por request (solo si hace falta).
Si el adapter ya tiene title+price, este agent NO se invoca (ahorro).
"""
from __future__ import annotations

from ..config import settings
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PriceInfo, ProductInfo, TokenUsage
from .dom_utils import compress_dom

_TOOL_SCHEMA = {
    "name": "submit_product_info",
    "description": (
        "Submit the structured product info (title, price, currency) extracted from the page. "
        "Only extract data CLEARLY visible. Return null fields if not present."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": ["string", "null"],
                "description": "Product title as visible on the page (e.g. 'Apple iPhone 15 128GB Negro').",
            },
            "price_amount": {
                "type": ["number", "null"],
                "description": "Numeric price (e.g. 1599999.0). Use the main displayed price, not crossed-out ones.",
            },
            "currency": {
                "type": ["string", "null"],
                "description": "ISO 4217 code (CLP, COP, MXN, ARS, BRL, PEN). Infer from the page context if not explicit.",
            },
        },
        "required": ["title", "price_amount", "currency"],
    },
}

_SYSTEM_PROMPT = """You are a product info extractor for LATAM e-commerce.

Given a (compressed) HTML/text DOM, you extract the product title, the
displayed price (numeric), and the currency code. You ONLY extract what is
clearly present in the page.

Return your answer using the `submit_product_info` tool.
If a field is not visible or is ambiguous, return null."""


_USER_PROMPT_TEMPLATE = """Extract the product info from this page DOM.

Source URL: {url}
Country hint: {country}

DOM (compressed):
---
{dom}
---

Use the `submit_product_info` tool. Return null for any field not clearly visible.
"""


async def enrich_product(
    initial_dom: str,
    *,
    url: str,
    current: ProductInfo | None = None,
    country: str | None = None,
    max_dom_chars: int = 20_000,
) -> tuple[ProductInfo | None, TokenUsage]:
    """Si current ya tiene title+price, NO invoca al LLM (ahorro).

    Returns
    -------
    product : ProductInfo | None
        El producto enriquecido, o el current sin cambios si el LLM no aporto nada.
    usage : TokenUsage
        Tokens consumidos. (0,0) si no se invoco al LLM.
    """
    # Skip si ya tenemos toda la info
    if current and current.title and current.price:
        return current, TokenUsage(input=0, output=0)

    if not settings.anthropic_api_key:
        # Sin LLM disponible: devolver current tal como esta (no rompemos)
        return current, TokenUsage(input=0, output=0)

    try:
        import anthropic
    except ImportError as e:
        raise ScraperError(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"Anthropic SDK not installed: {e}",
            stage=Stage.EXTRACTOR,
        ) from e

    compressed = compress_dom(initial_dom, max_chars=max_dom_chars)
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        url=url,
        country=country or "unknown",
        dom=compressed,
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=settings.model_name,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_product_info"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        # Si el LLM falla, devolvemos current como fallback (no rompemos request)
        return current, TokenUsage(input=0, output=0)

    usage = TokenUsage(
        input=response.usage.input_tokens,
        output=response.usage.output_tokens,
    )
    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        return current, usage

    title = tool_block.input.get("title")
    price_amount = tool_block.input.get("price_amount")
    currency = tool_block.input.get("currency")

    # Merge: usar lo del LLM solo si current no lo tenia
    final_title = (current.title if current and current.title else title) or title
    final_price = current.price if (current and current.price) else None
    if final_price is None and price_amount is not None and currency:
        try:
            final_price = PriceInfo(amount=float(price_amount), currency=currency.upper())
        except (ValueError, TypeError):
            final_price = None

    if not final_title and not final_price:
        return current, usage

    return ProductInfo(title=final_title, price=final_price), usage
