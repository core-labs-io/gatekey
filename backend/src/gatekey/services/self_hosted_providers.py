"""DB-backed CRUD service + process-local route cache for admin-registered
self-hosted inference endpoints (Phase 5 - Differentiators, 5.5 Unified
Governance for BYOK + Self-Hosted OSS Models).

See `gatekey/phase-5-product-spec.md` section 3 (AC5.5.x) and
`gatekey/phase-5-technical-design.md` section 2.3 for the full design
rationale this module implements. Every function operates against
`constants.DEFAULT_ORG_ID` only - see that module's docstring (mirrors
`services/model_policy.py`/`services/provider_keys.py`).

Credential shape reuse (AC5.5.2)
---------------------------------
Registration/re-verification reuse `providers.ollama.OllamaValidator`
as-is, parameterized by the row's own `base_url`/decrypted `bearer_token`
instead of a hardcoded "ollama" config - vLLM and Ollama both expose an
OpenAI-compatible surface, so no new provider-client module is needed (the
product spec is explicit about this). `get_decrypted_self_hosted_credential`
below returns a `services.proxy_keys.OllamaCredential` - reused as-is,
since that dataclass is already decoupled from any specific row identity
(just a `base_url`/`bearer_token` pair) - the ONE piece of new plumbing this
needs is a distinct AAD binding (see below), not a new credential shape.

Encryption / AAD binding
--------------------------
`self_hosted_providers.ciphertext`/`nonce`/`auth_tag` are the byte-for-byte
identical AES-256-GCM envelope shape `provider_keys` uses (see
`db.models.self_hosted_provider.SelfHostedProvider`'s own docstring) - but
the associated-data binding is DELIBERATELY DIFFERENT from `provider_keys`'
`f"{org_id}:{provider}"` (`services.encryption.build_aad`): here it is
`f"{org_id}:self_hosted:{self_hosted_provider_id}"` (built via
`build_aad(str(org_id), f"self_hosted:{provider_id}")` - `build_aad` itself
is generic, just `f"{org_id}:{provider}"`, so passing
`f"self_hosted:{provider_id}"` as its second argument produces exactly this
string). This distinct binding means a ciphertext can never be decrypted
successfully if copied between a `provider_keys` row and a
`self_hosted_providers` row, even within the same org, AND that no two
`self_hosted_providers` rows' ciphertexts are interchangeable with each
other either (the row's own id is baked into the AAD, not just the shared
literal `"self_hosted"` tag).

`SelfHostedModelRouteCache` (AC5.5.5)
----------------------------------------
Same whole-snapshot-replace, lock-free, GIL-atomic convention as
`services.model_policy.ModelPolicyCache`/`ContentAwareRuleCache` - see
those classes' docstrings for the full rationale. `load_self_hosted_model_
route_snapshot()` is the ONLY function that queries `self_hosted_providers`
with `WHERE verified = true` - every entry that ever lands in the cache is
therefore, by construction, already verified; `api.v1.gateway.common.
resolve_route()`'s cache-backed fallback does not need to separately check
a `verified` flag on the entry itself (an unverified provider's models are
simply never present in the cache at all - see
`db.models.self_hosted_provider.SelfHostedProvider`'s "verified gates
routing eligibility" note).

Model-id collision guard
---------------------------
Three independent guards apply when an admin registers/edits a self-hosted
provider's `models` list (`_validate_model_ids` below):

1. No entry may collide with a static `MODEL_REGISTRY` key - the static
   registry always wins at request time (`resolve_route()` tries it FIRST,
   unconditionally - AC5.5.5/design doc section 7.3's edge-case table), so
   registering a self-hosted model id that shadows a real registry key
   would silently make that self-hosted entry permanently unreachable. This
   is rejected at write time with a clear message rather than left as a
   confusing runtime no-op.
2. No entry may already be claimed by a DIFFERENT `self_hosted_providers`
   row for this org - `SelfHostedModelRouteCache` is a flat `model ->
   entry` mapping (one owning provider per model id), so two providers
   declaring the same model id would make the cache's "which provider does
   this route to" resolution ambiguous/last-writer-wins. Not specified
   explicitly by the phase spec, but a straightforward correctness
   requirement of the cache's own data shape, not a speculative future
   feature.
3. No entry may collide with a name already claimed by a `custom_models`
   row in this org (Custom Model Registry feature - see `gatekey/
   custom-model-registry-technical-design.md` section 4.1 guard #2 /
   section 5 row 15). This is this module's half of that feature's
   bidirectional collision guard: `services/custom_models.py`'s own
   write-time validation runs the mirror-image check against
   `SelfHostedProvider.models`, so a name collision between the two tables
   is rejected on whichever side is written second, regardless of write
   order. Queries the `CustomModel` ORM class DIRECTLY (never `services.
   custom_models`, the service module) - importing that module here would
   create a circular import, since it needs the mirror-image check against
   `SelfHostedProvider` (also queried via the ORM class directly, not this
   module's own service functions).

   CONCURRENCY (CMR-12/CMR-14 QA fix): guard #3 and its mirror image in
   `services/custom_models.py::_validate_custom_model_write` are a
   cross-table invariant with no single row of their own to lock across
   both tables. Both sides therefore take `SELECT ... FOR UPDATE` on the
   SAME per-org `org_settings` row
   (`_lock_org_settings_for_model_name_guard` below) before running their
   collision SELECT, mirroring `services/team_budget.py`'s ADR-5-style
   lock-then-check-then-write pattern - see that function's own docstring.
   This closes the race where a concurrent `register_self_hosted_provider
   (models=[X])` and `register_custom_model(name=X)` could both pass their
   collision SELECT before either committed and both succeed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.custom_model import CustomModel
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.self_hosted_provider import SelfHostedProvider
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.providers.base import ValidationStatus
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.providers.ollama import OllamaValidator
from gatekey.services.encryption import KeyProvider, build_aad, decrypt_secret, encrypt_secret
from gatekey.services.proxy_keys import OllamaCredential

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SelfHostedProviderNotFoundError(NotFoundError):
    """No `self_hosted_providers` row exists with the given id (for this org)."""

    def __init__(self, provider_id: uuid.UUID) -> None:
        super().__init__(f"No self-hosted provider found with id '{provider_id}'.")


class SelfHostedProviderNameConflictError(GatekeyError):
    """Another self-hosted provider already uses this `(org_id, name)` pair
    (`uq_self_hosted_providers_org_id_name`). 409, no DB write survives (the
    caller's transaction is rolled back before this is raised)."""

    status_code = 409
    code = "self_hosted_provider_name_conflict"

    def __init__(self, name: str) -> None:
        super().__init__(f"A self-hosted provider named '{name}' is already registered.")


class SelfHostedModelRegistryCollisionError(GatekeyError):
    """One or more `models` entries collide with a static `MODEL_REGISTRY`
    key - see module docstring "Model-id collision guard" #1. 422, no DB
    write happens in that case. Model ids are caller input, not secret
    material - safe in `message`."""

    status_code = 422
    code = "self_hosted_model_registry_collision"

    def __init__(self, colliding_models: list[str]) -> None:
        super().__init__(
            "The following model id(s) collide with an existing Gatekey model "
            "registry entry and cannot be registered as self-hosted models: "
            + ", ".join(sorted(colliding_models))
            + "."
        )
        self.colliding_models = colliding_models


class SelfHostedModelAlreadyClaimedError(GatekeyError):
    """One or more `models` entries are already claimed by a DIFFERENT
    self-hosted provider in this org - see module docstring "Model-id
    collision guard" #2. 422, no DB write happens in that case."""

    status_code = 422
    code = "self_hosted_model_already_claimed"

    def __init__(self, claimed_models: list[str]) -> None:
        super().__init__(
            "The following model id(s) are already registered to a different "
            "self-hosted provider in this organization: "
            + ", ".join(sorted(claimed_models))
            + "."
        )
        self.claimed_models = claimed_models


class SelfHostedModelCustomModelCollisionError(GatekeyError):
    """One or more `models` entries collide with a `custom_models` row's
    `name` in this org - see module docstring "Model-id collision guard"
    #3 (this module's half of the Custom Model Registry feature's
    bidirectional collision guard). 422, no DB write happens in that
    case."""

    status_code = 422
    code = "self_hosted_model_custom_model_collision"

    def __init__(self, colliding_models: list[str]) -> None:
        super().__init__(
            "The following model id(s) are already registered as a custom "
            "model's name in this organization and cannot also be "
            "registered as a self-hosted model: "
            + ", ".join(sorted(colliding_models))
            + "."
        )
        self.colliding_models = colliding_models


class SelfHostedCredentialNotConfiguredError(Exception):
    """No `self_hosted_providers` row exists for the given id at credential-
    fetch time (e.g. deleted between `resolve_route()`'s cache read and
    dispatch). Follows the same plain-`Exception`-with-`.message` pattern as
    `services.proxy_keys.ProviderKeyNotConfiguredError` - the route-handler
    layer (`api.v1.gateway.common.call_self_hosted_provider`) translates
    this into `errors.ProviderNotConfiguredError` (404)."""

    def __init__(self, provider_id: uuid.UUID) -> None:
        message = f"No self-hosted provider configured with id '{provider_id}'."
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id


class SelfHostedCredentialDecodeError(Exception):
    """Decryption succeeded but the plaintext isn't the expected JSON shape
    (data corruption, not an auth failure) - mirrors `services.proxy_keys.
    CredentialDecodeError`'s docstring/rationale exactly, including never
    chaining/logging the underlying `json.JSONDecodeError`/`TypeError`
    (its message can echo a fragment of the decrypted plaintext)."""

    def __init__(self) -> None:
        super().__init__("Failed to decode a decrypted self-hosted provider credential.")


# ---------------------------------------------------------------------------
# SelfHostedModelRouteCache (AC5.5.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelfHostedRouteEntry:
    """One routable self-hosted model id's cached routing/cost data.

    `cost_basis_per_gpu_hour` is config data (an admin-set USD rate), never
    secret material - safe to cache at the same tier as every other
    `*Cache` class' values (see `providers.pricing.compute_self_hosted_cost`'s
    caller, `api.v1.gateway.chat`, which reads this straight off the cache
    entry rather than a second DB round trip).
    """

    provider_id: uuid.UUID
    cost_basis_per_gpu_hour: Decimal


class SelfHostedModelRouteCache:
    """Process-local, in-memory `model id -> SelfHostedRouteEntry` map.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `services.model_policy.ModelPolicyCache`/
    `ContentAwareRuleCache` - see those classes' docstrings for the full
    rationale. Instantiated once per process and stored on `app.state`
    (`main.create_app`'s lifespan) - never construct a second instance and
    thread it through separately. See module docstring for why every entry
    here is, by construction, already `verified = true`.
    """

    def __init__(self, initial: dict[str, SelfHostedRouteEntry] | None = None) -> None:
        self._snapshot: dict[str, SelfHostedRouteEntry] = dict(initial or {})

    def get(self, model: str) -> SelfHostedRouteEntry | None:
        return self._snapshot.get(model)

    def known_model_ids(self) -> frozenset[str]:
        """Every currently-routable self-hosted model id - consumed by
        `services.model_policy.set_policy`/`set_team_model_policy`'s widened
        "unknown model" validation (AC5.5.6, design doc section 2.3(d))."""
        return frozenset(self._snapshot.keys())

    def set_all(self, snapshot: dict[str, SelfHostedRouteEntry]) -> None:
        """Full replace - the startup-warm write AND the write every
        register/edit/remove/re-verify admin handler pushes after its own
        commit (design doc section 2.3's "Invalidation on write")."""
        self._snapshot = dict(snapshot)


async def load_self_hosted_model_route_snapshot(
    session: AsyncSession,
) -> dict[str, SelfHostedRouteEntry]:
    """Query every VERIFIED `self_hosted_providers` row for the default org
    and flatten `.models` into a `model id -> SelfHostedRouteEntry` map.

    Used at process startup (to warm `SelfHostedModelRouteCache`, see
    `main.py`'s lifespan) and by every admin write handler in
    `api.v1.admin.self_hosted_providers` to re-derive the full mapping after
    a commit (a full re-derive, not an incremental single-entry update - see
    the design doc's stated rationale: this table is a small, low-write-
    frequency admin-config table, same size class as `backup_groups`/
    `content_aware_rules`). NEVER call this from a gateway route handler
    (same zero-DB hot-path rule every other `*Cache`'s loader function
    follows) - `resolve_route()` reads the already-warmed cache only.

    If two verified rows somehow both claim the same model id (should not
    happen - `_validate_model_ids` blocks this at write time, see module
    docstring "Model-id collision guard" #2), later rows in `id` order win;
    this is a defensive fallback ordering, not a sanctioned way to resolve a
    real collision.
    """
    stmt = (
        select(SelfHostedProvider)
        .where(SelfHostedProvider.org_id == DEFAULT_ORG_ID, SelfHostedProvider.verified.is_(True))
        .order_by(SelfHostedProvider.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    snapshot: dict[str, SelfHostedRouteEntry] = {}
    for row in rows:
        entry = SelfHostedRouteEntry(
            provider_id=row.id, cost_basis_per_gpu_hour=row.cost_basis_per_gpu_hour
        )
        for model in row.models:
            snapshot[model] = entry
    return snapshot


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_self_hosted_providers(session: AsyncSession) -> list[SelfHostedProvider]:
    stmt = (
        select(SelfHostedProvider)
        .where(SelfHostedProvider.org_id == DEFAULT_ORG_ID)
        .order_by(SelfHostedProvider.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_self_hosted_provider_by_id(
    session: AsyncSession, provider_id: uuid.UUID
) -> SelfHostedProvider | None:
    stmt = select(SelfHostedProvider).where(
        SelfHostedProvider.org_id == DEFAULT_ORG_ID, SelfHostedProvider.id == provider_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _lock_org_settings_for_model_name_guard(session: AsyncSession) -> None:
    """`SELECT ... FOR UPDATE` the org's `org_settings` row (upserting it
    first if absent, so there is always a row to lock - identical
    bootstrap to `services.team_budget.set_org_budget_ceiling`) to
    serialize this module's half of the bidirectional self-hosted-model /
    custom-model name-collision guard against
    `services.custom_models`'s mirror-image lock call - see module
    docstring "Model-id collision guard" #3 for the full rationale. Held
    only until the caller's own `session.commit()`/rollback (this
    module's register/edit functions commit directly on the same session,
    unlike `team_budget.py`'s flush-only convention) - never across an
    outbound HTTP call."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    await session.execute(
        select(OrgSettings.org_id).where(OrgSettings.org_id == DEFAULT_ORG_ID).with_for_update()
    )


async def _validate_model_ids(
    session: AsyncSession, *, models: list[str], exclude_provider_id: uuid.UUID | None
) -> None:
    """See module docstring "Model-id collision guard". Raises
    `SelfHostedModelRegistryCollisionError`/`SelfHostedModelAlreadyClaimedError`/
    `SelfHostedModelCustomModelCollisionError` - no DB write happens in any
    case (called BEFORE any write)."""
    requested = set(models)
    registry_collisions = requested & MODEL_REGISTRY.keys()
    if registry_collisions:
        raise SelfHostedModelRegistryCollisionError(sorted(registry_collisions))

    # Serialize against services.custom_models's mirror-image guard for the
    # remainder of this transaction (through the caller's own insert/update
    # + commit) - see module docstring "Model-id collision guard" #3 /
    # `_lock_org_settings_for_model_name_guard`'s own docstring for the full
    # CMR-12/CMR-14 race-condition rationale. Taken before EITHER
    # cross-cutting check below (both #2, same-table-different-row, and #3,
    # cross-table) so a concurrent same-table edit/register is also
    # serialized here, not just the cross-table case.
    await _lock_org_settings_for_model_name_guard(session)

    stmt = select(SelfHostedProvider).where(SelfHostedProvider.org_id == DEFAULT_ORG_ID)
    if exclude_provider_id is not None:
        stmt = stmt.where(SelfHostedProvider.id != exclude_provider_id)
    other_rows = (await session.execute(stmt)).scalars().all()
    claimed: set[str] = set()
    for row in other_rows:
        claimed.update(row.models)
    already_claimed = requested & claimed
    if already_claimed:
        raise SelfHostedModelAlreadyClaimedError(sorted(already_claimed))

    custom_model_stmt = select(CustomModel).where(CustomModel.org_id == DEFAULT_ORG_ID)
    custom_model_rows = (await session.execute(custom_model_stmt)).scalars().all()
    custom_model_names = {row.name for row in custom_model_rows}
    custom_model_collisions = requested & custom_model_names
    if custom_model_collisions:
        raise SelfHostedModelCustomModelCollisionError(sorted(custom_model_collisions))


def _encrypt_bearer_token(
    provider_id: uuid.UUID, bearer_token: str | None, *, key_provider: KeyProvider
) -> tuple[bytes, bytes, bytes]:
    """Returns `(ciphertext, nonce, auth_tag)` for `bearer_token` (empty
    string, not `None`, when the admin configured no token - same
    one-representation discipline `OllamaCredential.bearer_token` already
    enforces, see that dataclass' docstring)."""
    plaintext = json.dumps(bearer_token or "").encode("utf-8")
    aad = build_aad(str(DEFAULT_ORG_ID), f"self_hosted:{provider_id}")
    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)
    return encrypted.ciphertext, encrypted.nonce, encrypted.auth_tag


async def register_self_hosted_provider(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID | None = None,
    name: str,
    base_url: str,
    bearer_token: str | None,
    cost_basis_per_gpu_hour: Decimal,
    models: list[str],
    key_provider: KeyProvider,
) -> SelfHostedProvider:
    """Validate then insert a new `self_hosted_providers` row.

    `verified` always starts `false` (AC5.5.1/AC5.5.3) - registration never
    auto-verifies; the admin must separately call
    `reverify_self_hosted_provider()` (`POST .../verify`) to probe the
    endpoint live. `provider_id`, if given, is used as the row's id
    (letting a caller - the admin router - know the id up front, e.g. to
    write an audit entry naming it in the SAME transaction as this insert -
    see `api.v1.admin.self_hosted_providers`'s register handler); a fresh
    `uuid.uuid4()` is generated otherwise. Either way, the id is fixed
    BEFORE encryption, since it is baked into the AAD binding (module
    docstring).

    Raises `SelfHostedModelRegistryCollisionError`/
    `SelfHostedModelAlreadyClaimedError` (422, no write) or
    `SelfHostedProviderNameConflictError` (409, transaction rolled back) -
    see those classes' docstrings.
    """
    await _validate_model_ids(session, models=models, exclude_provider_id=None)

    resolved_id = provider_id if provider_id is not None else uuid.uuid4()
    ciphertext, nonce, auth_tag = _encrypt_bearer_token(
        resolved_id, bearer_token, key_provider=key_provider
    )
    row = SelfHostedProvider(
        id=resolved_id,
        org_id=DEFAULT_ORG_ID,
        name=name,
        base_url=base_url,
        ciphertext=ciphertext,
        nonce=nonce,
        auth_tag=auth_tag,
        cost_basis_per_gpu_hour=cost_basis_per_gpu_hour,
        verified=False,
        models=sorted(set(models)),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise SelfHostedProviderNameConflictError(name) from None
    await session.refresh(row)
    return row


async def edit_self_hosted_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    name: str | None = None,
    base_url: str | None = None,
    bearer_token: str | None = None,
    bearer_token_provided: bool = False,
    cost_basis_per_gpu_hour: Decimal | None = None,
    models: list[str] | None = None,
    key_provider: KeyProvider,
) -> SelfHostedProvider:
    """Partial update of an existing row - every parameter left at its
    default leaves that field unchanged. Raises
    `SelfHostedProviderNotFoundError` (404) if `provider_id` doesn't
    reference a row in this org, or the same 422/409 errors
    `register_self_hosted_provider` can raise for a `models`/`name`
    conflict.

    `bearer_token_provided` disambiguates "the admin submitted an explicit
    `bearer_token` field" from "the admin omitted it" - `bearer_token=None`
    is a legitimate, deliberate value (AC5.5.1/`OllamaCredential`'s
    one-representation discipline: no token configured), so a bare
    `bearer_token is not None` check would wrongly treat "clear the token"
    as "leave it unchanged". Re-encrypting the token OR changing `base_url`
    resets `verified` back to `false` - a changed endpoint/credential must
    be re-verified before it is routable again (AC5.5.3's "not verified
    until a live health probe succeeds" applies just as much to a stale
    verification of an endpoint that no longer looks the way it did when it
    was last probed).
    """
    row = await get_self_hosted_provider_by_id(session, provider_id)
    if row is None:
        raise SelfHostedProviderNotFoundError(provider_id)

    if models is not None:
        await _validate_model_ids(session, models=models, exclude_provider_id=provider_id)
        row.models = sorted(set(models))
    if name is not None:
        row.name = name
    if cost_basis_per_gpu_hour is not None:
        row.cost_basis_per_gpu_hour = cost_basis_per_gpu_hour
    if base_url is not None:
        row.base_url = base_url
        row.verified = False
    if bearer_token_provided:
        ciphertext, nonce, auth_tag = _encrypt_bearer_token(
            row.id, bearer_token, key_provider=key_provider
        )
        row.ciphertext, row.nonce, row.auth_tag = ciphertext, nonce, auth_tag
        row.verified = False

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise SelfHostedProviderNameConflictError(name or row.name) from None
    await session.refresh(row)
    return row


async def remove_self_hosted_provider(session: AsyncSession, provider_id: uuid.UUID) -> bool:
    """Delete one row. Returns `True` if a row was deleted. `usage_logs.
    self_hosted_provider_id` referencing it is left in place with `NULL`
    (`ON DELETE SET NULL` - see `db.models.usage_log.UsageLog`'s docstring)
    - a historical usage record must outlive the provider that generated
    it, same as every other nullable FK on that table."""
    row = await get_self_hosted_provider_by_id(session, provider_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def reverify_self_hosted_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    key_provider: KeyProvider,
    validator: OllamaValidator | None = None,
) -> SelfHostedProvider:
    """Manual re-verification (AC5.5.3): reuses `OllamaValidator.validate()`'s
    `GET {base_url}/v1/models` health probe as-is, parameterized by this
    row's own `base_url`/decrypted `bearer_token` - NOT wired into the
    Phase 4 `run_provider_key_health_check_if_due` job (that job is scoped
    to `provider_keys` backup groups only; extending continuous polling to
    self-hosted endpoints is an explicit deferred fast-follow, per the
    design doc).

    Raises `SelfHostedProviderNotFoundError` (404) if `provider_id` doesn't
    reference a row in this org. Always commits (whether the probe
    succeeded or failed) - `verified` is set to the probe's own outcome,
    never left stale.
    """
    row = await get_self_hosted_provider_by_id(session, provider_id)
    if row is None:
        raise SelfHostedProviderNotFoundError(provider_id)

    credential = await get_decrypted_self_hosted_credential(
        session, provider_id, key_provider=key_provider
    )
    active_validator = validator if validator is not None else OllamaValidator()
    result = await active_validator.validate(
        {"base_url": credential.base_url, "bearer_token": credential.bearer_token}
    )
    row.verified = result.status is ValidationStatus.VALID
    await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Credential fetch + dispatch (design doc section 2.3(b))
# ---------------------------------------------------------------------------


async def get_decrypted_self_hosted_credential(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    key_provider: KeyProvider,
) -> OllamaCredential:
    """Fetch and decrypt one `self_hosted_providers` row's `bearer_token`
    for an outbound call - the self-hosted-governance sibling of
    `services.proxy_keys.get_decrypted_provider_credential`.

    Returns a `services.proxy_keys.OllamaCredential` (`provider=
    "self_hosted"`, `base_url`/`bearer_token` from the row) - reused as-is,
    see module docstring "Credential shape reuse". `bearer_token` is never
    `None` (empty string = "not configured"), matching `OllamaCredential`'s
    own one-representation discipline.

    Raises:
        `SelfHostedCredentialNotConfiguredError` - no row with this id in
            this org. Route handlers translate this to `errors.
            ProviderNotConfiguredError` (404) - see
            `api.v1.gateway.common.call_self_hosted_provider`.
        `encryption.DecryptionError` - the row failed to decrypt (wrong
            master key, or ciphertext/nonce/auth_tag/AAD mismatch) - this
            module's own static/generic error class, safe to log/return
            as-is, propagated unchanged (same contract as `services.
            proxy_keys.get_decrypted_provider_credential`).
        `SelfHostedCredentialDecodeError` - decryption succeeded but the
            plaintext wasn't valid JSON in the expected shape.

    Never logs decrypted plaintext, on any of the above paths.
    """
    row = await get_self_hosted_provider_by_id(session, provider_id)
    if row is None:
        raise SelfHostedCredentialNotConfiguredError(provider_id)

    aad = build_aad(str(row.org_id), f"self_hosted:{row.id}")
    # DecryptionError propagates as-is - see docstring above.
    plaintext = decrypt_secret(
        row.ciphertext, nonce=row.nonce, auth_tag=row.auth_tag, aad=aad, key_provider=key_provider
    )
    try:
        bearer_token = json.loads(plaintext)
        if not isinstance(bearer_token, str):
            raise TypeError("decoded self-hosted bearer_token credential was not a string")
    except (json.JSONDecodeError, TypeError):
        # Deliberately swallow the underlying exception - see
        # `SelfHostedCredentialDecodeError`'s docstring for why its message
        # could otherwise carry a fragment of the decrypted plaintext.
        raise SelfHostedCredentialDecodeError() from None

    return OllamaCredential(provider="self_hosted", base_url=row.base_url, bearer_token=bearer_token)
