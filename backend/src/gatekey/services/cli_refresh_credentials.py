"""CLI-sync device-code auth flow + `cli_refresh_credentials` CRUD (Phase 3,
BD-25). See `docs/design/phase-3-security-compliance-design.md` section 8.2
and the product spec's AC8a.1-AC8a.5.

Two independent pieces:

- `DeviceAuthStore`: the ephemeral (pre-credential) device-code/user-code
  pending-approval state machine (OAuth 2.0 Device Authorization Grant
  shape). Deliberately NOT a DB table - nothing here is a durable secret or
  a long-lived credential; it's a short-lived (default 10 minute), one-shot
  handshake. A plain in-process dict, same "process-local state" category as
  `ModelPolicyCache` et al., but with a real, flagged limitation - see the
  class docstring.
- `create_cli_refresh_credential`/`get_active_cli_refresh_credential_by_hash`:
  the actual durable `cli_refresh_credentials` row CRUD, same
  SHA-256-not-a-KDF hashing discipline as every other high-entropy token in
  this codebase (`services/service_accounts.py`'s `hash_secret`, reused
  verbatim here rather than duplicated - a refresh credential is a random
  256-bit token, not a guessable password, so the same fast-hash rationale
  applies unchanged).

`compute_current_key_valid_until` is fork #3's (design doc section 8.2/10.3)
`valid_until` computation, kept pure/DB-free so it's cheaply unit-testable.
"""

from __future__ import annotations

import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gatekey.db.models.cli_refresh_credential import CliRefreshCredential
from gatekey.errors import GatekeyError
from gatekey.services.service_accounts import hash_secret
from gatekey.services.teams import list_teams_for_user

__all__ = [
    "REFRESH_CREDENTIAL_PREFIX",
    "DEFAULT_CURRENT_KEY_TTL",
    "DeviceAuthRecord",
    "DeviceAuthStore",
    "DevicePollOutcome",
    "compute_current_key_valid_until",
    "create_cli_refresh_credential",
    "generate_refresh_credential_secret",
    "get_active_cli_refresh_credential_by_hash",
    "resolve_team_id_for_device_approval",
]

# Prefix on every plaintext refresh-credential secret - same "recognize the
# credential shape before touching the DB" purpose as `gk_sk_`/`gk_pk_`
# (design doc section 8.2).
REFRESH_CREDENTIAL_PREFIX = "gk_rf_"
_TOKEN_ENTROPY_BYTES = 32

# Phase doc 3.7a's resolved default: no natural off-hours/rotation anchor
# for a personal key (this phase's `rotation_policies` scope never covers
# `personal_api_keys` - only org/service_account/provider_key, see
# `db/models/rotation_policy.py`) -> a fixed, conservative fallback TTL.
DEFAULT_CURRENT_KEY_TTL = timedelta(hours=1)


def generate_refresh_credential_secret() -> str:
    """Fresh plaintext `gk_rf_...` secret - 256 bits of entropy, same shape
    as `services.service_accounts.generate_*`/`services.personal_keys.
    generate_personal_key_secret`. Never persisted; only `hash_secret(...)`
    is stored."""
    return REFRESH_CREDENTIAL_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


async def create_cli_refresh_credential(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    bound_personal_key_id: uuid.UUID,
) -> tuple[CliRefreshCredential, str]:
    """Mint a fresh refresh credential bound to `bound_personal_key_id`.
    Returns `(row, plaintext_secret)` - the plaintext exists only in this
    return value, never persisted or logged. Flushes, does not commit (the
    route handler audits + commits, same contract as `services.
    personal_keys.create_personal_key`)."""
    secret = generate_refresh_credential_secret()
    row = CliRefreshCredential(
        org_id=org_id,
        user_id=user_id,
        bound_personal_key_id=bound_personal_key_id,
        secret_hash=hash_secret(secret),
    )
    session.add(row)
    await session.flush()
    return row, secret


async def get_active_cli_refresh_credential_by_hash(
    session: AsyncSession, secret_hash: bytes
) -> CliRefreshCredential | None:
    """Look up an active (non-revoked) refresh credential by `secret_hash` -
    single indexed-equality query, same anti-enumeration shape as
    `get_active_service_account_by_hash`/`get_active_personal_key_by_hash`
    (revoked and never-existed are indistinguishable to the caller).
    Eager-loads `bound_personal_key` since `api.deps.
    require_cli_refresh_credential` and `GET /v1/me/current-key` both need
    it on the same request without a second lazy-load round trip."""
    stmt = (
        select(CliRefreshCredential)
        .where(
            CliRefreshCredential.secret_hash == secret_hash,
            CliRefreshCredential.revoked_at.is_(None),
        )
        .options(selectinload(CliRefreshCredential.bound_personal_key))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_team_id_for_device_approval(
    session: AsyncSession, *, user_id: uuid.UUID, requested_team_id: uuid.UUID
) -> uuid.UUID:
    """Validate `requested_team_id` against the caller's own memberships.

    Deviation from the design doc's "team_id auto-selected per Phase 2's A1
    pattern if unambiguous, else required" wording: `team_id` is always
    required in the `approve` request body here, full stop - this matches
    the ACTUAL existing A1 precedent in this codebase far more closely than
    the design doc's summary of it. `schemas/personal_api_key.py`'s own
    docstring on `PersonalApiKeyCreateRequest.team_id` is explicit: "the
    frontend auto-selects it when the user belongs to exactly one team
    (A1), but the server never infers it" - i.e. A1 is a client-side UX
    convenience, not server-side inference logic. Reusing that exact
    contract here (frontend pre-fills, server always validates an explicit
    value) is simpler than inventing new server-side ambiguity-resolution
    logic for a case Phase 2 already deliberately chose not to build.
    """
    memberships = await list_teams_for_user(session, user_id)
    if not any(team.id == requested_team_id for team in memberships):
        raise GatekeyError(
            "You do not have the required role for this team.",
            code="forbidden",
            status_code=403,
        )
    return requested_team_id


def compute_current_key_valid_until(
    *, now: datetime, rotation_next_rotation_at: datetime | None
) -> datetime:
    """Fork #3 (design doc section 8.2): `RotationPolicy.next_rotation_at`
    for the bound key's resolved rotation config if one exists AND is still
    in the future, else `now + DEFAULT_CURRENT_KEY_TTL` (phase doc 3.7a's
    resolved 1-hour default). Pure/DB-free - the caller resolves whatever
    `rotation_next_rotation_at` means for the bound key (today: always
    `None`, since `rotation_policies` has no personal-key scope - see
    `DEFAULT_CURRENT_KEY_TTL`'s docstring above; kept as a parameter, not a
    hardcoded `None`, so this function doesn't need to change if a later
    phase adds one)."""
    if rotation_next_rotation_at is not None and rotation_next_rotation_at > now:
        return rotation_next_rotation_at
    return now + DEFAULT_CURRENT_KEY_TTL


# --- Device-code flow state machine (in-process, ephemeral) -----------------

# Excludes visually-ambiguous characters (0/O, 1/I/L) - a human retypes this.
_USER_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01IOL")
DEFAULT_DEVICE_CODE_TTL_SECONDS = 600  # 10 minutes
DEFAULT_POLL_INTERVAL_SECONDS = 5


def _generate_user_code() -> str:
    chars = [secrets.choice(_USER_CODE_ALPHABET) for _ in range(8)]
    return f"{''.join(chars[:4])}-{''.join(chars[4:])}"


@dataclass
class DeviceAuthRecord:
    device_code: str
    user_code: str
    expires_at: datetime
    approved: bool = False
    # Set once, by `approve()`; cleared by the first successful `poll()`
    # that delivers it (one-time-reveal, same discipline as every other
    # plaintext secret in this codebase).
    refresh_credential_plaintext: str | None = field(default=None, repr=False)


DevicePollOutcome = Literal["pending", "approved", "expired", "not_found"]


class DeviceAuthStore:
    """In-process store for pending device-code auth requests (design doc
    section 8.2, AC8a.2).

    ponytail: a plain dict, single-process-only. Every other in-process
    cache this codebase ships (`ModelPolicyCache` et al.) tolerates a stale
    READ across multiple worker processes because a background warm/PUT
    eventually catches every worker up; this store is a WRITE-coordination
    problem instead (`approve()` on worker A, `poll()` on worker B would
    never see the approval) - a real gap under a multi-worker deployment,
    not a stylistic shortcut. Acceptable for this phase because: (a) the
    documented deployment story is still single-replica `docker-compose up`
    (design doc section 2's "self-hosted/no-mandatory-phone-home" NFR
    accounting, and `DO-2`'s scheduler-loop note makes the same
    single-replica assumption elsewhere in this phase), and (b) the failure
    mode is a poll that never completes (CLI retries/times out), not a
    security or data-corruption issue. Upgrade path: back this with a DB
    table (or shared cache) the moment multi-worker deployment is supported
    - flagged here explicitly rather than silently shipped as
    multi-worker-safe.
    """

    def __init__(self) -> None:
        self._by_device_code: dict[str, DeviceAuthRecord] = {}
        self._by_user_code: dict[str, str] = {}  # user_code -> device_code

    def _sweep_expired(self, now: datetime) -> None:
        expired = [dc for dc, rec in self._by_device_code.items() if rec.expires_at <= now]
        for device_code in expired:
            rec = self._by_device_code.pop(device_code)
            self._by_user_code.pop(rec.user_code, None)

    def start(
        self,
        *,
        now: datetime | None = None,
        ttl_seconds: int = DEFAULT_DEVICE_CODE_TTL_SECONDS,
    ) -> DeviceAuthRecord:
        now = now or datetime.now(timezone.utc)
        self._sweep_expired(now)
        device_code = secrets.token_urlsafe(32)
        user_code = _generate_user_code()
        record = DeviceAuthRecord(
            device_code=device_code,
            user_code=user_code,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._by_device_code[device_code] = record
        self._by_user_code[user_code] = device_code
        return record

    def is_pending(self, *, user_code: str, now: datetime | None = None) -> bool:
        """Read-only check: does `user_code` refer to a not-yet-approved,
        non-expired request? Used by the `approve` route to reject an
        unknown/expired/already-used code BEFORE minting any DB rows
        (`PersonalApiKey`/`cli_refresh_credentials`) - cheaper to check
        first than to mint-then-roll-back, and avoids ever creating a live
        credential nothing can retrieve."""
        now = now or datetime.now(timezone.utc)
        self._sweep_expired(now)
        device_code = self._by_user_code.get(user_code)
        if device_code is None:
            return False
        return not self._by_device_code[device_code].approved

    def approve(
        self,
        *,
        user_code: str,
        refresh_credential_plaintext: str,
        now: datetime | None = None,
    ) -> bool:
        """Mark the pending request approved and attach the one-time-reveal
        plaintext. Returns False (no-op) if the `user_code` is unknown,
        already approved, or expired - the caller (the approve route) turns
        that into a 404, never distinguishing which case (anti-enumeration,
        same posture as every other lookup-failure in this codebase)."""
        now = now or datetime.now(timezone.utc)
        self._sweep_expired(now)
        device_code = self._by_user_code.get(user_code)
        if device_code is None:
            return False
        record = self._by_device_code[device_code]
        if record.approved:
            return False
        record.approved = True
        record.refresh_credential_plaintext = refresh_credential_plaintext
        return True

    def poll(
        self, *, device_code: str, now: datetime | None = None
    ) -> tuple[DevicePollOutcome, str | None]:
        """Returns `(outcome, plaintext_or_None)`. `plaintext` is populated
        exactly once, on the single `poll()` call that first observes
        `approved` - the record is deleted immediately after delivering it,
        so a replayed poll (or a second device sharing a stolen
        `device_code`) gets `not_found`, never a second copy of the secret."""
        now = now or datetime.now(timezone.utc)
        self._sweep_expired(now)
        record = self._by_device_code.get(device_code)
        if record is None:
            return "not_found", None
        if not record.approved:
            return "pending", None
        plaintext = record.refresh_credential_plaintext
        self._by_device_code.pop(device_code, None)
        self._by_user_code.pop(record.user_code, None)
        return "approved", plaintext
