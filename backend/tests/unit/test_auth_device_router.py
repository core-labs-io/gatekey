"""Router-level smoke tests for the no-auth device-code endpoints (Phase 3,
BD-25) - `POST /v1/auth/device/start` and `POST /v1/auth/device/poll` never
touch the DB (see `services/cli_refresh_credentials.py`'s `DeviceAuthStore`),
so these run against the real `create_app()` with a deliberately-unreachable
DSN, same pattern `test_main.py` already uses - if either route accidentally
started requiring a DB round trip, these would hang/error instead of passing.

`/approve` and `GET /v1/me/current-key` need a real session/DB and are left
to integration tests (same split as every other DB-backed route in this
codebase) - see `services/cli_refresh_credentials.py`'s module-level test
file for the DB-free pieces of that logic (`compute_current_key_valid_until`,
the `DeviceAuthStore` state machine itself).
"""

from __future__ import annotations

import base64
import os

from fastapi.testclient import TestClient

from gatekey.config import Settings
from gatekey.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )


def test_device_start_returns_pending_shape() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.post("/v1/auth/device/start")
        assert response.status_code == 200
        body = response.json()
        assert body["user_code"]
        assert body["device_code"]
        assert body["verification_uri"].endswith("/device")
        assert body["expires_in"] > 0
        assert body["interval"] > 0


def test_poll_before_approval_returns_202_pending() -> None:
    with TestClient(create_app(_settings())) as client:
        started = client.post("/v1/auth/device/start").json()
        response = client.post(
            "/v1/auth/device/poll", json={"device_code": started["device_code"]}
        )
        assert response.status_code == 202
        assert response.json() == {"status": "pending", "refresh_credential": None}


def test_poll_unknown_device_code_returns_404() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.post("/v1/auth/device/poll", json={"device_code": "bogus"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
