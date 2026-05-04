# Plan de Ejecución — Parte 2: Multi-Agent Scraping System

**Autor**: Sergio Iván Villamizar Delgado
**Contexto**: Technical Assessment — LATAM E-commerce Scraping Team (Fintech)
**Versión**: v1.0 — plan previo a ejecución
**Última actualización**: 2026-05-04

---

## 1. Objetivo

Construir un servicio Python con API HTTP que orqueste **al menos 2 agentes con responsabilidades distintas** para extraer los métodos de pago disponibles en checkout de e-commerce LATAM. El sistema debe:

- Soportar **al menos 2 sites** distintos (confirmados: Mercado Libre Argentina + Falabella Chile).
- Devolver **JSON estructurado** con `payment_methods[]`, `product`, `metadata`.
- Manejar **modos de fallo de manera explícita** (timeouts, CAPTCHA, geo-block, etc.).
- Reportar `llm_calls` y `llm_tokens` en cada respuesta — el evaluador los lee.

**Filosofía**: *"Smaller, polished system over a large unfinished one"* (cita literal del PDF). Cuts documentados al final.

---

## 2. Restricciones del enunciado (no negociables)

1. Implementación en **Python**.
2. Framework agéntico de elección (LangGraph, CrewAI, etc.) → **LangGraph**.
3. **Al menos 2 agentes** con responsabilidades distintas (no el mismo agente renombrado).
4. **LLM provider** de elección → **Anthropic Claude Haiku 4.5** + fallback documentado a Ollama.
5. **HTTP API** con endpoint que recibe URL y devuelve JSON estructurado → FastAPI.
6. **≥2 sites LATAM** soportados.
7. **JSON con payment methods + source URL** mínimo.
8. **README con setup + diagrama + design decisions**.
9. **pyproject.toml** (uv o poetry).
10. **.env.example** sin keys reales.
11. **≥3 ejemplos de URLs probadas** con expected output.

**Out of scope (regla del enunciado)**:
- No completar compras.
- No manejar login/auth.
- No bypass CAPTCHA / anti-bot.
- No UI.

---

## 3. Decisiones tomadas (CONFIRMADAS)

| Decisión | Valor confirmado | Razón |
|----------|------------------|-------|
| Framework de agentes | **LangGraph** | State machine explícita, replay de errores, control de loops; más serio en prod que CrewAI |
| LLM provider | **Anthropic Claude Haiku 4.5** | Rápido, barato, structured outputs nativos. Swap a Ollama documentado |
| Sites soportados | **Mercado Libre Argentina** + **Falabella Chile** | ML cubre el ejemplo del enunciado; Falabella tiene vista pre-login; cubren ARS/CLP |
| Fast path para ML | API pública `api.mercadolibre.com/sites/MLA/payment_methods` | Demuestra criterio TL: 0 LLM calls cuando hay alternativa determinística |
| Stealth | `playwright-stealth` + user-agent rotativo razonable; **NO bypass CAPTCHA** | Cumple regla del enunciado |
| HTTP framework | FastAPI + Pydantic v2 | Estándar de industria, schemas validados |
| Browser automation | Playwright | Mejor que Selenium para sites modernos |
| Logs | structlog + correlation_id por request | Observabilidad básica sin sobre-ingeniería |
| Container | Dockerfile (sin docker-compose) | Suficiente; compose es scope creep |
| Versionado | Commit etapa por etapa (P2.0, P2.1, ...) | Trazabilidad para el reviewer |
| Random seed | 42 en cualquier sampling | Reproducibilidad |
| Repo nombre | `TL_LATAM_Scraper` | Confirmado por usuario |

---

## 4. Arquitectura de alto nivel

```
                          POST /scrape
                              │
                              ▼
                    ┌──────────────────┐
                    │   FastAPI         │  ◄── pydantic v2 valida request
                    └────────┬──────────┘
                             │
                             ▼
                    ┌────────────────┐
                    │ LangGraph       │  ◄── state machine
                    │ Orchestrator    │
                    └────────┬────────┘
                             │
                             ▼
                    ┌────────────────┐
                    │ Dispatcher      │  ◄── función pura Python
                    │ (regex netloc)  │      0 LLM calls
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
         site=MLA      site=falabella   unknown
              │              │              │
              ▼              ▼              ▼
     ┌──────────────┐  ┌──────────────┐  ┌────────────┐
     │  ML Adapter  │  │ FalabellaAdpr│  │ ERROR      │
     │  (API HTTP)  │  │ (Playwright) │  │ UNSUPPORTED│
     │              │  │              │  │ _SITE      │
     │ 0 LLM calls  │  │ 0 LLM calls  │  └────────────┘
     │ ~200ms       │  │ → DOM inicial│
     └──────┬───────┘  └──────┬───────┘
            │                 │
            │                 ▼
            │         ┌─────────────────┐
            │         │  Agent 1:        │  ◄── LLM Agent (LangGraph subgraph)
            │         │  PaymentNavigator│      tools: click, type, wait, done
            │         │                  │      cap: 6 steps
            │         │  2-4 LLM calls   │
            │         └────────┬─────────┘
            │                  │
            │                  ▼ DOM del modal
            │         ┌─────────────────┐
            │         │  Agent 2:        │  ◄── LLM Agent
            │         │  PaymentExtractor│      structured output Pydantic
            │         │  (Pydantic)      │
            │         │  1 LLM call      │
            │         └────────┬─────────┘
            │                  │
            └──────────────────┼──────────┐
                               ▼          ▼
                       ┌──────────────────┐
                       │  Validator       │  ◄── función pura
                       │  (catálogo +     │      0 LLM calls
                       │   normalización) │
                       └────────┬─────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │  Response Builder│
                       │  + metadata:     │
                       │    llm_calls     │
                       │    llm_tokens    │
                       │    duration_ms   │
                       │    agent_steps   │
                       └──────────────────┘
```

**Performance budget** (a verificar con eval harness):
| Site | LLM calls | Latencia esperada | Costo (Haiku) |
|------|-----------|-------------------|---------------|
| Mercado Libre AR | 0 | ~200ms | $0 |
| Falabella CL (happy path) | 3-5 | ~15-25s | ~$0.001 |
| Falabella CL (CAPTCHA) | 1-2 | ~10s | <$0.0005 |

---

## 5. Plan por fases — vista resumen

| # | Fase | Tiempo | Entregable principal |
|---|------|--------|----------------------|
| P2.0 | Bootstrap del repo | 0.5h | Estructura, `pyproject.toml`, `Makefile`, structlog, FastAPI hello |
| P2.1 | Schemas Pydantic + códigos de error + endpoints | 1h | `/scrape` y `/health` con request/response del PDF |
| P2.2 | Dispatcher + SiteAdapter interface + adapter de ML (API pública) | 1.5h | E2E ML funcional con 0 LLM calls |
| P2.3 | Adapter de Falabella con Playwright | 1.5h | Captura DOM inicial pre-login |
| P2.4 | Extractor LLM (Claude) + Validator (función con catálogo) | 1h | DOM → `payment_methods[]` validado |
| P2.5 | LangGraph state machine que orquesta dispatcher → adapter → extractor → validator | 0.5h | Flujo end-to-end con metadata |
| P2.6 | Tests con HTML fixtures + 1 integration test | 0.5h | `pytest` corre verde |
| P2.7 | Eval harness con cases.yaml | 0.5h | `python -m eval.run` reporta P/R por site |
| P2.8 | Dockerfile + .env.example | 0.5h | `docker build` + `docker run` funciona |
| P2.9 | README con diagrama Mermaid + 3 ejemplos curl | 0.5h | Repo listo para review |
|   | **Total** | **8h** |  |

**Nota crítica**: el Navigator agent (Agent 1) lo voy a meter dentro de P2.3 (que es el adapter de Falabella) porque solo se usa en el path de Falabella. Para ML hay fast-path que skipea ambos agentes. La task P2.4 entonces es solo del Extractor + Validator.

---

## 6. Detalle por fase

### P2.0 — Bootstrap (0.5h)

**Estructura inicial**:

```
TL_LATAM_Scraper/
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI minimo: GET /health
│   ├── config.py              # pydantic-settings con .env
│   ├── logging.py             # structlog config
│   ├── schemas/               # vacio en P2.0, lleno en P2.1
│   ├── adapters/              # vacio en P2.0
│   ├── agents/                # vacio en P2.0
│   └── graph/                 # vacio en P2.0
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── test_health.py
├── eval/
├── docs/
├── pyproject.toml
├── Makefile
├── .env.example
├── .gitignore
├── README.md
└── PLAN_PARTE2.md
```

**Deps en pyproject.toml**:
- fastapi[standard], uvicorn[standard], pydantic v2, pydantic-settings
- httpx (para ML API)
- playwright + playwright-stealth
- langgraph, langchain-anthropic, anthropic
- structlog, python-dotenv, pyyaml
- (dev) pytest, pytest-asyncio, ruff

**Makefile targets**: `dev`, `test`, `eval`, `docker-build`, `docker-run`, `lint`.

---

### P2.1 — Schemas + Errores + Endpoints (1h)

**`app/schemas/request.py`**:
```python
class ScrapeRequest(BaseModel):
    url: HttpUrl
    country: str | None = None
    options: ScrapeOptions = Field(default_factory=ScrapeOptions)

class ScrapeOptions(BaseModel):
    extract_title: bool = True
    extract_price: bool = True
    timeout_seconds: int = 60
    force_agents: bool = False  # bypass MLAdapter, usar agentes siempre
```

**`app/schemas/response.py`**: replica del contrato exacto del PDF.

**`app/schemas/error.py`**: enum `ErrorCode` con los 11 códigos definidos + enum `Stage`.

**`app/main.py`**:
- `POST /scrape`: por ahora devuelve placeholder con shape válido
- `GET /health`: devuelve `{"status": "ok", "version": "0.1.0"}`
- Middleware de structlog que injecta `correlation_id` en el contexto

**Test**: `tests/test_schemas.py` valida que un dict con shape del PDF deserializa correctamente; un dict inválido tira `ValidationError`.

---

### P2.2 — Dispatcher + SiteAdapter + ML Adapter (1.5h)

**`app/dispatcher.py`**:
```python
def resolve_site(url: str) -> str | None:
    """Devuelve 'mercadolibre' | 'falabella' | None."""
    netloc = urlparse(url).netloc.lower()
    if re.search(r"mercadolibre\.com\.(ar|co|mx|cl|pe|uy)|articulo\.mercadolibre", netloc):
        return "mercadolibre"
    if re.search(r"falabella\.com(\.[a-z]{2})?", netloc):
        return "falabella"
    return None
```

**`app/adapters/base.py`** — Protocol:
```python
class SiteAdapter(Protocol):
    site_id: str
    requires_browser: bool  # False si solo httpx, True si Playwright
    
    async def fetch(self, url: str, options: ScrapeOptions) -> AdapterResult: ...
```

**`app/adapters/mercadolibre.py`** — implementación completa:
1. Extraer `item_id` del URL (regex sobre path).
2. `GET https://api.mercadolibre.com/items/{item_id}` para info del producto.
3. Extraer `site_id` (MLA, MLM, MLC, MLB) del `item_id` o del URL.
4. `GET https://api.mercadolibre.com/sites/{site_id}/payment_methods` para los métodos.
5. Mapear al schema canónico de `PaymentMethod[]`.
6. Marcar `metadata.llm_calls = 0`.

Test sobre URL real de ML AR: response válido en <2s.

---

### P2.3 — Adapter de Falabella + Navigator agent (1.5h)

**`app/adapters/falabella.py`**:
1. Inicializar Playwright con stealth + user-agent.
2. Navegar a la URL del producto.
3. Capturar DOM inicial + title + price (con selectores específicos de Falabella).
4. Devolver `AdapterResult(initial_dom=..., requires_navigator=True)`.

**`app/agents/navigator.py`** — **PaymentNavigator (Agent 1)**:

Subgraph de LangGraph con un solo nodo "decide" que loop-ea:
- Recibe state con DOM actual + acciones previas
- LLM (Claude Haiku) recibe DOM truncado + lista de acciones disponibles
- Devuelve siguiente acción: `click`, `type`, `wait`, o `done`
- Cap: 6 steps. Si excede → `LLM_BUDGET_EXCEEDED`.
- Si detecta CAPTCHA → `done(reason="captcha")` → escala a `ANTI_BOT_DETECTED`.

**`app/agents/navigator_tools.py`** — tools que el Navigator invoca via Playwright handle:
- `get_visible_text() -> str`
- `click_element(selector, text_contains=None) -> {success, new_url}`
- `wait_for_selector(selector, timeout_ms) -> bool`
- `screenshot() -> bytes` (uso opcional)
- `report_done(reason) -> None`

---

### P2.4 — Extractor + Validator (1h)

**`app/agents/extractor.py`** — **PaymentExtractor (Agent 2)**:
- Recibe DOM del modal de payment methods (capturado por Navigator).
- LLM call con prompt estricto + structured output Pydantic (`list[PaymentMethod]`).
- 1 LLM call total.
- Few-shot examples en el prompt para installments/cuotas.

**`app/validator.py`** — función pura:
```python
CANONICAL_BRANDS = {
    # (alias_lower, canonical_name)
    "visa": "Visa",
    "mastercard": "Mastercard",
    "master card": "Mastercard",
    "amex": "American Express",
    "diners": "Diners Club",
    "pse": "PSE",
    "efecty": "Efecty",
    "mercado pago": "Mercado Pago",
    "mercadopago": "Mercado Pago",
    "oxxo": "OXXO",
    "webpay": "Webpay Plus",
    "khipu": "Khipu",
    "servipag": "Servipag",
    "redcompra": "Redcompra",
    # ...
}

def normalize_payment_methods(extracted: list[PaymentMethod]) -> list[PaymentMethod]:
    """Aplica catalogo, deduplica, valida que el type sea consistente con la brand."""
```

---

### P2.5 — LangGraph state machine (0.5h)

**`app/graph/state.py`**: TypedDict con todo el estado del request (input, intermedios, error, métricas).

**`app/graph/builder.py`**: construye el `StateGraph` con nodos y edges:
```python
graph = StateGraph(AgentState)
graph.add_node("dispatcher", dispatcher_node)
graph.add_node("ml_adapter", ml_adapter_node)
graph.add_node("falabella_adapter", falabella_adapter_node)
graph.add_node("navigator", navigator_node)
graph.add_node("extractor", extractor_node)
graph.add_node("validator", validator_node)

graph.add_conditional_edges("dispatcher", route_by_site, {
    "mercadolibre": "ml_adapter",
    "falabella": "falabella_adapter",
    "unknown": END,
})
graph.add_edge("ml_adapter", "validator")
graph.add_edge("falabella_adapter", "navigator")
graph.add_edge("navigator", "extractor")
graph.add_edge("extractor", "validator")
graph.add_edge("validator", END)
```

`/scrape` endpoint invoca `graph.ainvoke(state)`.

---

### P2.6 — Tests + Fixtures (0.5h)

**Fixtures pre-grabadas** en `tests/fixtures/`:
- `ml_api_response.json` — respuesta real de la API de ML (cacheada para no depender de red en CI)
- `falabella_initial_dom.html` — DOM de página de producto
- `falabella_checkout_modal.html` — DOM del modal de payment methods
- `falabella_captcha.html` — caso negativo

**Tests**:
- `test_dispatcher.py`: 5 cases (ML CO, ML AR, Falabella CL, dominio no soportado, URL malformada)
- `test_extractor.py`: 4 cases sobre fixtures (extrae correcto; HTML vacío → PARSE_ERROR; CAPTCHA detectado; installments parseadas)
- `test_validator.py`: 5 cases (normaliza Visa/MC/AMEX, deduplica, type incorrecto rechazado)
- `test_integration.py` (opt-in con `@pytest.mark.live`): 1 E2E sobre ML real

---

### P2.7 — Eval harness (0.5h)

**`eval/cases.yaml`**:
```yaml
- name: ML Argentina iPhone
  url: https://articulo.mercadolibre.com.ar/MLA-...
  expected:
    site: mercadolibre
    payment_method_types: [credit_card, debit_card, wallet, bank_transfer, cash]
    payment_brands_subset: [Visa, Mastercard, Mercado Pago]

- name: Falabella Chile electrónica
  url: https://www.falabella.com/falabella-cl/product/...
  expected:
    site: falabella
    payment_method_types: [credit_card, debit_card]
    payment_brands_subset: [Visa, Mastercard, Webpay]

# ... 5-7 casos en total, incluyendo 1 negativo (UNSUPPORTED_SITE)
```

**`eval/run.py`**:
- Itera casos, llama el endpoint local
- Compara `payment_method_types` (set intersection) y `payment_brands_subset` (subset check)
- Reporta P/R por site
- Reporta `llm_calls` promedio + latencia p95
- Output: tabla en stdout + `eval/results.json`

---

### P2.8 — Dockerfile + .env.example (0.5h)

**Dockerfile**:
```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install uv && uv sync --frozen
COPY app/ app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**`.env.example`**:
```bash
# Anthropic API key — generar en https://console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-***

# Modelo. Default: claude-haiku-4-5-20251001 (rápido, barato)
MODEL_NAME=claude-haiku-4-5-20251001

# Caps operacionales
MAX_NAVIGATOR_STEPS=6
REQUEST_TIMEOUT_S=60

# Logging
LOG_LEVEL=INFO
```

---

### P2.9 — README + diagrama Mermaid + 3 curl examples (0.5h)

Estructura del README:
1. Project description (1 párrafo)
2. Prerequisites (Python 3.12+, Anthropic key, Docker opcional)
3. Setup (uv sync + playwright install + cp .env.example .env)
4. Run local (`make dev`)
5. Run Docker (`make docker-run`)
6. Architecture diagram (Mermaid — renderiza en GitHub)
7. **Design decisions** (sección clave para el reviewer):
   - Por qué LangGraph sobre CrewAI
   - Por qué fast-path determinístico para ML (0 LLM calls vs 3-5)
   - Por qué Validator es función Python, no agente
   - Trade-off entre Navigator agentic vs site adapters específicos
8. Performance budget (tabla con LLM calls, latencia, costo)
9. **Known limitations** (cuts documentados):
   - No bypass CAPTCHA
   - No login flows
   - No FX rates en vivo (mapping estático country→currency)
   - No cache persistente
   - 2 sites soportados (interface SiteAdapter para extender)
10. Extensibility (cómo agregar un site nuevo)
11. **3 ejemplos curl** con request + response real
12. Future work

---

## 7. Argumento del trade-off LLM vs determinístico

**Esta es la sección que el evaluador va a leer en la entrevista.**

> *"El enunciado pide al menos 2 agentes con responsabilidades distintas. Cumplimos con PaymentNavigator (decide cómo llegar al modal de payment methods) y PaymentExtractor (estructura el JSON final).*
>
> *Para Mercado Libre, NINGUNO de los dos agentes se invoca: hay un atajo determinístico vía la API pública `api.mercadolibre.com/sites/{site_id}/payment_methods`. Esto reduce LLM calls a 0 y latencia a ~200ms para ese site, manteniendo el mismo schema de respuesta. Es una decisión consciente de costo/latencia.*
>
> *Para Falabella, los dos agentes operan en cascada: Navigator decide la secuencia de clicks (típicamente 'Comprar' → 'Continuar') hasta llegar al modal; Extractor estructura el JSON. Total LLM calls esperado: 3-5.*
>
> *El sistema reporta `llm_calls` y `llm_tokens` en cada respuesta para que el equipo de operaciones pueda auditar el costo en producción. El presupuesto operativo es ≤2 LLM calls promedio por URL — el atajo de ML domina las estadísticas."*

---

## 8. Códigos de error (taxonomía completa)

| Código | Stage | Cuándo se dispara |
|--------|-------|-------------------|
| `INVALID_URL` | dispatcher | URL malformada, no parseable |
| `UNSUPPORTED_SITE` | dispatcher | Dominio no matchea ningún adapter |
| `GEO_BLOCKED` | navigator/adapter | Site responde con geo-block (HTTP 403, mensaje específico) |
| `LOGIN_REQUIRED` | navigator | El navigator detecta que el modal de payment requiere login |
| `CHECKOUT_UNREACHABLE` | navigator | Tras 6 steps no se llegó al modal |
| `OUT_OF_STOCK` | adapter/navigator | Producto no disponible para compra |
| `ANTI_BOT_DETECTED` | navigator/adapter | Cloudflare challenge, hCaptcha, recaptcha visible |
| `TIMEOUT` | cualquiera | Excede `request_timeout_s` |
| `LLM_BUDGET_EXCEEDED` | navigator | Más de `MAX_NAVIGATOR_STEPS` calls |
| `PARSE_ERROR` | extractor | LLM no produce JSON válido tras retries |
| `INTERNAL_ERROR` | cualquiera | Excepción no manejada (fallback) |

Cada error response del API tiene:
```json
{
  "status": "error",
  "source_url": "...",
  "error": {
    "code": "ANTI_BOT_DETECTED",
    "message": "Detected Cloudflare challenge on Falabella checkout page",
    "stage": "navigator"
  }
}
```

---

## 9. Riesgos y mitigaciones

| Riesgo | Probabilidad | Mitigación |
|--------|--------------|------------|
| Falabella cambia layout y rompe selectores | Media | SiteAdapter interface + tests con fixtures snapshot; documentar versión del adapter |
| LLM extrae payment methods con nombres inconsistentes | Media | Validator con catálogo canónico de marcas |
| CAPTCHA en Falabella en horarios pico | Media | Detección + ANTI_BOT_DETECTED; reintento exponencial documentado pero no implementado |
| Anthropic API rate limit | Baja | `MAX_NAVIGATOR_STEPS=6` cap + circuit breaker (TODO: future work) |
| Playwright pool agotado bajo carga | Baja en demo | Singleton del browser context con max-concurrent=N |
| Costo de LLM se dispara | Baja por diseño | El fast-path de ML domina; cap de 6 steps en Navigator |
| ML cambia su API pública | Muy baja | Es API documentada, ML la mantiene; fallback a Playwright si falla |

---

## 10. Lo que queda fuera (cuts documentados)

- **No CAPTCHA bypass / no anti-bot evasion**: regla del enunciado.
- **No login / auth flows**: regla del enunciado.
- **No completar compra**: regla del enunciado.
- **No UI**: regla del enunciado.
- **Solo 2 sites**: el enunciado pide ≥2 y "smaller polished" sobre "larger unfinished". Interface `SiteAdapter` permite extender.
- **No cache persistente** (in-memory por proceso). Future Work: Redis para invalidaciones por URL.
- **No proxy rotation**. Future Work: pool de proxies para anti-bot moderado.
- **No FX rates en vivo**. Mapeo estático `country → currency`. Future Work: servicio de FX cuando hay multi-país.
- **No retries automáticos del LLM** más allá de 1. Future Work: tenacity con backoff exponencial.
- **No tracing distribuido** (OpenTelemetry). Solo structlog con correlation_id.
- **No XGBoost-style model comparison**: no aplica.
- **Generic Adapter agentic para sites desconocidos**: documentado como Future Work en el README.

---

## 11. What I would do with more time

1. **Generic LLM Adapter** para sites no registrados — Navigator agentic completo con tools de Playwright. Permitiría onboarding rápido de nuevos sites sin código.
2. **Selector cache persistente** (Redis) por dominio: cuando el extractor encuentra un selector que funciona, lo memoriza. Próxima vez para esa URL/site, va directo al selector y skipea el LLM Navigator.
3. **Eval harness con LLM-as-a-judge** que compara semánticamente el output esperado vs el real, no solo set membership.
4. **Tests de carga** con `locust` o similar; medir saturation del Playwright pool.
5. **Tracing distribuido** con OpenTelemetry + Jaeger. Cada agent step se vería como span.
6. **Retry policies** con tenacity en cada nodo del graph, configurable por error code.
7. **Streaming response**: el endpoint emite eventos a medida que cada nodo del graph completa, útil para debugging en vivo.
8. **API key rotation** entre múltiples Anthropic accounts para distribuir rate limits.

---

## 12. Próximos pasos inmediatos

1. ~~Confirmar decisiones~~ — **CERRADO** (sección 3).
2. Ejecutar **P2.0 (Bootstrap)** + **P2.1 (Schemas + endpoints)** en una primera sesión consecutiva.
3. Crear repo en GitHub `TL_LATAM_Scraper`, hacer initial push.
4. Continuar con P2.2 (ML adapter — primer hito end-to-end del proyecto).

---

## Anexo A — Las 6 preguntas TL preparadas

| Pregunta | Respuesta lista |
|----------|-----------------|
| *"Si bajamos LLM budget a 1 call/URL, ¿qué cambias?"* | "Muevo Navigator a regex/CSS selectors específicos por site (ya tengo SiteAdapter interface). Extractor sigue siendo 1 call. Pierdo flexibilidad pero gano costo." |
| *"Cae Falabella el martes, ¿blast radius?"* | "Circuit breaker por adapter (3 fallos consecutivos → desactivado 5min). Retorna UNSUPPORTED_SITE con stage=adapter. Otros sites no afectados. Métrica: failures_per_site_5min." |
| *"Hazme un test que rompa el Extractor sin tocar otros agentes"* | "HTML fixture donde el bloque payment está en `<noscript>` vacío. Test verifica que devuelve PARSE_ERROR con stage=extractor, no crash." |
| *"1M URLs/día, ¿qué se rompe primero?"* | "Pool de Playwright (300MB por browser, ~2s spawn). Con 30 workers: ~12 RPS sostenidos. Métrica: playwright_pool_saturation. Si pasa 80% → escalar workers o agregar selector cache." |
| *"¿Por qué LangGraph y no CrewAI?"* | "LangGraph tiene state machine explícita con replay. En prod necesito reanudar un request que falló a mitad de camino. CrewAI es más declarativo pero opaco para debugging." |
| *"¿Cómo agregás un site nuevo?"* | "Implemento `SiteAdapter` Protocol: `matches(url)`, `fetch(url, options)`. Si el site requiere browser, también un `Navigator`-compatible. ~150 líneas para un site simple." |

---

*Plan vivo. Cualquier desviación queda documentada en el README final con su razón.*
