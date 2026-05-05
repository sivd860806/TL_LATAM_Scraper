"""LangGraph state machine para el pipeline de scraping (P2.5).

Define el grafo:

  START -> dispatcher
  dispatcher -> {adapter_ml, adapter_falabella, END(error)}
  adapter_ml -> {validator, END(error)}             (mode='direct', skip LLM)
  adapter_falabella -> {payment_extractor, END(error)} (mode='browser')
  payment_extractor -> {validator, product_enricher, END(error)}
  product_enricher -> validator
  validator -> END

Decisiones TL:

1. **State como TypedDict, no Pydantic BaseModel**. LangGraph hace merges
   parciales por key, y TypedDict es la forma idiomatica. Las field con
   tipos Pydantic (PaymentMethod, ProductInfo, TokenUsage) viven adentro
   del TypedDict — esto NO los desempaca.

2. **Errors propagados en state['error']**, no como exceptions. El grafo
   siempre completa ordenadamente; el endpoint /scrape lee final_state
   y re-raise el ScraperError para los handlers de FastAPI.

3. **Conditional edges separan logica de routing de logica de nodo**. Asi
   el diagrama exportable (Mermaid) es legible: cada nodo hace una sola
   cosa.

4. **`payment_extractor` SOLO se invoca para mode='browser'** (Falabella).
   ML va por la API directa con 0 LLM calls — la branch direct lo skipea.

5. **`product_enricher` solo si product_info esta incompleto** despues del
   adapter+extractor. Decision en route_after_extractor, no dentro del
   nodo, para que sea explicito en el grafo.

6. **Singleton compilado** (get_graph()): el grafo se compila una vez al
   primer request y se reutiliza. LangGraph compilation es CPU-bound; no
   queremos hacerlo por request.
"""
from __future__ import annotations

from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..adapters.base import AdapterResult
from ..adapters.falabella import FalabellaAdapter
from ..adapters.mercadolibre import MercadoLibreAdapter
from ..dispatcher import SITE_FALABELLA, SITE_MERCADOLIBRE, resolve_site
from ..logging import get_logger
from ..schemas.error import ErrorCode, ScraperError, Stage
from ..schemas.response import PaymentMethod, ProductInfo, TokenUsage

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# State definition
# -----------------------------------------------------------------------------
class ScrapeState(TypedDict, total=False):
    """Estado mutable que viaja por el grafo.

    `total=False` => todos los campos son opcionales en cualquier punto.
    LangGraph mergea parcialmente: cada nodo devuelve un dict con SOLO las
    keys que actualizo, y el merger las aplica al state.
    """
    # input
    url: str
    country: Optional[str]

    # post-dispatcher
    site_id: Optional[str]

    # post-adapter
    adapter_result: Optional[AdapterResult]

    # outputs finales
    payment_methods: list[PaymentMethod]
    product: Optional[ProductInfo]
    payment_methods_source: str

    # tracking operacional
    llm_calls: int
    llm_tokens: TokenUsage
    agent_steps: int

    # error path (si seteado, el endpoint lo re-raise)
    error: Optional[ScraperError]


# -----------------------------------------------------------------------------
# Node implementations
# -----------------------------------------------------------------------------
async def node_dispatcher(state: ScrapeState) -> dict:
    """Resuelve URL -> site_id (deterministico, sin LLM)."""
    url = state["url"]
    site_id = resolve_site(url)
    steps = state.get("agent_steps", 0) + 1

    if site_id is None:
        logger.warning("graph.dispatcher.unsupported", url=url)
        return {
            "error": ScraperError(
                code=ErrorCode.UNSUPPORTED_SITE,
                message=(
                    "No SiteAdapter registered for URL. "
                    "Supported: Mercado Libre and Falabella."
                ),
                stage=Stage.DISPATCHER,
            ),
            "agent_steps": steps,
        }

    logger.info("graph.dispatcher.resolved", site_id=site_id)
    return {"site_id": site_id, "agent_steps": steps}


async def node_adapter_ml(state: ScrapeState) -> dict:
    """Invoca MercadoLibreAdapter (modo direct: API publica, 0 LLM calls)."""
    adapter = MercadoLibreAdapter()
    steps = state.get("agent_steps", 0) + 1
    try:
        result = await adapter.fetch(state["url"], country=state.get("country"))
    except ScraperError as e:
        logger.warning("graph.adapter_ml.error", code=e.code.value)
        return {"error": e, "agent_steps": steps}

    logger.info(
        "graph.adapter_ml.done",
        n_methods=len(result.payment_methods),
        has_product=result.product is not None,
    )
    return {
        "adapter_result": result,
        "payment_methods": result.payment_methods,
        "product": result.product,
        "payment_methods_source": result.payment_methods_source,
        "agent_steps": steps,
    }


async def node_adapter_falabella(state: ScrapeState) -> dict:
    """Invoca FalabellaAdapter (modo browser: Playwright captura DOM)."""
    adapter = FalabellaAdapter()
    steps = state.get("agent_steps", 0) + 1
    try:
        result = await adapter.fetch(state["url"], country=state.get("country"))
    except ScraperError as e:
        logger.warning("graph.adapter_falabella.error", code=e.code.value)
        return {"error": e, "agent_steps": steps}

    logger.info(
        "graph.adapter_falabella.done",
        mode=result.mode,
        dom_kb=round(len(result.initial_dom or "") / 1024, 1),
        source=result.payment_methods_source,
    )
    return {
        "adapter_result": result,
        "product": result.product,
        "payment_methods_source": result.payment_methods_source,
        "agent_steps": steps,
    }


async def node_payment_extractor(state: ScrapeState) -> dict:
    """Agent #1 (LLM): extrae payment_methods estructurados del DOM."""
    # Import diferido para que el patch en tests funcione contra el modulo real.
    from ..agents.payment_extractor import extract_payment_methods

    steps = state.get("agent_steps", 0) + 1
    result = state.get("adapter_result")
    if result is None or result.mode != "browser":
        # Defensive: no deberiamos llegar aca para mode='direct'.
        return {"agent_steps": steps}

    try:
        methods, usage = await extract_payment_methods(
            result.initial_dom or "",
            url=state["url"],
            site_id=result.site_id,
            country=state.get("country"),
        )
    except ScraperError as e:
        logger.warning("graph.payment_extractor.error", code=e.code.value)
        return {"error": e, "agent_steps": steps}

    cur = state.get("llm_tokens", TokenUsage())
    logger.info(
        "graph.payment_extractor.done",
        n_methods=len(methods),
        tokens_in=usage.input,
        tokens_out=usage.output,
    )
    return {
        "payment_methods": methods,
        "llm_calls": state.get("llm_calls", 0) + 1,
        "llm_tokens": TokenUsage(
            input=cur.input + usage.input,
            output=cur.output + usage.output,
        ),
        "agent_steps": steps,
    }


async def node_product_enricher(state: ScrapeState) -> dict:
    """Agent #2 (LLM, conditional): refina title/price si el adapter no los capturo.

    Best-effort: si falla el LLM aca, NO bloqueamos el response — devolvemos
    el state sin cambios. La validacion final ocurre en validator.
    """
    from ..agents.product_enricher import enrich_product

    steps = state.get("agent_steps", 0) + 1
    result = state.get("adapter_result")
    if result is None:
        return {"agent_steps": steps}

    try:
        enriched, usage = await enrich_product(
            result.initial_dom or "",
            url=state["url"],
            current=state.get("product"),
            country=state.get("country"),
        )
    except Exception as e:
        # No fatal — best-effort.
        logger.warning("graph.product_enricher.failed", error=str(e))
        return {"agent_steps": steps}

    out: dict = {"agent_steps": steps}
    if enriched:
        out["product"] = enriched
    if usage and usage.input > 0:
        cur = state.get("llm_tokens", TokenUsage())
        out["llm_calls"] = state.get("llm_calls", 0) + 1
        out["llm_tokens"] = TokenUsage(
            input=cur.input + usage.input,
            output=cur.output + usage.output,
        )
    logger.info(
        "graph.product_enricher.done",
        tokens_in=usage.input if usage else 0,
        tokens_out=usage.output if usage else 0,
        had_enriched=enriched is not None,
    )
    return out


def node_validator(state: ScrapeState) -> dict:
    """Validacion final: dedupe + min 1 method.

    Pure function (sin I/O). Si payment_methods esta vacio, error PARSE_ERROR.
    """
    steps = state.get("agent_steps", 0) + 1
    methods = state.get("payment_methods", [])

    if not methods:
        site = state.get("site_id", "unknown")
        adapter_result = state.get("adapter_result")
        dom_kb = (
            round(len(adapter_result.initial_dom or "") / 1024, 1)
            if adapter_result and adapter_result.initial_dom
            else 0
        )
        return {
            "error": ScraperError(
                code=ErrorCode.PARSE_ERROR,
                message=(
                    f"No payment methods extracted for site {site} "
                    f"(adapter DOM: {dom_kb} KB). "
                    f"Possibly the page does not expose payment methods until "
                    f"a checkout step requiring authentication or address input."
                ),
                stage=Stage.VALIDATOR,
            ),
            "agent_steps": steps,
        }

    # Dedupe defensivo (los adapters/extractor ya deduplican, pero defense in depth).
    seen: set[tuple[str, str]] = set()
    deduped: list[PaymentMethod] = []
    for m in methods:
        key = (m.type, m.brand)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)

    return {"payment_methods": deduped, "agent_steps": steps}


# -----------------------------------------------------------------------------
# Conditional edges (routing logic — funcion pura sobre state)
# -----------------------------------------------------------------------------
def route_after_dispatcher(state: ScrapeState) -> str:
    if state.get("error"):
        return END
    site = state.get("site_id")
    if site == SITE_MERCADOLIBRE:
        return "adapter_ml"
    if site == SITE_FALABELLA:
        return "adapter_falabella"
    return END


def route_after_adapter(state: ScrapeState) -> str:
    if state.get("error"):
        return END
    result = state.get("adapter_result")
    if result is None:
        return END
    if result.mode == "direct":
        # ML: payment_methods ya seteado. Skip LLM.
        return "validator"
    # Falabella: LLM extraction necesaria.
    return "payment_extractor"


def route_after_extractor(state: ScrapeState) -> str:
    if state.get("error"):
        return END
    product = state.get("product")
    # Si el adapter ya capturo title+price, skip enricher (ahorro de LLM call).
    if product and product.title and product.price:
        return "validator"
    return "product_enricher"


# -----------------------------------------------------------------------------
# Graph builder + singleton
# -----------------------------------------------------------------------------
def build_scraper_graph():
    """Construye y compila el StateGraph del pipeline de scraping."""
    workflow = StateGraph(ScrapeState)

    workflow.add_node("dispatcher", node_dispatcher)
    workflow.add_node("adapter_ml", node_adapter_ml)
    workflow.add_node("adapter_falabella", node_adapter_falabella)
    workflow.add_node("payment_extractor", node_payment_extractor)
    workflow.add_node("product_enricher", node_product_enricher)
    workflow.add_node("validator", node_validator)

    workflow.set_entry_point("dispatcher")

    workflow.add_conditional_edges(
        "dispatcher",
        route_after_dispatcher,
        {
            "adapter_ml": "adapter_ml",
            "adapter_falabella": "adapter_falabella",
            END: END,
        },
    )
    workflow.add_conditional_edges(
        "adapter_ml",
        route_after_adapter,
        {
            "validator": "validator",
            "payment_extractor": "payment_extractor",
            END: END,
        },
    )
    workflow.add_conditional_edges(
        "adapter_falabella",
        route_after_adapter,
        {
            "validator": "validator",
            "payment_extractor": "payment_extractor",
            END: END,
        },
    )
    workflow.add_conditional_edges(
        "payment_extractor",
        route_after_extractor,
        {
            "validator": "validator",
            "product_enricher": "product_enricher",
            END: END,
        },
    )
    workflow.add_edge("product_enricher", "validator")
    workflow.add_edge("validator", END)

    return workflow.compile()


_compiled_graph = None


def get_graph():
    """Singleton del grafo compilado.

    Compilar un StateGraph de LangGraph es CPU-bound (~10-50ms). No queremos
    hacerlo por request — lo hacemos al primer uso y cacheamos.
    """
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_scraper_graph()
    return _compiled_graph


def render_mermaid() -> str:
    """Devuelve el diagrama Mermaid del grafo (para README/docs).

    LangGraph expone el grafo via .get_graph().draw_mermaid().
    """
    g = get_graph()
    try:
        return g.get_graph().draw_mermaid()
    except Exception as e:
        logger.warning("graph.mermaid.failed", error=str(e))
        return f"%% Mermaid render failed: {e}"
