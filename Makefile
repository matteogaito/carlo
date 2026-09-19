UV_CACHE_DIR ?= /tmp/carlo-uv-cache
export UV_CACHE_DIR

.PHONY: install migrate bootstrap-admin build prod-check prod-api api worker ui test deploy status logs stop undeploy

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
	cd backend && uv run uvicorn carlo.main:app --log-config logging.ini --host "$${CARLO_BIND_HOST:-127.0.0.1}" --port "$${CARLO_PORT:-8000}"

api:
	cd backend && uv run uvicorn carlo.main:app --log-config logging.ini --reload

worker:
	cd backend && uv run python -m carlo.worker

ui:
	cd frontend && npm run dev

test:
	node --test extensions/*.test.mjs
	cd backend && uv run python -m compileall -q carlo
	cd backend && uv run pytest -q
	cd backend && uv run alembic check
	cd frontend && npm test
	cd frontend && npm run build

deploy:
	@CARLO_SOURCE_ROOT="$(CURDIR)" CARLO_UV_BIN="$$(command -v uv)" CARLO_NPM_BIN="$$(command -v npm)" ./scripts/deploy.sh deploy

status:
	@./scripts/deploy.sh status

logs:
	@./scripts/deploy.sh logs

stop:
	@./scripts/deploy.sh stop

undeploy:
	@./scripts/deploy.sh undeploy
