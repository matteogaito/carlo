UV_CACHE_DIR ?= /tmp/carlo-uv-cache
export UV_CACHE_DIR

.PHONY: install migrate bootstrap-admin build prod-check prod-api api worker ui test

install:
	cd backend && uv sync
	cd frontend && npm ci

migrate:
	cd backend && uv run alembic upgrade head

bootstrap-admin:
	cd backend && uv run python -m carlo.admin

build:
	cd frontend && npm run build

prod-check:
	cd backend && uv run python -m carlo.production

prod-api: build prod-check
	cd backend && uv run uvicorn carlo.main:app --host "$${CARLO_BIND_HOST:-127.0.0.1}" --port "$${CARLO_PORT:-8000}"

api:
	cd backend && uv run uvicorn carlo.main:app --reload

worker:
	cd backend && uv run python -m carlo.worker

ui:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest -q
	cd backend && uv run alembic check
	cd frontend && npm test
	cd frontend && npm run build
