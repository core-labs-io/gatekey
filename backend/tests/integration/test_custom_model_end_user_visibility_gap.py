"""QA finding (CMR-12): the product spec's own section 1 user story is not
met - `gatekey/custom-model-registry-product-spec.md` section 1 explicitly
lists "the end-user Model Access view" as one of the surfaces where "I only
ever see a verified custom model appear indistinguishably alongside every
static-registry model, wherever models are already surfaced to me today
(Model Policy's checklist for an Org Admin/Auditor; the end-user Model Access
view; the OpenAI-compatible gateway itself)."

`GET /v1/model-access` (`api/v1/model_access.py::get_model_access_endpoint`,
the exact backend for the non-admin "Model Access" self-service screen,
`frontend/app/model-access/page.tsx`) iterates ONLY `sorted(MODEL_REGISTRY)`
- it has no dependency on `CustomModelRouteCache` (or
`SelfHostedModelRouteCache`) at all, confirmed by direct inspection (zero
occurrences of "self_hosted"/"custom_model" in that module). A verified,
fully-routable custom model therefore NEVER appears on this screen for a
Team Lead/Member/Auditor, even though it is fully usable at the gateway and
fully visible in Model Policy's admin checklist (CMR-11) - a real,
demonstrable violation of the product spec's own explicit user story, not
just a nice-to-have gap.

Not a NEW regression this feature introduced - `SelfHostedModelRouteCache`
has the IDENTICAL pre-existing omission from this same endpoint (Phase 5.5
never wired it here either, and no phase-5 doc flags it as a known
limitation). But `gatekey/custom-model-registry-technical-design.md`
section 5's 26-row "mandatory wiring checklist" never mentions
`api/v1/model_access.py` at all, so this specific commitment in the CMR
product spec's own section 1 was never actually implemented for custom
models either - flagged back to the orchestrator (design/wiring gap) rather
than silently treated as covered by "Model Policy checklist" alone, since
the product spec names these as two SEPARATE surfaces, not one.

`resolve_model_access()` itself is fully generic (no `MODEL_REGISTRY`
dependency - confirmed by reading `services/model_policy.py`), so a fix is
a small, well-contained addition to `get_model_access_endpoint`'s model
enumeration - not attempted here since it is out of QA's remit (tracked for
whichever agent picks up CMR-12/CMR-13 findings).

FIX (landed): `api/v1/model_access.py::get_model_access_endpoint` now unions
`CustomModelRouteCache.known_model_ids()` AND
`SelfHostedModelRouteCache.known_model_ids()` into its model enumeration -
both caches only ever contain `verified=true` rows by construction (see
each cache class's own docstring), so this fix closes the gap for BOTH
custom and self-hosted models in one pass without needing a separate
`verified` re-check. The `xfail(strict=True)` marker has been removed - the
test below now asserts the real, fixed behavior. A second test below
(added as part of the CMR-14 fix, not the original QA pass) proves an
UNVERIFIED custom model correctly still does NOT appear, exercising the
real DB-backed snapshot loader (`load_custom_model_route_snapshot`) rather
than just an in-memory cache built by hand.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

import asyncpg

from gatekey.api.deps import get_custom_model_route_cache
from gatekey.services.custom_models import (
    CustomModelCacheEntry,
    CustomModelRouteCache,
    load_custom_model_route_snapshot,
    register_custom_model,
)

from .conftest import to_asyncpg_dsn
from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    make_user,
    session_cookie_headers,
    sf,
)
from gatekey.providers.model_registry import ModelCapability
from decimal import Decimal
import uuid

pytestmark = pytest.mark.asyncio


async def _truncate_custom_models(migrated_database_url: str) -> None:
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE custom_models CASCADE")
    finally:
        await conn.close()


async def test_verified_custom_model_appears_in_end_user_model_access_view(
    app: FastAPI, sf
) -> None:
    cache = CustomModelRouteCache()
    entry = CustomModelCacheEntry(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="visible-model-native-id",
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("2.00"),
    )
    cache.set_all({"my-verified-custom-model-visibility": entry})

    member_id = await make_user(sf, "cmr12-visibility-member")
    cookie = await session_cookie_headers(sf, member_id)

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/v1/model-access", headers=cookie)
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 200, response.text
    models = {row["model"] for row in response.json()["models"]}
    assert "my-verified-custom-model-visibility" in models, (
        "verified custom model missing from the end-user Model Access view - "
        f"got models: {sorted(models)}"
    )


async def test_unverified_custom_model_does_not_appear_in_end_user_model_access_view(
    app: FastAPI, sf, migrated_database_url: str
) -> None:
    """Added alongside the CMR-14 fix (not part of QA's original xfail) -
    exercises the REAL DB-backed `load_custom_model_route_snapshot()`
    (never just a hand-built cache) against one verified and one unverified
    `custom_models` row, proving the end-user view's verified-only
    discipline holds end to end, matching the org-wide model-policy check's
    own verified-only rule (module docstring)."""
    await _truncate_custom_models(migrated_database_url)
    try:
        async with sf() as session:
            verified_row = await register_custom_model(
                session,
                name="cmr12-visibility-verified",
                provider="openai",
                native_model_id="visibility-verified-native-id",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.00"),
                output_price_per_million_usd=Decimal("2.00"),
                pricing_source=None,
            )
            verified_row.verified = True
            await session.commit()

            await register_custom_model(
                session,
                name="cmr12-visibility-unverified",
                provider="openai",
                native_model_id="visibility-unverified-native-id",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.00"),
                output_price_per_million_usd=Decimal("2.00"),
                pricing_source=None,
            )
            # Registration never auto-verifies (module docstring) - this
            # second row is left `verified=False` deliberately.

            snapshot = await load_custom_model_route_snapshot(session)

        cache = CustomModelRouteCache()
        cache.set_all(snapshot)

        member_id = await make_user(sf, "cmr12-visibility-member-unverified")
        cookie = await session_cookie_headers(sf, member_id)

        app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    response = await client.get("/v1/model-access", headers=cookie)
        finally:
            app.dependency_overrides.pop(get_custom_model_route_cache, None)

        assert response.status_code == 200, response.text
        models = {row["model"] for row in response.json()["models"]}
        assert "cmr12-visibility-verified" in models, (
            f"verified custom model missing from the end-user Model Access "
            f"view - got models: {sorted(models)}"
        )
        assert "cmr12-visibility-unverified" not in models, (
            "UNVERIFIED custom model leaked into the end-user Model Access "
            f"view - got models: {sorted(models)}"
        )
    finally:
        await _truncate_custom_models(migrated_database_url)
