"""Fix 4 (originally a QA/security review finding, fixed here):
`api.v1.gateway.common.check_response_cache()` used to resolve the cache
key's residency-zone component with `provider_key_metadata` HARD-CODED to
`None` (`response_cache_service.resolve_cache_residency_zone(route, None)`),
whereas the real residency ENFORCEMENT check (`check_residency()`, a few
functions above it in the same module) fetches the provider key's REAL
`key_metadata` from the database for `vertex_ai`/`ollama` routes before
resolving a region.

Per `services.residency.resolve_model_region()`'s own documented contract,
`provider_key_metadata=None` for `vertex_ai`/`ollama` ALWAYS resolves to
`None` (region unknown) regardless of what region the key is actually
configured for. `resolve_cache_residency_zone()` then normalizes an unknown
region to the literal string `"unknown"`. Practical impact (AC4.3.6's third
bullet): for `vertex_ai`/`ollama` specifically, EVERY cache entry was
written under `residency_zone="unknown"`, regardless of which real region
the key serving it was actually configured for - defeating the whole point
of `residency_zone` being part of the cache key for those two providers.

Fixed by extracting the exact same key_metadata lookup `check_residency()`
already performs into a shared `_resolve_provider_key_metadata()` helper and
calling it from `check_response_cache()` too, before resolving the cache
key's residency zone. This test file now proves the FIX: the real pipeline
call site DOES differentiate two different vertex_ai regions once real
provider-key metadata exists, via the actual `check_response_cache()` code
path a live request takes (not just by re-testing the underlying pure
function in isolation, which was already proven correct given real input
even before the fix - see the first test below).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import BackgroundTasks

from gatekey.api.deps import GatewayCallerContext
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers.model_registry import ModelCapability, ModelRoute
from gatekey.services import provider_keys as provider_keys_service
from gatekey.services.response_cache import (
    CachingSettingsCache,
    ResponseCache,
    TeamCachingSettingsSnapshot,
    resolve_cache_residency_zone,
)
from gatekey.services.shared_state import InProcessSharedStateStore

_VERTEX_ROUTE = ModelRoute(
    provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="gemini-1.5-pro"
)


def test_resolve_cache_residency_zone_can_differentiate_vertex_regions_given_real_metadata() -> None:
    """Sanity check: the underlying function itself is not broken - given
    real key metadata, two different vertex_ai regions DO resolve to
    different zones."""
    us_zone = resolve_cache_residency_zone(_VERTEX_ROUTE, {"location": "us-central1"})
    eu_zone = resolve_cache_residency_zone(_VERTEX_ROUTE, {"location": "europe-west4"})
    assert us_zone != eu_zone
    assert us_zone != "unknown"
    assert eu_zone != "unknown"


class _FakeVertexKeyRow:
    """Minimal stand-in for a `ProviderKey` row - only `key_metadata` is
    read by `_resolve_provider_key_metadata()`."""

    def __init__(self, location: str) -> None:
        self.key_metadata = {"location": location}


async def _check_response_cache_for_region(
    monkeypatch: pytest.MonkeyPatch, *, location: str
) -> str:
    async def _fake_get_key(session, provider):  # noqa: ANN001, ARG001
        assert provider == "vertex_ai"
        return _FakeVertexKeyRow(location)

    monkeypatch.setattr(provider_keys_service, "get_key", _fake_get_key)

    ctx = GatewayCallerContext(
        org_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        credential_type="service_account",
        user_id=uuid.uuid4(),
        team_id=uuid.uuid4(),
        name="test-service-account",
    )
    response_cache = ResponseCache(InProcessSharedStateStore())
    # Fix 6: `check_response_cache()` now reads from `CachingSettingsCache`
    # (cache-backed) instead of `load_effective_caching_config()` (live DB) -
    # seed this request's team directly (org entry absent -> `enabled=True`
    # default, see `resolve_effective_caching_config()`'s docstring).
    caching_settings_cache = CachingSettingsCache(
        team_settings={ctx.team_id: TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)}
    )

    # `session` only needs to be a value `provider_keys_service.get_key`
    # (monkeypatched above) accepts - it's never touched for real.
    result = await gateway_common.check_response_cache(
        session=None,  # type: ignore[arg-type]
        ctx=ctx,
        route=_VERTEX_ROUTE,
        request_body={"model": "gemini-1.5-pro", "messages": []},
        response_cache=response_cache,
        background_tasks=BackgroundTasks(),
        app=None,  # type: ignore[arg-type]
        caching_settings_cache=caching_settings_cache,
    )
    return result.residency_zone


@pytest.mark.asyncio
async def test_check_response_cache_differentiates_vertex_regions_via_real_pipeline_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 4, confirmed end-to-end: the real `check_response_cache()` call
    site now supplies the REAL key metadata (via the same lookup
    `check_residency()` already performed), so two different vertex_ai
    regions produce two different cache-key residency zones - never both
    collapsing into `"unknown"`."""
    us_zone = await _check_response_cache_for_region(monkeypatch, location="us-central1")
    eu_zone = await _check_response_cache_for_region(monkeypatch, location="europe-west4")

    assert us_zone != "unknown"
    assert eu_zone != "unknown"
    assert us_zone != eu_zone


@pytest.mark.asyncio
async def test_check_response_cache_falls_back_to_unknown_when_no_key_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No vertex_ai key configured at all yet -> `_resolve_provider_key_
    metadata()` returns `None` -> `"unknown"`, exactly as documented (not a
    crash, not a guessed region)."""

    async def _fake_get_key_none(session, provider):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(provider_keys_service, "get_key", _fake_get_key_none)

    ctx = GatewayCallerContext(
        org_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        credential_type="service_account",
        user_id=uuid.uuid4(),
        team_id=uuid.uuid4(),
        name="test-service-account",
    )
    response_cache = ResponseCache(InProcessSharedStateStore())
    caching_settings_cache = CachingSettingsCache(
        team_settings={ctx.team_id: TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)}
    )

    result = await gateway_common.check_response_cache(
        session=None,  # type: ignore[arg-type]
        ctx=ctx,
        route=_VERTEX_ROUTE,
        request_body={"model": "gemini-1.5-pro", "messages": []},
        response_cache=response_cache,
        background_tasks=BackgroundTasks(),
        app=None,  # type: ignore[arg-type]
        caching_settings_cache=caching_settings_cache,
    )
    assert result.residency_zone == "unknown"
