"""Device-code login + OS-keychain refresh-credential storage (design doc
section 8.1/8.2, phase doc 3.7a).

Uses the stdlib-abstraction-over-OS-keychains `keyring` library (Keychain
on macOS, Credential Manager on Windows, Secret Service on Linux) per the
ratified per-OS-scope decision - never a plaintext file for the refresh
credential itself (only the derived, short-lived personal-key cache in
`cache.py` touches disk, and only as plaintext-on-a-best-effort-0600-file,
which is the accepted, documented tradeoff that module's docstring
explains).
"""

from __future__ import annotations

import keyring

from gatekey_sync.client import DeviceAuthTimeoutError, GatekeySyncClient, GatekeySyncError

_KEYRING_SERVICE = "gatekey-sync"
_KEYRING_USERNAME = "refresh_credential"


def get_refresh_credential() -> str | None:
    return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)


def store_refresh_credential(secret: str) -> None:
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, secret)


def clear_refresh_credential() -> None:
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        # Already absent - clearing an absent credential is a no-op, not an
        # error (mirrors this codebase's revoke-is-idempotent convention).
        pass


def login(base_url: str, *, print_fn=print) -> None:
    """Interactive, one-time device-code login (phase doc 3.7a: "authorized
    once, interactively"). Prints the verification URL + user code, blocks
    polling until the user approves in the browser, then stores the
    resulting refresh credential in the OS keychain.
    """
    client = GatekeySyncClient(base_url)
    try:
        start = client.device_start()
    except GatekeySyncError as exc:
        print_fn(f"Could not start login: {exc}")
        raise SystemExit(1) from exc

    print_fn("To finish setting up Gatekey CLI sync:")
    print_fn(f"  1. Open: {start.verification_uri}")
    print_fn(f"  2. Enter code: {start.user_code}")
    print_fn("Waiting for approval...")

    try:
        refresh_credential = client.poll_until_approved(
            start.device_code, interval=start.interval, expires_in=start.expires_in
        )
    except (DeviceAuthTimeoutError, GatekeySyncError) as exc:
        print_fn(str(exc))
        raise SystemExit(1) from exc

    store_refresh_credential(refresh_credential)
    print_fn("Login complete. The refresh credential is stored in your OS keychain.")
