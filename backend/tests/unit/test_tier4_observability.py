"""Tier 4 ops/DX polish - unit coverage for the pieces that don't need a DB:

- X-Request-ID: header on success and error responses, caller-supplied ids
  honored (when sane) or replaced (when hostile), and the id embedded in
  every structured error body.
- GET /v1/models / /v1/models/{model}: OpenAI-compatible shape, policy
  filtering via the same resolution the gateway uses.
- Structured logging: extra={} fields actually reach the output in both
  text and json formats.
- BudgetExhaustedError: live figures as structured fields, not just prose.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gatekey.errors import BudgetExhaustedError
from gatekey.observability import (
    JsonFormatter,
    TextExtraFormatter,
    _sanitize_request_id,
)
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.services.model_policy import ModelPolicySnapshot
from tests.unit.gateway_test_support import build_authenticated_app

_CHAT_URL = "/v1/chat/completions"


# --- X-Request-ID ------------------------------------------------------------


def test_request_id_header_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    rid = response.headers.get("X-Request-ID")
    assert rid and len(rid) == 32  # uuid4().hex


def test_request_id_honors_sane_inbound_value(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "client-trace-42"})
    assert response.headers["X-Request-ID"] == "client-trace-42"


def test_request_id_replaces_hostile_inbound_value(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
    rid = response.headers["X-Request-ID"]
    assert rid != "bad id\twith spaces"
    assert len(rid) == 32


def test_error_body_carries_request_id_matching_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json={"model": "no-such-model-anywhere", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer gk_sk_test", "X-Request-ID": "err-trace-1"},
        )
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "model_not_found"
    assert body["request_id"] == "err-trace-1"
    assert response.headers["X-Request-ID"] == "err-trace-1"


def test_sanitize_request_id_rules() -> None:
    assert _sanitize_request_id("abc-DEF_1.2") == "abc-DEF_1.2"
    assert _sanitize_request_id("  padded  ") == "padded"
    assert _sanitize_request_id("") is None
    assert _sanitize_request_id(None) is None
    assert _sanitize_request_id("x" * 129) is None
    assert _sanitize_request_id("evil\r\nheader: injection") is None


# --- GET /v1/models ------------------------------------------------------------


def test_list_models_openai_shape_and_full_registry_when_no_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/v1/models", headers={"Authorization": "Bearer gk_sk_test"})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    listed = {entry["id"] for entry in body["data"]}
    # No policy configured (empty caches) = every registry model allowed.
    assert listed == set(MODEL_REGISTRY.keys())
    sample = body["data"][0]
    assert sample["object"] == "model"
    assert isinstance(sample["created"], int)
    assert sample["owned_by"] == "gatekey"
    # Sorted, deterministic ordering.
    assert [e["id"] for e in body["data"]] == sorted(listed)


def test_retrieve_model_allowed_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    known = sorted(MODEL_REGISTRY.keys())[0]
    with TestClient(app) as client:
        ok = client.get(f"/v1/models/{known}", headers={"Authorization": "Bearer gk_sk_test"})
        missing = client.get(
            "/v1/models/never-a-real-model", headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert ok.status_code == 200
    assert ok.json()["id"] == known
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"


def test_list_models_respects_org_denylist(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    denied = sorted(MODEL_REGISTRY.keys())[0]
    with TestClient(app) as client:
        # Same cache the gateway's own policy check reads (created by the
        # lifespan, so it only exists once the client context has started).
        app.state.model_policy_cache.set(
            ModelPolicySnapshot(mode="denylist", models=frozenset({denied}))
        )
        response = client.get("/v1/models", headers={"Authorization": "Bearer gk_sk_test"})
        blocked = client.get(
            f"/v1/models/{denied}", headers={"Authorization": "Bearer gk_sk_test"}
        )
    listed = {entry["id"] for entry in response.json()["data"]}
    assert denied not in listed
    assert listed == set(MODEL_REGISTRY.keys()) - {denied}
    # A denied model is indistinguishable from a nonexistent one.
    assert blocked.status_code == 404


# --- structured logging ---------------------------------------------------------


def _record_with_extras() -> logging.LogRecord:
    record = logging.LogRecord(
        name="gatekey",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="gateway_request",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-123"
    record.model = "gpt-4o"
    return record


def test_text_formatter_appends_extra_fields() -> None:
    formatter = TextExtraFormatter(fmt="%(levelname)s %(name)s %(message)s")
    line = formatter.format(_record_with_extras())
    assert line.startswith("INFO gatekey gateway_request")
    assert "request_id='req-123'" in line
    assert "model='gpt-4o'" in line


def test_json_formatter_emits_extras_as_fields() -> None:
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record_with_extras()))
    assert payload["message"] == "gateway_request"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "gatekey"
    assert payload["request_id"] == "req-123"
    assert payload["model"] == "gpt-4o"
    assert "timestamp" in payload


# --- budget error structured fields ----------------------------------------------


def test_budget_exhausted_error_carries_structured_figures() -> None:
    exc = BudgetExhaustedError(
        name="alice", budget_usd=Decimal("100.00"), current_spend_usd=Decimal("100.25")
    )
    assert exc.extra == {"budget_usd": "100.00", "current_spend_usd": "100.25"}
    assert "100.25" in exc.message
