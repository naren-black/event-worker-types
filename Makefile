COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: help up down down-v logs ps restart-worker test lint smoke drop-file test-idempotency test-idempotency-duplicate test-minio-sts clean

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
	@echo "  test-idempotency  Stop worker, drop file(s) via SFTP, restart worker, show metrics/logs"
	@echo "  test-idempotency-duplicate  Replay a duplicate event to trigger idempotency_hits_total"
	@echo "  test-minio-sts  PoC: Dex OIDC -> MinIO STS AssumeRoleWithWebIdentity -> scoped S3 access"
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

drop-file:
	./scripts/drop-test-file.sh

# Stops the worker (watcher offline), drops file(s) via SFTP, then restarts
# the worker - the watcher does not backfill on startup, so files dropped
# while it was down should NOT appear in events_published_total /
# events_consumed_total below.
test-idempotency:
	@echo "==> Metrics before"
	@curl -s http://localhost:8080/metrics | grep -E '^(events_published_total|events_consumed_total|idempotency_hits_total)'
	@echo "==> Stopping worker"
	$(COMPOSE) stop worker
	@echo "==> Dropping file(s) onto SFTP while worker is down"
	FILE_COUNT=$${FILE_COUNT:-1} VERIFY=0 ./scripts/drop-test-files.py
	@echo "==> Restarting worker"
	$(COMPOSE) start worker
	@sleep 8
	@echo "==> Worker logs since restart"
	$(COMPOSE) logs --since=10s worker
	@echo "==> Metrics after"
	@curl -s http://localhost:8080/metrics | grep -E '^(events_published_total|events_consumed_total|idempotency_hits_total)'

# Drops one file, lets it process once, then replays a second event with the
# same idempotencyKey via the RabbitMQ management API - exercises the
# is_done() dedupe path (idempotency_hits_total, events_consumed_total{result="duplicate"}).
test-idempotency-duplicate:
	./scripts/replay-duplicate-event.py

# PoC: local OIDC (Dex) -> MinIO STS AssumeRoleWithWebIdentity -> short-lived,
# policy-scoped S3 credentials - the IRSA / Workload-Identity-Federation
# pattern, demonstrated against MinIO (not used by the worker itself).
test-minio-sts:
	./scripts/test-minio-sts.py

clean:
	rm -rf worker/.venv worker/.pytest_cache worker/.ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
