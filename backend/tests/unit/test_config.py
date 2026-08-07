"""Unit tests for config.py - fail-fast validation of Settings."""

from __future__ import annotations

import base64
import os

import pytest
from pydantic import ValidationError

from gatekey.config import Settings


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        "GATEKEY_ADMIN_TOKEN": "test-admin-token",
        "GATEKEY_MASTER_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    env.update(overrides)
    return env


def test_settings_load_with_valid_env(monkeypatch: pytest.MonkeyPatch):
    for key, value in _base_env().items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert len(settings.master_key_bytes()) == 32
    assert settings.GATEKEY_PROVIDER_VALIDATION_TIMEOUT_SECONDS == 8.0


def test_settings_rejects_missing_master_key(monkeypatch: pytest.MonkeyPatch):
    env = _base_env()
    del env["GATEKEY_MASTER_KEY"]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GATEKEY_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_wrong_length_master_key(monkeypatch: pytest.MonkeyPatch):
    env = _base_env(GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(16)).decode())
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_non_base64_master_key(monkeypatch: pytest.MonkeyPatch):
    env = _base_env(GATEKEY_MASTER_KEY="not-valid-base64!!!")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_empty_admin_token(monkeypatch: pytest.MonkeyPatch):
    env = _base_env(GATEKEY_ADMIN_TOKEN="")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- Phase 2: OIDC / SMTP optional-group validators --------------------------


def test_settings_oidc_unset_is_valid_and_disabled(monkeypatch: pytest.MonkeyPatch):
    for key, value in _base_env().items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.oidc_enabled() is False
    assert settings.GATEKEY_SESSION_COOKIE_SECURE is True
    assert settings.GATEKEY_SESSION_TTL_HOURS == 12


def test_settings_oidc_partial_config_fails_fast(monkeypatch: pytest.MonkeyPatch):
    for key, value in _base_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GATEKEY_OIDC_ISSUER_URL", "https://idp.example.com")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_oidc_full_config_is_valid(monkeypatch: pytest.MonkeyPatch):
    for key, value in _base_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GATEKEY_OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("GATEKEY_OIDC_CLIENT_ID", "gatekey-backend")
    monkeypatch.setenv("GATEKEY_OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("GATEKEY_OIDC_REDIRECT_URI", "http://localhost:8000/v1/auth/sso/callback")
    settings = Settings(_env_file=None)
    assert settings.oidc_enabled() is True


def test_settings_smtp_requires_host_and_from_address(monkeypatch: pytest.MonkeyPatch):
    for key, value in _base_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GATEKEY_SMTP_USERNAME", "mailer")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("GATEKEY_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("GATEKEY_SMTP_FROM_ADDRESS", "alerts@example.com")
    assert Settings(_env_file=None).smtp_enabled() is True
