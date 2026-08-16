"""Operational plumbing: request IDs, structured logging, /metrics, /readyz.

Request IDs
    Every response carries an `X-Request-ID` header: the caller's own value
    (if it sent one that passes `_sanitize_request_id`) or a fresh opaque
    id. The gateway routes reuse the same id for `usage_logs.request_id`
    and every structured log line, so a caller-reported id correlates all
    the way through. Error envelopes embed it as `error.request_id`
    (see `errors.py`).

Structured logging
    `configure_logging()` installs a formatter that actually emits the
    `extra={...}` fields this codebase has always attached to its log calls
    (previously silently dropped - no formatter ever rendered them).
    `GATEKEY_LOG_FORMAT=text` (default) appends them as `key=value` pairs;
    `json` emits one JSON object per line for log pipelines.

Metrics & readiness
    `/metrics` exposes Prometheus counters/histograms recorded by the same
    middleware that assigns request ids (labels use the ROUTE TEMPLATE,
    e.g. `/v1/teams/{team_id}`, never the raw path - bounded cardinality).
    `/readyz` verifies the database (and Redis, when configured) actually
    respond - unlike `/healthz`, which only proves the process is up.
    Note the production Caddyfile deliberately does not route `/metrics`
    or `/readyz` from the public domain; they are for inside-the-network
    scrapers and orchestrators.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

logger = logging.getLogger("gatekey")

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

HTTP_REQUESTS_TOTAL = Counter(
    "gatekey_http_requests_total",
    "HTTP requests handled, by method, route template, and status code.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "gatekey_http_request_duration_seconds",
    "HTTP request wall-clock duration, by method and route template.",
    ["method", "route"],
)


def _sanitize_request_id(raw: str | None) -> str | None:
    """Accept a caller-supplied request id only if it is short and inert.

    The id is echoed into a response header and log lines, so reject
    anything with header-invalid or log-hostile characters rather than
    trying to escape it.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    return candidate if _REQUEST_ID_RE.fullmatch(candidate) else None


def get_request_id(request: Request) -> str | None:
    """The id assigned by the middleware, if it ran (None in bare unit tests)."""
    return getattr(request.state, "request_id", None)


def _route_label(request: Request) -> str:
    """Route TEMPLATE for metric labels - bounded cardinality by design."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def install_observability(app: FastAPI) -> None:
    """Request-id assignment + HTTP metrics middleware, /metrics, /readyz."""

    @app.middleware("http")
    async def _request_id_and_metrics(request: Request, call_next: Any) -> Response:
        request_id = (
            _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            # The outermost ServerErrorMiddleware turns this into the
            # generic 500 (whose body/header the error handler stamps
            # itself) - record the metric here, then let it propagate.
            HTTP_REQUESTS_TOTAL.labels(request.method, _route_label(request), "500").inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(request.method, _route_label(request)).observe(
                time.perf_counter() - started
            )
            raise
        # Error handlers may already have stamped the id (they run inside
        # this middleware); don't overwrite - the values are identical.
        if REQUEST_ID_HEADER not in response.headers:
            response.headers[REQUEST_ID_HEADER] = request_id
        route_label = _route_label(request)
        HTTP_REQUESTS_TOTAL.labels(request.method, route_label, str(response.status_code)).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(request.method, route_label).observe(
            time.perf_counter() - started
        )
        return response

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/readyz", tags=["meta"])
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: the process can actually serve traffic right now.

        Checks the database (always) and Redis (only when configured -
        an unconfigured Redis is 'not applicable', never a failure).
        """
        checks: dict[str, str] = {}
        ready = True

        engine = getattr(request.app.state, "db_engine", None)
        if engine is None:
            checks["database"] = "not_initialized"
            ready = False
        else:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                checks["database"] = "ok"
            except Exception:
                logger.warning("readyz_database_check_failed", exc_info=True)
                checks["database"] = "unavailable"
                ready = False

        settings = request.app.state.settings
        if settings.GATEKEY_REDIS_URL:
            store = getattr(request.app.state, "shared_state_store", None)
            try:
                if store is None:
                    raise RuntimeError("shared state store not initialized")
                await store.ping()
                checks["redis"] = "ok"
            except Exception:
                logger.warning("readyz_redis_check_failed", exc_info=True)
                checks["redis"] = "unavailable"
                ready = False
        else:
            checks["redis"] = "not_configured"

        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "unavailable", "checks": checks},
        )


# --- logging ----------------------------------------------------------------

# Attributes present on every LogRecord - anything else was passed via
# `extra={...}` and is the structured payload we want to surface.
_STANDARD_LOG_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_LOG_ATTRS and not key.startswith("_")
    }


class TextExtraFormatter(logging.Formatter):
    """Human-readable line with the extra fields appended as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extra_fields(record)
        if extras:
            rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
            return f"{base} {rendered}"
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_format: str, level: str) -> None:
    """Install the structured formatter on the root logger (idempotent).

    Applied to the root handler so `gatekey.*`, `uvicorn.*`, `alembic`, and
    library loggers all flow through one formatter. uvicorn's own handlers
    (attached before app startup when run via the uvicorn CLI) are re-
    formatted in place rather than removed, so its access log keeps working.
    """
    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = TextExtraFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(formatter)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
