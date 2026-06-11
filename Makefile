COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: help up down down-v logs ps restart-worker test lint smoke drop-file clean

help:
	@echo "Targets:"
	@echo "  up              Build and start the full stack (detached)"
	@echo "  down            Stop the stack"
	@echo "  down-v          Stop the stack and remove volumes (full reset)"
	@echo "  logs            Tail logs for all services"
	@echo "  ps              Show service status"
	@echo "  restart-worker  Restart only the worker (idempotency-on-restart demo)"
	@echo "  test            Run lint, security scans and unit tests"
	@echo "  lint            Run ruff against the worker source/tests"
	@echo "  smoke           up + wait for health + drop a test file + verify both clouds"
	@echo "  drop-file       Drop a synthetic order file onto a running stack"
	@echo "  clean           Remove caches and the worker virtualenv"

up:
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

down-v:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

restart-worker:
	$(COMPOSE) restart worker

test:
	./scripts/run-tests.sh

lint:
	cd worker && .venv/bin/ruff check src tests

smoke: up
	./scripts/healthcheck.sh
	./scripts/drop-test-file.sh

drop-file:
	./scripts/drop-test-file.sh

clean:
	rm -rf worker/.venv worker/.pytest_cache worker/.ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
