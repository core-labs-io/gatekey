"""Unit tests for providers/ollama.py's `OllamaValidator` using a mocked
HTTP transport (no network). Mirrors test_providers_openai.py's coverage
shape."""

from __future__ import annotations

import httpx
import pytest

from gatekey.providers.base import ValidationStatus
from gatekey.providers.ollama import OllamaValidator

# Captured before any monkeypatching below - see test_providers_openai.py's
# identical comment for why this is necessary.
_RealAsyncClient = httpx.AsyncClient


def _make_fake_client(handler):
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    return fake_client


@pytest.mark.asyncio
async def test_valid_key_returns_valid(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/v1/models"
        assert request.headers["authorization"] == "Bearer secret-bearer"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": "secret-bearer"}
    )
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_valid_key_with_no_bearer_token_uses_placeholder(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ollama"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_normalized(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/v1/models"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434/", "bearer_token": None}
    )
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_invalid_key_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": "bad"}
    )
    assert result.status == ValidationStatus.INVALID_KEY
    assert "bad" not in (result.detail or "")


@pytest.mark.asyncio
async def test_server_error_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_unreachable_connect_error_returns_provider_unreachable(
    monkeypatch: pytest.MonkeyPatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_timeout_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_unexpected_status_returns_unknown_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    monkeypatch.setattr("gatekey.providers.ollama.httpx.AsyncClient", _make_fake_client(handler))

    result = await OllamaValidator(timeout_seconds=1.0).validate(
        {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_malformed_payload_missing_base_url_returns_unknown_error():
    result = await OllamaValidator(timeout_seconds=1.0).validate({})
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_malformed_payload_non_string_base_url_returns_unknown_error():
    result = await OllamaValidator(timeout_seconds=1.0).validate({"base_url": 12345})
    assert result.status == ValidationStatus.UNKNOWN_ERROR
