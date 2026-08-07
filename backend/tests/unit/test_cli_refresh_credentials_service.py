"""Unit tests for `services/cli_refresh_credentials.py` (Phase 3, BD-25).

Covers the parts that don't need a real DB: the device-code flow's
`DeviceAuthStore` state machine (AC8a.2), the rotate-on-fetch `valid_until`
fallback (fork #3, AC8a.4), and the plaintext-secret format. DB-backed CRUD
(`create_cli_refresh_credential`/`get_active_cli_refresh_credential_by_hash`)
follows this codebase's existing split (`test_service_accounts_service.py`'s
docstring) - covered by an integration test against a real Postgres, not
here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gatekey.services.cli_refresh_credentials import (
    DEFAULT_CURRENT_KEY_TTL,
    REFRESH_CREDENTIAL_PREFIX,
    DeviceAuthStore,
    compute_current_key_valid_until,
    generate_refresh_credential_secret,
)


def _utc(*args, **kwargs) -> datetime:
    return datetime(*args, tzinfo=timezone.utc, **kwargs)


# --- generate_refresh_credential_secret --------------------------------------


def test_generated_secret_has_expected_prefix_and_entropy() -> None:
    secret = generate_refresh_credential_secret()
    assert secret.startswith(REFRESH_CREDENTIAL_PREFIX)
    assert len(secret) > len(REFRESH_CREDENTIAL_PREFIX) + 32
    assert generate_refresh_credential_secret() != secret  # never repeats


# --- compute_current_key_valid_until (fork #3) --------------------------------


def test_valid_until_falls_back_to_default_ttl_when_no_rotation_config() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    result = compute_current_key_valid_until(now=now, rotation_next_rotation_at=None)
    assert result == now + DEFAULT_CURRENT_KEY_TTL


def test_valid_until_uses_future_rotation_config_when_present() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    next_rotation = _utc(2026, 8, 5, 2, 0)  # further out than the 1h fallback
    result = compute_current_key_valid_until(now=now, rotation_next_rotation_at=next_rotation)
    assert result == next_rotation


def test_valid_until_ignores_a_stale_past_rotation_config() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    stale = _utc(2026, 8, 3, 2, 0)  # already in the past
    result = compute_current_key_valid_until(now=now, rotation_next_rotation_at=stale)
    assert result == now + DEFAULT_CURRENT_KEY_TTL


# --- DeviceAuthStore state machine (AC8a.2) -----------------------------------


def test_start_produces_a_pending_record() -> None:
    store = DeviceAuthStore()
    now = _utc(2026, 8, 4, 10, 0)
    record = store.start(now=now, ttl_seconds=600)
    assert record.approved is False
    assert record.refresh_credential_plaintext is None
    outcome, plaintext = store.poll(device_code=record.device_code, now=now)
    assert (outcome, plaintext) == ("pending", None)


def test_poll_unknown_device_code_is_not_found() -> None:
    store = DeviceAuthStore()
    outcome, plaintext = store.poll(device_code="does-not-exist")
    assert (outcome, plaintext) == ("not_found", None)


def test_approve_unknown_user_code_is_rejected() -> None:
    store = DeviceAuthStore()
    assert store.approve(user_code="AAAA-1111", refresh_credential_plaintext="secret") is False


def test_full_happy_path_start_approve_poll_delivers_secret_once() -> None:
    store = DeviceAuthStore()
    now = _utc(2026, 8, 4, 10, 0)
    record = store.start(now=now, ttl_seconds=600)

    assert store.is_pending(user_code=record.user_code, now=now) is True
    approved = store.approve(
        user_code=record.user_code, refresh_credential_plaintext="gk_rf_secret", now=now
    )
    assert approved is True
    assert store.is_pending(user_code=record.user_code, now=now) is False

    # First poll after approval delivers the plaintext exactly once.
    outcome, plaintext = store.poll(device_code=record.device_code, now=now)
    assert (outcome, plaintext) == ("approved", "gk_rf_secret")

    # A replayed poll of the same device_code never gets a second copy.
    outcome2, plaintext2 = store.poll(device_code=record.device_code, now=now)
    assert (outcome2, plaintext2) == ("not_found", None)


def test_approve_is_not_idempotent_second_call_rejected() -> None:
    store = DeviceAuthStore()
    now = _utc(2026, 8, 4, 10, 0)
    record = store.start(now=now, ttl_seconds=600)
    assert store.approve(user_code=record.user_code, refresh_credential_plaintext="s1", now=now)
    # Already approved - a second confirmation attempt (e.g. a doubled
    # browser click) must not mint/attach a second secret.
    assert not store.approve(user_code=record.user_code, refresh_credential_plaintext="s2", now=now)


def test_expired_device_code_is_rejected_on_poll() -> None:
    store = DeviceAuthStore()
    start_time = _utc(2026, 8, 4, 10, 0)
    record = store.start(now=start_time, ttl_seconds=60)
    later = start_time + timedelta(seconds=61)
    outcome, plaintext = store.poll(device_code=record.device_code, now=later)
    assert (outcome, plaintext) == ("not_found", None)


def test_expired_user_code_cannot_be_approved() -> None:
    store = DeviceAuthStore()
    start_time = _utc(2026, 8, 4, 10, 0)
    record = store.start(now=start_time, ttl_seconds=60)
    later = start_time + timedelta(seconds=61)
    assert store.is_pending(user_code=record.user_code, now=later) is False
    assert not store.approve(
        user_code=record.user_code, refresh_credential_plaintext="s1", now=later
    )


def test_user_codes_are_unique_across_starts() -> None:
    store = DeviceAuthStore()
    now = _utc(2026, 8, 4, 10, 0)
    codes = {store.start(now=now, ttl_seconds=600).user_code for _ in range(50)}
    assert len(codes) == 50
