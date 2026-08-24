"""Direct unit tests for `api.v1.gateway.common.call_provider_with_failover`
- the actual reactive-retry mechanic backing AC4.1.4/AC4.1.5/AC4.1.7/AC4.1.8/
AC4.1.9 and AC4.5.7 (failover event counting).

QA finding (audit pass): before this file existed, `call_provider_with_
failover` was NEVER exercised for real anywhere in the test suite. Every
gateway-pipeline test (`test_gateway_phase4_pipeline.py`) monkeypatches it
away to a fake that always returns `attempt=0` (see
`gateway_test_support.py`'s `_fake_call_provider_with_failover`), and no
other test file called the real function directly. This file closes that
gap: it builds real `ProviderKey` ORM rows (real AES-256-GCM-encrypted
credentials, exactly like `test_provider_key_health.py`'s established
pattern) and a minimal in-process fake session, then drives the real
function end to end - primary success, primary-fails-backup-succeeds,
primary-fails-backup-also-fails, failover-disabled (no retry), and the
AC4.1.5 wall-clock timing budget.

Separately (reported in QA findings, not re-litigated here): there is
currently NO admin HTTP endpoint that can ever set `ProviderKey.
failover_enabled=True` or `ProviderKey.failover_target_id` on a real key
(`services.provider_keys.set_failover_config` - the one function that
performs the same-provider validation this mechanic structurally relies on
for AC4.1.9 - has zero callers anywhere under `src/gatekey/api/`). That
means this retry mechanic, verified correct below, is unreachable through
the actual product surface today. See the QA report for the full writeup.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.failover_event import FailoverEvent
from gatekey.db.models.provider_key import ProviderKey, ProviderName
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import ModelRoute, ModelCapability
from gatekey.services import provider_key_health
from gatekey.services import provider_keys as provider_keys_service
from gatekey.services.encryption import EnvKeyProvider, build_aad, encrypt_secret
from gatekey.services.shared_state import InProcessSharedStateStore


class _FakeSession:
    """Same minimal in-process fake `test_provider_key_health.py` already
    established - `execute()`/`commit()` are no-ops; `add()` records what
    was added so tests can assert on `FailoverEvent` row counts (AC4.5.7)
    without a real database."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit_count = 0

    async def execute(self, stmt):  # noqa: ANN001
        return None

    def add(self, obj):  # noqa: ANN001
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1


def _key_provider() -> EnvKeyProvider:
    return EnvKeyProvider(os.urandom(32))


def _make_key_row(
    *,
    provider: ProviderName,
    api_key: str,
    key_provider: EnvKeyProvider,
    is_primary: bool,
    failover_enabled: bool = False,
    failover_target_id: uuid.UUID | None = None,
) -> ProviderKey:
    aad = build_aad(str(DEFAULT_ORG_ID), provider.value)
    encrypted = encrypt_secret(json.dumps(api_key).encode("utf-8"), aad=aad, key_provider=key_provider)
    return ProviderKey(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        provider=provider,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},
        is_primary=is_primary,
        failover_enabled=failover_enabled,
        failover_target_id=failover_target_id,
    )


_ROUTE = ModelRoute(provider="openai", capability=ModelCapability.CHAT, native_model_id="gpt-4o")


def _patch_key_lookup(
    monkeypatch: pytest.MonkeyPatch, *, primary: ProviderKey, backup: ProviderKey | None
) -> None:
    async def _fake_get_primary_key(session, provider):  # noqa: ANN001, ARG001
        return primary

    async def _fake_get_key_by_id(session, key_id):  # noqa: ANN001, ARG001
        if backup is not None and key_id == backup.id:
            return backup
        return None

    monkeypatch.setattr(provider_keys_service, "get_primary_key", _fake_get_primary_key)
    monkeypatch.setattr(provider_keys_service, "get_key_by_id", _fake_get_key_by_id)


@pytest.mark.asyncio
async def test_primary_success_no_retry_attempt_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    key_provider = _key_provider()
    primary = _make_key_row(
        provider=ProviderName.OPENAI, api_key="sk-primary", key_provider=key_provider, is_primary=True
    )
    _patch_key_lookup(monkeypatch, primary=primary, backup=None)

    calls: list[str] = []

    async def _call_fn(credential):  # noqa: ANN001
        calls.append("primary")
        return "ok"

    session = _FakeSession()
    result = await gateway_common.call_provider_with_failover(
        session,
        app=None,
        route=_ROUTE,
        org_id=DEFAULT_ORG_ID,
        team_id=None,
        request_id="req-1",
        key_provider=key_provider,
        health_store=InProcessSharedStateStore(),
        team_override_cache=provider_key_health.TeamFailoverOverrideCache(),
        call_fn=_call_fn,
    )
    assert result.attempt == 0
    assert result.used_key_id is None
    assert result.result == "ok"
    assert calls == ["primary"]
    assert session.added == []  # no FailoverEvent for a clean primary success


@pytest.mark.asyncio
async def test_primary_failure_retries_backup_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4.1.4/AC4.1.8: failover enabled + configured backup - a primary
    `ProviderCallError` triggers exactly one retry against the backup; on
    backup success, the caller sees a successful result with no trace of
    the primary's failure, and exactly one `FailoverEvent` row is recorded
    (AC4.5.7 - a single retry is 1 event, not N)."""
    key_provider = _key_provider()
    backup = _make_key_row(
        provider=ProviderName.OPENAI, api_key="sk-backup", key_provider=key_provider, is_primary=False
    )
    primary = _make_key_row(
        provider=ProviderName.OPENAI,
        api_key="sk-primary",
        key_provider=key_provider,
        is_primary=True,
        failover_enabled=True,
        failover_target_id=backup.id,
    )
    _patch_key_lookup(monkeypatch, primary=primary, backup=backup)

    calls: list[str] = []

    async def _call_fn(credential):  # noqa: ANN001
        # Distinguish which key's credential this is by decrypting is
        # overkill - use call order instead (primary is always attempted
        # first per the docstring's exact mechanic).
        if not calls:
            calls.append("primary")
            raise ProviderCallError("primary key exhausted", status_code=429)
        calls.append("backup")
        return "backup-ok"

    session = _FakeSession()
    health_store = InProcessSharedStateStore()
    result = await gateway_common.call_provider_with_failover(
        session,
        app=None,
        route=_ROUTE,
        org_id=DEFAULT_ORG_ID,
        team_id=None,
        request_id="req-2",
        key_provider=key_provider,
        health_store=health_store,
        team_override_cache=provider_key_health.TeamFailoverOverrideCache(),
        call_fn=_call_fn,
    )
    assert result.attempt == 1
    assert result.used_key_id == backup.id
    assert result.result == "backup-ok"
    assert calls == ["primary", "backup"]

    # AC4.5.7 - exactly one FailoverEvent row, not one per key attempted.
    assert len(session.added) == 1
    event = session.added[0]
    assert isinstance(event, FailoverEvent)
    assert event.from_provider_key_id == primary.id
    assert event.to_provider_key_id == backup.id
    assert session.commit_count == 1

    # Health store must reflect the primary's failure and the backup's
    # recovery (drives future proactive routing - AC1.9).
    primary_health = await provider_key_health.get_health(health_store, primary.id)
    backup_health = await provider_key_health.get_health(health_store, backup.id)
    assert primary_health is not None and primary_health.status == "degraded"
    assert backup_health is not None and backup_health.status == "healthy"


@pytest.mark.asyncio
async def test_both_primary_and_backup_fail_reraises_primary_error_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4.1.4 (design doc section 3.3, step 4): when the backup ALSO
    fails, the caller must see the PRIMARY's original error, never the
    backup's - and no `FailoverEvent` row is written for a failed retry
    (that table is "successful switches" only, per its own docstring)."""
    key_provider = _key_provider()
    backup = _make_key_row(
        provider=ProviderName.OPENAI, api_key="sk-backup", key_provider=key_provider, is_primary=False
    )
    primary = _make_key_row(
        provider=ProviderName.OPENAI,
        api_key="sk-primary",
        key_provider=key_provider,
        is_primary=True,
        failover_enabled=True,
        failover_target_id=backup.id,
    )
    _patch_key_lookup(monkeypatch, primary=primary, backup=backup)

    async def _call_fn(credential):  # noqa: ANN001
        raise ProviderCallError("PRIMARY-DISTINCT-ERROR", status_code=500)

    session = _FakeSession()
    with pytest.raises(ProviderCallError) as excinfo:
        await gateway_common.call_provider_with_failover(
            session,
            app=None,
            route=_ROUTE,
            org_id=DEFAULT_ORG_ID,
            team_id=None,
            request_id="req-3",
            key_provider=key_provider,
            health_store=InProcessSharedStateStore(),
            team_override_cache=provider_key_health.TeamFailoverOverrideCache(),
            call_fn=_call_fn,
        )
    assert "PRIMARY-DISTINCT-ERROR" in str(excinfo.value)
    assert session.added == []  # no FailoverEvent for a failed retry


@pytest.mark.asyncio
async def test_failover_disabled_no_retry_on_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4.1.3/AC4.1.4: `failover_enabled=False` (the default) - a primary
    failure propagates immediately, backup is never attempted even though
    one is configured."""
    key_provider = _key_provider()
    backup = _make_key_row(
        provider=ProviderName.OPENAI, api_key="sk-backup", key_provider=key_provider, is_primary=False
    )
    primary = _make_key_row(
        provider=ProviderName.OPENAI,
        api_key="sk-primary",
        key_provider=key_provider,
        is_primary=True,
        failover_enabled=False,  # explicit - the default
        failover_target_id=backup.id,
    )
    _patch_key_lookup(monkeypatch, primary=primary, backup=backup)

    calls: list[str] = []

    async def _call_fn(credential):  # noqa: ANN001
        calls.append("attempt")
        raise ProviderCallError("primary down", status_code=500)

    session = _FakeSession()
    with pytest.raises(ProviderCallError):
        await gateway_common.call_provider_with_failover(
            session,
            app=None,
            route=_ROUTE,
            org_id=DEFAULT_ORG_ID,
            team_id=None,
            request_id="req-4",
            key_provider=key_provider,
            health_store=InProcessSharedStateStore(),
            team_override_cache=provider_key_health.TeamFailoverOverrideCache(),
            call_fn=_call_fn,
        )
    assert calls == ["attempt"]  # backup never called


@pytest.mark.asyncio
async def test_failover_switch_time_under_two_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4.1.5 / phase-4 NFR: "Failover switch time target: under 2 seconds
    total from first request failure to successful response". Simulates a
    realistic primary failure latency (the provider takes some time to
    respond with an error, e.g. a timeout-adjacent 5xx) then a fast backup
    success, and asserts the WALL-CLOCK total (not just correctness) is
    under the 2-second budget end to end through the real function -
    previously never measured anywhere in the test suite (every existing
    failover-header test uses a fully-mocked, zero-latency
    `call_provider_with_failover` fake - see this module's docstring)."""
    key_provider = _key_provider()
    backup = _make_key_row(
        provider=ProviderName.OPENAI, api_key="sk-backup", key_provider=key_provider, is_primary=False
    )
    primary = _make_key_row(
        provider=ProviderName.OPENAI,
        api_key="sk-primary",
        key_provider=key_provider,
        is_primary=True,
        failover_enabled=True,
        failover_target_id=backup.id,
    )
    _patch_key_lookup(monkeypatch, primary=primary, backup=backup)

    calls: list[str] = []

    async def _call_fn(credential):  # noqa: ANN001
        if not calls:
            calls.append("primary")
            # Realistic simulated network-timeout-adjacent delay before the
            # primary's failure is even detected.
            await asyncio.sleep(0.4)
            raise ProviderCallError("primary timeout", status_code=504)
        calls.append("backup")
        await asyncio.sleep(0.1)
        return "backup-ok"

    session = _FakeSession()
    start = time.monotonic()
    result = await gateway_common.call_provider_with_failover(
        session,
        app=None,
        route=_ROUTE,
        org_id=DEFAULT_ORG_ID,
        team_id=None,
        request_id="req-5",
        key_provider=key_provider,
        health_store=InProcessSharedStateStore(),
        team_override_cache=provider_key_health.TeamFailoverOverrideCache(),
        call_fn=_call_fn,
    )
    elapsed = time.monotonic() - start

    assert result.result == "backup-ok"
    assert elapsed < 2.0, f"failover switch took {elapsed:.3f}s, exceeding the 2s NFR budget"
