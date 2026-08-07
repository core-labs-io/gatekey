"""Unit tests for errors.py - structured envelope and log redaction."""

from __future__ import annotations

import base64
import contextlib
import json
import os
from collections.abc import Iterator

from fastapi.testclient import TestClient

from gatekey.config import Settings
from gatekey.errors import redact, redact_json_safe
from gatekey.main import create_app

_ADMIN_TOKEN = "test-token"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN=_ADMIN_TOKEN,
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )


@contextlib.contextmanager
def _client() -> Iterator[TestClient]:
    # Entering as a context manager triggers the app's lifespan (which sets
    # up `app.state.db_session_factory`) - the PUT endpoints declare a DB
    # session dependency even though these particular requests never reach
    # a query, since the dependency still needs to be *resolvable*.
    app = create_app(settings=_settings())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_redact_scrubs_known_secret_fields():
    payload = {
        "api_key": "sk-abc123",
        "user": "alice",
        "nested": {"service_account_json": {"private_key": "-----BEGIN..."}},
    }
    redacted = redact(payload)
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["user"] == "alice"
    assert redacted["nested"]["service_account_json"] == "***REDACTED***"


def test_redact_handles_lists_of_dicts():
    payload = [{"token": "secret-token"}, {"model": "gpt-4"}]
    redacted = redact(payload)
    assert redacted[0]["token"] == "***REDACTED***"
    assert redacted[1]["model"] == "gpt-4"


def test_redact_is_case_insensitive_on_keys():
    payload = {"API_KEY": "sk-abc123", "Authorization": "Bearer xyz"}
    redacted = redact(payload)
    assert redacted["API_KEY"] == "***REDACTED***"
    assert redacted["Authorization"] == "***REDACTED***"


def test_redact_does_not_mutate_input():
    payload = {"api_key": "sk-abc123"}
    redact(payload)
    assert payload["api_key"] == "sk-abc123"


def test_redact_passes_through_non_secret_scalars():
    assert redact("just a string") == "just a string"
    assert redact(42) == 42
    assert redact(None) is None


def test_redact_json_safe_strips_input_key_regardless_of_field_name():
    # Simulates a pydantic v2 `ValidationError.errors()` entry: the raw
    # submitted value lives under "input", independent of the field that
    # failed (`loc`) - plain `redact()` would never touch this key.
    errors = [
        {
            "type": "string_too_long",
            "loc": ("body", "api_key"),
            "msg": "String should have at most 4096 characters",
            "input": "sk-super-secret-key-value",
            "ctx": {"max_length": 4096},
        }
    ]
    safe = redact_json_safe(errors)
    assert "input" not in safe[0]
    assert "sk-super-secret-key-value" not in json.dumps(safe)
    # Non-secret fields survive.
    assert safe[0]["type"] == "string_too_long"
    assert safe[0]["msg"] == "String should have at most 4096 characters"


def test_redact_json_safe_also_applies_field_name_redaction():
    errors = [{"loc": ("body", "api_key"), "api_key": "sk-abc123", "input": "sk-abc123"}]
    safe = redact_json_safe(errors)
    assert safe[0]["api_key"] == "***REDACTED***"
    assert "input" not in safe[0]


# --- Regression tests: full round trip through the real FastAPI app -------
#
# Both security-reviewer and qa-engineer reproduced secret material (raw
# submitted api_key / service_account_json, including private key PEM
# material) being echoed verbatim in the 422 response body whenever pydantic
# validation failed on request body parsing. These tests build a REAL
# `RequestValidationError` by sending genuinely bad payloads through the
# real app via `TestClient` and assert the secret is absent from the full
# response body - not just from a hand-built dict passed to a redaction
# helper directly. No DB is touched since validation fails before any write.


def test_oversized_api_key_not_echoed_in_validation_error_response():
    secret = "sk-" + ("a" * 5000)  # over the 4096-char max_length bound
    with _client() as client:
        response = client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": secret},
            headers=_auth_headers(),
        )
    assert response.status_code == 422
    assert secret not in response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert secret not in json.dumps(body)


def test_vertex_ai_service_account_json_as_string_not_echoed():
    fake_private_key = (
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw"
        "-----END PRIVATE KEY-----"
    )
    # service_account_json submitted as a JSON string instead of an object -
    # this is the exact repro both reviewers used.
    bad_payload = {
        "service_account_json": json.dumps({"private_key": fake_private_key}),
        "project_id": "my-project",
        "location": "us-central1",
    }
    with _client() as client:
        response = client.put(
            "/v1/admin/providers/vertex_ai/key",
            json=bad_payload,
            headers=_auth_headers(),
        )
    assert response.status_code == 422
    assert fake_private_key not in response.text
    assert "BEGIN PRIVATE KEY" not in response.text


def test_extra_unexpected_field_value_not_echoed():
    secret = "extra-field-secret-value-should-not-leak"
    with _client() as client:
        response = client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-valid-looking-key", "unexpected_field": secret},
            headers=_auth_headers(),
        )
    assert response.status_code == 422
    assert secret not in response.text


def test_wrong_content_type_malformed_body_not_echoed():
    # Malformed JSON (truncated) still containing what looks like a secret
    # value - triggers FastAPI's own body-parse RequestValidationError
    # (loc: ("body",)), not the schema-level one raised in providers.py.
    secret = "sk-should-never-appear-in-response"
    raw_body = f'{{"api_key": "{secret}"'.encode()  # missing closing brace
    with _client() as client:
        response = client.put(
            "/v1/admin/providers/openai/key",
            content=raw_body,
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert secret not in response.text
