# syntax=docker/dockerfile:1.7
# ============================================================================
# TL LATAM Scraper -- Dockerfile
#
# Decision TL: usamos python:3.12-slim como base (NO la imagen oficial de
# Playwright) por dos razones:
#   1. La imagen `mcr.microsoft.com/playwright/python:v1.49.0-jammy` trae
#      Python 3.10, pero nuestro pyproject.toml requiere >=3.12.
#   2. Ser explicitos sobre que libs del sistema instalamos hace el deploy
#      reproducible y auditable -- no dependemos de que Microsoft no cambie
#      su imagen base.
#
# El precio: el Dockerfile es mas largo y la primera build tarda ~3 min
# (apt-get + pip + playwright install). Subsiguientes builds son <30s
# gracias al layer caching.
# ============================================================================

FROM python:3.12-slim

# ---- Metadata ---------------------------------------------------------------
LABEL org.opencontainers.image.title="TL LATAM Scraper"
LABEL org.opencontainers.image.description="Multi-Agent Scraping System for LATAM E-commerce"
LABEL org.opencontainers.image.source="https://github.com/sivd860806/TL_LATAM_Scraper"
LABEL org.opencontainers.image.licenses="MIT"

# ---- System libs requeridas por Playwright Chromium ------------------------
# Lista derivada de `playwright install-deps chromium` (validada con WSL2 hoy).
# `curl` lo necesitamos para el HEALTHCHECK.
#
# Decision TL: NO pineamos versiones especificas de los paquetes apt.
# Pinear (e.g. libnspr4=2:4.35-1) amarra el Dockerfile a versiones que
# Debian rota cada ~6 meses, generando builds rotos sin avisar. Para
# Chromium dependencies las APIs son estables y los security patches
# llegan automaticamente -- el trade-off de reproducibilidad vs
# mantenibilidad nos lleva a NO pinear aqui.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libnspr4 libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libatspi2.0-0 fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ---- Runtime config ---------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    TMPDIR=/tmp \
    LOG_FORMAT=json \
    PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright

# ---- Non-root user (security) ----------------------------------------------
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home app

WORKDIR /app

# ---- Install Python deps (cacheable layer) ---------------------------------
# Copiamos solo los archivos necesarios para instalar el package primero,
# asi `pip install .` se cachea cuando solo cambia codigo en app/.
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app app/ ./app/

RUN pip install --no-cache-dir .

# ---- Install Chromium browser (con el user app) ----------------------------
USER app
RUN python -m playwright install chromium

# ---- Healthcheck + entrypoint ----------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

EXPOSE 8000

# Sin --reload (eso es solo para development con uvicorn local).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
