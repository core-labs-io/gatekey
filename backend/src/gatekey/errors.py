"""Structured error envelope, exception handlers, and log redaction helpers.

Every error response from the API follows the shape:

    {"error": {"code": "<machine_readable_code>", "message": "<human readable>"}}

`message` must never contain secret material (provider keys, service account
JSON, admin tokens, etc.) - callers raising `GatekeyError` are responsible for
keeping messages redacted; as a defense-in-depth backstop, `redact()` should
be used any time a raw dict (request body, provider error response, etc.) is
about to be logged, and `redact_json_safe()` should be used for anything
derived from `pydantic.ValidationError.errors()` / `RequestValidationError.
errors()` specifically - those attach an "input" key (the raw submitted
value) to every error dict, keyed independently of the field name that
failed, so plain `redact()`'s field-name matching cannot catch it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("gatekey")

# Field names (case-insensitive) that are always scrubbed before logging.
# Keep this list in sync with any new secret-shaped fields introduced by
# later phases (e.g. new provider payload shapes).
_REDACTED_FIELD_NAMES = {
    "api_key",
    "service_account_json",
    "private_key",
    "token",
    "authorization",
    "ciphertext",
    "nonce",
    "auth_tag",
    "master_key",
    "gatekey_admin_token",
    "gatekey_master_key",
    # Phase 1.2 service-account keys (see
    # `db/models/service_account_key.py` / `schemas/service_account_key.py`):
    # `secret` is the plaintext `gk_sk_...` credential (returned exactly
    # once, by the create endpoint, and never elsewhere); `secret_hash` is
    # its SHA-256 digest, which itself is a valid auth-lookup input and so
    # is treated as sensitive even though it isn't reversible.
    "secret",
    "secret_hash",
    # Phase 1 addition (Ollama provider): optional bearer token for an
    # Ollama instance sitting behind an authenticating reverse proxy - see
    # `services/proxy_keys.py`'s `OllamaCredential`.
    "bearer_token",
    # Phase 2 (security review L-5): a Slack-style incoming-webhook URL
    # embeds a bearer-equivalent secret in its path - see
    # `db/models/team.py`'s encrypted-at-rest columns.
    "webhook_url",
}

_REDACTED_PLACEHOLDER = "***REDACTED***"


def redact(value: Any) -> Any:
    """Recursively scrub known-secret field names from a JSON-like structure.

    Returns a new structure; does not mutate the input. Safe to call on
    arbitrary request bodies / provider responses before logging them.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and key.lower() in _REDACTED_FIELD_NAMES:
                redacted[key] = _REDACTED_PLACEHOLDER
            else:
                redacted[key] = redact(val)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def _strip_input_keys(value: Any) -> Any:
    """Recursively drop any dict key literally named "input" (case-insensitive).

    `pydantic.ValidationError.errors()` (and therefore
    `RequestValidationError.errors()`) attaches an "input" key to every
    error dict holding the raw value that failed validation - keyed
    independently of the field name, so `redact()`'s field-name matching
    cannot catch it. Even with `errors(include_input=False)` used at the
    raise site, this stays as a defense-in-depth backstop for any
    `RequestValidationError` built elsewhere without that flag. Returns a
    new structure; does not mutate the input.
    """
    if isinstance(value, dict):
        return {
            key: _strip_input_keys(val)
            for key, val in value.items()
            if not (isinstance(key, str) and key.lower() == "input")
        }
    if isinstance(value, list):
        return [_strip_input_keys(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_input_keys(item) for item in value)
    return value


def redact_json_safe(value: Any) -> Any:
    """Defense-in-depth redaction for `RequestValidationError.errors()` output.

    Strips any "input" key (see `_strip_input_keys`), then applies
    `redact()`'s known-secret-field-name scrubbing to whatever remains.
    Returns a new structure; does not mutate the input. Use this - not plain
    `redact()` - for anything derived from `.errors()`.
    """
    return redact(_strip_input_keys(value))


class GatekeyError(Exception):
    """Base class for application errors that map to a structured response.

    `message` is shown to the API caller and may be logged - it must never
    contain raw secret material. Use `detail` sparingly for additional
    internal context that is still safe to log.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        # Phase 4 (BD-2, rate limiting): a small number of error paths need
        # to attach caller-facing response headers alongside the structured
        # JSON body (e.g. `Retry-After` on a 429) - see `RateLimitExceededError`
        # below and `register_exception_handlers`' use of this. `None` (the
        # overwhelming majority of every other `GatekeyError`) adds zero
        # headers, exactly today's behavior.
        self.headers = headers
        # Phase 4: a handful of error bodies need caller-facing structured
        # fields beyond `code`/`message` (AC4.2.4's `retry_after_seconds`/
        # `hard_limit`) - merged into the `error` object by
        # `register_exception_handlers` below. `None` (every other
        # `GatekeyError`) adds nothing, exactly today's response shape.
        self.extra = extra


class NotFoundError(GatekeyError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnauthorizedError(GatekeyError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(GatekeyError):
    """Valid credential, insufficient role (Phase 2 RBAC).

    Kept distinct from `UnauthorizedError` (no/invalid credential) - the
    standard 401-vs-403 split this codebase already draws
    (`ModelDeniedError` is the existing 403 precedent). Messages must stay
    generic on team-scoped routes: `require_team_role` deliberately never
    distinguishes "team not found" from "insufficient role"
    (anti-enumeration - see `api.deps.require_team_role`).
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class OidcUnavailableError(GatekeyError):
    """The configured OIDC issuer could not be reached / returned an
    unusable discovery or JWKS document (Phase 2, `services/oidc.py`).

    502 - an upstream (IdP) availability problem, not a caller error.
    `message` must never echo raw IdP response bodies.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "sso_unavailable"


# ---------------------------------------------------------------------------
# Phase 1.2 (BD-10): gateway-specific errors.
#
# Raised by the gateway route handlers (`api/v1/gateway/*.py`, BD-9) and
# reuse the exact same generic `{"error": {"code", "message"}}` envelope as
# every other `GatekeyError` - the gateway does NOT get a second,
# OpenAI-shaped error serializer (design doc section 8; a deliberate,
# already-made decision, not something to reconsider here).
# ---------------------------------------------------------------------------


class ModelNotFoundError(GatekeyError):
    """The requested `model` isn't in the gateway's model registry.

    Raised by gateway route handlers when `providers.model_registry.
    resolve_model()` raises `UnknownModelError` - see that exception's
    docstring for why the model name is safe to include in `message`.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "model_not_found"


class ProviderNotConfiguredError(GatekeyError):
    """No provider key is configured for the resolved model's provider.

    Raised by gateway route handlers when `services.proxy_keys.
    get_decrypted_provider_credential()` raises `ProviderKeyNotConfiguredError`.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "provider_not_configured"


class ModelDeniedError(GatekeyError):
    """The requested model is not permitted by the org's model access policy.

    Phase 1.3 (Model Access Governance - Basic). Raised by
    `api.v1.gateway.common.check_model_policy()` after `resolve_route()` has
    already succeeded for the same `model` string - see that function's
    docstring and `docs/design/phase-1.3-model-governance.md` section 3.1
    for why this guarantees no case/alias/whitespace bypass of the policy's
    `models` list. The model name is safe to include in `message` for the
    same reason `ModelNotFoundError`'s is (caller input, not secret
    material).
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "model_denied"

    def __init__(
        self,
        model: str,
        *,
        blocking_layer: Literal["org", "team", "member", "content_classification"] = "org",
    ) -> None:
        # Phase 2 (BD-13); Phase 3 (BD-6) adds the content_classification
        # branch (AC4.1); per-team-member narrowing adds the "member" branch
        # one layer below "team" - the message names the blocking layer so
        # the caller/frontend can render plain language per AC3.3 -
        # `code`/`status_code` are unchanged across all four.
        if blocking_layer == "team":
            message = (
                f"Model '{model}' is permitted by this organization's model access "
                "policy but excluded by your team's model restriction (team restriction)."
            )
        elif blocking_layer == "member":
            message = (
                f"Model '{model}' is permitted by your team but not assigned to you "
                "specifically - ask your team lead to enable it for your account "
                "(member restriction)."
            )
        elif blocking_layer == "content_classification":
            message = (
                f"Model '{model}' is not permitted for this request: the prompt was "
                "flagged by content-aware routing (PII detected) and this model is not "
                "in the allowed set for that category (content classification)."
            )
        else:
            message = (
                f"Model '{model}' is not permitted by this organization's "
                "model access policy (org policy)."
            )
        super().__init__(message)
        self.blocking_layer = blocking_layer


class ResidencyViolationError(GatekeyError):
    """A request would violate an active hard-block data-residency rule
    (Phase 3, design doc section 3.2/3.3, AC3.6).

    403, never a silent reroute - `code="residency_violation"`. Raised by
    `api.v1.gateway.common.check_residency()` only when the resolved
    `ResidencyDecision.behavior == "hard_block"`; a "warn" outcome never
    raises (the request proceeds unchanged). `provider`/`region` are
    non-secret routing metadata, safe in `message` (same justification as
    `ModelNotFoundError`'s model name).
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "residency_violation"

    def __init__(self, provider: str, region: str | None) -> None:
        region_desc = region if region is not None else "an unresolved/unknown region"
        super().__init__(
            f"This request would route to provider '{provider}' in {region_desc}, "
            "which violates an active data-residency rule for your organization or team."
        )
        self.provider = provider
        self.region = region


class DlpBlockedError(GatekeyError):
    """A DLP scan finding resolved to `block` (Phase 3, design doc section
    3.2/9.2, AC2.5). 403, `code="dlp_blocked"` - the request never reaches
    the provider. `message` deliberately never echoes the flagged content
    itself (only detector/pattern names, which are configuration, not
    caller-submitted secret material)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "dlp_blocked"

    def __init__(self, finding_names: list[str]) -> None:
        super().__init__(
            "This request was blocked by data-loss-prevention policy: flagged by "
            + ", ".join(sorted(set(finding_names)))
            + "."
        )
        self.finding_names = finding_names


class OutsideAllowedScheduleError(GatekeyError):
    """The authenticated gateway caller's resolved access schedule does not
    permit a request at this instant (Phase 3, design doc section 5.3,
    AC9.6). 403, `code="outside_allowed_schedule"` - never a generic 403 or
    silent failure. Raised by `api.v1.gateway.common.check_access_schedule()`
    only when no active emergency override covers the current instant
    either. `message` carries no schedule configuration detail beyond what
    the caller already knows it's bound by (its own credential's resolved
    window), consistent with every other block error's "safe, non-secret
    caller-facing detail" posture.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "outside_allowed_schedule"

    def __init__(self) -> None:
        super().__init__(
            "This credential is only permitted to make requests within its "
            "configured access schedule. This request falls outside that "
            "window and no active emergency override applies."
        )


class UnsupportedRequestError(GatekeyError):
    """The request shape isn't supported by the target endpoint/provider in
    this phase.

    Name-clash note: this is deliberately named the same as
    `providers.base.UnsupportedRequestError`, a plain `Exception` (not
    HTTP-aware) raised by the provider translation layer for shapes it
    can't translate in Phase 1.2's contract (e.g. `n > 1` against
    Anthropic/Vertex AI). The two classes live in different modules and
    serve different layers - see the gateway route handler modules
    (`api/v1/gateway/*.py`) for the explicit import-aliasing convention
    used to keep the two unambiguous at call sites. Route handlers catch
    the provider-layer exception and re-raise this one; this class is also
    raised directly for endpoint-level rejections that never reach the
    provider layer at all: legacy `/v1/completions` with `stream: true`, a
    model resolved to a capability that doesn't match the endpoint, and a
    non-OpenAI model requested against legacy `/v1/completions`.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    code = "unsupported_request"


# Upstream provider HTTP status codes that are meaningful to the *caller* of
# the gateway (not just to Gatekey's own outbound call to the provider) and
# are therefore passed through as-is by `ProviderUpstreamError` rather than
# being flattened to a generic 502. Deliberately excludes 401/403: an
# upstream 401/403 reflects a problem with *Gatekey's own* stored provider
# credential, not with the gateway caller's request, so surfacing it as
# 401/403 back to the caller would be a misleading signal (and could look
# like the caller's own service-account auth failed, which it didn't).
# Upstream 5xx and network-level failures (`upstream_status_code is None`)
# always flatten to 502.
_PASSTHROUGH_UPSTREAM_STATUS_CODES = frozenset({400, 404, 422, 429})


class ProviderUpstreamError(GatekeyError):
    """The upstream provider returned an error, or was unreachable, during
    an inference call.

    Raised by gateway route handlers when `providers.base.ProviderCallError`
    propagates out of a `create_*`/`stream_*` inference call. `message`
    should be taken directly from `ProviderCallError.message`, which is
    already documented as safe to log/return (it never echoes a raw
    provider response body). See `_PASSTHROUGH_UPSTREAM_STATUS_CODES` above
    for exactly which upstream status codes are passed through unchanged
    versus flattened to 502.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "provider_upstream_error"

    def __init__(self, message: str, *, upstream_status_code: int | None = None) -> None:
        resolved_status_code = (
            upstream_status_code
            if upstream_status_code in _PASSTHROUGH_UPSTREAM_STATUS_CODES
            else status.HTTP_502_BAD_GATEWAY
        )
        super().__init__(message, status_code=resolved_status_code)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if extra:
        body.update(extra)
    # Tier 4 ops polish: every error body carries the request correlation id
    # (also echoed as the X-Request-ID response header) so a caller's "my
    # request failed" report can be matched to server logs/usage rows.
    if request_id is not None:
        body["request_id"] = request_id
        headers = {**(headers or {}), "X-Request-ID": request_id}
    return JSONResponse(
        status_code=status_code,
        content={"error": body},
        headers=headers,
    )


def _request_id_of(request: Request) -> str | None:
    """The id assigned by `observability.install_observability`'s middleware
    (None only when an app was built without it, e.g. bare unit fixtures)."""
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Register structured-error handlers on the given FastAPI app."""

    @app.exception_handler(GatekeyError)
    async def _handle_gatekey_error(request: Request, exc: GatekeyError) -> JSONResponse:
        request_id = _request_id_of(request)
        logger.info(
            "gatekey_error",
            extra={"code": exc.code, "path": request.url.path, "request_id": request_id},
        )
        return _error_response(
            exc.status_code,
            exc.code,
            exc.message,
            headers=exc.headers,
            extra=exc.extra,
            request_id=request_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return _error_response(
            exc.status_code, "http_error", detail, request_id=_request_id_of(request)
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # exc.errors() can echo back submitted values (including secret
        # material) via its "input" key, independent of field name; strip
        # that and redact known-secret field names defensively before this
        # ever reaches a log line or the response. Belt-and-suspenders with
        # the `include_input=False` used at the raise site in providers.py.
        safe_errors = redact_json_safe(exc.errors())
        request_id = _request_id_of(request)
        logger.info(
            "request_validation_error",
            extra={"path": request.url.path, "request_id": request_id},
        )
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"Request validation failed: {safe_errors}",
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Never include exception args/repr in the response - they may
        # contain secret material bubbled up from a lower layer. Full
        # (redacted-at-source) details go to the server log only.
        request_id = _request_id_of(request)
        logger.exception(
            "unhandled_exception", extra={"path": request.url.path, "request_id": request_id}
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
            request_id=request_id,
        )
# ---------------------------------------------------------------------------
# Phase 1.4 (Budget - Basic): raised by
# api.v1.gateway.common.check_budget_available().
# ---------------------------------------------------------------------------


class BudgetExhaustedError(GatekeyError):
    """The user has exhausted its per-user spend budget (Phase 1.4).

    402 Payment Required, not 403 - budget exhaustion is a quota/billing
    state, not an authorization decision, unlike ModelDeniedError's 403.
    `message` includes the user's name, budget, and current spend - same
    "caller/state input, not secret material" justification ModelDeniedError
    already uses for the model name.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "budget_exhausted"

    def __init__(self, *, name: str, budget_usd: Decimal, current_spend_usd: Decimal) -> None:
        super().__init__(
            f"User '{name}' has exhausted its budget of ${budget_usd:,.2f} USD "
            f"(current spend: ${current_spend_usd:,.2f} USD). "
            "Contact your administrator to increase the budget.",
            # Tier 4 (OpenAPI/DX polish): the live figures as structured
            # fields, not only prose - callers can render/alert on them
            # without parsing the message. Strings (not floats) to preserve
            # decimal precision, matching every other money field this API
            # returns.
            extra={
                "budget_usd": str(budget_usd),
                "current_spend_usd": str(current_spend_usd),
            },
        )
        self.budget_usd = budget_usd
        self.current_spend_usd = current_spend_usd


class OrgBudgetExhaustedError(GatekeyError):
    """The ORG-WIDE spend safeguard (added alongside migration `0045`) has
    been exhausted - distinct from `BudgetExhaustedError` (a single user's
    own budget) on purpose: these are different situations for the caller.
    A user hitting their own cap should talk to their team lead; the whole
    org hitting this one means every team/user is blocked regardless of
    their individual budgets, and only an org admin can raise the org
    ceiling or reset the counter (`POST /v1/admin/org-settings/reset-spend`).

    402 Payment Required, same reasoning as `BudgetExhaustedError`.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "org_budget_exhausted"

    def __init__(self, *, budget_usd: Decimal, current_spend_usd: Decimal) -> None:
        super().__init__(
            f"The organization has exhausted its org-wide budget of "
            f"${budget_usd:,.2f} USD (current spend: ${current_spend_usd:,.2f} USD). "
            "Contact your Gatekey org admin.",
            extra={
                "budget_usd": str(budget_usd),
                "current_spend_usd": str(current_spend_usd),
            },
        )
        self.budget_usd = budget_usd
        self.current_spend_usd = current_spend_usd


class TeamMembershipRemovedError(GatekeyError):
    """The caller's key resolves to a `(team_id, user_id)` whose
    `TeamMembership` has been removed (added by migration `0049`, soft-
    delete) - the same real, reachable outcome that used to be structurally
    impossible under the old hard-delete-blocked-while-keys-exist guard
    (ADR-4). Removing a member now takes effect immediately (no separate
    "revoke their keys first" step) - this is what actually enforces that:
    the key still authenticates, but every gateway request past that point
    is rejected here. 403, not 401 - the credential itself is still valid,
    the caller has simply lost standing on this specific team.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "team_membership_removed"

    def __init__(self) -> None:
        super().__init__(
            "This key's team membership has been removed. Contact your "
            "team lead or org admin if this is unexpected."
        )


# ---------------------------------------------------------------------------
# Phase 2 (BD-9): assignment-time budget-ceiling enforcement, raised by
# `services/team_budget.py` (design doc section 3.3 / ADR-5 / A3).
# ---------------------------------------------------------------------------


class BudgetCeilingExceededError(GatekeyError):
    """A budget assignment would push the allocated total past the
    constraining ceiling (team ceiling, or org ceiling for team-ceiling
    edits).

    422 - the write is well-formed but violates the allocation invariant.
    `message` carries the live headroom figure (re-derived inside the
    `SELECT ... FOR UPDATE` lock, never trusted from the client) so the UI
    can render "Max: $X (has $X unallocated)". Dollar figures are org
    budget state, not secret material - same justification as
    `BudgetExhaustedError`.
    """

    status_code = 422
    code = "budget_ceiling_exceeded"

    def __init__(self, *, headroom: Decimal, requested: Decimal) -> None:
        super().__init__(
            f"Requested budget of ${requested:,.2f} USD exceeds the available "
            f"headroom of ${max(headroom, Decimal(0)):,.2f} USD under the ceiling."
        )
        self.headroom = headroom
        self.requested = requested


class BudgetCeilingBelowAllocationError(GatekeyError):
    """A ceiling reduction would drop below what is already allocated
    beneath it (A3: members' budgets under a team ceiling, or team ceilings
    under the org ceiling) - rejected rather than silently leaving the
    allocation over its own ceiling.
    """

    status_code = 422
    code = "budget_ceiling_below_current_allocation"

    def __init__(
        self, *, requested_ceiling: Decimal, allocated_total: Decimal, allocated_noun: str
    ) -> None:
        super().__init__(
            f"Cannot reduce ceiling to ${requested_ceiling:,.2f} USD - "
            f"{allocated_noun} are currently allocated "
            f"${allocated_total:,.2f} USD in total."
        )
        self.requested_ceiling = requested_ceiling
        self.allocated_total = allocated_total


# ---------------------------------------------------------------------------
# Phase 4 (Reliability & Cost Efficiency): raised by
# `api.v1.gateway.common.check_rate_limit()`.
# ---------------------------------------------------------------------------


class RateLimitExceededError(GatekeyError):
    """A configured rate limit rejected this request (Phase 4, design doc
    section 2.2/AC4.2.4). 429, `code="rate_limit_exceeded"` - carries
    `Retry-After` (seconds) as a caller-facing response header, mirroring
    the standard HTTP rate-limiting convention. `hard_limit=True` means the
    rule's `on_limit` is `reject` (rejected immediately); `hard_limit=False`
    means the caller was queued (`queue_and_retry`) but the queue's
    `max_queue_wait_seconds` TTL expired before the limit cleared (AC4.2.5) -
    both are surfaced as this same error, distinguished only by
    `retry_after_seconds`/`hard_limit` in the body per AC4.2.4."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limit_exceeded"

    def __init__(
        self,
        *,
        retry_after_seconds: int,
        limit: int,
        hard_limit: bool,
    ) -> None:
        message = (
            "Rate limit exceeded. Retry after "
            f"{retry_after_seconds} second(s)."
            if hard_limit
            else "Rate limit exceeded and the queued retry window expired. "
            "Please retry this request."
        )
        super().__init__(
            message,
            headers={"Retry-After": str(max(0, retry_after_seconds))},
            extra={"retry_after_seconds": retry_after_seconds, "hard_limit": hard_limit},
        )
        self.retry_after_seconds = retry_after_seconds
        self.limit = limit
        self.hard_limit = hard_limit


# ---------------------------------------------------------------------------
# Phase 5 (Differentiators, 5.2 Hash-Chained Audit Ledger): raised by
# `services.compliance_settings`.
# ---------------------------------------------------------------------------


class ChainPurgeMutualExclusivityError(GatekeyError):
    """The hash chain (`compliance_settings.chain_enabled`) and a finite
    `audit_retention_days` purge window can never both be configured at once
    (Phase 5, AC5.2.7) - deleting a row structurally breaks a hash chain.

    422 - the write is well-formed but violates the mutual-exclusivity
    invariant. The Postgres `chk_chain_purge_mutually_exclusive` CHECK
    (migration `0038`) is the ultimate backstop; this is the clean app-layer
    rejection so a client sees a structured error, never a raw
    `IntegrityError`/500 (`services/compliance_settings.py` also catches the
    CHECK violation itself and re-raises this same error, for the same
    reason)."""

    status_code = 422
    code = "chain_purge_mutually_exclusive"


# ---------------------------------------------------------------------------
# Phase 5 (Differentiators, 5.1 Shadow AI Discovery): raised by
# `services.shadow_ai`.
# ---------------------------------------------------------------------------


class ShadowAiEnforcementConfirmationRequiredError(GatekeyError):
    """AC5.1.7: turning `shadow_ai_ingest_config.enforcement_mode` on (from
    `detect_only` to `notification`/`webhook`, or switching between the two
    intrusive modes) requires the caller to explicitly pass `confirm: true`
    in the same request body - the API-contract-level equivalent of the
    admin console's "this is intrusive - are you sure?" confirm dialog.

    422 - the write is well-formed but missing the required confirmation;
    never silently accepted. Re-submitting the identical `enforcement_mode`
    a config already has (no transition) does NOT require `confirm` again -
    see `services.shadow_ai.set_shadow_ai_config`'s docstring."""

    status_code = 422
    code = "shadow_ai_enforcement_confirmation_required"

    def __init__(self, enforcement_mode: str) -> None:
        super().__init__(
            f"Enabling '{enforcement_mode}' enforcement is an intrusive action and "
            "requires explicit confirmation - resubmit with \"confirm\": true.",
        )
        self.enforcement_mode = enforcement_mode


class ShadowAiWebhookUrlRequiredError(GatekeyError):
    """`enforcement_mode = "webhook"` requires a non-empty `webhook_url` in
    the same request - mirrors `services.teams.set_team_alert_config`'s
    identical "cannot enable webhook alerts without a webhook URL configured"
    guard. 422, no DB write."""

    status_code = 422
    code = "shadow_ai_webhook_url_required"

    def __init__(self) -> None:
        super().__init__(
            "Cannot enable webhook enforcement without a webhook_url configured."
        )


# ---------------------------------------------------------------------------
# Tier 4 (OpenAPI hygiene): the error envelope as a declared schema, so SDK
# consumers and codegen see the real error shape instead of FastAPI's
# default `{"detail": ...}` (which this API never returns).
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402  (deliberate tail section)


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable error code.")
    message: str = Field(description="Human-readable explanation. Never contains secrets.")
    request_id: str | None = Field(
        default=None,
        description="Correlation id, identical to the X-Request-ID response header.",
    )


class ErrorEnvelope(BaseModel):
    """Every non-2xx response from this API uses this one shape. Some codes
    add extra sibling fields next to `code`/`message` (e.g. rate limits add
    `retry_after_seconds`/`hard_limit`; budget exhaustion adds
    `budget_usd`/`current_spend_usd`)."""

    error: ErrorBody


# Router-level `responses=` declaration for the public gateway surface -
# documents the envelope on every status the gateway actually returns.
GATEWAY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Unsupported/malformed request."},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid gateway credential."},
    402: {"model": ErrorEnvelope, "description": "Budget exhausted."},
    403: {"model": ErrorEnvelope, "description": "Denied by policy (model/DLP/residency/schedule)."},
    404: {"model": ErrorEnvelope, "description": "Unknown model or unconfigured provider."},
    429: {"model": ErrorEnvelope, "description": "Rate limit exceeded (see Retry-After)."},
    502: {"model": ErrorEnvelope, "description": "Upstream provider error or unreachable."},
}
