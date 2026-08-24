"""Live per-provider "what models does this provider actually have" catalog
lookup (Model Catalog + Cross-Provider Fallback Chains technical design doc,
Part A / section 1). Builds directly on the Custom Model Registry (CMR) -
see `services/custom_models.py`'s module docstring for the conventions this
module reuses (`get_decrypted_provider_credential()`'s credential-fetch
path, the `ProviderCallError` -> `errors.ProviderUpstreamError` translation
pattern) rather than restating them.

A separate module from `services/custom_models.py`, not appended to it - a
read-only, no-DB-write, admin-console-triggered live lookup is a genuinely
different concern from that module's CRUD/verification/routing-cache
responsibilities (design doc section 1.5's own framing).

Zero-I/O Vertex AI carve-out
------------------------------
`provider == "vertex_ai"` is rejected immediately, before any credential
fetch or other I/O - see `CustomModelLiveListingUnsupportedError`'s
docstring for the full rationale (design doc section 1.1): Vertex AI Model
Garden's listing response shape is not independently verified the way
OpenAI/Anthropic/OpenRouter's are, its project/location-scoping semantics
for a listing call (as opposed to a real inference call) are ambiguous, and
it returns no per-model pricing even when it works. A Vertex AI custom
model is registered today (and remains registered) by typing
`native_model_id` manually - this endpoint still accepts `"vertex_ai"` as a
valid `provider` path value so the frontend never needs its own,
independently-maintained copy of "which providers support live listing".

The "known static price" reverse index
------------------------------------------
OpenAI/Anthropic's live listing responses carry no pricing at all - built
once, at import time (mirroring `providers/pricing.py`'s own "hand-curated
dict at import time" convention), `_NATIVE_ID_TO_PRICING` joins every
`MODEL_REGISTRY` entry to its `PRICING_TABLE` row, keyed by
`(provider, native_model_id)`. Every returned OpenAI/Anthropic
`AvailableModelEntry` whose `native_model_id` happens to match a
`MODEL_REGISTRY` route for that same provider gets its price fields
populated straight from this lookup; everything else gets `None`. This
reuses `providers/pricing.py`'s own `PRICING_TABLE`/`_validate_completeness()`
completeness guarantee (every `MODEL_REGISTRY` key already has a matching
`PRICING_TABLE` row) - no separate correctness check is needed here.
OpenRouter entries are the one exception: they already carry real, live
per-model pricing straight from `providers.openrouter.list_models()`'s own
response parsing, so this reverse index is never consulted for them - see
`list_available_models()` below.

See `schemas/custom_model.py::AvailableModelEntry` for the response shape
this module builds, and that schema's own docstring for why it must never
import `providers.pricing.PRICING_TABLE` itself (this module is where that
join happens instead).
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.custom_model import CustomModel
from gatekey.errors import GatekeyError, ProviderNotConfiguredError, ProviderUpstreamError
from gatekey.providers import anthropic as anthropic_provider
from gatekey.providers import openai as openai_provider
from gatekey.providers import openrouter as openrouter_provider
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.providers.pricing import PRICING_TABLE, PricingEntry
from gatekey.schemas.custom_model import AvailableModelEntry
from gatekey.services.encryption import KeyProvider
from gatekey.services.proxy_keys import (
    ApiKeyCredential,
    ProviderKeyNotConfiguredError,
    get_decrypted_provider_credential,
)


class CustomModelLiveListingUnsupportedError(GatekeyError):
    """`provider == "vertex_ai"` was requested against the live-listing
    endpoint - Vertex AI Model Garden's listing response shape has not been
    independently verified against this codebase's other three providers'
    confirmed shapes, and its `publishers/google/models` endpoint returns no
    per-model pricing even when it works - see the Model Catalog technical
    design doc section 1.1 for the full, deliberate rationale. 422; register
    a Vertex AI custom model by typing `native_model_id` manually instead.
    """

    status_code = 422
    code = "custom_model_live_listing_unsupported"

    def __init__(self) -> None:
        super().__init__(
            "Live model listing is not supported for provider 'vertex_ai' - "
            "type native_model_id manually when registering a Vertex AI "
            "custom model instead. See the Model Catalog design doc section "
            "1.1 for why this provider is excluded from live listing."
        )


class _NativeIdKey(NamedTuple):
    provider: str
    native_model_id: str


def _build_native_id_to_pricing_index() -> dict[_NativeIdKey, PricingEntry]:
    """Build the "known static price" reverse index once, at import time -
    see module docstring.

    Deliberately keyed on `(provider, native_model_id)`, NOT on the
    `MODEL_REGISTRY` gateway-facing key itself - a live-listing entry's
    `native_model_id` is the provider's own real model id string (e.g.
    `"gpt-4o"`), the same value `ModelRoute.native_model_id` carries, not
    Gatekey's own registry key (which happens to be identical for most
    entries here, but is not guaranteed to be in general - e.g. the
    `ollama/`/`openrouter/`-prefixed registry keys carry a DIFFERENT
    `native_model_id`).

    Maps to a 2-tuple of `(input_price, output_price)` `Decimal`s via a
    lookup table keyed exactly like the entries dict itself, so
    `list_available_models()` gets a single dict lookup, not a second
    `PRICING_TABLE` join at request time.
    """
    index: dict[_NativeIdKey, PricingEntry] = {}
    for route in MODEL_REGISTRY.values():
        pricing_entry = PRICING_TABLE.get(route.native_model_id)
        # Defensive only: `providers.pricing._validate_completeness()`
        # already guarantees every MODEL_REGISTRY key (not native_model_id)
        # has a PRICING_TABLE row, keyed by the registry key - lookup here
        # is by native_model_id, which can differ (see docstring above), so
        # this branch IS reachable for entries whose registry key isn't
        # itself a valid PRICING_TABLE key (falls back to unpriced, never
        # crashes the catalog listing over it).
        if pricing_entry is None:
            continue
        index[_NativeIdKey(route.provider, route.native_model_id)] = pricing_entry
    return index


# NOTE: `_build_native_id_to_pricing_index()` above stores the WHOLE
# `PricingEntry`, not a bare tuple - see `_native_id_to_pricing_entry()`.
_NATIVE_ID_TO_PRICING = _build_native_id_to_pricing_index()


def _native_id_to_pricing_entry(provider: str, native_model_id: str) -> tuple[Decimal | None, Decimal | None]:
    """Look up `(input_price, output_price)` for one OpenAI/Anthropic
    live-listing entry against the reverse index above. `(None, None)` if
    this `(provider, native_model_id)` pair doesn't match any current
    `MODEL_REGISTRY` route for that provider."""
    entry = _NATIVE_ID_TO_PRICING.get(_NativeIdKey(provider, native_model_id))
    if entry is None:
        return None, None
    return entry.input_price_per_million_usd, entry.output_price_per_million_usd


def _build_native_id_to_registry_name_index() -> dict[_NativeIdKey, str]:
    """`(provider, native_model_id) -> MODEL_REGISTRY key`, built once at
    import time - the "is this live-listing entry already routable, and
    under what name" half of `routable_as` (see `AvailableModelEntry`'s
    docstring). Covers every provider (unlike `_NATIVE_ID_TO_PRICING`, which
    only needs openai/anthropic - vertex_ai never reaches this endpoint, but
    its registry entries are harmless to index anyway) since a provider-key
    admin flow (Model Policy's "select models to enable" picker) needs to
    know this for openrouter too, where it's genuinely useful (an
    `openrouter/...`-prefixed registry key's `native_model_id` has no
    `openrouter/` prefix, so the two strings differ and a caller cannot
    derive one from the other without this index)."""
    return {_NativeIdKey(route.provider, route.native_model_id): name for name, route in MODEL_REGISTRY.items()}


_NATIVE_ID_TO_REGISTRY_NAME = _build_native_id_to_registry_name_index()


async def _verified_custom_model_names_by_native_id(session: AsyncSession, provider: str) -> dict[str, str]:
    """`native_model_id -> name` for every VERIFIED `custom_models` row this
    org has registered for `provider` - the other half of `routable_as`
    (an unverified custom model isn't actually routable yet, so it must not
    be reported as one - mirrors `CustomModelRouteCache`'s own
    `verified = true` gate, see `services.custom_models.
    load_custom_model_route_snapshot()`'s docstring)."""
    stmt = select(CustomModel.native_model_id, CustomModel.name).where(
        CustomModel.org_id == DEFAULT_ORG_ID, CustomModel.provider == provider, CustomModel.verified.is_(True)
    )
    return {row[0]: row[1] for row in (await session.execute(stmt)).all()}


def _routable_as(
    provider: str, native_model_id: str, *, verified_custom_by_native_id: dict[str, str]
) -> str | None:
    """The Gatekey-facing model name this live-listing entry is ALREADY
    routable under, if any - `None` if it would need to be registered as a
    Custom Model (with admin-set pricing) before it could ever be enabled in
    org model policy. The static registry always wins over a same-provider
    custom-model match, mirroring `services.custom_models._validate_custom_
    model_write()`'s identical "the static registry always wins at request
    time" precedent - though a genuine collision here would mean the SAME
    native_model_id is claimed by both a registry route and a custom model
    for this provider, which that write-time guard already prevents from
    ever being registered in the first place."""
    registry_name = _NATIVE_ID_TO_REGISTRY_NAME.get(_NativeIdKey(provider, native_model_id))
    if registry_name is not None:
        return registry_name
    return verified_custom_by_native_id.get(native_model_id)


async def list_available_models(
    session: AsyncSession,
    provider: str,
    *,
    key_provider: KeyProvider,
    http_client: httpx.AsyncClient,
) -> list[AvailableModelEntry]:
    """Live "what models does this provider actually have" lookup for one
    of the four BYOK providers - see module docstring / design doc section
    1.5.

    Raises:
        `CustomModelLiveListingUnsupportedError` (422) - `provider ==
            "vertex_ai"`. Checked FIRST, before any I/O of any kind.
        `errors.ProviderNotConfiguredError` (404) - no `provider_keys` row
            configured for `provider` in this org yet. Enforced uniformly
            for all three listable providers, including OpenRouter, even
            though OpenRouter's own live GET needs no auth at all - see
            `providers/openrouter.py`'s `list_models()` docstring for why.
        `errors.ProviderUpstreamError` (502-shaped) - the live listing call
            itself failed (bad/revoked key, transient network error, a
            non-2xx response) - the identical translation
            `services.custom_models.verify_custom_model()` already performs
            on the same underlying `providers.base.ProviderCallError`.

    Returns the mapped `AvailableModelEntry` list, sorted by
    `native_model_id`.
    """
    if provider == "vertex_ai":
        raise CustomModelLiveListingUnsupportedError()

    try:
        credential = await get_decrypted_provider_credential(
            session, provider, key_provider=key_provider
        )
    except ProviderKeyNotConfiguredError as exc:
        raise ProviderNotConfiguredError(exc.message) from None

    # `services.proxy_keys._API_KEY_PROVIDERS` is exactly
    # `("openai", "anthropic", "openrouter")` - the three providers
    # reachable at this point (vertex_ai, the only `ServiceAccountCredential`
    # shape, is excluded above) always decrypt to an `ApiKeyCredential`.
    # This assertion documents that invariant rather than silently
    # mis-typing `credential` below.
    assert isinstance(credential, ApiKeyCredential), (
        f"list_available_models(): provider={provider!r} resolved a non-API-key "
        "credential - should be unreachable, vertex_ai (the only "
        "ServiceAccountCredential provider) is excluded above."
    )

    try:
        if provider == "openai":
            entries = await openai_provider.list_models(http_client, credential)
            entries = [
                _apply_registry_pricing(entry, provider="openai") for entry in entries
            ]
        elif provider == "anthropic":
            entries = await anthropic_provider.list_models(http_client, credential)
            entries = [
                _apply_registry_pricing(entry, provider="anthropic") for entry in entries
            ]
        elif provider == "openrouter":
            # OpenRouter's list_models() already fills in live pricing
            # itself - no reverse-index lookup applies (module docstring).
            entries = await openrouter_provider.list_models(http_client)
        else:
            # Unreachable: `get_decrypted_provider_credential()` above would
            # already have raised for any provider outside SUPPORTED_PROVIDERS,
            # and vertex_ai is excluded before any I/O. Kept as an explicit,
            # safe failure mode for any caller invoking this service
            # directly with an unexpected `provider` string, mirroring
            # `services.custom_models.CustomModelUnsupportedProviderError`'s
            # docstring rationale.
            raise AssertionError(
                f"list_available_models(): no live-listing dispatch for "
                f"provider {provider!r} - should be unreachable."
            )
    except ProviderCallError as exc:
        raise ProviderUpstreamError(exc.message, upstream_status_code=exc.status_code) from None

    verified_custom_by_native_id = await _verified_custom_model_names_by_native_id(session, provider)
    entries = [
        entry.model_copy(
            update={
                "routable_as": _routable_as(
                    provider, entry.native_model_id, verified_custom_by_native_id=verified_custom_by_native_id
                )
            }
        )
        for entry in entries
    ]

    return sorted(entries, key=lambda entry: entry.native_model_id)


def _apply_registry_pricing(entry: AvailableModelEntry, *, provider: str) -> AvailableModelEntry:
    """Fill in `input_price_per_million_usd`/`output_price_per_million_usd`
    for one OpenAI/Anthropic `AvailableModelEntry` from the "known static
    price" reverse index, if its `native_model_id` matches a current
    `MODEL_REGISTRY` route for `provider` - left as-is (both `None`,
    exactly as `providers.openai.list_models()`/`providers.anthropic.
    list_models()` already return them) otherwise. See module docstring.
    """
    input_price, output_price = _native_id_to_pricing_entry(provider, entry.native_model_id)
    if input_price is None and output_price is None:
        return entry
    return entry.model_copy(
        update={
            "input_price_per_million_usd": input_price,
            "output_price_per_million_usd": output_price,
        }
    )
