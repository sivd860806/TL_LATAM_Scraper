.PHONY: setup dev test test-live eval lint format docker-build docker-run clean help

help:
	@echo "Targets disponibles:"
	@echo "  setup          Instala deps (uv sync) + chromium para Playwright"
	@echo "  dev            Levanta el server con auto-reload (uvicorn + FastAPI)"
	@echo "  test           Corre tests unit (sin internet, sin LLM)"
	@echo "  test-live      Corre tests live (requiere ANTHROPIC_API_KEY y red)"
	@echo "  eval           Corre el eval harness contra eval/cases.yaml"
	@echo "  lint           ruff check + mypy"
	@echo "  format         ruff format"
	@echo "  docker-build   Construye la imagen Docker"
	@echo "  docker-run     Corre el contenedor en localhost:8000"
	@echo "  clean          Limpia caches y artefactos"

setup:
	uv sync --all-extras
	uv run playwright install chromium

dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	uv run pytest

test-live:
	uv run pytest -m live

eval:
	uv run python -m eval.run

lint:
	uv run ruff check app/ tests/ eval/
	uv run mypy app/ --ignore-missing-imports

format:
	uv run ruff format app/ tests/ eval/

docker-build:
	docker build -t tl-latam-scraper:latest .

docker-run:
	docker run --rm -p 8000:8000 --env-file .env tl-latam-scraper:latest

clean:
	rm -rf .pytest_cache/ .ruff_cache/ .mypy_cache/ __pycache__/ \
	       app/__pycache__/ app/**/__pycache__/ \
	       tests/__pycache__/ eval/__pycache__/ \
	       eval/results.json
