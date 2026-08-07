"""Unit tests for providers/anthropic.py using a mocked HTTP transport (no network)."""

from __future__ import annotations

import httpx
import pytest

from gatekey.providers.anthropic import ANTHROPIC_API_VERSION, AnthropicValidator
from gatekey.providers.base import ValidationStatus

# See test_providers_openai.py for why this must be captured before patching.
_RealAsyncClient = httpx.AsyncClient


def _make_fake_client(handler):
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    return fake_client


@pytest.mark.asyncio
async def test_valid_key_returns_valid(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-ant-good"
        assert request.headers["anthropic-version"] == ANTHROPIC_API_VERSION
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "gatekey.providers.anthropic.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await AnthropicValidator(timeout_seconds=1.0).validate({"api_key": "sk-ant-good"})
    assert result.status == ValidationStatus.VALID


@pytest.mark.asyncio
async def test_invalid_key_returns_invalid_key(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "authentication_error"})

    monkeypatch.setattr(
        "gatekey.providers.anthropic.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await AnthropicValidator(timeout_seconds=1.0).validate({"api_key": "sk-ant-bad"})
    assert result.status == ValidationStatus.INVALID_KEY
    assert "sk-ant-bad" not in (result.detail or "")


@pytest.mark.asyncio
async def test_connect_error_returns_provider_unreachable(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        "gatekey.providers.anthropic.httpx.AsyncClient", _make_fake_client(handler)
    )

    result = await AnthropicValidator(timeout_seconds=1.0).validate({"api_key": "sk-ant-whatever"})
    assert result.status == ValidationStatus.PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_malformed_payload_returns_unknown_error():
    result = await AnthropicValidator(timeout_seconds=1.0).validate({"api_key": ""})
    assert result.status == ValidationStatus.UNKNOWN_ERROR
