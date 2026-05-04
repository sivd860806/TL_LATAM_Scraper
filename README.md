# TL LATAM Scraper

Multi-Agent Scraping System para extraer métodos de pago de checkout en e-commerce LATAM. Entrega de la **Parte 2** del Technical Assessment para LATAM E-commerce Scraping Team — Fintech.

## Status

En desarrollo. Plan completo en [`PLAN_PARTE2.md`](PLAN_PARTE2.md).

| Fase | Status |
|------|--------|
| P2.0 Bootstrap | en progreso |
| P2.1 Schemas + endpoints | pendiente |
| P2.2 Dispatcher + ML adapter | pendiente |
| P2.3 Falabella adapter + Navigator | pendiente |
| P2.4 Extractor + Validator | pendiente |
| P2.5 LangGraph state machine | pendiente |
| P2.6 Tests + fixtures | pendiente |
| P2.7 Eval harness | pendiente |
| P2.8 Dockerfile | pendiente |
| P2.9 README final + diagrama | pendiente |

## Quickstart (development)

```bash
# 1. Setup (instala deps + Chromium para Playwright)
make setup

# 2. Configurar env
cp .env.example .env
# editar .env y poner ANTHROPIC_API_KEY

# 3. Levantar el server con auto-reload
make dev

# 4. Probar /health
curl http://localhost:8000/health
```

## Quickstart (Docker)

```bash
make docker-build
make docker-run
```

## Decisiones clave (cerradas)

| Decisión | Valor | Razón resumida |
|----------|-------|----------------|
| Framework agéntico | LangGraph | State machine explícita, replay de errores |
| LLM | Claude Haiku 4.5 | Rápido, barato, structured outputs nativos |
| Sites soportados | Mercado Libre AR + Falabella CL | Ejemplo del enunciado + cubre ARS/CLP |
| Fast path para ML | API pública (0 LLM calls) | Demuestra criterio TL |
| Browser | Playwright + stealth | Más moderno que Selenium; sin bypass CAPTCHA |
| HTTP | FastAPI + Pydantic v2 | Estándar, validación automática |
| Versionado | Commit etapa por etapa | Trazabilidad para el reviewer |

Ver detalle en `PLAN_PARTE2.md` sección 3.

## Arquitectura (preview)

```
POST /scrape → Dispatcher (regex) ─┬─ ML Adapter (API pública, 0 LLM)
                                    └─ Falabella Adapter (Playwright)
                                         ↓
                                    PaymentNavigator (LLM Agent 1)
                                         ↓
                                    PaymentExtractor (LLM Agent 2)
                                         ↓
                                    Validator (función)
                                         ↓
                                    Response + metadata
```

Diagrama Mermaid completo se agrega en P2.9.

## Performance budget

| Site | LLM calls | Latencia | Costo (Haiku) |
|------|-----------|----------|---------------|
| Mercado Libre AR | 0 | ~200ms | $0 |
| Falabella CL (happy) | 3-5 | ~15-25s | ~$0.001 |

Métricas reales se completan tras correr `eval/run.py` (P2.7).
