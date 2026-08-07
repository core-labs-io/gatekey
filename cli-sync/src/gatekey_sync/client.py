"""Thin HTTP client against the backend's device-auth + current-key
endpoints (design doc section 9.8's API contract).

Deliberately dumb - no retry/backoff logic beyond `poll_until_approved`'s
own explicit polling loop, no request signing beyond the plain `Authorization:
Bearer` header. Every method raises `AuthRejectedError` on an HTTP 401
specifically (the one status code `cli.py`'s recovery logic branches on) and
`GatekeySyncError` for everything else network/HTTP-shaped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import httpx


class GatekeySyncError(Exception):
    """Base class for this package's own errors - never a raw `httpx`
    exception leaks out of this module to `cli.py`, so the CLI layer has one
    thing to catch."""


class AuthRejectedError(GatekeySyncError):
    """The backend returned 401 for a stored refresh credential or a
    just-obtained one - i.e. the credential is not (or no longer) valid.
    `cli.py` treats this as the signal to clear the stored credential and
    tell the user to `login` again (see `client.py` module docstring)."""


class DeviceAuthTimeoutError(GatekeySyncError):
    """The user never approved the device-code request within `login`'s
    timeout window."""


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class CurrentKey:
    secret: str
    valid_until: datetime


class GatekeySyncClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def device_start(self) -> DeviceStart:
        try:
            response = httpx.post(self._url("/v1/auth/device/start"), timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GatekeySyncError(f"Could not reach Gatekey at {self._base_url}: {exc}") from exc
        body = response.json()
        return DeviceStart(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            expires_in=body["expires_in"],
            interval=body["interval"],
        )

    def poll_until_approved(self, device_code: str, *, interval: int, expires_in: int) -> str:
        """Blocks, polling at `interval` seconds, until approved or
        `expires_in` seconds have elapsed. Returns the plaintext refresh
        credential. Raises `DeviceAuthTimeoutError` on expiry."""
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            try:
                response = httpx.post(
                    self._url("/v1/auth/device/poll"),
                    json={"device_code": device_code},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                raise GatekeySyncError(f"Could not reach Gatekey: {exc}") from exc
            if response.status_code == 404:
                raise DeviceAuthTimeoutError(
                    "The device code expired or was never approved. Run `gatekey-sync login` again."
                )
            if response.status_code == 202:
                time.sleep(interval)
                continue
            response.raise_for_status()
            body = response.json()
            if body["status"] == "approved" and body.get("refresh_credential"):
                return str(body["refresh_credential"])
            time.sleep(interval)
        raise DeviceAuthTimeoutError(
            "Timed out waiting for approval. Run `gatekey-sync login` again."
        )

    def fetch_current_key(self, refresh_credential: str) -> CurrentKey:
        try:
            response = httpx.get(
                self._url("/v1/me/current-key"),
                headers={"Authorization": f"Bearer {refresh_credential}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GatekeySyncError(f"Could not reach Gatekey: {exc}") from exc
        if response.status_code == 401:
            raise AuthRejectedError("Gatekey rejected the stored refresh credential.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GatekeySyncError(f"Gatekey returned an error: {exc}") from exc
        body = response.json()
        return CurrentKey(secret=body["secret"], valid_until=_parse_datetime(body["valid_until"]))


def _parse_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw)
