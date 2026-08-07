"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from presidio_analyzer import AnalyzerEngine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.config import Settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.team_membership import TeamMembership
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError, UnauthorizedError
from gatekey.providers.base import ProviderValidator
from gatekey.providers.registry import build_validator_registry
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.services.encryption import EnvKeyProvider, KeyProvider
from gatekey.services.model_policy import ContentAwareRuleCache, ModelPolicyCache, TeamModelPolicyCache
from gatekey.services.cli_refresh_credentials import (
    REFRESH_CREDENTIAL_PREFIX,
    get_active_cli_refresh_credential_by_hash,
)
from gatekey.services.personal_keys import (
    PERSONAL_SECRET_PREFIX,
    get_active_personal_key_by_hash,
)
from gatekey.services.access_schedules import AccessScheduleCache
from gatekey.services.custom_models import CustomModelRouteCache
from gatekey.services.degradation import DegradationPolicyCache
from gatekey.services.provider_key_health import TeamFailoverOverrideCache
from gatekey.services.rate_limit import RateLimitCache
from gatekey.services.residency import ResidencyRuleCache
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache
from gatekey.services.shared_state import SharedStateStore
from gatekey.services.scim import ScimError, get_scim_config, scim_token_matches
from gatekey.services.shadow_ai import (
    SHADOW_AI_INGEST_TOKEN_PREFIX,
    get_shadow_ai_ingest_config,
    shadow_ai_ingest_token_matches,
)
from gatekey.services.service_accounts import (
    SECRET_PREFIX,
    get_active_service_account_by_hash,
    hash_secret,
)
from gatekey.services.sessions import (
    BREAK_GLASS_SESSION_CONTEXT,
    SessionContext,
    try_get_session_context,
)

if TYPE_CHECKING:
    from gatekey.services.response_cache import CacheInvalidator, CachingSettingsCache

# `auto_error=False` so we control the error envelope/shape (via
# `UnauthorizedError`) rather than FastAPI's default plain-text 403.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    """Fetch the `Settings` instance stashed on `app.state` at startup."""
    return request.app.state.settings


def get_validator_registry(
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, ProviderValidator]:
    """Build a fresh provider -> validator mapping for this request.

    Cheap to construct per request (each validator just wraps the
    configured timeout) - see `providers.registry.build_validator_registry`.
    """
    return build_validator_registry(
        timeout_seconds=settings.GATEKEY_PROVIDER_VALIDATION_TIMEOUT_SECONDS
    )


def get_key_provider(settings: Settings = Depends(get_settings_dep)) -> KeyProvider:
    """Build the `KeyProvider` (master key source) for this request.

    Never logs or otherwise persists the decoded key bytes beyond this
    short-lived object - see `services.encryption.EnvKeyProvider`.
    """
    return EnvKeyProvider.from_settings(settings)


def get_provider_http_client(request: Request) -> httpx.AsyncClient:
    """Fetch the shared, pooled `httpx.AsyncClient` stashed on `app.state` at startup.

    Built exactly once per process in `main.create_app`'s lifespan (design
    doc section 6.1: `httpx.Limits(max_keepalive_connections=20,
    max_connections=100)`) - every gateway route handler (BD-9) depends on
    this instead of constructing its own client per request, which would
    both defeat connection pooling and add avoidable TLS handshake latency
    to every outbound provider call.
    """
    return request.app.state.provider_http_client


def get_vertex_token_cache(request: Request) -> VertexAITokenCache:
    """Fetch the shared `VertexAITokenCache` stashed on `app.state` at startup.

    Built exactly once per process in `main.create_app`'s lifespan - see
    `VertexAITokenCache`'s docstring for why this must be a single
    long-lived instance shared across requests, not constructed per call.
    """
    return request.app.state.vertex_token_cache


def get_model_policy_cache(request: Request) -> ModelPolicyCache:
    """Fetch the shared, in-process `ModelPolicyCache` stashed on `app.state`.

    Phase 1.3 (Model Access Governance - Basic). Built once per process in
    `main.create_app`'s lifespan - see `services.model_policy.
    ModelPolicyCache` and `docs/design/phase-1.3-model-governance.md`
    section 2 for why this is a single, process-lifetime instance rather
    than constructed per request.
    """
    return request.app.state.model_policy_cache


def get_team_model_policy_cache(request: Request) -> TeamModelPolicyCache:
    """Fetch the shared, in-process `TeamModelPolicyCache` stashed on
    `app.state` (Phase 2, BD-12) - built once per process in
    `main.create_app`'s lifespan, same single-instance contract as
    `get_model_policy_cache` above."""
    return request.app.state.team_model_policy_cache


def get_content_aware_rule_cache(request: Request) -> ContentAwareRuleCache:
    """Fetch the shared, in-process `ContentAwareRuleCache` stashed on
    `app.state` (Phase 3, BD-5) - same single-instance contract as
    `get_model_policy_cache` above."""
    return request.app.state.content_aware_rule_cache


def get_residency_rule_cache(request: Request) -> ResidencyRuleCache:
    """Fetch the shared, in-process `ResidencyRuleCache` stashed on
    `app.state` (Phase 3, BD-3) - same single-instance contract as
    `get_model_policy_cache` above."""
    return request.app.state.residency_rule_cache


def get_access_schedule_cache(request: Request) -> AccessScheduleCache:
    """Fetch the shared, in-process `AccessScheduleCache` stashed on
    `app.state` (Phase 3, BD-16) - same single-instance contract as
    `get_model_policy_cache` above."""
    return request.app.state.access_schedule_cache


def get_shared_state_store(request: Request) -> SharedStateStore:
    """Fetch the shared, process-lifetime `SharedStateStore` stashed on
    `app.state` (Phase 4, BD-1/BD-2) - `InProcessSharedStateStore` by
    default, `RedisSharedStateStore` when `GATEKEY_REDIS_URL` is configured.
    Built once per process in `main.create_app`'s lifespan - same
    single-instance contract as `get_model_policy_cache` above."""
    return request.app.state.shared_state_store


def get_cache_invalidator(store: SharedStateStore = Depends(get_shared_state_store)) -> "CacheInvalidator":
    """Build a `services.response_cache.CacheInvalidator` for this request
    (Fix 3, security review - see `services.residency`/`services.dlp`'s
    `cache_invalidator` parameters). Cheap to construct per request (just
    wraps the shared `SharedStateStore`, same as `ResponseCache` itself) -
    no process-lifetime state of its own, unlike the `*Cache` singletons
    above."""
    from gatekey.services.response_cache import CacheInvalidator

    return CacheInvalidator(store)


def get_team_failover_override_cache(request: Request) -> TeamFailoverOverrideCache:
    """Fetch the shared, in-process `TeamFailoverOverrideCache` stashed on
    `app.state` (Phase 4, BD-4) - same single-instance contract as
    `get_model_policy_cache` above."""
    return request.app.state.team_failover_override_cache


def get_rate_limit_cache(request: Request) -> RateLimitCache:
    """Fetch the shared, in-process `RateLimitCache` stashed on `app.state`
    (Phase 4, BD-2) - same single-instance contract as `get_model_policy_
    cache` above. Fix 6 (NFR gap): now actually wired into `main.py`'s
    lifespan and read by `check_rate_limit()` - previously constructed
    nowhere and unused."""
    return request.app.state.rate_limit_cache


def get_caching_settings_cache(request: Request) -> "CachingSettingsCache":
    """Fetch the shared, in-process `CachingSettingsCache` stashed on
    `app.state` (Phase 4, BD-3) - same single-instance contract as
    `get_model_policy_cache` above. Fix 6 (NFR gap): now actually wired
    into `main.py`'s lifespan and read by `check_response_cache()` -
    previously constructed nowhere and unused."""
    return request.app.state.caching_settings_cache


def get_degradation_policy_cache(request: Request) -> DegradationPolicyCache:
    """Fetch the shared, in-process `DegradationPolicyCache` stashed on
    `app.state` (Phase 4, BD-5) - same single-instance contract as
    `get_model_policy_cache` above. Fix 6 (NFR gap): now actually wired
    into `main.py`'s lifespan and read by `check_and_apply_degradation()` -
    previously constructed nowhere and unused."""
    return request.app.state.degradation_policy_cache


def get_self_hosted_model_route_cache(request: Request) -> SelfHostedModelRouteCache:
    """Fetch the shared, in-process `SelfHostedModelRouteCache` stashed on
    `app.state` (Phase 5 - Differentiators, 5.5) - same single-instance
    contract as `get_model_policy_cache` above. Built once per process in
    `main.create_app`'s lifespan, warmed via `_warm_self_hosted_model_route_
    cache`. Threaded ONLY into `api/v1/gateway/chat.py`'s handler (design
    doc wiring checklist "5.3 (Self-Hosted Governance, 5.5)" row 4) -
    `completions.py`/`embeddings.py` never depend on this, which is what
    structurally enforces AC5.5.4's "chat completions only" constraint."""
    return request.app.state.self_hosted_model_route_cache


def get_custom_model_route_cache(request: Request) -> CustomModelRouteCache:
    """Fetch the shared, in-process `CustomModelRouteCache` stashed on
    `app.state` (Custom Model Registry / Admin-Managed BYOK Models, CMR-4) -
    same single-instance contract as `get_self_hosted_model_route_cache`
    above. Built once per process in `main.create_app`'s lifespan, warmed
    via `_warm_custom_model_route_cache` (technical design doc section 5
    row 3 - a separate task, CMR-6, wires the actual construction/warming;
    this dependency is correct to add now regardless, since it only reads
    `request.app.state` at request time, same as every other `*Cache`
    dependency in this module). Threaded into BOTH `api/v1/gateway/chat.py`
    and `api/v1/gateway/embeddings.py` (unlike self-hosted, which is
    chat-only) - `completions.py` never depends on this, which is what
    structurally enforces the product spec's "custom models are never
    routable at `/v1/completions`" non-goal (technical design doc section
    2.2/section 5 row 9)."""
    return request.app.state.custom_model_route_cache


def get_dlp_analyzer_engine(request: Request) -> AnalyzerEngine:
    """Fetch the shared, process-lifetime Presidio `AnalyzerEngine` stashed
    on `app.state` (Phase 3, BD-1) - built once in `main.create_app`'s
    lifespan (loading a spaCy model is expensive, ~1-2s) - see
    `services.dlp.build_analyzer_engine`."""
    return request.app.state.dlp_analyzer_engine


def get_source_ip(
    request: Request, settings: Settings = Depends(get_settings_dep)
) -> str | None:
    """Best-effort resolution of the caller's source IP for audit entries
    (Phase 3, AC1.1/AC1.2, design doc section 7.1).

    `GATEKEY_TRUST_PROXY_HEADERS` (off by default - self-hosted deployments
    may sit directly on the internet with no trusted reverse proxy in
    front, in which case `X-Forwarded-For` is fully caller-spoofable):
    when enabled, the first hop of `X-Forwarded-For` (falling back to
    `X-Real-IP`) is trusted; otherwise the TCP peer address
    (`request.client.host`) is used directly. Returns `None` (never raises)
    if neither is available - a missing source IP must never block an
    audit write.
    """
    if settings.GATEKEY_TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            first_hop = forwarded_for.split(",")[0].strip()
            if first_hop:
                return first_hop
        real_ip = request.headers.get("x-real-ip")
        if real_ip and real_ip.strip():
            return real_ip.strip()
    return request.client.host if request.client is not None else None


def _matches_break_glass_token(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> bool:
    """Constant-time check of the bearer token against GATEKEY_ADMIN_TOKEN.

    Cheap and DB-free - always tried FIRST by every privileged dependency
    below, preserving Phase 1's break-glass check/timing unchanged. Never
    logs the submitted or expected token.
    """
    if credentials is None or not credentials.credentials:
        return False
    settings: Settings = request.app.state.settings
    return hmac.compare_digest(
        credentials.credentials.encode("utf-8"),
        settings.GATEKEY_ADMIN_TOKEN.encode("utf-8"),
    )


async def try_get_privileged_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
) -> SessionContext | None:
    """Break-glass bearer token (as the org_admin-equivalent
    `BREAK_GLASS_SESSION_CONTEXT` sentinel) OR a real session cookie.

    The shared resolution for every Phase 2 privileged surface (`require_
    role`/`require_team_role` factories, `get_privileged_session`) - product
    spec locked decision #1 says GATEKEY_ADMIN_TOKEN keeps full Org Admin
    rights indefinitely, so a self-hosted operator who never configures SSO
    can still drive every admin surface. Personal-scope routes
    (`/v1/keys` self-serve, `/v1/me/*`, onboarding, `/v1/auth/*`) must NOT
    use this - they stay on the cookie-only `get_current_session`.
    """
    if _matches_break_glass_token(request, credentials):
        return BREAK_GLASS_SESSION_CONTEXT
    return await try_get_session_context(request, session)


async def get_privileged_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> SessionContext:
    """`get_current_session` widened with the break-glass bearer path -
    the base dependency for the `require_role`/`require_team_role` factories
    and for routes that branch on `org_role` in their handler (e.g. the
    `GET /v1/teams` listing routes)."""
    ctx = await try_get_privileged_context(request, credentials, session)
    if ctx is None:
        raise UnauthorizedError("Missing or invalid session.")
    return ctx


@dataclass(frozen=True)
class AdminContext:
    """Identity of the authenticated admin actor (Phase 2, design doc 2.3).

    `actor_user_id` is None for the break-glass token path (A4) - the audit
    trail records the `"system:admin_token"` sentinel label instead.
    """

    actor_user_id: uuid.UUID | None
    actor_label: str
    org_id: uuid.UUID


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AdminContext:
    """Break-glass bearer token OR an org_admin session cookie - either
    satisfies this dependency (AC1.4/AC1.5, design doc 2.3).

    Checked in that order: the break-glass path is a cheap, no-DB
    constant-time comparison (`hmac.compare_digest`, so response timing
    doesn't leak how many leading characters matched) and is tried first,
    exactly preserving Phase 1's original check/timing for that path
    unchanged; only falls through to a session lookup if no valid bearer
    token was presented. Never logs the submitted or expected token,
    including on failure.

    Existing Phase 1 admin routers declare this router-level
    (`dependencies=[Depends(require_admin)]`) and ignore the return value -
    the `None` -> `AdminContext` return-type change is purely additive.
    """
    if _matches_break_glass_token(request, credentials):
        return AdminContext(
            actor_user_id=None, actor_label="system:admin_token", org_id=DEFAULT_ORG_ID
        )

    ctx = await try_get_session_context(request, session)
    if ctx is not None and ctx.org_role == "org_admin":
        return AdminContext(
            actor_user_id=ctx.user_id, actor_label=ctx.display_label, org_id=ctx.org_id
        )

    raise UnauthorizedError("Missing or invalid admin credential.")


async def require_admin_or_auditor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AdminContext:
    """`require_admin` (break-glass token OR org_admin session) widened to
    also accept an *auditor* session - for the Phase 2 read-only surfaces
    the design grants auditors (section 5.8's usage summary).

    Chosen shape (per BD-19/5.8 wiring note): keep `require_admin`
    untouched for every existing admin route, and layer this as a separate
    dependency rather than parameterizing `require_admin` - the simplest
    change that leaves the existing trust boundary byte-for-byte intact.
    Returns the same `AdminContext` shape either way (an auditor's context
    just carries their own user id/label).
    """
    try:
        return await require_admin(request, credentials, session)
    except UnauthorizedError:
        ctx = await try_get_session_context(request, session)
        if ctx is not None and ctx.org_role == "auditor":
            return AdminContext(
                actor_user_id=ctx.user_id, actor_label=ctx.display_label, org_id=ctx.org_id
            )
        raise


def require_role(*allowed_org_roles: Literal["org_admin", "auditor"]):
    """Factory for ORG-WIDE-role-only routes (design doc 2.4).

    A member/team_lead session (`org_role` NULL) is always rejected here
    regardless of any team they lead, by design: leading a team is not an
    org-wide privilege. The break-glass bearer token passes as an org_admin-
    equivalent caller (`BREAK_GLASS_SESSION_CONTEXT`, via
    `get_privileged_session`) - locked decision #1/A4.
    """

    async def _dep(ctx: SessionContext = Depends(get_privileged_session)) -> SessionContext:
        if ctx.org_role not in allowed_org_roles:
            raise ForbiddenError("This action requires an org-wide role.")
        return ctx

    return _dep


@dataclass(frozen=True)
class TeamRoleContext:
    """Result of `require_team_role` - the session plus the resolved
    role-for-this-specific-team (a user can hold different roles on
    different teams, AC1.2)."""

    session: SessionContext
    team_id: uuid.UUID
    role: Literal["team_lead", "member", "org_admin"]
    via_bypass: bool


async def _get_team_membership(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID | None
) -> TeamMembership | None:
    # Lives here (private) until BD-14's `services/teams.py` exists to own it.
    stmt = select(TeamMembership).where(
        TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def require_team_role(
    *allowed_team_roles: Literal["team_lead", "member"], org_admin_bypass: bool = True
):
    """Factory for TEAM-scoped routes (design doc 2.4). The returned
    dependency consumes the route's own `team_id` path parameter.

    `org_admin_bypass=True` (default): an `org_admin` session always passes,
    membership or not - Org Admin has full control over every team (locked
    architecture decision). The break-glass bearer token rides the same
    bypass (it resolves to the org_admin-equivalent
    `BREAK_GLASS_SESSION_CONTEXT` via `get_privileged_session`). Set False
    only for a route that must stay strictly team-internal even to an Org
    Admin (none this phase).

    On failure (no membership, or insufficient role) raises the SAME generic
    403 regardless of whether the team exists at all - deliberately never
    distinguishing "team not found" from "insufficient role", mirroring
    `require_service_account`'s anti-enumeration discipline.
    """

    async def _dep(
        team_id: uuid.UUID,
        ctx: SessionContext = Depends(get_privileged_session),
        session: AsyncSession = Depends(get_db_session),
    ) -> TeamRoleContext:
        if org_admin_bypass and ctx.org_role == "org_admin":
            return TeamRoleContext(session=ctx, team_id=team_id, role="org_admin", via_bypass=True)
        membership = await _get_team_membership(session, team_id=team_id, user_id=ctx.user_id)
        if membership is None or membership.role.value not in allowed_team_roles:
            raise ForbiddenError("You do not have the required role for this team.")
        return TeamRoleContext(
            session=ctx, team_id=team_id, role=membership.role.value, via_bypass=False
        )

    return _dep


@dataclass(frozen=True)
class ServiceAccountContext:
    """Identity of the authenticated service-account caller, resolved by
    `require_service_account`.

    `org_id` is the matched row's *real* `org_id`, not the `DEFAULT_ORG_ID`
    constant directly - deliberate forward-compatibility so that when
    Phase 2 multi-org support lands, this dependency's contract (and every
    gateway route handler built against it) does not need to change; only
    how `org_id` ends up on the row does. See design doc section 9.
    """

    org_id: uuid.UUID
    service_account_id: uuid.UUID
    user_id: uuid.UUID  # NEW - Phase 1.4 (Budget - Basic); the budget-owning cost-center this key charges against.
    name: str


async def require_service_account(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> ServiceAccountContext:
    """Require a valid `Authorization: Bearer gk_sk_...` service-account key.

    This is a completely separate, non-overlapping trust boundary from
    `require_admin` above (human admin token vs. per-app service-account
    credential) - see design doc section 4. Used by every gateway route
    handler (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`
    - see `api/v1/gateway/*.py`) as their auth dependency.

    Rejects with 401 `UnauthorizedError` (using an identical, generic
    message on every rejection path) if:
      - the `Authorization` header is missing or malformed,
      - the token doesn't start with the `gk_sk_` prefix, or
      - no active (non-revoked) service-account key matches the token's
        hash.
    The last case deliberately does not distinguish "never existed" from
    "revoked" - doing so would let an attacker probe which token prefixes
    correspond to real (if revoked) credentials vs. ones that never
    existed. Never logs the submitted token on any path, on success or
    failure - mirrors `require_admin`'s care about this.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing or malformed Authorization header.")

    submitted = credentials.credentials
    if not submitted.startswith(SECRET_PREFIX):
        raise UnauthorizedError("Invalid service account key.")

    secret_hash = hash_secret(submitted)
    row = await get_active_service_account_by_hash(session, secret_hash)
    if row is None:
        raise UnauthorizedError("Invalid service account key.")

    return ServiceAccountContext(
        org_id=row.org_id,
        service_account_id=row.id,
        user_id=row.user_id,
        name=row.name,
    )


@dataclass(frozen=True)
class GatewayCallerContext:
    """Identity of the authenticated gateway caller, resolved by
    `require_gateway_credential` (Phase 2, design doc 2.5).

    `user_id` is the budget/policy identity - the owning human or app's
    user, exactly what `ServiceAccountContext.user_id` was. `team_id` is the
    resolved team context (A6/AC5.5); None = legacy flat-budget path (a
    pre-Phase-2 `ServiceAccountKey` with no `team_id`).
    """

    org_id: uuid.UUID
    credential_id: uuid.UUID
    credential_type: Literal["service_account", "personal"]
    user_id: uuid.UUID
    team_id: uuid.UUID | None
    name: str


# One generic message for every rejection path in `require_gateway_credential`
# - wrong/unknown prefix, revoked, expired, never existed - so a probing
# caller learns nothing about which credential shapes/tokens are real (same
# anti-enumeration posture as `require_service_account`).
_GENERIC_GATEWAY_CREDENTIAL_MESSAGE = "Invalid gateway credential."


async def require_gateway_credential(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> GatewayCallerContext:
    """Unified gateway auth: dispatch on the bearer token's prefix.

    `gk_sk_` -> `ServiceAccountKey` lookup (the existing hash/lookup logic,
    unchanged); `gk_pk_` -> `PersonalApiKey` lookup (same shape PLUS the
    SQL-side `expires_at` freshness check - see
    `get_active_personal_key_by_hash`). Any other/missing prefix -> the same
    generic 401. Replaces `require_service_account` as the auth dependency
    on all three gateway routes; `require_service_account` itself is kept
    unchanged as the concrete `gk_sk_` trust boundary elsewhere.

    Never logs the submitted token on any path.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing or malformed Authorization header.")

    submitted = credentials.credentials
    if submitted.startswith(SECRET_PREFIX):
        sa_row = await get_active_service_account_by_hash(session, hash_secret(submitted))
        if sa_row is None:
            raise UnauthorizedError(_GENERIC_GATEWAY_CREDENTIAL_MESSAGE)
        return GatewayCallerContext(
            org_id=sa_row.org_id,
            credential_id=sa_row.id,
            credential_type="service_account",
            user_id=sa_row.user_id,
            team_id=sa_row.team_id,
            name=sa_row.name,
        )
    if submitted.startswith(PERSONAL_SECRET_PREFIX):
        pk_row = await get_active_personal_key_by_hash(session, hash_secret(submitted))
        if pk_row is None:
            raise UnauthorizedError(_GENERIC_GATEWAY_CREDENTIAL_MESSAGE)
        return GatewayCallerContext(
            org_id=pk_row.org_id,
            credential_id=pk_row.id,
            credential_type="personal",
            user_id=pk_row.owner_user_id,
            team_id=pk_row.team_id,
            name=pk_row.name,
        )
    raise UnauthorizedError(_GENERIC_GATEWAY_CREDENTIAL_MESSAGE)


@dataclass(frozen=True)
class CliRefreshCredentialContext:
    """Identity of an authenticated CLI-sync refresh-credential caller
    (Phase 3, BD-25, design doc section 8.2).

    A completely separate, non-overlapping trust boundary from every other
    dependency in this module - a `gk_rf_...` token is NEVER accepted by
    `require_gateway_credential`/`require_admin`/`get_current_session` (it
    isn't a session cookie or either gateway-credential prefix), and
    `require_gateway_credential` never accepts a `gk_rf_...` token either
    (wrong prefix). Its only power is `GET /v1/me/current-key`.
    """

    credential_id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    bound_personal_key_id: uuid.UUID


async def require_cli_refresh_credential(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> CliRefreshCredentialContext:
    """Require a valid `Authorization: Bearer gk_rf_...` CLI refresh
    credential (design doc section 8.2). Same anti-enumeration discipline as
    `require_service_account`/`require_gateway_credential` - one generic
    401 for missing/malformed header, wrong prefix, revoked, or never-
    existed. Never logs the submitted token.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing or malformed Authorization header.")

    submitted = credentials.credentials
    if not submitted.startswith(REFRESH_CREDENTIAL_PREFIX):
        raise UnauthorizedError("Invalid CLI refresh credential.")

    row = await get_active_cli_refresh_credential_by_hash(session, hash_secret(submitted))
    if row is None:
        raise UnauthorizedError("Invalid CLI refresh credential.")

    return CliRefreshCredentialContext(
        credential_id=row.id,
        org_id=row.org_id,
        user_id=row.user_id,
        bound_personal_key_id=row.bound_personal_key_id,
    )


@dataclass(frozen=True)
class ScimContext:
    """Identity of the authenticated SCIM client (the org's IdP), resolved
    by `require_scim_token` (Phase 3, BD-20, design doc section 6.2)."""

    org_id: uuid.UUID


async def require_scim_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> ScimContext:
    """Require a valid SCIM bearer token (`scim_config.bearer_token_hash`).

    Raises `services.scim.ScimError` (RFC 7644 shape), NOT `UnauthorizedError`
    - every `/scim/v2/...` route expects the RFC's own error envelope, not
    this codebase's generic `{"error": {...}}` shape (see `services.scim`'s
    module docstring for the full rationale and where the dedicated
    exception handler is registered). Constant-time
    (`services.scim.scim_token_matches`'s `hmac.compare_digest`) - see that
    function's docstring for why this fetches the one per-org candidate row
    rather than doing a `WHERE bearer_token_hash = :hash` lookup. A single
    generic message covers every failure mode (missing header, disabled
    config, wrong token) - same anti-enumeration posture as every other auth
    dependency in this module.
    """
    if credentials is None or not credentials.credentials:
        raise ScimError(401, "Missing or malformed Authorization header.")
    config = await get_scim_config(session)
    if not scim_token_matches(config, credentials.credentials):
        raise ScimError(401, "Invalid SCIM bearer token.")
    return ScimContext(org_id=config.org_id)  # type: ignore[union-attr]


@dataclass(frozen=True)
class ShadowAiIngestContext:
    """Identity of the authenticated Shadow AI ingestion feed caller,
    resolved by `require_shadow_ai_ingest_token` (Phase 5 - Differentiators,
    5.1 Shadow AI Discovery). See `gatekey/phase-5-technical-design.md`
    section 2.5 "Key Decision" for the full trust-boundary design."""

    org_id: uuid.UUID


async def require_shadow_ai_ingest_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> ShadowAiIngestContext:
    """Require a valid `Authorization: Bearer gk_sai_...` Shadow AI
    ingestion token (`shadow_ai_ingest_config.ingest_token_hash`).

    A FOURTH, fully non-overlapping trust boundary - deliberately NOT
    `require_admin` (break-glass token / org_admin session), NOT
    `require_gateway_credential` (dispatches only on the `gk_sk_`/`gk_pk_`
    prefixes, falls through to a generic 401 for anything else including a
    `gk_sai_...` token), and NOT `require_scim_token` (compares against a
    DIFFERENT column, `scim_config.bearer_token_hash`, on a different table -
    even a hash collision attempt would need to match a different row's
    stored digest). Conversely, a real admin session, break-glass token,
    service-account key, personal key, or SCIM token can never satisfy THIS
    dependency either - it only accepts the `gk_sai_` prefix and only
    compares against `shadow_ai_ingest_config`'s own column. See
    `gatekey/phase-5-technical-design.md` section 2.5's "Confirmed non-reuse,
    both directions" for the full non-overlap proof this dependency
    implements.

    Fail-closed until setup (AC5.1.4): `shadow_ai_ingest_token_matches`
    returns `False` for a missing config row or one with
    `ingest_token_hash IS NULL` - the ingestion endpoint rejects every
    request with the same generic 401 until an Org Admin has generated a
    token. Never logs the submitted token, on any path.

    **Router placement warning** (design doc section 2.5): this dependency
    must be declared on the ONE ingest route itself
    (`api/v1/shadow_ai_ingest.py`), never at router level on a router that
    also carries `dependencies=[Depends(require_admin)]` - doing so would
    let every admin session/break-glass token ALSO satisfy this endpoint,
    defeating the whole point of a distinct trust boundary.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Invalid shadow AI ingestion token.")

    submitted = credentials.credentials
    if not submitted.startswith(SHADOW_AI_INGEST_TOKEN_PREFIX):
        raise UnauthorizedError("Invalid shadow AI ingestion token.")

    config = await get_shadow_ai_ingest_config(session)
    if not shadow_ai_ingest_token_matches(config, submitted):
        raise UnauthorizedError("Invalid shadow AI ingestion token.")

    return ShadowAiIngestContext(org_id=config.org_id)  # type: ignore[union-attr]
