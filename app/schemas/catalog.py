"""Catalogo canonico de marcas de payment methods en LATAM.

Fuente unica de verdad para normalizacion de nombres. Cualquier alias
detectado por el LLM se mapea a la forma canonica antes de devolverse.

Esta tabla se va a expandir con cada site nuevo soportado. Por ahora
cubre los metodos mas comunes en AR, CL, CO, MX, BR y PE.
"""
from __future__ import annotations

# Map de alias_lowercase -> nombre canonico
# Todas las claves estan normalizadas: lowercase, sin tildes, espacios colapsados
CANONICAL_BRANDS: dict[str, str] = {
    # ---- Tarjetas de credito/debito internacionales ----
    "visa": "Visa",
    "visa credito": "Visa",
    "visa debito": "Visa",
    "visa debit": "Visa",
    "tarjeta visa": "Visa",
    "mastercard": "Mastercard",
    "master card": "Mastercard",
    "mc": "Mastercard",
    "amex": "American Express",
    "american express": "American Express",
    "diners": "Diners Club",
    "diners club": "Diners Club",
    "discover": "Discover",

    # ---- Wallets globales/regionales ----
    "mercado pago": "Mercado Pago",
    "mercadopago": "Mercado Pago",
    "mp": "Mercado Pago",
    "paypal": "PayPal",
    "pago efectivo": "PagoEfectivo",
    "pagoefectivo": "PagoEfectivo",

    # ---- Argentina ----
    "naranja": "Tarjeta Naranja",
    "tarjeta naranja": "Tarjeta Naranja",
    "cabal": "Cabal",
    "ahora 12": "Ahora 12",
    "ahora 18": "Ahora 18",
    "rapipago": "Rapipago",
    "pagofacil": "Pago Fácil",
    "pago facil": "Pago Fácil",

    # ---- Chile ----
    "webpay": "Webpay Plus",
    "webpay plus": "Webpay Plus",
    "transbank": "Webpay Plus",
    "redcompra": "Redcompra",
    "red compra": "Redcompra",
    "khipu": "Khipu",
    "servipag": "Servipag",
    "mach": "MACH",
    "onepay": "Onepay",

    # ---- Colombia ----
    "pse": "PSE",
    "efecty": "Efecty",
    "baloto": "Baloto",
    "daviplata": "Daviplata",
    "nequi": "Nequi",
    "bancolombia": "Bancolombia",

    # ---- Mexico ----
    "oxxo": "OXXO",
    "spei": "SPEI",
    "kueski pay": "Kueski Pay",
    "mercado pago mx": "Mercado Pago",
    "paynet": "Paynet",
    "7 eleven": "7-Eleven",

    # ---- Brasil ----
    "pix": "Pix",
    "boleto": "Boleto",
    "boleto bancario": "Boleto",
    "elo": "Elo",
    "hipercard": "Hipercard",
    "cielo": "Cielo",

    # ---- Peru ----
    "yape": "Yape",
    "plin": "Plin",
    "tunki": "Tunki",
    "lukita": "Lukita",
    "billetera bcp": "Yape",
}


def normalize_brand_key(raw: str) -> str:
    """Normaliza una cadena para usar como key del catalogo.

    Reglas:
    - lowercase
    - quita tildes (o -> o, a -> a, etc.)
    - colapsa espacios
    - quita prefijos comunes ('tarjeta de credito ', 'pago con ', etc.)
    """
    s = raw.lower().strip()
    s = s.replace("á", "a").replace("é", "e").replace("í", "i") \
         .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    s = " ".join(s.split())
    # quitar prefijos comunes
    for prefix in (
        "tarjeta de credito ", "tarjeta de debito ",
        "tarjeta de credito/debito ", "tarjeta ",
        "pago con ", "transferencia ", "deposito ",
    ):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def lookup_brand(raw: str) -> str | None:
    """Busca una marca en el catalogo. Devuelve nombre canonico o None."""
    key = normalize_brand_key(raw)
    return CANONICAL_BRANDS.get(key)
