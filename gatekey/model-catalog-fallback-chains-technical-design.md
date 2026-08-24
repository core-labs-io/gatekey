
# Model Catalog + Cross-Provider Fallback Chains - Technical Design

Status: DESIGN (not yet implemented). Written for the Custom Model Registry
(CMR) feature surface - see `gatekey/custom-model-registry-technical-design.md`
("CMR doc" below) and `gatekey/custom-model-registry-product-spec.md`, which
this design builds on directly and does not restate except where the delta
matters. Familiarity with `backend/src/gatekey/services/custom_models.py`'s
module docstring (the bidirectional name-collision guards, the "no FK from
usage_logs" decision, `CustomModelRouteCache`'s verified-gate mechanism) is
assumed throughout.

Two independent, additive slices, built and shippable together:

- **Part A** - a live "what models does this provider actually have" lookup,
  surfaced in a new admin console page (Model Catalog) so registering a
  custom model no longer requires the admin to already know the provider's
  exact model id string.
- **Part B** - automatic, admin-configured, single-level, cross-provider
  fallback chains on `custom_models` rows: if the originally-dispatched
  model's provider call fails (after Phase 4's existing same-provider
  key-level failover has already been exhausted for it), Gatekey walks an
  ordered list of other model names and serves the first one that succeeds.

Neither part touches `MODEL_REGISTRY`/`PRICING_TABLE` (still pure, hand-
curated, code-only) or `self_hosted_providers` (still its own registration
flow) - both remain fully in scope only as *targets*: a custom model's
fallback chain may point at a registry model, another verified custom model,
or a self-hosted model id.

---

## 1. Part A - Live per-provider model listing

### 1.1 Decision: Vertex AI is scoped OUT of live listing this pass

`GET https://{location}-aiplatform.googleapis.com/v1/publishers/google/models`
is real, but three things make it materially riskier than the other three
providers, none of which the product owner's prompt resolved for us:

1. **Response shape is not independently verified** the way OpenAI's
   `{data:[{id,...}]}` / Anthropic's `{data:[{id,display_name,...}]}` /
   OpenRouter's `{data:[{id,name,pricing}]}` shapes are (those three are
   given as confirmed facts in the brief; Vertex AI's Model Garden listing
   response is not).
2. **Project-id sourcing is ambiguous.** `provider_key_metadata["project"]`/
   `["location"]` exist for *credential* purposes (which service-account key
   to sign with, which regional endpoint to call) - Model Garden's
   `publishers/google/models` listing is not actually project-scoped the
   same way a real inference call is (Google's own `google` publisher models
   are visible cluster-wide, not per-project), so it's unclear the org's
   configured `project`/`location` even changes the result versus just
   picking any valid project id to authenticate with. Guessing at this is
   the kind of unverified-API-shape risk the CMR doc's own precedent
   (`verify_custom_model()`) explicitly built one real, minimal live call to
   avoid ever having to guess about.
3. It's the only one of the four with **zero live pricing available at all**
   even if the listing itself works (Google doesn't return per-model pricing
   from this endpoint) - so the payoff for the added fragility is strictly
   smaller than OpenAI/Anthropic (still get a real id list) or OpenRouter
   (real id list *and* real pricing).

Decision: Vertex AI custom models are registered by **typing the
`native_model_id` manually** (unchanged from today - the CMR registration
form already has a free-text field for this). The live-listing endpoint
(1.3) still accepts `vertex_ai` as a path value so the frontend's "pick a
provider" step never needs a client-side provider allowlist that silently
diverges from the backend's - it responds with a dedicated 422
(`CustomModelLiveListingUnsupportedError`, 1.4) whose message says exactly
this, and the frontend renders a plain text input instead of the live
dropdown when it receives that error. This is a documented, deliberate gap,
not a partial implementation - a live Vertex AI Model Garden listing is a
legitimate, separate follow-up if the response shape gets independently
confirmed later.

### 1.2 Normalized response shape and the "known static price" answer

```python
# schemas/custom_model.py (new)
class AvailableModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    native_model_id: str
    display_name: str
    input_price_per_million_usd: Decimal | None
    output_price_per_million_usd: Decimal | None
```

Rather than a separate "is this a known model" boolean the frontend would
have to interpret, **the backend directly resolves and returns actual
prices whenever it has any authoritative source for them** - the frontend's
rule becomes just "if either price field is non-null, prefill it (still
editable); otherwise leave both blank":

- **OpenRouter**: `pricing.prompt`/`pricing.completion` from the live
  response, scaled `* 1_000_000` (the brief's confirmed per-token-USD-string
  ->  per-million-USD conversion), `round`ed to 6 decimal places to match
  `custom_models.input_price_per_million_usd`'s `NUMERIC(12,6)`. A small
  number of OpenRouter listings use a `"-1"` (or otherwise non-numeric)
  sentinel for variable/negotiated pricing - parsed defensively: on a
  `Decimal()` parse failure or a negative value, both price fields are left
  `None` for that entry rather than surfacing a nonsense negative price.
- **OpenAI / Anthropic**: no live pricing in the response at all. The
  backend builds a **reverse index once at import time**
  (`_native_id_to_pricing: dict[tuple[str, str], PricingEntry]`, keyed by
  `(provider, native_model_id)`) by iterating `MODEL_REGISTRY` and joining
  each entry to its `PRICING_TABLE` row - the exact same completeness
  invariant `pricing.py`'s own `_validate_completeness()` already guarantees
  holds for every `MODEL_REGISTRY` key. Every returned OpenAI/Anthropic
  `AvailableModelEntry` whose `native_model_id` happens to match a
  `MODEL_REGISTRY` route for that provider gets its price fields populated
  straight from that lookup; everything else gets `None`. This lives in the
  new `services/model_catalog.py` module (1.5), not in `schemas/
  custom_model.py` - the schema module docstring is explicit that it must
  never import `PRICING_TABLE`.
- **Vertex AI**: not reachable (1.1) - N/A.

This is a strictly better contract than a boolean: the frontend needs *zero*
copy of `PRICING_TABLE`/`MODEL_REGISTRY`, and "known static price" and "live
OpenRouter price" are indistinguishable to the caller by design - both are
just "the backend already knows a real price for this."

### 1.3 New endpoint

```
GET /v1/admin/custom-models/available/{provider}
```

- `provider`: `Literal["openai", "anthropic", "vertex_ai", "openrouter"]`
  (same 4-value BYOK literal `CustomModelProvider` already defines - reused,
  not duplicated).
- RBAC: `require_admin_or_auditor` - a read, no mutation, same posture as
  every other `GET` on this router (module docstring's RBAC table).
- 200: `list[AvailableModelEntry]`, sorted by `native_model_id`.
- 404 (`errors.ProviderNotConfiguredError`, same shape/message every real
  gateway request for an unconfigured provider already produces): no
  `provider_keys` row configured for `provider` yet. Deliberately gated
  identically for all four providers even though OpenRouter's live GET is
  itself unauthenticated - see 1.5's rationale.
- 422 (`CustomModelLiveListingUnsupportedError`, new, 1.4): `provider ==
  "vertex_ai"`.
- 502-shaped (`errors.ProviderUpstreamError`, same translation
  `verify_custom_model()` already performs on `ProviderCallError`): the live
  listing call itself failed (bad key, transient network error, non-2xx
  response).

No write, no audit entry (mirrors `verify_custom_model()`'s "a live
provider-facing action with no lasting side effect writes no audit row"
posture only insofar as this one has *no* side effect at all, not even a
`verified`-flag flip - purely informational).

### 1.4 New error

```python
class CustomModelLiveListingUnsupportedError(GatekeyError):
    """`provider == "vertex_ai"` was requested against the live-listing
    endpoint - Vertex AI Model Garden's listing response shape has not been
    independently verified against this codebase's other three providers'
    confirmed shapes, and its `publishers/google/models` endpoint returns no
    per-model pricing even when it works - see the Model Catalog technical
    design doc section 1.1 for the full, deliberate rationale. 422; register
    a Vertex AI custom model by typing `native_model_id` manually instead."""
    status_code = 422
    code = "custom_model_live_listing_unsupported"
```

### 1.5 New service module: `services/model_catalog.py`

A new module (not appended to `services/custom_models.py`, which owns CRUD/
verification/routing, a different concern) with one entry point:

```python
async def list_available_models(
    session: AsyncSession,
    provider: str,
    *,
    key_provider: KeyProvider,
    http_client: httpx.AsyncClient,
) -> list[AvailableModelEntry]:
```

- `provider == "vertex_ai"` raises `CustomModelLiveListingUnsupportedError`
  immediately - zero I/O, checked before any credential fetch.
- Fetches the org's decrypted credential for `provider` via the IDENTICAL
  `services.proxy_keys.get_decrypted_provider_credential()` call
  `verify_custom_model()` already uses (`ProviderKeyNotConfiguredError` ->
  `errors.ProviderNotConfiguredError`, same translation). This credential
  gate is enforced uniformly across all four accepted providers, including
  OpenRouter - even though OpenRouter's live `GET /api/v1/models` needs no
  auth at all, requiring a configured `provider_keys` row first (a) keeps
  one consistent "you must have set this provider up before you can browse
  its catalog" story for the admin instead of a provider-dependent
  exception, and (b) avoids turning this endpoint into an anonymous,
  unauthenticated-outbound-request proxy reachable by anyone with
  `org_admin`/`auditor` access regardless of whether the org even uses
  OpenRouter.
- Dispatches to a new, provider-module-local `list_models()` function
  (`providers/openai.py`, `providers/anthropic.py`, `providers/
  openrouter.py` each get one, mirroring how `create_chat_completion` is
  already per-provider-module) which performs the live GET and raises
  `providers.base.ProviderCallError` on a non-2xx response or transport
  error - reusing that base module's existing status-code-mapping helper,
  the same one every `create_chat_completion`/`create_embeddings` already
  calls, so listing failures get the identical treatment real gateway calls
  already get. `list_available_models()` catches `ProviderCallError` and
  re-raises `errors.ProviderUpstreamError(exc.message, upstream_status_code=
  exc.status_code)`, byte-for-byte `verify_custom_model()`'s own translation.
- Anthropic's `list_models()` requests `?limit=1000` (the documented max) in
  a **single** call, deliberately not following `after_id`/`before_id`
  pagination cursors - Anthropic's real model count is nowhere near 1000 and
  is not expected to be for the foreseeable future; implementing cursor-
  following for a catalog this small is unjustified complexity. Documented
  as a deliberate bound, not an oversight, mirroring this codebase's
  "explicit, justified constant" convention.
- Maps each provider's raw entries into `AvailableModelEntry` per 1.2, then
  returns the list sorted by `native_model_id`.

### 1.6 Frontend flow (detail in section 5)

Provider dropdown (4 BYOK values) -> on change, call 1.3's endpoint -> on
200, populate a second "model" dropdown from `native_model_id`/
`display_name`; picking an entry prefills (still editable) the pricing
fields from that entry's price fields, or leaves them blank -> on 404
(`provider_not_configured`), show "configure a provider key for X first"
inline, no dropdown -> on 422 (`vertex_ai`), fall back to a plain text
`native_model_id` input with no dropdown at all, no error banner (this is
the expected, documented shape for that one provider, not a failure state).

---

## 2. Part B - Automatic cross-provider fallback chains

### 2.1 Schema change: `custom_models.fallback_model_names`

```sql
ALTER TABLE custom_models
  ADD COLUMN fallback_model_names JSONB NOT NULL DEFAULT '[]'::jsonb;
```

New migration `0050_add_fallback_model_names_to_custom_models.py` (latest
existing is `0049_soft_delete_team_memberships.py`). `JSONB` list-of-strings,
not a normalized child table - identical convention to
`self_hosted_providers.models` (`db/models/self_hosted_provider.py`, `JSONB,
nullable=False, server_default=text("'[]'::jsonb")`), which is the direct
structural precedent for "a small, admin-authored, ordered list of model-id
strings that belongs to one parent row." An empty list (the default) means
"no fallback chain configured" - byte-for-byte pre-this-feature behavior for
every existing custom model.

ORM (`db/models/custom_model.py`):

```python
fallback_model_names: Mapped[list[str]] = mapped_column(
    JSONB, nullable=False, server_default=text("'[]'::jsonb")
)
```

No new CHECK constraint for chain length/self-reference/duplicates at the DB
level - unlike pricing's `> 0`/capability-pairing guards, "does every name
in this list currently resolve to a routable model" is not expressible as a
static SQL CHECK (it depends on the live contents of two other tables plus
`MODEL_REGISTRY`), so this is app-layer-only validation (2.3), matching how
`self_hosted_providers.models`' own three collision guards are *also*
app-layer-only with no DB CHECK equivalent - same precedent, same reason.

### 2.2 `CustomModelCacheEntry` gains the chain

```python
@dataclass(frozen=True)
class CustomModelCacheEntry:
    ...
    fallback_model_names: tuple[str, ...] = ()
```

`load_custom_model_route_snapshot()` populates it from
`tuple(row.fallback_model_names)`. Immutable tuple (not the mutable `list`
the DB/JSONB round-trip naturally produces) - matches this class's existing
`@dataclass(frozen=True)` discipline and, more importantly, is what makes
the "single-level, no recursion" constraint *structurally* enforced at the
one call site that walks it (2.5): the fallback-walk loop binds `candidates
= entry.fallback_model_names` exactly once, before the loop starts, and
never re-derives a list from inside the loop body - there is no code path
left that could even attempt to fetch a second-order chain, immutability or
not, because the loop simply never looks up `custom_model_cache.get(...)
.fallback_model_names` a second time for anything other than the ORIGINAL
entry. (The tuple type is redundant-but-cheap defense in depth on top of
that, not the primary mechanism - the primary mechanism is "the loop body
has no line of code that does a second cache lookup for a fallback list.")

### 2.3 Write-time validation (`services/custom_models.py`)

New helper, called from `_validate_custom_model_write` (extended with a
`fallback_model_names: list[str]` parameter) whenever `fallback_model_names`
is part of a create or an edit that touches it:

```python
_MODEL_FALLBACK_MAX_CHAIN_LENGTH = 5
```

Justification (mirroring `_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS`'s
"explicit, reasoned constant" style): each configured entry, on total
exhaustion, costs one full extra pipeline pass plus one real outbound
provider round trip (2.5) - worst-case added request latency scales
linearly with chain length, and unlike the existing same-provider key
failover (which is capped at exactly one retry by construction, since a
`ProviderKey` has exactly one `failover_target_id`), a cross-provider chain
needs more than one candidate to be useful at all (the whole point is
surviving more than one provider having a bad moment simultaneously). Five
is small enough to bound worst-case latency to roughly the cost of five
ordinary requests' worth of timeout budget, while still comfortably
exceeding "more than one backup" for any realistic set of interchangeable
models an admin would configure.

```python
class CustomModelFallbackChainTooLongError(GatekeyError):
    """`len(fallback_model_names) > 5` (`_MODEL_FALLBACK_MAX_CHAIN_LENGTH`)
    - see the Model Catalog technical design doc section 2.3 for the
    latency-bound rationale. 422, no DB write."""
    status_code = 422
    code = "custom_model_fallback_chain_too_long"

class CustomModelFallbackSelfReferenceError(GatekeyError):
    """A custom model's own `name` appears in its own `fallback_model_names`
    - trivially a no-op loop even under the single-level walk (the model
    would just skip itself at hop time), rejected at write time instead of
    left as a confusing dead entry. 422, no DB write."""
    status_code = 422
    code = "custom_model_fallback_self_reference"

class CustomModelFallbackDuplicateEntryError(GatekeyError):
    """`fallback_model_names` contains the same name twice (exact-match,
    case-sensitive - the same string-identity semantics `resolve_route()`'s
    dict lookup already uses everywhere else in this codebase). 422, no DB
    write."""
    status_code = 422
    code = "custom_model_fallback_duplicate_entry"

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
```

```python
async def _validate_fallback_model_names(
    session: AsyncSession, *, own_name: str, fallback_model_names: list[str]
) -> None:
    if len(fallback_model_names) > _MODEL_FALLBACK_MAX_CHAIN_LENGTH:
        raise CustomModelFallbackChainTooLongError()
    if own_name in fallback_model_names:
        raise CustomModelFallbackSelfReferenceError()
    if len(set(fallback_model_names)) != len(fallback_model_names):
        raise CustomModelFallbackDuplicateEntryError()

    verified_custom_names = await _verified_custom_model_names_for_org(session)  # new, mirrors _self_hosted_model_ids_for_org but WHERE verified = true
    verified_self_hosted_ids = await _verified_self_hosted_model_ids_for_org(session)  # new, mirrors _self_hosted_model_ids_for_org but only rows WHERE verified = true
    for candidate in fallback_model_names:
        if (
            candidate in MODEL_REGISTRY
            or candidate in verified_custom_names
            or candidate in verified_self_hosted_ids
        ):
            continue
        raise CustomModelFallbackUnresolvableModelError(candidate)
```

`_verified_custom_model_names_for_org`/`_verified_self_hosted_model_ids_for_org`
are two small new query helpers alongside the existing
`_self_hosted_model_ids_for_org` (same file, same "query the ORM class
directly, not the sibling service module" discipline the existing
collision-guard helpers already establish, for the identical circular-
import reason). Note this validation deliberately does **not** need the
`org_settings`-row lock the name-collision guard takes (item 2 of the CMR
doc's "Bidirectional collision guard") - a fallback-chain write racing a
concurrent verify/register/delete on one of its *candidates* is not a
correctness hazard the way the name-collision race was (nobody can end up
with two rows silently claiming the same name); worst case, a chain entry
that was momentarily valid at write time becomes unresolvable moments later
and is silently skipped at walk time (2.5) - already an accepted, designed-
for outcome, not a bug this needs to prevent.

### 2.4 CRUD wiring (`schemas/custom_model.py`, `services/custom_models.py`, `api/v1/admin/custom_models.py`)

- `CustomModelCreateRequest`/`Response`: `fallback_model_names: list[str] =
  Field(default_factory=list, max_length=5)` (the `max_length=5` Pydantic
  bound is a cheap, redundant early rejection in front of
  `CustomModelFallbackChainTooLongError` - same "defense in depth" posture
  `input_price_per_million_usd: Field(gt=0)` already has in front of
  `CustomModelPricingInvalidError`).
- `CustomModelUpdateRequest`: `fallback_model_names: list[str] | None =
  None`, with the identical `model_fields_set`-based provided-vs-omitted
  disambiguation `output_price_per_million_usd` already uses (an edit must
  be able to explicitly clear a chain back to `[]`, which a bare `is not
  None` check can't distinguish from "omitted"). `edit_custom_model_endpoint`
  computes `fallback_model_names_provided = "fallback_model_names" in
  payload.model_fields_set` and threads it through, mirroring
  `output_price_provided` exactly.
- `register_custom_model`/`edit_custom_model` gain a `fallback_model_names:
  list[str]` (register, default `[]`) / `fallback_model_names: list[str] |
  None = None, fallback_model_names_provided: bool = False` (edit)
  parameter pair, call `_validate_fallback_model_names()` whenever the
  effective value is being set (register: always; edit: only when
  `fallback_model_names_provided`), and persist to the new column.
- Editing `fallback_model_names` does **not** reset `verified` - unlike
  `native_model_id`/`provider`/`capability`, the row's own routability is
  unaffected by what its fallback chain points at (only what happens if its
  *own* provider call fails).
- `CustomModelResponse` gains `fallback_model_names: list[str]` (plain
  passthrough - no `shadowed_by_registry`-style computed-field complexity).

### 2.5 The fallback walk - new shared helper in `api/v1/gateway/common.py`

One new dataclass and one new function, alongside `call_provider_with_
failover`/`call_self_hosted_provider` (same "wraps the credential-fetch/
provider-call step" tier - a fourth sibling in that family, not a rewrite of
the other three):

```python
@dataclass(frozen=True)
class ModelFallbackResult:
    """Outcome of `dispatch_with_model_fallback()`. `fallback_attempt=0`
    (the overwhelming majority of requests, including every request on a
    model with no configured chain) means the originally-dispatched model's
    own call succeeded - byte-for-byte pre-this-feature behavior.
    `fallback_attempt=N>0` means the Nth entry in that model's own
    `fallback_model_names` (1-indexed) is what actually served the request;
    `served_route`/`served_model` are THAT candidate's resolved route/name,
    to be used for every downstream step (cost computation, response-cache
    key, usage-log `model` column) from this point on - mirrors `effective_
    route`/`effective_model`'s existing role for graceful degradation."""
    failover: FailoverCallResult
    served_route: ModelRoute
    served_model: str
    fallback_attempt: int
    fallback_from_model: str | None  # the model whose chain was walked, iff fallback_attempt > 0


async def dispatch_with_model_fallback(
    session: AsyncSession,
    app: FastAPI,
    ctx: GatewayCallerContext,
    *,
    original_route: ModelRoute,
    original_model: str,
    custom_model_cache: CustomModelRouteCache,
    self_hosted_cache: SelfHostedModelRouteCache | None,
    model_policy_cache: ModelPolicyCache,
    team_model_policy_cache: TeamModelPolicyCache,
    content_aware_cache: ContentAwareRuleCache,
    residency_cache: ResidencyRuleCache,
    category_findings: frozenset[str],
    source_ip: str | None,
    request_id: str,
    key_provider: KeyProvider,
    health_store: SharedStateStore,
    team_override_cache: TeamFailoverOverrideCache,
    build_call_fn: Callable[[ModelRoute], Callable[[ProviderCredential], Awaitable[T]]],
) -> ModelFallbackResult:
    ...
```

Mechanic:

1. Dispatch the ORIGINAL hop exactly as today: `call_self_hosted_provider`
   or `call_provider_with_failover` (whichever `original_route.provider`
   selects), `call_fn=build_call_fn(original_route)`. Success -> return
   immediately with `fallback_attempt=0` - **this is the only new code path
   every existing request without a configured chain ever takes**, and it
   is byte-for-byte today's call site, just relocated one level of
   indirection inward.
2. `ProviderCallError` from step 1 (i.e. same-provider key failover, if any
   applied, has *already* been exhausted for the original model - this
   function never touches `call_provider_with_failover`'s own internals,
   it only reacts to what escapes it): capture the exception as
   `primary_exc`. Look up `candidates = custom_model_cache.get(
   original_model)`'s `.fallback_model_names` if `original_route.
   custom_model_id is not None`, else `()` - **bound once, here, before the
   loop below; never re-read inside it** (this is the single-level
   enforcement point - see 2.2).
3. For each `candidate_name` in `candidates` (already capped at 5 by 2.3's
   write-time guard - this loop performs no additional runtime cap of its
   own, since the source list can never exceed it):
   - `resolve_route(candidate_name, self_hosted_cache=..., custom_model_
     cache=...)` - on `ModelNotFoundError`, `continue` (2.3's documented
     "routability can change after write time" case).
   - Re-run, in order, against the candidate: `check_model_policy()`,
     `check_content_classification()` (reusing `category_findings` - see
     below), `check_residency()`, `check_budget_available()`, then dispatch
     (`call_self_hosted_provider`/`call_provider_with_failover` again,
     `call_fn=build_call_fn(candidate_route)`) - this is deliberately the
     FULL per-model pipeline, not a bare retry: a fallback candidate can be
     a different provider (different residency exposure), a different
     custom model (different admin-set price), or a registry/self-hosted
     model with its own independent policy standing, and every one of those
     must be genuinely re-vetted, not assumed compatible with whatever
     already passed for the original model.
   - Any of `ModelDeniedError`, `ResidencyViolationError`,
     `BudgetExhaustedError`, `OrgBudgetExhaustedError`,
     `TeamMembershipRemovedError`, `ProviderNotConfiguredError`,
     `ProviderCallError` raised by that candidate's own pipeline pass ->
     `continue` to the next candidate. This is deliberate and uniform:
     *any* real reason a specific candidate can't serve this request is
     "skip and try the next one," not "abort the whole chain" - the
     product decision only ever singles out unresolvability by name as the
     must-skip case, but the same reasoning extends cleanly to every other
     per-model rejection reason, and treating them differently (e.g.
     aborting on a policy denial but skipping an unresolvable name) would
     make the chain's actual behavior depend on *why* a candidate failed in
     a way nothing in the product decision asks for. Each of these checks
     still performs and commits its OWN synchronous audit entry as part of
     its normal contract (`residency.hard_block`, `dlp`... - unaffected by
     this function swallowing the exception afterward) - a real policy
     decision was made for that candidate and is worth auditing regardless
     of whether a later candidate ultimately serves the request; this
     mirrors `revalidate_degraded_model()`'s existing, narrower version of
     the identical tolerance (it already swallows `ModelDeniedError`/
     `ResidencyViolationError` for degradation's one substituted model).
   - First candidate that dispatches successfully: return `ModelFallback
     Result(fallback_attempt=<1-indexed position>, served_route=candidate_
     route, served_model=candidate_name, fallback_from_model=original_
     model, failover=<that hop's own FailoverCallResult>)`.
4. Every candidate exhausted (or `candidates` was empty to begin with):
   re-raise `primary_exc` UNCHANGED - the ORIGINAL model's own
   `ProviderCallError`, propagating out of this function exactly as it does
   today with no chain configured at all. The calling route handler's
   existing `except ProviderCallError as exc: raise ProviderUpstreamError(...)`
   block needs no changes to handle this - it already wraps whatever this
   call site raises.

**DLP scan is never re-run per hop.** `category_findings` (and any
redaction already applied to the outgoing request body) is computed exactly
once, before this function is ever called, from the ORIGINAL prompt text -
which does not change across hops (only the *destination* changes, not the
content being sent). Re-running Presidio per candidate would (a) be pure
wasted latency on the failure path for content that provably hasn't changed,
and (b) write a confusing, duplicated `dlp_scan_results` trail for what a
human reviewing it would reasonably read as "one request." This generalizes
`revalidate_degraded_model()`'s existing identical reuse of `category_
findings` for degradation's single substitution to fallback's (possibly
several) substitutions - same precedent, same rationale, no new decision.

**Budget check DOES re-run per hop**, even though it is not model-specific
(it's per-user/per-team/per-org) - this is intentionally the one "redundant
against the model itself but not against time" check in the loop: it
re-validates that the caller hasn't crossed a hard budget boundary *since*
the original dispatch attempt (a concurrent request from the same
user/team could have pushed them over budget in the intervening round
trip), which is exactly what `check_budget_available()` already exists to
catch on every ordinary request. If it does trip mid-chain, that's treated
as a skip-and-continue like everything else in step 3 (not a chain-level
abort) - re-checking a *later* candidate is still valid/necessary
regardless of what happened on an earlier one's budget-adjacent timing, and
total exhaustion still surfaces the primary's original `ProviderUpstreamError`
per the confirmed "always the primary's error" rule, not a budget error the
client never asked about.

### 2.6 Wiring into `chat.py` / `embeddings.py`

`completions.py` is **untouched** - it never passes `custom_model_cache` to
`resolve_route()` at all (custom models, and therefore fallback chains,
have never been routable there - CMR doc section 2.2's existing non-goal,
unchanged), so `dispatch_with_model_fallback()` is never reachable from
that route and needs no wiring there.

In both `chat.py` and `embeddings.py`, the existing block

```python
try:
    if effective_route.provider == "self_hosted":
        failover = await call_self_hosted_provider(...)
    else:
        failover = await call_provider_with_failover(...)
except ProviderCallError as exc:
    raise ProviderUpstreamError(...) from None
```

is replaced with a call to `dispatch_with_model_fallback(...)` (same
`try/except ProviderCallError -> ProviderUpstreamError` wrapper around it,
unchanged - `dispatch_with_model_fallback()` re-raises the primary's
`ProviderCallError` on total exhaustion, so this existing translation still
fires correctly), and:

```python
result = await dispatch_with_model_fallback(..., original_route=effective_route, original_model=effective_model, build_call_fn=lambda route: (lambda credential: _non_streaming_call_against(route, credential)), ...)
failover = result.failover
effective_route = result.served_route   # REASSIGNED - was `effective_route`, now the winning hop
effective_model = result.served_model   # REASSIGNED
```

This is the whole diff for every downstream line: cost computation
(self-hosted GPU-hour vs. custom-model per-token vs. registry
`PRICING_TABLE`), `write_response_cache`, `record_usage_charge`,
`record_usage_log` all already key off `effective_route`/`effective_model`
today and need **zero further changes** - they transparently pick up
whichever provider/pricing the winning hop actually used, exactly the same
way they already transparently handle degradation's model substitution.
Two additions only:

- `write_response_cache(..., skip_write=dlp_result.redacted_texts is not
  None or degradation_outcome.triggered or result.fallback_attempt > 0)` -
  a fallback-served response must never be cached under the *originally
  requested* model's cache key, for the identical reason a degraded
  response isn't (a later, non-failing request for that model must never
  silently receive a different model's cached response with no live signal
  that a substitution happened).
- Two new response headers, mirroring `build_failover_headers()`'s
  always-present-attempt/present-only-if-used shape:
  `X-Gatekey-Model-Fallback-Attempt: <n>` (always present, `"0"` on the
  overwhelming majority) and `X-Gatekey-Model-Fallback-From: <name>`
  (present only when `fallback_attempt > 0`).

**Streaming (`chat.py` only) - scope boundary, not a gap.** Model-level
fallback applies identically to the streaming and non-streaming branches at
the exact same wrapping point, because `_streaming_call`'s `call_fn` only
ever calls `gen.__anext__()` ONCE, to obtain `first_item` - a
`ProviderCallError` raised by that first `__anext__()` call (connection
refused, auth failure, an immediate 4xx/5xx before any bytes stream back)
propagates out of `call_fn` exactly the same way a non-streaming call's
exception does, and is caught at exactly the same point. A failure that
happens **mid-stream**, after `first_item` was already yielded and the
`StreamingResponse` has already started sending bytes to the client, is a
wholly different, already-existing code path (`_sse_event_stream`'s own
`except ProviderCallError` -> `stream_error`/`result_status = "provider_
error"`) that this design does not touch and must not touch - there is no
way to silently swap providers or restart a response the client has already
started receiving. This is not a new limitation introduced by this feature;
it is the exact same scope boundary Phase 4's existing same-provider
key-level failover already has for the identical reason (its own `call_fn`
has the identical single-`__anext__()`-call shape).

### 2.7 `usage_logs` - new columns

Checked what already exists first (per the instructions): `failover_
attempt`/`failover_key_id` (migration `0031`) are key-level, not model-
level, and `original_model`/`degraded_from_model`/`degraded_to_model`
(migrations `0029`/`0031`) are already claimed by graceful degradation's
own, distinct substitution (budget-proximity-triggered, decided BEFORE
dispatch) - reusing any of these five for model-fallback (dispatch-failure-
triggered, decided AFTER a call already failed) would conflate two
independently-triggerable substitutions that can both legitimately apply to
the SAME request (a degraded model can itself then fail over). Two new
columns, added by migration `0050` alongside `fallback_model_names`:

```python
model_fallback_attempt: Mapped[int] = mapped_column(
    Integer, nullable=False, server_default=text("0")
)
model_fallback_from_model: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Directly mirrors `failover_attempt`/`failover_key_id`'s existing shape and
naming convention (an int count + the "from" identifier, `0`/`NULL` on the
overwhelming majority of rows), scoped to models instead of keys.
`model_fallback_from_model` is a plain `Text` column with **no FK** -
consistent with `degraded_from_model`/`degraded_to_model`'s identical
no-FK-to-a-model-names-table choice (there is no models table to reference;
`custom_models.name` isn't even unique across time the way an id would be),
not with `failover_key_id`'s FK-to-`provider_keys` (a real row it can
reference). New index `ix_usage_logs_model_fallback` on
`(model_fallback_attempt, model_fallback_from_model)`, mirroring `ix_usage_
logs_failover`'s composite-index shape exactly, for the analogous "how often
did fallback actually fire, and from which model" dashboard queries.

`services/usage_logs.py::record_usage_log()` gains `model_fallback_attempt:
int = 0, model_fallback_from_model: str | None = None` (both defaulted,
byte-for-byte pre-feature behavior for every existing call site), threaded
from `chat.py`/`embeddings.py`'s (and `_sse_event_stream`'s, for chat's
streaming path - same treatment `failover_attempt`/`failover_key_id`
already get there) call sites off `result.fallback_attempt`/`result.
fallback_from_model`.

`model` (the existing column) keeps its existing meaning unchanged: the
model that ULTIMATELY served/was charged - i.e. `effective_model` after
this feature's reassignment in 2.6, exactly the same "always the winner"
convention `degraded_to_model` already established relative to `model`.

---

## 3. API contract summary

| Method | Path | RBAC | Request | Response | New/Changed |
|---|---|---|---|---|---|
| GET | `/v1/admin/custom-models/available/{provider}` | admin_or_auditor | - | `list[AvailableModelEntry]` | New (Part A) |
| POST | `/v1/admin/custom-models` | org_admin | `CustomModelCreateRequest` (+`fallback_model_names`) | `CustomModelResponse` (+`fallback_model_names`) | Changed (Part B) |
| PUT | `/v1/admin/custom-models/{id}` | org_admin | `CustomModelUpdateRequest` (+`fallback_model_names`) | `CustomModelResponse` (+`fallback_model_names`) | Changed (Part B) |
| GET | `/v1/admin/custom-models` / `/{id}` | admin_or_auditor | - | `CustomModelResponse` (+`fallback_model_names`) | Changed (Part B, additive field) |
| DELETE | `/v1/admin/custom-models/{id}` | org_admin | - | 204 | Unchanged |
| POST | `/v1/admin/custom-models/{id}/verify` | org_admin | - | `CustomModelResponse` (+`fallback_model_names`) | Unchanged behavior, additive field |
| POST | `/v1/chat/completions` | gateway credential | unchanged | unchanged body; **new headers** `X-Gatekey-Model-Fallback-Attempt`/`-From` | Changed (Part B, transparent) |
| POST | `/v1/embeddings` | gateway credential | unchanged | unchanged body; new headers, same as above | Changed (Part B, transparent) |
| POST | `/v1/completions` | gateway credential | unchanged | unchanged | Untouched (custom models never routable here) |

---

## 4. Non-functional requirements this design must not silently drop

- **Latency (worst case)**: total added worst-case latency on a fully-
  exhausted chain is bounded by `_MODEL_FALLBACK_MAX_CHAIN_LENGTH` (5)
  extra full pipeline passes + provider round trips (2.3). Each hop still
  respects whatever per-provider timeout the underlying `httpx.AsyncClient`
  is already configured with (unchanged, not loosened by this feature) - a
  slow-but-eventually-failing primary provider is still bounded by that
  provider's own timeout on hop 1, not amplified per candidate.
- **Concurrency / atomicity**: `check_budget_available()`'s existing
  atomic-under-concurrency contract (CMR doc / Phase 1.4 design doc) is
  reused verbatim per hop (2.5) - this feature adds no new shared mutable
  state of its own (no counters, no locks) beyond calling that existing,
  already-safe function one extra time per candidate.
- **No plaintext provider keys in logs**: every hop's credential fetch goes
  through the exact same `get_decrypted_provider_credential`/`get_
  decrypted_self_hosted_credential` paths every existing request already
  uses - no new credential-handling code is introduced by this feature at
  all (Part A's live-listing endpoint reuses the identical credential-fetch
  call `verify_custom_model()` already makes).
- **OpenAI-compatible surface preserved**: the request/response body shape
  for `/v1/chat/completions` and `/v1/embeddings` is unchanged by Part B -
  every effect is either a new, purely additive, opt-in-to-notice response
  header, or entirely invisible (the response body's own `model` field
  continues to reflect whatever the serving provider call itself returns,
  unchanged from today for every route). Part A adds one new admin-only
  endpoint; no existing endpoint's contract changes.
- **Self-hosted-first / self-deploy docs**: no new external dependency, no
  new mandatory outbound call at startup or on any existing request path
  that lacks a configured fallback chain (byte-for-byte pre-feature
  behavior when `fallback_model_names == []`, the default for every
  existing and newly-created row that doesn't opt in). Part A's live
  listing is admin-console-triggered only, never part of the always-on
  gateway hot path.

---

## 5. Forward-looking flag: rework risk in a later phase

`gatekey/phase-6-ecosystem-scale.md` should be checked before this ships if
it introduces multi-org support: `fallback_model_names` validation (2.3) and
the runtime walk (2.5) are both scoped to `DEFAULT_ORG_ID` exactly like
every other CMR function - a real multi-org cutover would need the
candidate-resolution helpers (`_verified_custom_model_names_for_org`, etc.)
to become genuinely org-scoped rather than hardcoded to the single default
org, which is additive (parameterize an existing query, not a redesign) and
not expected to require touching the walk's control flow itself.

No other later-phase rework risk identified: Part A adds a new, narrowly-
scoped admin endpoint with no schema coupling to anything else; Part B's
`fallback_model_names` column and the two new `usage_logs` columns are
purely additive and independent of every other Phase 1-5 feature's own
schema.

---

## 6. Task breakdown

Dependency graph: **DB migration -> ORM model -> service layer -> API
router -> gateway wiring**, with frontend able to start on Part A's UI
shell against a mocked endpoint contract in parallel with backend work,
and Part B's frontend UI blocked only on the CRUD schema change (not on the
gateway-wiring task, which has no frontend surface at all).

### database-admin

1. Migration `0050`: `custom_models.fallback_model_names` (JSONB,
   `NOT NULL DEFAULT '[]'::jsonb`) + `usage_logs.model_fallback_attempt`
   (Integer, `NOT NULL DEFAULT 0`) + `usage_logs.model_fallback_from_model`
   (Text, nullable) + `ix_usage_logs_model_fallback` index. No FK, no CHECK
   (2.1/2.7). **No dependency** - can start immediately.
2. Update `db/models/custom_model.py` and `db/models/usage_log.py` ORM
   classes to match. Depends on (1) landing (or can be written in the same
   PR).

### backend-developer

3. `services/model_catalog.py` (new) + `providers/{openai,anthropic,
   openrouter}.py::list_models()` + `schemas/custom_model.py::
   AvailableModelEntry` + `CustomModelLiveListingUnsupportedError` (Part A,
   1.2-1.5). **Independent of everything else** - can start immediately,
   in parallel with task 1.
4. `GET /v1/admin/custom-models/available/{provider}` router endpoint
   (1.3). Depends on (3).
5. `services/custom_models.py`: `_validate_fallback_model_names()` + the
   three new error classes + `_verified_custom_model_names_for_org`/
   `_verified_self_hosted_model_ids_for_org` helpers + threading the new
   parameter through `register_custom_model`/`edit_custom_model`/
   `_validate_custom_model_write` (2.3). Depends on (2).
6. `schemas/custom_model.py`: `fallback_model_names` on
   Create/Update/Response + `CustomModelCacheEntry.fallback_model_names`
   in `services/custom_models.py` + `load_custom_model_route_snapshot()`
   populating it (2.2, 2.4). Depends on (2), can run in parallel with (5).
7. `api/v1/admin/custom_models.py`: thread `fallback_model_names`/
   `fallback_model_names_provided` through the POST/PUT handlers + response
   builder. Depends on (5), (6).
8. `api/v1/gateway/common.py`: `ModelFallbackResult` +
   `dispatch_with_model_fallback()` (2.5). Depends on (6) (needs
   `CustomModelCacheEntry.fallback_model_names`).
9. Wire `dispatch_with_model_fallback()` into `chat.py` (both streaming and
   non-streaming branches) and `embeddings.py`, including the two new
   response headers and the `write_response_cache` `skip_write` addition
   (2.6). Depends on (8).
10. `services/usage_logs.py::record_usage_log()` new parameters +
    `chat.py`/`embeddings.py`/`_sse_event_stream` call-site threading (2.7).
    Depends on (9).

Parallelizable: (3)+(4) run fully independent of (5)-(10). Within (5)-(10),
(5) and (6) can run in parallel (both depend only on (2)); (7) depends on
both; (8) depends on (6) only (not (5)/(7) - the write-side validation and
the runtime walk are independent code paths that only share the cache
entry's shape); (9) and (10) are strictly sequential after (8).

### frontend-developer

11. New Model Catalog admin console page shell + route + nav entry.
    **Independent** - can start immediately against a mocked API contract
    from section 3.
12. Provider dropdown -> live model dropdown (calling task 4's endpoint) ->
    pricing fields prefilled-but-editable from `AvailableModelEntry` ->
    manual-entry fallback for the `vertex_ai` 422 case (1.6) -> "add model"
    submit calling the existing (already-built) POST endpoint. Depends on
    (4) for a real (non-mocked) integration; UI itself can be built against
    (11)'s shell in parallel.
13. Table of registered custom models (reusing the already-existing GET
    list endpoint - no backend change needed) with edit/verify/remove
    actions (also already-existing endpoints - this is the first-ever
    frontend for CMR's fully-built backend, per the brief's framing).
    Depends on (11) only.
14. Fallback-sequence picker/reorder UI per row: a multi-select-with-
    ordering control sourced from the union of (a) `MODEL_REGISTRY` keys
    (static, can be bundled/fetched once, e.g. via a small new read-only
    listing if one doesn't already exist - flag to backend-developer if
    the frontend has no existing way to enumerate `MODEL_REGISTRY` keys
    today), (b) the org's own other registered custom models (from task
    13's list), and (c) the org's registered self-hosted model ids (an
    existing self-hosted-providers list endpoint, if the frontend doesn't
    already have this from prior tiers). Submits via the existing PUT
    endpoint once task 7 lands. Depends on (7); the picker's own UI shell
    can be built in parallel against (13)'s data.

### qa-engineer (post-implementation)

- Part A: unconfigured-provider 404, `vertex_ai` 422 (and that the frontend
  correctly falls back to manual entry rather than showing a raw error),
  OpenRouter's `"-1"`-pricing-sentinel entries rendering as blank (not a
  negative price) in the picker, a live-listing failure (bad/revoked key)
  surfacing as a clean 502-shaped error rather than a raw stack trace.
- Part B write-time: chain length 6 rejected, self-reference rejected,
  duplicate entry rejected, a name that resolves to nothing rejected, a
  name that resolves to an UNVERIFIED custom model rejected (must be
  verified, not merely registered), editing `fallback_model_names` does
  NOT reset `verified` on the row itself.
- Part B runtime, the core scenarios: (a) primary succeeds -> chain never
  touched, `X-Gatekey-Model-Fallback-Attempt: 0`; (b) primary fails
  (simulate via a bad key or a deliberately-wrong `native_model_id`),
  candidate 1 also fails, candidate 2 succeeds -> served response reflects
  candidate 2's provider/pricing, `usage_logs.model == <candidate 2's
  name>`, `model_fallback_attempt == 2`, `model_fallback_from_model ==
  <primary's name>`; (c) every candidate fails -> client receives the
  PRIMARY's original upstream error message, not any candidate's; (d) a
  candidate that's policy-denied/residency-blocked for THIS caller is
  skipped, not surfaced, and its own audit entry (e.g. `residency.hard_
  block`) still lands; (e) a fallback-served response is never written to
  the response cache under the original model's key (Phase 4 caching
  regression check); (f) a candidate that was verified at write time but
  gets un-verified/deleted before the request runs is silently skipped at
  walk time, not a 500; (g) streaming: a pre-first-byte provider failure
  triggers the same fallback chain as non-streaming; a mid-stream failure
  (after the first chunk was already sent) does NOT trigger fallback and
  behaves exactly as it does today (existing `stream_error` SSE frame).
- Confirm `POST /v1/completions` is provably unaffected (no fallback
  ever fires there, since custom models were never routable there to begin
  with) - a regression-guard test, not a new capability to validate.

### security-reviewer (post-implementation)

- **Budget-bypass / cost-accuracy risk (the one explicitly flagged in the
  brief)**: fallback-chain writes are already gated at `org_admin` only -
  the same trust boundary that already fully controls every custom model's
  price today, so this feature introduces no NEW privilege an org_admin
  didn't already have (they could already register a custom model priced
  at an arbitrary rate). The actual NEW risk is that fallback makes a
  mispriced/lower-quality substitution **automatic and easy to miss**
  rather than a deliberate, visible reroute: verify that (a) the two new
  `X-Gatekey-Model-Fallback-*` headers and the two new `usage_logs` columns
  give an org enough visibility to detect "how often is my primary model
  actually failing over and to what" without needing to inspect raw
  provider logs, and (b) nothing in the walk (2.5) allows a fallback hop to
  be charged using a DIFFERENT (cheaper) model's pricing than the one that
  actually served the request - confirm `compute_custom_model_cost()`/
  `PRICING_TABLE` lookups downstream always key off `served_route`/
  `served_model` (the winning hop), never `original_model`.
- Confirm total-exhaustion error messages never leak a DIFFERENT provider's
  raw upstream error text than the primary's - re-verify `primary_exc` is
  captured before the loop and is the exact, unmodified object re-raised on
  exhaustion (2.5 step 4), not a re-wrapped or last-candidate's error.
  Message-safety review of whatever text Anthropic/OpenAI/OpenRouter's live
  listing endpoints can put into a `ProviderUpstreamError` (Part A) - same
  "never echo a raw provider response body" bar every other upstream-error
  path in this codebase already holds itself to.
- Confirm each hop's `check_residency()`/`check_content_classification()`
  re-run in the walk cannot be short-circuited by a cache-staleness bug
  (i.e. that `ResidencyRuleCache`/`ContentAwareRuleCache` reads inside the
  loop are the same live, process-warmed caches every ordinary request
  already reads - no per-hop caching of a stale earlier decision).
- Confirm Part A's live-listing endpoint cannot be used as an open outbound
  proxy/SSRF vector - `provider` is a closed 4-value literal (no
  admin-supplied URL), and every outbound call target is one of exactly
  four fixed, hardcoded hostnames (`api.openai.com`/`api.anthropic.com`/
  `openrouter.ai`/Vertex - N/A per 1.1), never derived from request input.
