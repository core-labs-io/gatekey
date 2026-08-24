"""DB-backed CRUD service + process-local route cache for admin-registered
custom models (Custom Model Registry / Admin-Managed BYOK Models).

See `gatekey/custom-model-registry-technical-design.md` (the authoritative
spec this module implements - "technical design doc" below) and
`gatekey/custom-model-registry-product-spec.md` section 2/12 for the
product-level rationale. Mirrors `services/self_hosted_providers.py`'s CRUD/
cache/collision-guard/verified-gate shape everywhere the two features are
genuinely alike, and diverges only where BYOK-specific requirements demand
it - see that module's docstring for the precedent this one reuses.

No new credential type
------------------------
Unlike self-hosted, a custom model stores no secret of any kind - it rides
the org's EXISTING, already-encrypted `provider_keys` row for its
`provider`, fetched via the identical `services.proxy_keys.
get_decrypted_provider_credential()` every real gateway request already
calls (`verify_custom_model()` below). There is no new AES-GCM envelope, no
new AAD binding, and (unlike self-hosted's `get_decrypted_self_hosted_
credential`) no credential-fetch function of this module's own.

Real per-token pricing, not a GPU-hour estimate
--------------------------------------------------
`compute_custom_model_cost()` replicates `services.budget.compute_cost()`'s
exact per-token arithmetic against the row's own admin-entered
`input_price_per_million_usd`/`output_price_per_million_usd` - this is
real, invoice-grade pricing (an admin-entered real provider rate), never an
"estimated" figure the way `providers.pricing.compute_self_hosted_cost()`'s
GPU-hour proxy is. It is placed here (not `providers/pricing.py`) per the
technical design doc's explicit instruction - it operates on a
services-layer `CustomModelCacheEntry`, not a `providers`-layer
`PricingEntry`.

`ModelRoute.custom_model_id` - the discriminator, not `route.provider`
---------------------------------------------------------------------------
A custom model's `ModelRoute.provider` carries the REAL BYOK provider value
(`"openai"`/etc.), unlike self-hosted's synthetic `"self_hosted"` sentinel -
this is what lets a custom model route through the gateway's existing
provider-dispatch branches completely unmodified. The tradeoff: no code can
tell "is this a custom-model request" from `route.provider` alone anymore.
`providers.model_registry.ModelRoute.custom_model_id` (populated only by
`resolve_route()`'s custom-model-cache fallback, wired in a later task) is
the sole sanctioned discriminator every downstream cost/audit branch must
test - see that field's own docstring and technical design doc section 2.2.

`CustomModelRouteCache` (verified-gate mechanism)
------------------------------------------------------
Same whole-snapshot-replace, lock-free, GIL-atomic convention as
`SelfHostedModelRouteCache` - see that class's docstring for the full
rationale. `load_custom_model_route_snapshot()` is the ONLY function that
queries `custom_models` with `WHERE verified = true` - every entry that
ever lands in the cache is therefore, by construction, already verified;
nothing downstream needs to separately re-check a `verified` flag on the
entry itself (an unverified custom model's name is simply never present in
the cache at all).

Bidirectional collision guard - this module's half only
-------------------------------------------------------------
Two independent name-collision guards apply when an admin registers/edits a
custom model (`_validate_custom_model_write` below):

1. `name` may never collide with a static `MODEL_REGISTRY` key - the static
   registry always wins at request time (`resolve_route()` tries it FIRST,
   unconditionally), so registering a custom-model name that shadows a real
   registry key would silently make that custom-model entry permanently
   unreachable. Rejected at write time with a clear message (422) rather
   than left as a confusing runtime no-op - technical design doc section
   2.1 guard #1 / section 4.1's own stated NFR.
2. `name` may never collide with a model id already claimed by a
   `self_hosted_providers` row in this org (technical design doc section
   2.1 guard #2 / section 5 row 16) - queries the `SelfHostedProvider` ORM
   class DIRECTLY (`_self_hosted_model_ids_for_org` below), never
   `services.self_hosted_providers` (the service module), to avoid a
   circular import: that module needs the mirror-image check against
   `CustomModel` (technical design doc section 5 row 15, a SEPARATE task,
   not implemented by this module) and importing each other's service
   layer in both directions would be circular. This file implements only
   ITS OWN half of the bidirectional guard.

   CONCURRENCY (CMR-12/CMR-14 QA fix): this guard and its mirror image in
   `services/self_hosted_providers.py::_validate_model_ids` are a
   cross-table invariant - `custom_models` and `self_hosted_providers` are
   different tables with no single row of their own to lock across both.
   Both sides therefore take `SELECT ... FOR UPDATE` on the SAME per-org
   `org_settings` row (`_lock_org_settings_for_model_name_guard` below)
   BEFORE running their collision SELECT, exactly mirroring
   `services/team_budget.py`'s `_lock_team`/`set_org_budget_ceiling`
   ADR-5-style lock-then-check-then-write pattern (and Phase 5.2's
   `compliance_settings`-row lock for its own, differently-scoped
   cross-table invariant). `org_settings` is reused purely as a stable,
   already-guaranteed-derivable per-org mutex here - its own
   budget-ceiling columns are untouched by this lock's callers. This
   closes the race where a concurrent `register_custom_model(name=X)` and
   `register_self_hosted_provider(models=[X])` could both pass their
   collision SELECT before either committed and both succeed.
3. `name` may never collide with ANOTHER `custom_models` row in this org -
   enforced via the table's own `UNIQUE(org_id, name)` constraint (a 409 on
   `IntegrityError`, exactly `SelfHostedProviderNameConflictError`'s
   pattern), not a separate pre-write SELECT.

Embeddings-provider guard (technical design doc section 2.5a)
-------------------------------------------------------------------
`capability == "embeddings"` is only valid for `provider in ("openai",
"vertex_ai")` - confirmed by direct inspection of `providers/anthropic.py`/
`providers/openrouter.py` (neither has a `create_embeddings` function at
all) and `api/v1/gateway/embeddings.py`'s dispatch, which 422s any other
provider on every single request. Without this write-time guard, an admin
could register a `capability=embeddings` custom model on `anthropic`/
`openrouter` that passes registration and verification-adjacent checks fine
and then 422s on every real request - a confusing, always-broken state
this guard prevents from ever being created.

Verification never charges budget or writes `usage_logs`
--------------------------------------------------------------
`verify_custom_model()` fires exactly one minimal live provider call and
NEVER calls `check_model_policy`/`check_residency`/`run_dlp_scan`/
`check_budget_available`/`record_usage_charge` - mirrors Phase 5.4's canary
calls' identical bypass of the same gateway-pipeline steps (AC5.4.9's
canary-cost-isolation principle). A 30-second per-row cooldown
(`_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS`) is tracked via a lightweight
in-process, per-`custom_model_id` timestamp marker (NOT a DB column -
`db.models.custom_model.CustomModel` has none, by CMR-1's design, and this
is explicitly named as an acceptable alternative to a `custom_model.
test_call` audit-entry query in the technical design doc section 2.3) -
this is a soft, best-effort, process-local abuse guard (resets on restart),
not a hard distributed rate limiter, matching the design doc's own framing
of it as a "defensive cost/abuse guard, not a product requirement".

Audit entry for `verify_custom_model()`, deliberately NOT written here
------------------------------------------------------------------------------
The technical design doc's section 2.3 pseudocode shows a `custom_model.
test_call` audit write as the final step of the verify flow. This module
does NOT write it directly - `write_audit_entry()` requires an
`AdminContext`/`SessionContext` actor, which only exists at the HTTP
route-handler layer, and the real, verified precedent this module mirrors
(`services.self_hosted_providers.reverify_self_hosted_provider`) follows
the identical discipline: the service function performs and commits the
live probe: the calling admin-router endpoint (a later task, not part of
this module) writes the audit entry afterward, exactly the way `api/v1/
admin/self_hosted_providers.py`'s `reverify_self_hosted_provider_endpoint`
already does. `verify_custom_model()`'s caller can time its own call (the
same way that router could, via `time.perf_counter()`) to get the
`latency_ms` the audit entry's `new_value` wants.

No FK from `usage_logs`
--------------------------
See `db.models.custom_model.CustomModel`'s module docstring - deliberate,
not implemented here or anywhere else.

`fallback_model_names` write-time validation (Model Catalog + Cross-Provider
Fallback Chains, Part B)
-------------------------------------------------------------------------------
See `gatekey/model-catalog-fallback-chains-technical-design.md` section 2.3
for the full rationale. `_validate_fallback_model_names()` is a SEPARATE
helper from `_validate_custom_model_write()`'s other guards (called from it,
not merged into it) because it validates a fundamentally different kind of
thing - not this row's own field values, but whether each entry in a list
resolves to some OTHER routable model right now. Deliberately short-circuits
to a zero-I/O no-op when `fallback_model_names` is empty (the overwhelming
majority of every create/edit, including every pre-this-feature row) rather
than unconditionally issuing the two lookup queries below - matching this
codebase's established "zero I/O for the common, feature-not-configured
case" discipline (e.g. `check_and_apply_degradation()`'s identical early
return in `api/v1/gateway/common.py`).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.custom_model import CustomModel
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.self_hosted_provider import SelfHostedProvider
from gatekey.errors import (
    GatekeyError,
    NotFoundError,
    ProviderNotConfiguredError,
    ProviderUpstreamError,
)
from gatekey.providers import anthropic as anthropic_provider
from gatekey.providers import openai as openai_provider
from gatekey.providers import openrouter as openrouter_provider
from gatekey.providers import vertex_ai as vertex_ai_provider
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.schemas.chat import ChatCompletionRequest, ChatMessage, EmbeddingsRequest
from gatekey.services.encryption import KeyProvider
from gatekey.services.proxy_keys import (
    ApiKeyCredential,
    ProviderCredential,
    ProviderKeyNotConfiguredError,
    ServiceAccountCredential,
    get_decrypted_provider_credential,
)

# Strict subset of `providers.registry.SUPPORTED_PROVIDERS` - deliberately
# excludes `"ollama"` (see module docstring / `db.models.custom_model.
# CustomModel`'s own docstring). Mirrors `chk_custom_models_provider`.
_BYOK_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "vertex_ai", "openrouter"})

# See module docstring "Embeddings-provider guard". Confirmed by direct
# inspection of `providers/anthropic.py`/`providers/openrouter.py` (neither
# module defines a `create_embeddings` function) and `api/v1/gateway/
# embeddings.py`'s provider dispatch.
_EMBEDDINGS_CAPABLE_PROVIDERS: frozenset[str] = frozenset({"openai", "vertex_ai"})

# Module constant, mirroring the existing convention of hardcoded,
# non-`Settings`-configurable scheduler/cooldown constants elsewhere in this
# codebase (e.g. `PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS`) - technical
# design doc section 6.2.
_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS = 30

# Model Catalog + Cross-Provider Fallback Chains (technical design doc
# section 2.3): each configured entry, on total exhaustion, costs one full
# extra pipeline pass plus one real outbound provider round trip (section
# 2.5) - worst-case added request latency scales linearly with chain length,
# and unlike the existing same-provider key failover (which is capped at
# exactly one retry by construction, since a `ProviderKey` has exactly one
# `failover_target_id`), a cross-provider chain needs more than one
# candidate to be useful at all (the whole point is surviving more than one
# provider having a bad moment simultaneously). Five is small enough to
# bound worst-case latency to roughly the cost of five ordinary requests'
# worth of timeout budget, while still comfortably exceeding "more than one
# backup" for any realistic set of interchangeable models an admin would
# configure.
_MODEL_FALLBACK_MAX_CHAIN_LENGTH = 5

# Fixed, minimal verification payloads (technical design doc section 2.3
# step 4) - synthetic, non-user content, never logged/charged.
_VERIFY_CHAT_PROMPT = "ping"
_VERIFY_CHAT_MAX_TOKENS = 8
_VERIFY_EMBEDDINGS_INPUT = "gatekey custom model verification"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CustomModelNotFoundError(NotFoundError):
    """No `custom_models` row exists with the given id (for this org)."""

    def __init__(self, custom_model_id: uuid.UUID) -> None:
        super().__init__(f"No custom model found with id '{custom_model_id}'.")


class CustomModelNameConflictError(GatekeyError):
    """Another custom model already uses this `(org_id, name)` pair
    (`uq_custom_models_org_id_name`). 409, no DB write survives (the
    caller's transaction is rolled back before this is raised) - see module
    docstring "Bidirectional collision guard" item 3."""

    status_code = 409
    code = "custom_model_name_conflict"

    def __init__(self, name: str) -> None:
        super().__init__(f"A custom model named '{name}' is already registered.")
        self.name = name


class CustomModelNameRegistryCollisionError(GatekeyError):
    """`name` collides with a static `MODEL_REGISTRY` key - see module
    docstring "Bidirectional collision guard" item 1. 422, no DB write
    happens in that case. Model names are caller input, not secret
    material - safe in `message`."""

    status_code = 422
    code = "custom_model_name_registry_collision"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"'{name}' collides with an existing Gatekey model registry entry "
            "and cannot be registered as a custom model."
        )
        self.name = name


class CustomModelNameSelfHostedCollisionError(GatekeyError):
    """`name` is already claimed by a `self_hosted_providers` row's `models`
    list in this org - see module docstring "Bidirectional collision guard"
    item 2. 422, no DB write happens in that case."""

    status_code = 422
    code = "custom_model_name_self_hosted_collision"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"'{name}' is already registered as a self-hosted model id in this "
            "organization and cannot also be registered as a custom model."
        )
        self.name = name


class CustomModelOllamaProviderError(GatekeyError):
    """`provider == "ollama"` was requested - `ollama` has its own
    registration mechanism (self-hosted providers), never this one. 422."""

    status_code = 422
    code = "custom_model_ollama_provider_not_supported"

    def __init__(self) -> None:
        super().__init__(
            "provider 'ollama' is not supported for custom models - register an "
            "Ollama endpoint under Providers -> Self-Hosted Models instead."
        )


class CustomModelUnsupportedProviderError(GatekeyError):
    """`provider` isn't one of the four supported BYOK providers at all.
    Should not be reachable via the API layer (`CustomModelProvider` is a
    `Literal` of exactly the four supported values) - kept as an explicit,
    safe failure mode for any caller invoking this service directly,
    mirroring `services.proxy_keys.UnsupportedProviderCredentialError`'s
    docstring rationale. 422."""

    status_code = 422
    code = "custom_model_unsupported_provider"

    def __init__(self, provider: str) -> None:
        super().__init__(f"provider '{provider}' is not supported for custom models.")
        self.provider = provider


class CustomModelEmbeddingsProviderUnsupportedError(GatekeyError):
    """`capability == "embeddings"` requested for a provider with no real
    embeddings support - see module docstring "Embeddings-provider guard".
    422, no DB write happens in that case."""

    status_code = 422
    code = "custom_model_embeddings_provider_unsupported"

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"capability 'embeddings' is not supported for provider '{provider}' "
            "- only 'openai' and 'vertex_ai' support embeddings custom models."
        )
        self.provider = provider


class CustomModelCapabilityPricingMismatchError(GatekeyError):
    """`capability`/`output_price_per_million_usd` disagree - mirrors the
    DB's own `chk_custom_models_capability_output_price` CHECK exactly
    (defense in depth, matching this codebase's established convention).
    422, no DB write happens in that case."""

    status_code = 422
    code = "custom_model_capability_pricing_mismatch"

    def __init__(self, capability: ModelCapability) -> None:
        if capability is ModelCapability.CHAT:
            message = "capability 'chat' requires a non-null output_price_per_million_usd."
        else:
            message = "capability 'embeddings' must not set output_price_per_million_usd."
        super().__init__(message)
        self.capability = capability


class CustomModelPricingInvalidError(GatekeyError):
    """`input_price_per_million_usd`/`output_price_per_million_usd` (when
    set) must both be strictly `> 0` - hard-blocked here AND at the DB
    level (`chk_custom_models_input_price_positive`/`chk_custom_models_
    output_price_positive`), matching the resolved product decision that
    $0/near-$0 pricing must never be usable to bypass budget enforcement
    (product spec section 12). 422. The API layer's own `Field(gt=0)`
    constraints on `schemas.custom_model.CustomModel{Create,Update}Request`
    already reject this before it ever reaches this service - this is a
    second, independent backstop for any caller invoking the service
    directly."""

    status_code = 422
    code = "custom_model_pricing_invalid"

    def __init__(self) -> None:
        super().__init__(
            "input_price_per_million_usd and output_price_per_million_usd (when "
            "set) must both be strictly greater than $0."
        )


class CustomModelVerifyCooldownError(GatekeyError):
    """A verification attempt for this row landed within the last
    `_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS` - see module docstring
    "Verification never charges budget or writes usage_logs". 429, with a
    `Retry-After` header."""

    status_code = 429
    code = "custom_model_verify_cooldown"

    def __init__(self, retry_after_seconds: float) -> None:
        wait_seconds = max(1, int(retry_after_seconds) + 1)
        super().__init__(
            "Verification for this custom model was attempted too recently; "
            f"wait about {wait_seconds}s before trying again.",
            headers={"Retry-After": str(wait_seconds)},
        )
        self.retry_after_seconds = retry_after_seconds


class CustomModelFallbackChainTooLongError(GatekeyError):
    """`len(fallback_model_names) > 5` (`_MODEL_FALLBACK_MAX_CHAIN_LENGTH`)
    - see the Model Catalog technical design doc section 2.3 for the
    latency-bound rationale. 422, no DB write."""

    status_code = 422
    code = "custom_model_fallback_chain_too_long"

    def __init__(self) -> None:
        super().__init__(
            f"fallback_model_names may contain at most "
            f"{_MODEL_FALLBACK_MAX_CHAIN_LENGTH} entries."
        )


class CustomModelFallbackSelfReferenceError(GatekeyError):
    """A custom model's own `name` appears in its own `fallback_model_names`
    - trivially a no-op loop even under the single-level walk (the model
    would just skip itself at hop time), rejected at write time instead of
    left as a confusing dead entry. 422, no DB write."""

    status_code = 422
    code = "custom_model_fallback_self_reference"

    def __init__(self) -> None:
        super().__init__("fallback_model_names may not include the custom model's own name.")


class CustomModelFallbackDuplicateEntryError(GatekeyError):
    """`fallback_model_names` contains the same name twice (exact-match,
    case-sensitive - the same string-identity semantics `resolve_route()`'s
    dict lookup already uses everywhere else in this codebase). 422, no DB
    write."""

    status_code = 422
    code = "custom_model_fallback_duplicate_entry"

    def __init__(self) -> None:
        super().__init__("fallback_model_names may not contain the same entry twice.")


class CustomModelFallbackUnresolvableModelError(GatekeyError):
    """One entry in `fallback_model_names` does not currently resolve to any
    routable model (not a `MODEL_REGISTRY` key, not another VERIFIED
    `custom_models` row in this org, not a model id claimed by a VERIFIED
    `self_hosted_providers` row in this org). Write-time-only - see
    `resolve_and_dispatch_hop()`'s docstring (section 2.5) for why the
    runtime walk treats this exact same condition as a skip, not a crash,
    since routability can change again after this check passes (a
    referenced custom model can be un-verified or deleted later). 422, no
    DB write."""

    status_code = 422
    code = "custom_model_fallback_unresolvable_model"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"fallback_model_names entry '{name}' does not currently resolve "
            "to a routable model (not a known registry model, verified "
            "custom model, or verified self-hosted model id in this org)."
        )


# ---------------------------------------------------------------------------
# CustomModelRouteCache (technical design doc section 5 row 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomModelCacheEntry:
    """One routable custom model's cached routing/pricing data.

    Deliberately NOT a `ModelRoute` itself - `resolve_route()`'s
    custom-model fallback (a later task) constructs a fresh `ModelRoute`
    FROM this entry's fields (`provider=entry.provider`,
    `capability=entry.capability`, `native_model_id=entry.native_model_id`,
    `custom_model_id=entry.id`), the identical relationship
    `SelfHostedRouteEntry` has to the `ModelRoute` self-hosted's own
    fallback builds - see technical design doc section 2.2's pseudocode.
    `input_price_per_million_usd`/`output_price_per_million_usd` are
    config data (an admin-set USD rate), never secret material - safe to
    cache at the same tier as every other `*Cache` class' values, mirroring
    `SelfHostedRouteEntry.cost_basis_per_gpu_hour`'s identical rationale.

    `fallback_model_names` (Model Catalog + Cross-Provider Fallback Chains,
    Part B) - an immutable `tuple`, not the mutable `list` the DB/JSONB
    round-trip naturally produces. This is what makes the "single-level, no
    recursion" constraint *structurally* enforced at the one call site that
    walks it (`api.v1.gateway.common.dispatch_with_model_fallback()`): that
    loop binds `candidates = entry.fallback_model_names` exactly once,
    before the loop starts, and never re-derives a list from inside the loop
    body - there is no code path left that could even attempt to fetch a
    second-order chain. See the Model Catalog technical design doc section
    2.2 for the full rationale (the tuple type is redundant-but-cheap
    defense in depth on top of that discipline, not the primary mechanism).
    """

    id: uuid.UUID
    provider: str
    capability: ModelCapability
    native_model_id: str
    input_price_per_million_usd: Decimal
    output_price_per_million_usd: Decimal | None
    fallback_model_names: tuple[str, ...] = ()


class CustomModelRouteCache:
    """Process-local, in-memory `name -> CustomModelCacheEntry` map.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `SelfHostedModelRouteCache`/`services.model_policy.
    ModelPolicyCache` - see those classes' docstrings for the full
    rationale. Instantiated once per process and stored on `app.state`
    (`main.create_app`'s lifespan, a later task) - never construct a second
    instance and thread it through separately. See module docstring for why
    every entry here is, by construction, already `verified = true`.
    """

    def __init__(self, initial: dict[str, CustomModelCacheEntry] | None = None) -> None:
        self._snapshot: dict[str, CustomModelCacheEntry] = dict(initial or {})

    def get(self, model: str) -> CustomModelCacheEntry | None:
        return self._snapshot.get(model)

    def known_model_ids(self) -> frozenset[str]:
        """Every currently-routable custom-model name - consumed by
        `services.model_policy.set_policy`/`set_team_model_policy`'s
        widened "unknown model" validation (a later task, technical design
        doc section 5 row 13)."""
        return frozenset(self._snapshot.keys())

    def set_all(self, snapshot: dict[str, CustomModelCacheEntry]) -> None:
        """Full replace - the startup-warm write AND the write every
        register/edit/remove/verify admin handler pushes after its own
        commit (technical design doc section 2.1's "Invalidation on
        write")."""
        self._snapshot = dict(snapshot)


async def load_custom_model_route_snapshot(
    session: AsyncSession,
) -> dict[str, CustomModelCacheEntry]:
    """Query every VERIFIED `custom_models` row for the default org into a
    `name -> CustomModelCacheEntry` map.

    This is the ONLY function that queries `custom_models` with `WHERE
    verified = true` - the cache-membership rule this produces is the
    entire routing-eligibility enforcement mechanism (technical design doc
    section 2.1/5 row 2): an unverified custom model is simply never
    present in the resulting snapshot, so nothing downstream needs to
    separately re-check a `verified` flag on the entry itself.

    Used at process startup (to warm `CustomModelRouteCache`, a later task)
    and by every admin write handler (also a later task) to re-derive the
    full mapping after a commit - a full re-derive, not an incremental
    single-entry update, mirroring `load_self_hosted_model_route_snapshot`'s
    identical stated rationale (a small, low-write-frequency admin-config
    table). NEVER call this from a gateway route handler (same zero-DB
    hot-path rule every other `*Cache`'s loader function follows) -
    `resolve_route()` reads the already-warmed cache only.

    Two different rows can never legitimately claim the same `name` (the
    table's own `UNIQUE(org_id, name)` constraint prevents it at the DB
    level), so no defensive last-writer-wins ordering note is needed here,
    unlike `load_self_hosted_model_route_snapshot`'s (whose `models` list
    has no equivalent per-entry uniqueness constraint).
    """
    stmt = (
        select(CustomModel)
        .where(CustomModel.org_id == DEFAULT_ORG_ID, CustomModel.verified.is_(True))
        .order_by(CustomModel.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    snapshot: dict[str, CustomModelCacheEntry] = {}
    for row in rows:
        snapshot[row.name] = CustomModelCacheEntry(
            id=row.id,
            provider=row.provider,
            capability=row.capability,
            native_model_id=row.native_model_id,
            input_price_per_million_usd=row.input_price_per_million_usd,
            output_price_per_million_usd=row.output_price_per_million_usd,
            fallback_model_names=tuple(row.fallback_model_names),
        )
    return snapshot


# ---------------------------------------------------------------------------
# compute_custom_model_cost (technical design doc section 5 row 12)
# ---------------------------------------------------------------------------


def compute_custom_model_cost(
    entry: CustomModelCacheEntry, *, prompt_tokens: int, completion_tokens: int | None
) -> Decimal:
    """Compute USD cost from actual provider-reported token counts, against
    THIS custom model's own admin-entered real per-token rates.

    Exactly the same per-token formula `services.budget.compute_cost()`
    uses for a static `MODEL_REGISTRY` model - `input_price *
    prompt_tokens / 1_000_000 [+ output_price * completion_tokens /
    1_000_000]` - never the self-hosted GPU-hour proxy
    (`providers.pricing.compute_self_hosted_cost`). `completion_tokens=None`
    selects the embeddings formula (no output-token term, matching
    `entry.output_price_per_million_usd is None` for an embeddings-capability
    row); an int (including `0`) selects the chat formula.

    The return value is handed directly to `api.v1.gateway.common.
    record_usage_charge()`'s `precomputed_cost_usd` parameter (a later
    task's wiring) - same `Decimal` type/precision contract that
    parameter already expects from `providers.pricing.
    compute_self_hosted_cost()`'s self-hosted equivalent.
    """
    cost = (entry.input_price_per_million_usd * prompt_tokens) / Decimal(1_000_000)
    if completion_tokens is not None:
        assert entry.output_price_per_million_usd is not None, (
            f"custom model (id={entry.id!r}) has completion_tokens but no "
            "output price - CustomModelRouteCache only ever holds rows that "
            "already passed the capability/output-price write-time guard, so "
            "this indicates a real bug, not a valid embeddings-model charge."
        )
        cost += (entry.output_price_per_million_usd * completion_tokens) / Decimal(1_000_000)
    return cost


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_custom_models(session: AsyncSession) -> list[CustomModel]:
    stmt = (
        select(CustomModel)
        .where(CustomModel.org_id == DEFAULT_ORG_ID)
        .order_by(CustomModel.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_custom_model_by_id(
    session: AsyncSession, custom_model_id: uuid.UUID
) -> CustomModel | None:
    stmt = select(CustomModel).where(
        CustomModel.org_id == DEFAULT_ORG_ID, CustomModel.id == custom_model_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _lock_org_settings_for_model_name_guard(session: AsyncSession) -> None:
    """`SELECT ... FOR UPDATE` the org's `org_settings` row (upserting it
    first if absent, so there is always a row to lock - identical
    bootstrap to `services.team_budget.set_org_budget_ceiling`) to
    serialize this module's half of the bidirectional custom-model /
    self-hosted-model name-collision guard against
    `services.self_hosted_providers`'s mirror-image lock call - see module
    docstring "Bidirectional collision guard" item 2 for the full
    rationale. Held only until the caller's own `session.commit()`/
    rollback (this module's register/edit functions commit directly on
    the same session, unlike `team_budget.py`'s flush-only convention) -
    never across an outbound HTTP call."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    await session.execute(
        select(OrgSettings.org_id).where(OrgSettings.org_id == DEFAULT_ORG_ID).with_for_update()
    )


async def _self_hosted_model_ids_for_org(session: AsyncSession) -> set[str]:
    """Every model id claimed by ANY `self_hosted_providers` row in this
    org - see module docstring "Bidirectional collision guard" item 2 for
    why this queries the `SelfHostedProvider` ORM class directly rather
    than `services.self_hosted_providers` (avoids a circular import)."""
    stmt = select(SelfHostedProvider).where(SelfHostedProvider.org_id == DEFAULT_ORG_ID)
    rows = (await session.execute(stmt)).scalars().all()
    claimed: set[str] = set()
    for row in rows:
        claimed.update(row.models)
    return claimed


async def _verified_custom_model_names_for_org(session: AsyncSession) -> set[str]:
    """Every `name` claimed by a VERIFIED `custom_models` row in this org -
    used by `_validate_fallback_model_names()` (Model Catalog technical
    design doc section 2.3) to check whether a `fallback_model_names` entry
    resolves to another routable custom model. Mirrors `_self_hosted_model_
    ids_for_org`'s "query the ORM class directly" discipline (no circular
    import), but - unlike that helper - filters `WHERE verified = true`:
    only a verified custom model is ever actually routable
    (`CustomModelRouteCache`'s own docstring), so an unverified one must not
    be accepted as a resolvable fallback target."""
    stmt = select(CustomModel.name).where(
        CustomModel.org_id == DEFAULT_ORG_ID, CustomModel.verified.is_(True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return set(rows)


async def _verified_self_hosted_model_ids_for_org(session: AsyncSession) -> set[str]:
    """Every model id claimed by a VERIFIED `self_hosted_providers` row in
    this org - the self-hosted-target sibling of
    `_verified_custom_model_names_for_org()` above, used by
    `_validate_fallback_model_names()`. Unlike `_self_hosted_model_ids_for_
    org` (the name-collision guard's helper, which deliberately does NOT
    filter on `verified` - a colliding name is a hazard regardless of
    verification state), this one DOES filter `WHERE verified = true`: only
    a verified self-hosted endpoint is ever actually routable
    (`SelfHostedModelRouteCache`'s own docstring), so an unverified one must
    not be accepted as a resolvable fallback target."""
    stmt = select(SelfHostedProvider).where(
        SelfHostedProvider.org_id == DEFAULT_ORG_ID, SelfHostedProvider.verified.is_(True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    claimed: set[str] = set()
    for row in rows:
        claimed.update(row.models)
    return claimed


async def _validate_fallback_model_names(
    session: AsyncSession, *, own_name: str, fallback_model_names: list[str]
) -> None:
    """Write-time validation for `fallback_model_names` (Model Catalog +
    Cross-Provider Fallback Chains technical design doc section 2.3).

    Zero-I/O no-op when `fallback_model_names` is empty (the overwhelming
    majority of every create/edit) - see module docstring. No DB write
    happens in any raised case (called BEFORE the row is inserted/
    committed, same discipline as `_validate_custom_model_write`).

    Raises `CustomModelFallbackChainTooLongError` (`len() >
    _MODEL_FALLBACK_MAX_CHAIN_LENGTH`), `CustomModelFallbackSelfReference
    Error` (`own_name` appears in its own chain),
    `CustomModelFallbackDuplicateEntryError` (an exact-match duplicate
    entry), or `CustomModelFallbackUnresolvableModelError` (an entry that is
    neither a `MODEL_REGISTRY` key, a verified custom model name in this
    org, nor a verified self-hosted model id in this org) - see those
    classes' docstrings.
    """
    if not fallback_model_names:
        return
    if len(fallback_model_names) > _MODEL_FALLBACK_MAX_CHAIN_LENGTH:
        raise CustomModelFallbackChainTooLongError()
    if own_name in fallback_model_names:
        raise CustomModelFallbackSelfReferenceError()
    if len(set(fallback_model_names)) != len(fallback_model_names):
        raise CustomModelFallbackDuplicateEntryError()

    verified_custom_names = await _verified_custom_model_names_for_org(session)
    verified_self_hosted_ids = await _verified_self_hosted_model_ids_for_org(session)
    for candidate in fallback_model_names:
        if (
            candidate in MODEL_REGISTRY
            or candidate in verified_custom_names
            or candidate in verified_self_hosted_ids
        ):
            continue
        raise CustomModelFallbackUnresolvableModelError(candidate)


async def _validate_custom_model_write(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    capability: ModelCapability,
    input_price_per_million_usd: Decimal,
    output_price_per_million_usd: Decimal | None,
    fallback_model_names: list[str],
) -> None:
    """Runs every write-time guard EXCEPT the own-table name-uniqueness
    guard (enforced via `UNIQUE(org_id, name)` + `IntegrityError` at
    commit time - see module docstring "Bidirectional collision guard" item
    3). Called with the FULL EFFECTIVE post-write field values on both
    register and edit - see `register_custom_model`/`edit_custom_model`.
    Raises the first applicable error below; no DB write happens in any
    case (called BEFORE the row is inserted/committed)."""
    if input_price_per_million_usd <= 0:
        raise CustomModelPricingInvalidError()
    if output_price_per_million_usd is not None and output_price_per_million_usd <= 0:
        raise CustomModelPricingInvalidError()

    if provider == "ollama":
        raise CustomModelOllamaProviderError()
    if provider not in _BYOK_PROVIDERS:
        raise CustomModelUnsupportedProviderError(provider)

    if capability is ModelCapability.EMBEDDINGS and provider not in _EMBEDDINGS_CAPABLE_PROVIDERS:
        raise CustomModelEmbeddingsProviderUnsupportedError(provider)

    if capability is ModelCapability.CHAT and output_price_per_million_usd is None:
        raise CustomModelCapabilityPricingMismatchError(capability)
    if capability is ModelCapability.EMBEDDINGS and output_price_per_million_usd is not None:
        raise CustomModelCapabilityPricingMismatchError(capability)

    if name in MODEL_REGISTRY:
        raise CustomModelNameRegistryCollisionError(name)

    # Model Catalog + Cross-Provider Fallback Chains (Part B, technical
    # design doc section 2.3): deliberately checked here, BEFORE the
    # DB-touching collision guard below - a no-op (zero I/O) whenever
    # `fallback_model_names` is empty (see `_validate_fallback_model_names`'s
    # own docstring), and its own pure sub-checks (chain-too-long/self-
    # reference/duplicate-entry) can therefore still reject before ANY DB
    # access, matching this function's "cheapest checks first" discipline
    # for every guard above.
    await _validate_fallback_model_names(session, own_name=name, fallback_model_names=fallback_model_names)

    # Serialize against services.self_hosted_providers's mirror-image guard
    # for the remainder of this transaction (through the caller's own
    # insert/update + commit) - see module docstring "Bidirectional
    # collision guard" item 2 / `_lock_org_settings_for_model_name_guard`'s
    # own docstring for the full CMR-12/CMR-14 race-condition rationale.
    await _lock_org_settings_for_model_name_guard(session)

    self_hosted_ids = await _self_hosted_model_ids_for_org(session)
    if name in self_hosted_ids:
        raise CustomModelNameSelfHostedCollisionError(name)


async def register_custom_model(
    session: AsyncSession,
    *,
    custom_model_id: uuid.UUID | None = None,
    name: str,
    provider: str,
    native_model_id: str,
    capability: ModelCapability,
    input_price_per_million_usd: Decimal,
    output_price_per_million_usd: Decimal | None,
    pricing_source: str | None,
    fallback_model_names: list[str] | None = None,
) -> CustomModel:
    """Validate then insert a new `custom_models` row.

    `verified` always starts `False` (technical design doc section 2.1) -
    registration never auto-verifies; the admin must separately call
    `verify_custom_model()` (`POST .../verify`, a later task) to probe the
    target provider live. `pricing_as_of` is always server-set to
    `date.today()` (never admin-entered - see `db.models.custom_model.
    CustomModel`'s docstring). `custom_model_id`, if given, is used as the
    row's id (letting a caller - the admin router, a later task - know the
    id up front, e.g. to write an audit entry naming it in the SAME
    transaction as this insert, mirroring `register_self_hosted_provider`'s
    identical `provider_id` parameter); a fresh `uuid.uuid4()` is generated
    otherwise.

    Raises `CustomModelPricingInvalidError`/`CustomModelOllamaProviderError`/
    `CustomModelUnsupportedProviderError`/
    `CustomModelEmbeddingsProviderUnsupportedError`/
    `CustomModelCapabilityPricingMismatchError`/
    `CustomModelNameRegistryCollisionError`/
    `CustomModelNameSelfHostedCollisionError`/
    `CustomModelFallbackChainTooLongError`/
    `CustomModelFallbackSelfReferenceError`/
    `CustomModelFallbackDuplicateEntryError`/
    `CustomModelFallbackUnresolvableModelError` (422, no write) or
    `CustomModelNameConflictError` (409, transaction rolled back) - see
    those classes' docstrings.

    `fallback_model_names` (Model Catalog + Cross-Provider Fallback Chains,
    Part B) defaults to `[]` ("no fallback chain configured") and is ALWAYS
    validated (`_validate_custom_model_write`'s new parameter), regardless
    of whether it was explicitly passed - unlike `edit_custom_model`'s
    `_provided`-gated re-validation, there is no "unchanged from the
    existing row" case on a fresh insert.
    """
    effective_fallback_model_names = list(fallback_model_names) if fallback_model_names else []
    await _validate_custom_model_write(
        session,
        name=name,
        provider=provider,
        capability=capability,
        input_price_per_million_usd=input_price_per_million_usd,
        output_price_per_million_usd=output_price_per_million_usd,
        fallback_model_names=effective_fallback_model_names,
    )

    resolved_id = custom_model_id if custom_model_id is not None else uuid.uuid4()
    row = CustomModel(
        id=resolved_id,
        org_id=DEFAULT_ORG_ID,
        name=name,
        provider=provider,
        native_model_id=native_model_id,
        capability=capability,
        input_price_per_million_usd=input_price_per_million_usd,
        output_price_per_million_usd=output_price_per_million_usd,
        pricing_source=pricing_source,
        pricing_as_of=date.today(),
        verified=False,
        fallback_model_names=effective_fallback_model_names,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise CustomModelNameConflictError(name) from None
    await session.refresh(row)
    return row


async def edit_custom_model(
    session: AsyncSession,
    custom_model_id: uuid.UUID,
    *,
    name: str | None = None,
    provider: str | None = None,
    native_model_id: str | None = None,
    capability: ModelCapability | None = None,
    input_price_per_million_usd: Decimal | None = None,
    output_price_per_million_usd: Decimal | None = None,
    output_price_per_million_usd_provided: bool = False,
    pricing_source: str | None = None,
    fallback_model_names: list[str] | None = None,
    fallback_model_names_provided: bool = False,
) -> CustomModel:
    """Partial update of an existing row - every parameter left at its
    default leaves that field unchanged.

    `output_price_per_million_usd_provided` disambiguates "the admin
    submitted an explicit `output_price_per_million_usd` field (possibly
    `null`)" from "the admin omitted it" - identical rationale to
    `edit_self_hosted_provider`'s `bearer_token_provided`: a `capability`
    edit from `"chat"` to `"embeddings"` must be able to explicitly CLEAR a
    previously-required price to `None`, which a bare `is not None` check
    could never distinguish from "leave the existing price alone". Every
    other field here has no such ambiguity (mirrors self-hosted's identical
    choice to give only ONE field this treatment).

    Editing `native_model_id`, `provider`, or `capability` resets `verified`
    back to `False` (technical design doc section 2.1) - a changed target
    model/provider must be re-verified before it is routable again, exactly
    `edit_self_hosted_provider`'s `base_url`/credential-change rationale.
    `capability` is included in this reset set (a real gap found during
    implementation review, not in the original design doc list): a row
    verified via a chat completion call and then edited to `embeddings`
    without changing `native_model_id` would otherwise stay `verified=True`
    despite never having had an embeddings call actually succeed against
    that model id - the previous verification proved nothing about the new
    capability. Editing ONLY pricing fields (`input_price_per_million_usd`,
    `output_price_per_million_usd`, `pricing_source`) does NOT reset
    `verified`, and always re-sets `pricing_as_of = date.today()` whenever
    ANY pricing field is touched.

    The full write-time guard set (`_validate_custom_model_write`) is
    re-run against the EFFECTIVE post-edit values whenever any of
    `name`/`provider`/`capability`/`input_price_per_million_usd`/
    `output_price_per_million_usd` is being changed - skipped entirely for
    an edit that touches only `pricing_source` (matching `edit_self_hosted_
    provider`'s identical "only re-validate what could actually have
    changed" discipline), since the row's already-persisted state was
    necessarily valid the last time this guard ran. Also re-run whenever
    `fallback_model_names_provided` (Model Catalog + Cross-Provider Fallback
    Chains, Part B) - see that parameter's own note below.

    `fallback_model_names`/`fallback_model_names_provided` mirror `output_
    price_per_million_usd`/`output_price_per_million_usd_provided`'s exact
    `_provided`-boolean disambiguation: an edit must be able to explicitly
    clear a chain back to `[]` (both an explicit `[]` AND an explicit `null`
    clear it - either way the row ends up with `[]`), which a bare `is not
    None` check on `fallback_model_names` alone could never distinguish from
    "omitted". Editing `fallback_model_names` does NOT reset `verified`
    (technical design doc section 2.4) - unlike `native_model_id`/
    `provider`/`capability`, the row's own routability is unaffected by what
    its fallback chain points at (only what happens if its OWN provider call
    fails).

    Raises `CustomModelNotFoundError` (404) if `custom_model_id` doesn't
    reference a row in this org, or any of the same 422/409 errors
    `register_custom_model` can raise.
    """
    row = await get_custom_model_by_id(session, custom_model_id)
    if row is None:
        raise CustomModelNotFoundError(custom_model_id)

    effective_name = name if name is not None else row.name
    effective_provider = provider if provider is not None else row.provider
    effective_capability = capability if capability is not None else row.capability
    effective_input_price = (
        input_price_per_million_usd
        if input_price_per_million_usd is not None
        else row.input_price_per_million_usd
    )
    effective_output_price = (
        output_price_per_million_usd
        if output_price_per_million_usd_provided
        else row.output_price_per_million_usd
    )
    effective_fallback_model_names = (
        (list(fallback_model_names) if fallback_model_names is not None else [])
        if fallback_model_names_provided
        else (list(row.fallback_model_names) if row.fallback_model_names else [])
    )

    revalidate = (
        name is not None
        or provider is not None
        or capability is not None
        or input_price_per_million_usd is not None
        or output_price_per_million_usd_provided
        or fallback_model_names_provided
    )
    if revalidate:
        await _validate_custom_model_write(
            session,
            name=effective_name,
            provider=effective_provider,
            capability=effective_capability,
            input_price_per_million_usd=effective_input_price,
            output_price_per_million_usd=effective_output_price,
            fallback_model_names=effective_fallback_model_names,
        )

    if name is not None:
        row.name = name
    if provider is not None:
        row.provider = provider
    if native_model_id is not None:
        row.native_model_id = native_model_id
    if capability is not None:
        row.capability = capability
    if input_price_per_million_usd is not None:
        row.input_price_per_million_usd = input_price_per_million_usd
    if output_price_per_million_usd_provided:
        row.output_price_per_million_usd = output_price_per_million_usd
    if pricing_source is not None:
        row.pricing_source = pricing_source
    if fallback_model_names_provided:
        row.fallback_model_names = effective_fallback_model_names

    # Deliberately based on "was this parameter provided at all" (not "did
    # it actually change the stored value") - identical convention to
    # `edit_self_hosted_provider`'s `base_url`/token-change reset.
    if native_model_id is not None or provider is not None or capability is not None:
        row.verified = False

    pricing_touched = (
        input_price_per_million_usd is not None
        or output_price_per_million_usd_provided
        or pricing_source is not None
    )
    if pricing_touched:
        row.pricing_as_of = date.today()

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise CustomModelNameConflictError(name or row.name) from None
    await session.refresh(row)
    return row


async def remove_custom_model(session: AsyncSession, custom_model_id: uuid.UUID) -> bool:
    """Hard delete one row (technical design doc section 2.1) - no
    `usage_logs` FK exists to this table (see `db.models.custom_model.
    CustomModel`'s module docstring), so there is nothing to `SET NULL`.
    Returns `True` if a row was deleted."""
    row = await get_custom_model_by_id(session, custom_model_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Verification (technical design doc section 2.3)
# ---------------------------------------------------------------------------

# Per-process, in-memory verify-cooldown marker - see module docstring
# "Verification never charges budget or writes usage_logs". `time.
# monotonic()` values, never wall-clock, so a system clock adjustment can
# never shorten/lengthen the effective cooldown.
_verify_last_attempt_monotonic: dict[uuid.UUID, float] = {}


def _check_and_record_verify_cooldown(custom_model_id: uuid.UUID) -> None:
    now = time.monotonic()
    last_attempt = _verify_last_attempt_monotonic.get(custom_model_id)
    if last_attempt is not None:
        elapsed = now - last_attempt
        if elapsed < _CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS:
            raise CustomModelVerifyCooldownError(_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS - elapsed)
    _verify_last_attempt_monotonic[custom_model_id] = now


async def _verify_chat_call(
    row: CustomModel,
    credential: ProviderCredential,
    *,
    http_client: httpx.AsyncClient,
    vertex_token_cache: VertexAITokenCache,
) -> None:
    request = ChatCompletionRequest(
        model=row.native_model_id,
        messages=[ChatMessage(role="user", content=_VERIFY_CHAT_PROMPT)],
        max_tokens=_VERIFY_CHAT_MAX_TOKENS,
        stream=False,
    )
    if row.provider == "openai" and isinstance(credential, ApiKeyCredential):
        await openai_provider.create_chat_completion(http_client, row.native_model_id, request, credential)
    elif row.provider == "anthropic" and isinstance(credential, ApiKeyCredential):
        await anthropic_provider.create_chat_completion(http_client, row.native_model_id, request, credential)
    elif row.provider == "vertex_ai" and isinstance(credential, ServiceAccountCredential):
        await vertex_ai_provider.create_chat_completion(
            http_client, row.native_model_id, request, credential, vertex_token_cache
        )
    elif row.provider == "openrouter" and isinstance(credential, ApiKeyCredential):
        await openrouter_provider.create_chat_completion(http_client, row.native_model_id, request, credential)
    else:  # pragma: no cover - should be unreachable, see below.
        raise AssertionError(
            f"verify_custom_model(): no chat credential-shape dispatch for "
            f"provider {row.provider!r} - should be unreachable given "
            "write-time guards / services.proxy_keys' provider dispatch."
        )


async def _verify_embeddings_call(
    row: CustomModel,
    credential: ProviderCredential,
    *,
    http_client: httpx.AsyncClient,
    vertex_token_cache: VertexAITokenCache,
) -> None:
    request = EmbeddingsRequest(model=row.native_model_id, input=_VERIFY_EMBEDDINGS_INPUT)
    if row.provider == "openai" and isinstance(credential, ApiKeyCredential):
        await openai_provider.create_embeddings(http_client, row.native_model_id, request, credential)
    elif row.provider == "vertex_ai" and isinstance(credential, ServiceAccountCredential):
        await vertex_ai_provider.create_embeddings(
            http_client, row.native_model_id, request, credential, vertex_token_cache
        )
    else:  # pragma: no cover - unreachable: guard #6/#17 blocks this combination at write time.
        raise AssertionError(
            f"verify_custom_model(): capability='embeddings' custom model with "
            f"provider={row.provider!r} - should be unreachable given the "
            "write-time embeddings-provider guard."
        )


async def verify_custom_model(
    session: AsyncSession,
    custom_model_id: uuid.UUID,
    *,
    key_provider: KeyProvider,
    http_client: httpx.AsyncClient,
    vertex_token_cache: VertexAITokenCache,
) -> CustomModel:
    """One-time, on-demand live verification call (technical design doc
    section 2.3): fires exactly one minimal real call against `row.
    provider` using the org's EXISTING decrypted BYOK credential (the
    IDENTICAL `services.proxy_keys.get_decrypted_provider_credential()`
    every real gateway request already calls - no new credential path). A
    `capability == "chat"` row gets one minimal chat completion; a
    `capability == "embeddings"` row gets one minimal embeddings call on a
    fixed short string.

    On success: `row.verified = True`, committed. On `ProviderCallError`
    (including a wrong-but-real `native_model_id`): `row.verified = False`
    (reverted/left False), committed, and `errors.ProviderUpstreamError` is
    raised carrying the real upstream error message VERBATIM (never
    swallowed) - an HTTP 502-shaped response (or the original upstream
    status code, for the same passthrough-status subset `errors.
    ProviderUpstreamError` already defines for the live gateway path).

    Raises `CustomModelNotFoundError` (404) if `custom_model_id` doesn't
    reference a row in this org. Raises `errors.ProviderNotConfiguredError`
    (404) if no `provider_keys` row is configured for `row.provider` yet -
    the IDENTICAL failure shape a real gateway request for this provider
    would produce (`services.proxy_keys.ProviderKeyNotConfiguredError`,
    translated exactly as `api.v1.gateway.common.fetch_credential()`
    already translates it). Raises `CustomModelVerifyCooldownError` (429)
    if this row was verified within the last
    `_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS` seconds (checked and recorded
    BEFORE the credential fetch/live call - see module docstring).

    NEVER calls `check_model_policy`/`check_residency`/`run_dlp_scan`/
    `check_budget_available`/`record_usage_charge` and NEVER writes a
    `usage_logs` row (module docstring "Verification never charges budget
    or writes usage_logs") - this is a synthetic, admin-triggered,
    non-user-content probe, not a billable gateway request.
    """
    row = await get_custom_model_by_id(session, custom_model_id)
    if row is None:
        raise CustomModelNotFoundError(custom_model_id)

    _check_and_record_verify_cooldown(custom_model_id)

    try:
        credential = await get_decrypted_provider_credential(
            session, row.provider, key_provider=key_provider
        )
    except ProviderKeyNotConfiguredError as exc:
        raise ProviderNotConfiguredError(exc.message) from None

    try:
        if row.capability is ModelCapability.CHAT:
            await _verify_chat_call(
                row, credential, http_client=http_client, vertex_token_cache=vertex_token_cache
            )
        else:
            await _verify_embeddings_call(
                row, credential, http_client=http_client, vertex_token_cache=vertex_token_cache
            )
    except ProviderCallError as exc:
        row.verified = False
        await session.commit()
        raise ProviderUpstreamError(exc.message, upstream_status_code=exc.status_code) from None

    row.verified = True
    await session.commit()
    await session.refresh(row)
    return row
