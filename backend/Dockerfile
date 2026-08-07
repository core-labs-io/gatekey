# Gatekey backend (FastAPI) - Phase 1.7 deployment.
# Multi-stage build: install deps + package in a build stage, run from a
# slim runtime image. See ../docker-compose.yml for how this is wired
# together with Postgres and the frontend for the "docker-compose up" flow.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: none beyond what's already in python:slim - this project has
# no native extension dependencies once cryptography's manylinux wheel is
# used (default on this base image/arch combination).
COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

EXPOSE 8000

# Runs migrations then starts the API server. Migrations are idempotent
# (alembic tracks applied revisions in its own table) so this is safe to
# run on every container start, including restarts - this is what makes
# `docker-compose up` alone (no separate manual `alembic upgrade head` step)
# sufficient to get a working instance (Phase 1.7 success criterion).
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
