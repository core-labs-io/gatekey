"""Unit tests for providers/openrouter.py's `OpenRouterValidator` using a
mocked HTTP transport (no network). Mirrors test_providers_openai.py's
coverage shape."""

from __future__ import annotations

import httpx
import pytest

from gatekey.providers.base import ValidationStatus
from gatekey.providers.openrouter import OPENROUTER_AUTH_KEY_URL, OpenRouterValidator

_RealAsyncClient = httpx.AsyncClient


def _make_fake_client(handler):
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    return fake_client


@pytest.mark.asyncio
async def test_valid_key_returns_valid(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENROUTER_AUTH_KEY_URL  # regression guard: must NOT be the public /models endpoint (see module docstring)
        assert request.headers["authorization"] == "Bearer sk-or-good"
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-good"})
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_invalid_key_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_api_key"})

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-bad"})
    assert result.status == ValidationStatus.INVALID_KEY
    assert "sk-or-bad" not in (result.detail or "")


@pytest.mark.asyncio
async def test_server_error_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_unreachable_connect_error_returns_provider_unreachable(
    monkeypatch: pytest.MonkeyPatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_timeout_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_unexpected_status_returns_unknown_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    monkeypatch.setattr(
        "gatekey.providers.openrouter.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await OpenRouterValidator(timeout_seconds=1.0).validate({"api_key": "sk-or-whatever"})
    assert result.status == ValidationStatus.UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_malformed_payload_returns_unknown_error():
    result = await OpenRouterValidator(timeout_seconds=1.0).validate({})
    assert result.status == ValidationStatus.UNKNOWN_ERROR
