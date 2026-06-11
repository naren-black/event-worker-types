# syntax=docker/dockerfile:1

# Build context is the repo root (see infra/docker-compose.yml: worker.build.context: ..)
# because the runtime needs both worker/requirements.txt and worker/src.

FROM python:3.12-slim AS builder

WORKDIR /build

COPY worker/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

# wget is used by both this image's HEALTHCHECK and the compose-level
# healthcheck for the worker service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --home-dir /home/worker --shell /usr/sbin/nologin --create-home worker

COPY --from=builder /install /usr/local

WORKDIR /app
COPY worker/src ./src

RUN mkdir -p /data && chown -R worker:worker /app /data

USER worker

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD wget -qO- http://localhost:8080/health || exit 1

ENTRYPOINT ["python", "-m", "src.main"]
