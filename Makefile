# Development entry points. Everything runs without Docker except `make up`.

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help setup api worker web test lint walkthrough up down clean openapi

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install backend and frontend dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e "backend[dev,postgres]"
	cd frontend && npm install

api: ## Run the API with reload
	cd backend && ../$(VENV)/bin/uvicorn app.main:app --reload --port 8000

worker: ## Run a standalone compute worker
	cd backend && ../$(VENV)/bin/python worker.py

web: ## Run the web application in development mode
	cd frontend && npm run dev

test: ## Run the backend test suite
	cd backend && ../$(VENV)/bin/python -m pytest -q

lint: ## Lint the backend and type-check the frontend
	cd backend && ../$(VENV)/bin/python -m ruff check app tests
	cd frontend && npm run typecheck

walkthrough: ## Run the end-to-end controlled workflow against a scratch database
	$(PY) scripts/walkthrough.py

openapi: ## Write the OpenAPI contract to docs/openapi.json
	cd backend && ../$(VENV)/bin/python -c "import json,pathlib; from app.main import create_app; pathlib.Path('../docs/openapi.json').write_text(json.dumps(create_app().openapi(), indent=2))"

up: ## Start the pilot stack with Docker Compose
	docker compose up --build -d
	@echo "API      http://localhost:$${API_PORT:-8000}/docs"
	@echo "Web      http://localhost:$${WEB_PORT:-8080}"

down: ## Stop the stack
	docker compose down

clean: ## Remove local databases, object stores and build output
	rm -rf var backend/var frontend/dist frontend/node_modules/.vite
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
