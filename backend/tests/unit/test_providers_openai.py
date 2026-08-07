"""Unit tests for providers/openai.py using a mocked HTTP transport (no network)."""

from __future__ import annotations

import httpx
import pytest

from gatekey.providers.base import ValidationStatus
from gatekey.providers.openai import OpenAIValidator

# Captured before any monkeypatching below - `providers.openai` does
# `import httpx`, so patching `gatekey.providers.openai.httpx.AsyncClient`
# mutates the real `httpx` module's attribute (it's the same object).
# `fake_client()` must therefore build the real client via this captured
# reference, not via `httpx.AsyncClient`, or it would recurse into itself.
_RealAsyncClient = httpx.AsyncClient


def _make_fake_client(handler):
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    return fake_client


@pytest.mark.asyncio
async def test_valid_key_returns_valid(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-good"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr("gatekey.providers.openai.httpx.AsyncClient", _make_fake_client(handler))

    result = await OpenAIValidator(timeout_seconds=1.0).validate({"api_key": "sk-good"})
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_invalid_key_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_api_key"})

    monkeypatch.setattr("gatekey.providers.openai.httpx.AsyncClient", _make_fake_client(handler))

    result = await OpenAIValidator(timeout_seconds=1.0).validate({"api_key": "sk-bad"})
    assert result.status == ValidationStatus.INVALID_KEY
    assert "sk-bad" not in (result.detail or "")


@pytest.mark.asyncio
async def test_server_error_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr("gatekey.providers.openai.httpx.AsyncClient", _make_fake_client(handler))

    result = await OpenAIValidator(timeout_seconds=1.0).validate({"api_key": "sk-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_timeout_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    monkeypatch.setattr("gatekey.providers.openai.httpx.AsyncClient", _make_fake_client(handler))

    result = await OpenAIValidator(timeout_seconds=1.0).validate({"api_key": "sk-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_unexpected_status_returns_unknown_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    monkeypatch.setattr("gatekey.providers.openai.httpx.AsyncClient", _make_fake_client(handler))

    result = await OpenAIValidator(timeout_seconds=1.0).validate({"api_key": "sk-whatever"})
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_malformed_payload_returns_unknown_error():
    result = await OpenAIValidator(timeout_seconds=1.0).validate({})
    assert result.status == ValidationStatus.UNKNOWN_ERROR
