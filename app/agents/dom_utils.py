"""Utilidades para comprimir DOM antes de pasarlo al LLM.

Problema: el DOM crudo de Falabella es ~1.6 MB (verificado en runtime).
Eso es ~400k tokens de Claude. El context window de Haiku 4.5 es 200k pero
queremos mantenernos por debajo de 30k tokens (~120 KB) para:
  - controlar costo (~$0.0005 por LLM call)
  - reducir latencia (less tokens to process)
  - no saturar el context con HTML basura (scripts, SVGs inline, etc.)

Estrategia de compresion:
  1. Strip <script>, <style>, <svg>, <noscript>, <link>, <meta>
     (excepto og:price y similar que tienen info del producto).
  2. Strip comments HTML.
  3. Strip atributos extra (style, class largas, data-*) -- mantenemos
     solo los importantes (id, href, src, role, aria-label).
  4. Colapsar whitespace.
  5. Si despues de todo eso el HTML aun es > max_chars, truncar al fragmento
     que contenga keywords financieros (Visa, PSE, Mastercard, etc.).
"""
from __future__ import annotations

import re
from typing import Iterable

# Keywords que sugieren que un fragmento del DOM contiene info de payment.
# Si tenemos que truncar, priorizamos fragmentos con estos terminos.
PAYMENT_KEYWORDS = (
    "pago", "tarjeta", "credito", "credito", "debito", "debito",
    "cuotas", "cuota", "meses sin interes", "interes",
    "visa", "mastercard", "amex", "american express", "diners",
    "pse", "efecty", "baloto", "daviplata", "nequi",
    "webpay", "khipu", "servipag", "mach", "redcompra",
    "mercado pago", "mercadopago",
    "oxxo", "spei", "kueski",
    "rapipago", "pago facil", "pagofacil",
    "pix", "boleto",
    "transferencia", "deposito",
    "checkout", "comprar", "pagar",
    "$", "cop", "clp", "mxn", "ars", "brl", "pen", "uyu",
)

ATTRS_TO_KEEP = {"id", "href", "src", "role", "aria-label", "data-name", "data-price",
                  "data-internet-price", "name", "content", "property", "type"}


def _strip_tags_completely(html: str, tag_names: Iterable[str]) -> str:
    """Remueve <tag>...</tag> completamente (incluyendo el contenido)."""
    for t in tag_names:
        html = re.sub(rf"<{t}\b[^>]*>.*?</{t}>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Self-closing
        html = re.sub(rf"<{t}\b[^>]*/?>", "", html, flags=re.IGNORECASE)
    return html


def _strip_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _strip_unimportant_attrs(html: str) -> str:
    """Remueve atributos largos que no aportan info de pago.

    Conservamos solo los attrs en ATTRS_TO_KEEP. El resto los borramos.
    Heuristica simple basada en regex (no parser real para mantener velocidad).
    """
    def _process_tag(match):
        tag_open = match.group(0)
        # Encontrar el nombre del tag
        tag_name_m = re.match(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)", tag_open)
        if not tag_name_m:
            return tag_open
        is_closing = tag_name_m.group(1) == "/"
        tag_name = tag_name_m.group(2)
        if is_closing:
            return f"</{tag_name}>"
        # Extraer y filtrar attrs
        attrs_part = tag_open[tag_name_m.end():].rstrip(">").rstrip("/")
        kept = []
        for attr_match in re.finditer(
            r'([a-zA-Z][a-zA-Z0-9-_]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
            attrs_part,
        ):
            name = attr_match.group(1).lower()
            val = attr_match.group(3) or attr_match.group(4) or attr_match.group(5) or ""
            if name in ATTRS_TO_KEEP:
                kept.append(f'{name}="{val[:200]}"')  # truncar valor largo
        attrs_str = (" " + " ".join(kept)) if kept else ""
        return f"<{tag_name}{attrs_str}>"

    return re.sub(r"<[^>]+>", _process_tag, html)


def _collapse_whitespace(html: str) -> str:
    html = re.sub(r"\s+", " ", html)
    html = re.sub(r">\s+<", "><", html)
    return html.strip()


def compress_dom(html: str, max_chars: int = 30_000) -> str:
    """Comprime el DOM HTML para pasarlo al LLM.

    Pipeline:
      1. Strip tags pesados (script, style, svg, noscript, link)
      2. Strip comments
      3. Filtrar atributos a los importantes
      4. Colapsar whitespace
      5. Si excede max_chars, truncar al fragmento con mas keywords de pago.

    Parameters
    ----------
    html : str
        HTML crudo (el `initial_dom` del adapter).
    max_chars : int
        Cap del output. 30_000 chars ~ 7.5k tokens ~ $0.0005 con Haiku.
    """
    h = html

    # 1) Strip tags pesados
    h = _strip_tags_completely(h, [
        "script", "style", "svg", "noscript", "link", "iframe",
    ])

    # 2) Comments
    h = _strip_comments(h)

    # 3) Atributos
    h = _strip_unimportant_attrs(h)

    # 4) Whitespace
    h = _collapse_whitespace(h)

    # 5) Truncate si es muy largo
    if len(h) > max_chars:
        h = _truncate_with_keywords(h, max_chars)

    return h


def _truncate_with_keywords(html: str, max_chars: int) -> str:
    """Si el HTML excede max_chars, elegimos la ventana de max_chars que
    contenga MAS keywords de pago.

    Estrategia simple: dividir el HTML en bloques de max_chars,
    contar keywords en cada bloque, devolver el bloque con mas keywords.
    """
    if len(html) <= max_chars:
        return html

    # Strategy: sliding window de max_chars con stride de max_chars/2
    stride = max_chars // 2
    best_block = html[:max_chars]
    best_score = _count_keywords(best_block)

    for start in range(stride, len(html) - stride, stride):
        end = min(start + max_chars, len(html))
        block = html[start:end]
        score = _count_keywords(block)
        if score > best_score:
            best_score = score
            best_block = block

    return best_block


def _count_keywords(text: str) -> int:
    """Cuenta cuantos PAYMENT_KEYWORDS aparecen en el texto (case-insensitive)."""
    text_lower = text.lower()
    return sum(text_lower.count(k) for k in PAYMENT_KEYWORDS)


def estimate_tokens(text: str) -> int:
    """Estimacion grosera de tokens: 1 token ~= 4 chars en ingles, 3 en espanol con HTML."""
    return len(text) // 3
