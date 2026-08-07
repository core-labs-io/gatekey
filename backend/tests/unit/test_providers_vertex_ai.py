"""Unit tests for providers/vertex_ai.py.

Mocks google-auth credential construction/refresh and the HTTP transport -
no real network calls, no real GCP credentials required.
"""

from __future__ import annotations

import google.auth.exceptions
import httpx
import pytest

from gatekey.providers.base import ValidationStatus
from gatekey.providers.vertex_ai import VertexAIValidator

# Captured before any monkeypatching below - see test_providers_openai.py
# for why `fake_client()` must build the real client via this reference
# rather than via `httpx.AsyncClient` (which would recurse into itself
# once patched, since `providers.vertex_ai.httpx` is the same module
# object as the top-level `httpx` import here).
_RealAsyncClient = httpx.AsyncClient

FAKE_SERVICE_ACCOUNT_JSON = {
    "type": "service_account",
    "project_id": "fake-project",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "fake@fake-project.iam.gserviceaccount.com",
}


class _FakeCredentials:
    def __init__(self, token: str | None = "fake-bearer-token", refresh_error: Exception | None = None):
        self.token = None
        self._final_token = token
        self._refresh_error = refresh_error

    def refresh(self, request):
        if self._refresh_error is not None:
            raise self._refresh_error
        self.token = self._final_token


def _patch_credentials(monkeypatch: pytest.MonkeyPatch, credentials: _FakeCredentials) -> None:
    monkeypatch.setattr(
        "gatekey.providers.vertex_ai.Credentials.from_service_account_info",
        lambda info, scopes=None: credentials,
    )


def _patch_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr("gatekey.providers.vertex_ai.httpx.AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_valid_credentials_return_valid(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials(token="fake-bearer-token"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake-bearer-token"
        assert "locations/us-central1/models" in str(request.url)
        return httpx.Response(200, json={"models": []})

    _patch_http(monkeypatch, handler)

    result = await VertexAIValidator(timeout_seconds=2.0).validate(
        {
            "service_account_json": FAKE_SERVICE_ACCOUNT_JSON,
            "project_id": "fake-project",
            "location": "us-central1",
        }
    )
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_401_from_vertex_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials(token="fake-bearer-token"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _patch_http(monkeypatch, handler)

    result = await VertexAIValidator(timeout_seconds=2.0).validate(
        {
            "service_account_json": FAKE_SERVICE_ACCOUNT_JSON,
            "project_id": "fake-project",
            "location": "us-central1",
        }
    )
    assert result.status == ValidationStatus.INVALID_KEY


@pytest.mark.asyncio
async def test_refresh_error_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    refresh_error = google.auth.exceptions.RefreshError("credentials rejected")
    _patch_credentials(monkeypatch, _FakeCredentials(refresh_error=refresh_error))

    result = await VertexAIValidator(timeout_seconds=2.0).validate(
        {
            "service_account_json": FAKE_SERVICE_ACCOUNT_JSON,
            "project_id": "fake-project",
            "location": "us-central1",
        }
    )
    assert result.status == ValidationStatus.INVALID_KEY
    assert "credentials rejected" not in (result.detail or "")


@pytest.mark.asyncio
async def test_malformed_service_account_json_returns_unknown_error(
    monkeypatch: pytest.MonkeyPatch,
):
    malformed_error = google.auth.exceptions.MalformedError("bad json")
    _patch_credentials(monkeypatch, _FakeCredentials(refresh_error=malformed_error))

    result = await VertexAIValidator(timeout_seconds=2.0).validate(
        {
            "service_account_json": FAKE_SERVICE_ACCOUNT_JSON,
            "project_id": "fake-project",
            "location": "us-central1",
        }
    )
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_missing_fields_returns_unknown_error():
    result = await VertexAIValidator(timeout_seconds=2.0).validate(
        {"service_account_json": {}, "project_id": "", "location": "us-central1"}
    )
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_shared_timeout_budget_applies_to_whole_call(monkeypatch: pytest.MonkeyPatch):
    """Refresh alone taking longer than the overall timeout should time out
    the whole validate() call, proving a single shared budget (not 8s per
    step)."""

    class _SlowCredentials(_FakeCredentials):
        def refresh(self, request):
            import time

            time.sleep(0.3)
            super().refresh(request)

    _patch_credentials(monkeypatch, _SlowCredentials(token="fake-bearer-token"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    _patch_http(monkeypatch, handler)

    result = await VertexAIValidator(timeout_seconds=0.1).validate(
        {
            "service_account_json": FAKE_SERVICE_ACCOUNT_JSON,
            "project_id": "fake-project",
            "location": "us-central1",
        }
    )
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE
    assert "Timed out" in (result.detail or "")
