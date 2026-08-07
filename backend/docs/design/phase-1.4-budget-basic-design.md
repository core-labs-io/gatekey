---
title: Phase 1.4 — Budget (Basic) — Architecture Design
status: accepted
author: architect
last_updated: 2026-07-17
---

# Phase 1.4 — Budget (Basic) — Design

Scope: a minimal `User` cost-center entity, required `service_account_keys.user_id`
attribution, a static per-model USD pricing table, a pre-call hard-cutoff budget gate,
and post-call atomic usage charging across `/v1/chat/completions` (streaming and
non-streaming), `/v1/completions`, and `/v1/embeddings`. Builds directly on Phase 1.1
(admin auth, `constants.DEFAULT_ORG_ID` single-org precedent, `ProviderKey`'s
envelope-encryption/atomic-upsert patterns), Phase 1.2 (`api/v1/gateway/common.py`'s
`resolve_route -> ... -> fetch_credential` chain, `ServiceAccountContext`,
`service_account_keys`), and Phase 1.3 (`check_model_policy()`'s call-site pattern,
`ModelPolicyCache`'s "why not cached" contrast point).

Source of truth for scope/ACs/already-resolved ambiguities:
`docs/design/phase-1.4-budget-basic-product-spec.md` (product-owner spec, all of §1-§13).
This document does not re-litigate those decisions; it designs against them. Two items
the product spec flagged back (§12) are now finalized inputs to this design, not open
questions:

1. **`BudgetExhaustedError` status code: `402 Payment Required`** (not 403).
2. **Pricing table: static in-code module** (`providers/pricing.py`), not admin-editable
   this slice.

---

## 1. Data model & storage

### 1.1 New table `users`

```
users
  id                  uuid PRIMARY KEY
  org_id              uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  name                text NOT NULL
  budget_usd          numeric(20,10) NULL        -- NULL = unmetered
  current_spend_usd   numeric(20,10) NOT NULL DEFAULT 0
  created_at          timestamptz NOT NULL DEFAULT now()
  updated_at          timestamptz NOT NULL DEFAULT now()

  INDEX ix_users_org_id (org_id)
```

**ADR-1: monetary column precision is `NUMERIC(20, 10)`, not a smaller/currency-typical
scale.**
- Decision: both `budget_usd` and `current_spend_usd` use 10 digits after the decimal
  point (and 10 before, comfortably headroom for accumulated spend at any pilot scale).
- Rationale: the product spec's hold-the-line item is "no float, ever, anywhere on the
  charge path" (§2) — but a fixed-point *decimal* type still truncates at whatever scale
  the column declares, and per-token pricing rates are small enough that a naively
  "currency-typical" `NUMERIC(x, 4)` (4 decimal places, i.e. fractions of a cent) would
  silently round many individual charges to `$0.0000` before they ever get a chance to
  accumulate. Concrete example: `gpt-4o-mini`'s standard input rate is on the order of
  $0.15 per million tokens; a 10-token prompt costs `10 * 0.15 / 1_000_000 =
  $0.0000015` — already below a 4-decimal-place column's resolution. At `NUMERIC(20,10)`
  that same charge (`$0.0000015000`) is stored exactly, and thousands of such charges
  accumulate without systematic under-counting. This is the same "accurate cost/usage
  data" success criterion the product spec's float-ban is protecting (§2) — a scale
  that's too coarse is a second, quieter way to violate the same requirement, not
  protected by "use `Decimal` instead of `float`" alone.
- Alternative considered: `NUMERIC(12, 4)` (typical accounting-ledger shape). Rejected
  per the above — it would pass every unit test that charges a handful of large,
  round-number requests, then silently under-charge in production against the small,
  realistic per-request costs this system actually needs to track.
- Python-side: `Decimal` arithmetic (`services/budget.py`, below) is not rounded at any
  intermediate step — only the database column's declared scale ever truncates, and it's
  chosen wide enough that this is a non-issue in practice.

### 1.2 `service_account_keys` gains a required `user_id`

```
ALTER TABLE service_account_keys
  ADD COLUMN user_id uuid REFERENCES users(id) ON DELETE RESTRICT;  -- NOT NULL after backfill

INDEX ix_service_account_keys_user_id (user_id)
```

Migration sequence (new Alembic revision `0004`, `down_revision = "0003"`), following
`0001`'s idempotent-fixed-UUID-seed pattern exactly:

```python
"""create users table and attribute service_account_keys to a budget-owning user

Phase 1.4 (Budget - Basic). See gatekey.db.models.user.User for the ORM side
and docs/design/phase-1.4-budget-basic-design.md section 1 for the full
rationale (monetary column precision ADR-1, default-legacy-user backfill
ADR-7). This migration is the source of truth for actual DDL.

Backfill strategy (product spec section 1, "Migration of pre-existing
service-account keys"): every pre-1.4 service_account_keys row gets
attributed to one auto-created, unmetered (budget_usd = NULL) default user
per org, so existing pilot traffic keeps working with zero required admin
action. Uses a FIXED, well-known UUID for that default user
(00000000-0000-0000-0000-000000000002), mirroring 0001's own fixed
DEFAULT_ORG_ID seed convention - deterministic and idempotent
(ON CONFLICT (id) DO NOTHING), not gen_random_uuid()/uuid-ossp (no such
extension is otherwise required by this codebase).

Scoped to the single default org (00000000-0000-0000-0000-000000000001)
seeded by 0001 - Phase 1 has no multi-org signup flow yet (constants.
DEFAULT_ORG_ID).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_LEGACY_USER_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_LEGACY_USER_NAME = "Unassigned (pre-1.4 legacy keys)"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("budget_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column(
            "current_spend_usd", sa.Numeric(precision=20, scale=10),
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    # Idempotent seed of the single default/legacy user - see module
    # docstring. Safe to re-run.
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, org_id, name, budget_usd, current_spend_usd, created_at, updated_at)
            VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), :name, NULL, 0, now(), now())
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=DEFAULT_LEGACY_USER_ID, org_id=DEFAULT_ORG_ID, name=DEFAULT_LEGACY_USER_NAME)
    )

    op.add_column(
        "service_account_keys",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE service_account_keys SET user_id = CAST(:default_user_id AS uuid) "
            "WHERE user_id IS NULL"
        ).bindparams(default_user_id=DEFAULT_LEGACY_USER_ID)
    )
    op.alter_column("service_account_keys", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_service_account_keys_user_id", "service_account_keys", "users",
        ["user_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_service_account_keys_user_id", "service_account_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_service_account_keys_user_id", table_name="service_account_keys")
    op.drop_constraint("fk_service_account_keys_user_id", "service_account_keys", type_="foreignkey")
    op.drop_column("service_account_keys", "user_id")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
```

**ADR-7: the pre-1.4 default/legacy user uses a fixed, well-known UUID
(`...002`), not `gen_random_uuid()`.** Mirrors `0001_create_orgs_and_provider_keys.py`'s
own `DEFAULT_ORG_ID = "...001"` convention exactly — deterministic across every
environment this migration runs against, idempotent via `ON CONFLICT (id) DO NOTHING`,
and avoids introducing a new Postgres extension dependency (`pgcrypto`/`uuid-ossp`) that
nothing else in this codebase currently requires (every other table's `id` default is
supplied app-side via `default=uuid.uuid4` on the ORM column, not DB-side).

### 1.3 ORM models

`db/models/user.py` (new):

```python
class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_org_id", "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    org: Mapped["Org"] = relationship("Org", back_populates="users")
    service_account_keys: Mapped[list["ServiceAccountKey"]] = relationship(
        "ServiceAccountKey", back_populates="user"
    )
```

`db/models/service_account_key.py` gains:

```python
user_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
)
...
user: Mapped["User"] = relationship("User", back_populates="service_account_keys")
```

plus `Index("ix_service_account_keys_user_id", "user_id")` in `__table_args__`.

Register `User` in `db/models/__init__.py` (same convention as `ModelPolicy` in 1.3) so
`Base.metadata` is complete for Alembic autogenerate. Adding `Org.users` relationship
is optional/nice-to-have, same status as 1.3's `Org.model_policy` — not required for
correctness.

### 1.4 ADR-2: reconciling `DELETE /v1/admin/users/{id}` with `ON DELETE RESTRICT`

The product spec's own text is in slight tension with itself here: AC-1-8/1-9 describe
the block condition as "no **active (non-revoked)** service-account key references it,"
but the mechanism it names for enforcing that block is the FK's `ON DELETE RESTRICT` —
and a real `RESTRICT` FK blocks on *any* referencing row, active or revoked (revoking a
key sets `revoked_at`, it never deletes the row). There is no way to implement "revoked
keys don't block deletion" purely via this FK, because the referencing row still exists.

**Resolution (architect's call, flagged explicitly rather than silently picked):** a
`User` cannot be hard-deleted while **any** `ServiceAccountKey` row (active or revoked)
still references it. In practice this only differs from the AC's literal "active-only"
phrasing for the edge case of a user whose *only* keys have all been revoked — under
this resolution, that user is still blocked from deletion; under the AC's literal text,
it would not be.

- Why: implementing true "revoked keys don't block" would require *not* relying on the
  FK for enforcement (i.e., nullifying or cascading `user_id` on revoke, or soft-deleting
  the key row) — extra mutation on the revoke path for a case (freeing up a defunct
  user's id) with no expressed urgency in the product spec, and it would erase a
  revoked key's "which user did this used to belong to" trail, which is exactly the kind
  of thing a support/debugging investigation into a revoked credential wants intact.
- The primary, explicitly-tested scenario (AC-1-9: create user, create *active* key,
  attempt delete → 409) is identical under both interpretations. This resolution is
  strictly more conservative (blocks a strict superset of what the literal AC text
  blocks) and never silently deletes something the spec wanted kept.
- **Flagged for product-owner/QA confirmation**, not silently decided: if a test suite
  written directly against AC-1-8's literal text expects a user-with-only-revoked-keys
  to be deletable, that test will fail against this design and needs either the test
  adjusted or this ADR revisited (would require moving off pure `ON DELETE RESTRICT`).

Implementation: `services/users.delete_user()` does not need to catch `IntegrityError` on
literal `409`s from other causes — it can either pre-check via a `SELECT 1 FROM
service_account_keys WHERE user_id = :id LIMIT 1` (any row, not just active) and raise a
clean `UserInUseError` before attempting `DELETE`, or attempt the `DELETE` directly and
catch `IntegrityError` (mirroring the product spec's own suggested mechanism verbatim).
Either is correct under this ADR; the pre-check form gives a cleaner, more specific log
line and avoids a guaranteed-to-fail `DELETE` statement, so that's the recommended
concrete shape:

```python
async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool | None:
    """Hard-delete a User. Returns True (deleted), False (blocked - still
    referenced by >=1 service_account_keys row, active or revoked - see
    design doc ADR-2), or None (no such user)."""
    row = await get_user(session, user_id)
    if row is None:
        return None
    in_use = (
        await session.execute(
            select(ServiceAccountKey.id).where(ServiceAccountKey.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    if in_use is not None:
        return False
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()
    return True
```

---

## 2. Pricing table (`providers/pricing.py`)

Static, in-code, mirrors `providers/model_registry.py`'s "pure module, zero I/O,
hand-curated dict at import time" pattern exactly (product spec §3, orchestrator-
confirmed, not admin-editable this slice).

```python
@dataclass(frozen=True)
class PricingEntry:
    input_price_per_million_usd: Decimal
    output_price_per_million_usd: Decimal | None   # None only for EMBEDDINGS routes
    as_of: str      # ISO date the figure was sourced, e.g. "2026-07-17"
    source: str     # URL/citation - see section 5 below for the sourcing instruction


PRICING_TABLE: dict[str, PricingEntry] = {
    # one entry per MODEL_REGISTRY key - see section 5 for exact list/instructions
}


class PricingEntryMissingError(Exception):
    """Never caught-and-treated-as-$0 (AC-3-3/AC-5-4) - let it propagate to
    the app-wide unhandled-exception handler (errors.register_exception_handlers),
    which logs loudly and returns a generic 500."""


def get_pricing_entry(model: str) -> PricingEntry:
    """`model` MUST already be a literal MODEL_REGISTRY key that has passed
    resolve_route() in this same request - same discipline as
    check_model_policy()'s docstring. Raises PricingEntryMissingError if
    missing."""
    try:
        return PRICING_TABLE[model]
    except KeyError:
        raise PricingEntryMissingError(
            f"No pricing entry for model {model!r} - internal configuration "
            "error (every MODEL_REGISTRY key must have a matching "
            "PRICING_TABLE entry), never a valid $0 charge."
        ) from None
```

`PricingEntry` is a record (not a bare 2-`Decimal` tuple) per the product spec's own
forward-compat flag (§9): a future per-character/per-request rate shape (not needed by
any current pilot model — all are token-priced) should be addable as new optional
fields here, not a schema rewrite.

**Completeness invariant test** (`tests/unit/test_pricing.py`, AC-3-1/3-2/3-3):

```python
def test_pricing_table_covers_every_registry_model():
    assert PRICING_TABLE.keys() == MODEL_REGISTRY.keys()

def test_pricing_shape_matches_capability():
    for model, route in MODEL_REGISTRY.items():
        entry = PRICING_TABLE[model]
        assert isinstance(entry.input_price_per_million_usd, Decimal)
        if route.capability is ModelCapability.CHAT:
            assert isinstance(entry.output_price_per_million_usd, Decimal)
        else:  # EMBEDDINGS
            assert entry.output_price_per_million_usd is None
```

This must fail the build the moment a model is added to `MODEL_REGISTRY` without a
matching `PRICING_TABLE` entry — discoverable at test time, never only at request time.

---

## 3. Streaming usage capture — the hardest problem in this slice

### 3.1 What each provider's wire protocol actually offers

| Provider | Does usage arrive in-band on every streaming call, or only if requested? | Where |
|---|---|---|
| OpenAI | **Opt-in only.** Absent unless the request sets `stream_options: {"include_usage": true}`. When set, a terminal frame with `"choices": []` and populated `"usage"` is appended after the normal finish_reason chunk; all other chunks gain a `"usage": null` key. | One extra terminal SSE frame. |
| Anthropic | **Always present**, no request flag needed. `message_start`'s `message.usage.input_tokens` (prompt tokens) and `message_delta`'s `usage.output_tokens` (final, cumulative completion tokens, sent immediately before `message_stop`). | Two existing event types, no new frame from Anthropic itself. |
| Vertex AI (Gemini) | **Always present**, no request flag. `usageMetadata` (`promptTokenCount`/`candidatesTokenCount`/`totalTokenCount`) is attached to every `streamGenerateContent` chunk, cumulatively. | Present on (in practice, per Google's documented behavior) every chunk; least formally guaranteed of the three — flagged below as the one worth an empirical/recorded-fixture check before shipping. |

### 3.2 Decision: always request usage from upstream internally; only forward the
extra frame to the caller if the caller opted in

- **Upstream request side**: `providers/openai.py`'s `stream_chat_completion` **always**
  sets `body["stream_options"] = {"include_usage": True}` on the outbound request to
  OpenAI, unconditionally — independent of whatever the gateway caller sent. This is
  purely internal plumbing for Gatekey's own billing; Anthropic/Vertex need no
  equivalent flag (usage is already always in-band for them).
- **Gatekey's own wire contract to its callers**: `schemas/chat.py`'s
  `ChatCompletionRequest` gains a typed `stream_options: ChatCompletionStreamOptions |
  None = None` field (`{"include_usage": bool = False}`) — modeled explicitly rather
  than silently dropped by the existing `extra="ignore"` posture, because Gatekey's own
  forwarding policy needs to know what the *caller* asked for. `ChatCompletionChunk`
  gains `usage: ChatCompletionUsage | None = None`.
- **Why not just always forward the extra frame to every caller** (since OpenAI's own
  real API already includes it whenever Gatekey requests it upstream)? Because doing so
  unconditionally would be an observable, uninvited wire-format change for every existing
  Phase 1.2/1.3 streaming integration: today, a Gatekey chat stream never emits a frame
  with an empty `choices` array; a caller that never asked for `stream_options.
  include_usage` and is not defensively coded against `choices == []` (a real risk — the
  product's own non-negotiable is "OpenAI-compatible API surface maintained across
  phases... a design that breaks backward compatibility... needs explicit
  justification") should see *zero* behavior change from this phase landing. Gating the
  extra frame behind the caller's own `stream_options.include_usage` opt-in — exactly
  mirroring real OpenAI's own opt-in semantics — means an existing integration that never
  touches this new field sees byte-for-byte the same stream shape as before. This also
  means Gatekey now supports `stream_options.include_usage` uniformly for **all three**
  providers (a capability real Anthropic/Gemini don't offer at all), which is a
  compatibility *improvement*, not a compromise.
- **Precedent-consistent, not a new risk**: every relayed chunk (including ordinary
  content chunks) will now always carry a `"usage": null` key even when the caller never
  opted in — this *is* a small, additive wire-format change (new JSON key present, always
  null) for every existing streaming caller. This is judged acceptable because it
  exactly matches an existing, already-shipped convention in this exact codebase:
  `ChatCompletionChunk.finish_reason` has been present-and-usually-null on every chunk
  since Phase 1.2 (see `schemas/chat.py`) — this codebase's wire contract has never been
  "omit unset OpenAI-shaped fields," it has always been "every declared schema field
  serializes, defaulting to null." `usage` follows that same, already-established rule.
  Flagged explicitly here (not just in a code comment) because the instructions
  specifically call for surfacing this class of compatibility tradeoff, not just making
  the call silently.

### 3.3 Per-provider mechanics: uniform terminal "usage chunk" shape

All three providers' `stream_chat_completion` generators are extended to (at most once,
as the very last item) yield one additional `ChatCompletionChunk` with `choices=[]` and
`usage=<populated ChatCompletionUsage>` — the same shape OpenAI's real API already uses,
now reused uniformly for Anthropic's and Vertex's *synthetic* chunk streams too (both
already synthesize their entire chunk sequence; adding one more synthesized frame in the
same style is consistent with what they already do elsewhere in this codebase).

- **`providers/openai.py`**: no translation-loop code change — this is pure passthrough
  already; the schema addition alone means OpenAI's real usage frame now parses
  correctly via the existing `ChatCompletionChunk.model_validate(json.loads(payload))`
  call. Only change: unconditionally add `stream_options` to the outbound body (§3.2).
- **`providers/anthropic.py`**: capture `input_tokens` from `message_start`'s
  `data["message"]["usage"]["input_tokens"]`. On `message_delta`, after yielding the
  existing finish_reason chunk (unchanged), if both `input_tokens` and
  `data["usage"]["output_tokens"]` are available, yield one more chunk: `choices=[]`,
  `usage=ChatCompletionUsage(prompt_tokens=input_tokens, completion_tokens=output_tokens,
  total_tokens=input_tokens + output_tokens)`.
- **`providers/vertex_ai.py`**: track `last_usage: ChatCompletionUsage | None` across the
  loop, updated from `usageMetadata` on every candidate frame that has one (same field
  extraction `_translate_chat_response` already does for the non-streaming path). After
  the upstream SSE loop ends (Gemini has no explicit "stream done" event — the HTTP
  stream simply closes), if `last_usage is not None`, yield the terminal `choices=[]`
  usage chunk.

### 3.4 Route-handler responsibility (`api/v1/gateway/chat.py`)

`_sse_event_stream` is the **one place** that (a) always captures the usage chunk's
`usage` payload for billing regardless of what the caller asked for, and (b) decides
whether to actually relay that specific frame to the client, based on the client's own
`stream_options.include_usage`. Provider-translation modules stay provider-shape-focused;
"what does the caller actually see" policy lives in exactly one place, matching this
codebase's existing separation of concerns (same principle as `check_model_policy()`
being the one place that knows about model policy).

```python
def _is_usage_chunk(chunk: ChatCompletionChunk) -> bool:
    """The one terminal, empty-`choices` frame each provider's translation
    layer (or real upstream OpenAI response) emits when usage reporting is
    available - see providers/*.py `stream_chat_completion` and design doc
    section 3. Never true for an ordinary content/role/finish_reason chunk
    (those always have exactly one entry in `choices`)."""
    return not chunk.choices and chunk.usage is not None


async def _sse_event_stream(
    *,
    request: Request,
    first_item: Any,
    remaining: AsyncIterator[ChatCompletionChunk],
    timer: LatencyTimer,
    request_id: str,
    provider: str,
    model: str,
    idempotency_key: str | None,
    session: AsyncSession,        # NEW
    user_id: uuid.UUID,           # NEW
    client_wants_usage: bool,     # NEW - body.stream_options.include_usage
) -> AsyncIterator[bytes]:
    disconnected = False
    result_status = "ok"
    captured_usage: ChatCompletionUsage | None = None

    def _handle(chunk: ChatCompletionChunk) -> bytes | None:
        nonlocal captured_usage
        if _is_usage_chunk(chunk):
            captured_usage = chunk.usage
            return _sse_frame(chunk) if client_wants_usage else None
        return _sse_frame(chunk)

    try:
        if first_item is not _STREAM_EMPTY:
            frame = _handle(first_item)
            if frame is not None:
                yield frame
        async for chunk in remaining:
            if await request.is_disconnected():
                disconnected = True
                await remaining.aclose()
                break
            frame = _handle(chunk)
            if frame is not None:
                yield frame
    except ProviderUnsupportedRequestError:
        result_status = "unsupported_request"
        logger.warning("gateway_stream_unsupported_request", extra={"request_id": request_id})
    except ProviderCallError as exc:
        result_status = "provider_error"
        logger.warning(
            "gateway_stream_provider_error",
            extra={"request_id": request_id, "upstream_status_code": exc.status_code},
        )
    finally:
        if disconnected:
            result_status = "client_disconnected"
        else:
            if result_status == "ok":
                # Phase 1.4 (US-5/6/7): charge only on a clean,
                # non-disconnected, non-errored completion - AC-6-1's "exactly
                # one call site", AC-6-2/6-3's "never charge a failed or
                # aborted request".
                if captured_usage is not None:
                    try:
                        await record_usage_charge(
                            session,
                            user_id=user_id,
                            model=model,
                            prompt_tokens=captured_usage.prompt_tokens,
                            completion_tokens=captured_usage.completion_tokens,
                        )
                    except Exception:
                        # Bytes are already on the wire; the HTTP status can
                        # no longer change (headers already flushed) even
                        # for a PricingEntryMissingError, which in the
                        # non-streaming path becomes a real 500 - see design
                        # doc section 3.5. Logged loudly, not retried
                        # (product spec section 6's accepted best-effort gap).
                        result_status = "charge_failed"
                        logger.error(
                            "gateway_stream_charge_failed",
                            exc_info=True,
                            extra={"request_id": request_id},
                        )
                else:
                    # Explicit, logged gap (design doc section 3.5) - never
                    # a silent $0 charge.
                    result_status = "usage_unavailable"
                    logger.warning(
                        "gateway_stream_usage_unavailable",
                        extra={"request_id": request_id, "provider": provider, "model": model},
                    )
            yield b"data: [DONE]\n\n"
        timer.mark("flush_complete")
        log_gateway_request(
            request_id=request_id, endpoint=_ENDPOINT, provider=provider, model=model,
            stream=True, status=result_status, timer=timer, idempotency_key=idempotency_key,
        )
```

`create_chat_completion`'s streaming branch computes `client_wants_usage =
body.stream_options is not None and body.stream_options.include_usage` and threads
`session=session, user_id=ctx.user_id, client_wants_usage=client_wants_usage` into the
call above.

### 3.5 Explicit gaps — documented, not silently charged as `$0`

- **Genuine provider-side usage-unavailable** (stream completes cleanly but
  `captured_usage` stays `None`): logged as `gateway_stream_usage_unavailable`, request
  is **not** charged (same fail-toward-not-charging principle the product spec already
  applies to aborted streams, §6). Expected likelihood per provider: OpenAI/Anthropic —
  near-zero (both have a documented, contractual usage-reporting guarantee once the
  gateway's request shape is honored); Vertex AI — flagged as the one worth an
  empirical/recorded-fixture integration test before shipping, since Google's public
  docs describe `usageMetadata`'s presence on every chunk less formally than
  OpenAI's/Anthropic's own streaming-usage contracts.
- **A streaming `PricingEntryMissingError`** (a model resolvable and routable but missing
  from `PRICING_TABLE` — should be unreachable if `tests/unit/test_pricing.py` passes,
  but the runtime guard exists regardless per AC-5-4) degrades to the same logged,
  not-charged, best-effort gap as any other post-response charge failure in the
  streaming case — **not** a 500, because response headers (status 200) are already
  flushed by the time this is discovered. This is a real asymmetry with the
  non-streaming path (§4 below), where the same error *does* propagate to a genuine
  500 before any bytes are sent — flagged explicitly as an unavoidable property of
  streaming responses in general (same class of limitation as "charge-write failure
  after bytes are already on the wire," product spec §6), not something this design can
  paper over.

---

## 4. `api/v1/gateway/common.py` additions

Module docstring gains one more step in the documented chain (mirroring how 1.3 added
`check_model_policy`):

```
resolve_route -> check_model_policy -> [capability/provider check] ->
check_budget_available -> fetch_credential -> [provider call] -> record_usage_charge
```

```python
async def check_budget_available(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Enforce the per-user hard spend cutoff (Phase 1.4, US-4).

    Raises `errors.BudgetExhaustedError` (402) if the user's `budget_usd` is
    not NULL and `current_spend_usd >= budget_usd` - "exhausted" means fully
    used (`>=`, not `>`). This can only ever check whether the user is
    *already* over budget from previous requests, never whether *this*
    request will push them over (a specific request's cost is unknowable
    before the provider responds, so no pre-call estimate is used or
    permitted) - see design doc `phase-1.4-budget-basic-design.md` section 3
    of the product spec for the accepted "N completes, N+1 is blocked"
    semantics (AC-4-2).

    Unlike `check_model_policy()`, this is deliberately NOT zero-I/O:
    `current_spend_usd`/`budget_usd` are per-user mutable state that changes
    on every charged request, so (unlike the org-wide policy snapshot) this
    is not a candidate for `ModelPolicyCache`'s in-process-cache pattern -
    it reads through to the database on every call. It is still cheaper than
    `fetch_credential()` (a single indexed point lookup vs. decrypt), so the
    existing ordering still saves work on the reject path.

    Call this only *after* `resolve_route()`, `check_model_policy()`, and
    the endpoint's own capability/provider check have already succeeded,
    and *before* `fetch_credential()` (AC-4-6) - see module docstring for
    the full ordering.
    """
    state = await get_budget_state(session, user_id)
    if state is None:
        # Should be unreachable: user_id is FK-enforced off the
        # authenticated ServiceAccountKey row, and a user referenced by any
        # service-account key (active or revoked) can never be deleted
        # (ON DELETE RESTRICT - design doc section 1.4/ADR-2).
        raise AssertionError(
            f"authenticated caller's user_id {user_id} does not reference an existing user"
        )
    if is_budget_exhausted(state):
        raise BudgetExhaustedError(
            name=state.name, budget_usd=state.budget_usd, current_spend_usd=state.current_spend_usd
        )


async def record_usage_charge(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    model: str,
    prompt_tokens: int,
    completion_tokens: int | None,
) -> Decimal:
    """Charge `user_id` for actual provider-reported usage on `model`
    (Phase 1.4, US-5/US-6/US-7).

    Thin wrapper around `services.budget.record_usage_charge` - see that
    function's docstring for the atomic-write/idempotency contract this
    relies on (AC-6-1, AC-7-1). `completion_tokens=None` selects the
    embeddings cost formula (no output-token term, AC-5-2); an int
    (including `0`) selects the chat/completions formula (AC-5-1).

    Call this ONLY after a provider response with confirmed, complete usage
    has been received - see each gateway route handler's call site
    (`chat.py`, `completions.py`, `embeddings.py`) for exactly where.
    `model` MUST be the exact same string already passed to
    `resolve_route()`/`check_model_policy()` in this same request - same
    "same variable, never re-derived" discipline documented on
    `check_model_policy()`.

    Raises `providers.pricing.PricingEntryMissingError` if `model` has no
    pricing entry - callers must let this propagate uncaught in the
    non-streaming path (it becomes a logged 500 via the app-wide unhandled-
    exception handler); never catch this and charge $0 (AC-5-4). See design
    doc section 3.5 for why the streaming path cannot offer the same
    500-on-pricing-gap guarantee.
    """
    return await budget_service.record_usage_charge(
        session,
        user_id=user_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
```

`api/deps.py`: `ServiceAccountContext` gains `user_id: uuid.UUID`; `require_service_account`
populates it from `row.user_id` (the already-fetched `ServiceAccountKey` row — one more
column read off a row already in hand, no new query):

```python
@dataclass(frozen=True)
class ServiceAccountContext:
    org_id: uuid.UUID
    service_account_id: uuid.UUID
    user_id: uuid.UUID   # NEW - Phase 1.4
    name: str
```

---

## 5. `services/budget.py` (new)

```python
@dataclass(frozen=True)
class UserBudgetState:
    id: uuid.UUID
    name: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal


async def get_budget_state(session: AsyncSession, user_id: uuid.UUID) -> UserBudgetState | None:
    """Single indexed-PK SELECT of one user's current budget/spend state -
    the gateway hot-path cost this feature adds (see section 8, NFR
    accounting)."""
    stmt = select(User.id, User.name, User.budget_usd, User.current_spend_usd).where(User.id == user_id)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return UserBudgetState(id=row.id, name=row.name, budget_usd=row.budget_usd, current_spend_usd=row.current_spend_usd)


def is_budget_exhausted(state: UserBudgetState) -> bool:
    """AC-4-1/4-3/4-5: NULL budget is never exhausted; exhausted means
    current_spend_usd >= budget_usd."""
    return state.budget_usd is not None and state.current_spend_usd >= state.budget_usd


def compute_cost(model: str, *, prompt_tokens: int, completion_tokens: int | None) -> Decimal:
    """AC-5-1/5-2. Raises providers.pricing.PricingEntryMissingError for an
    unpriced model - never $0 (AC-5-4/3-3)."""
    entry = get_pricing_entry(model)
    cost = (entry.input_price_per_million_usd * prompt_tokens) / Decimal(1_000_000)
    if completion_tokens is not None:
        assert entry.output_price_per_million_usd is not None, (
            f"model {model!r} has completion_tokens but no output price - "
            "PRICING_TABLE completeness invariant violated (should be "
            "unreachable if tests/unit/test_pricing.py passes)."
        )
        cost += (entry.output_price_per_million_usd * completion_tokens) / Decimal(1_000_000)
    return cost


async def record_usage_charge(
    session: AsyncSession, *, user_id: uuid.UUID, model: str,
    prompt_tokens: int, completion_tokens: int | None,
) -> Decimal:
    """The write is a single `UPDATE users SET current_spend_usd =
    current_spend_usd + :cost WHERE id = :user_id RETURNING
    current_spend_usd` statement (AC-7-1) - never a read-modify-write in
    application code, mirroring services.provider_keys.add_or_replace_key /
    services.model_policy.set_policy's atomic-upsert pattern. This is what
    makes AC-7-2 (N concurrent charges never lose an update) hold - see
    section 9's concurrency-semantics note for exactly what is and is not
    guaranteed under a race with check_budget_available().
    """
    cost = compute_cost(model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(current_spend_usd=User.current_spend_usd + cost)
        .returning(User.current_spend_usd)
    )
    result = await session.execute(stmt)
    new_total = result.scalar_one_or_none()
    await session.commit()
    if new_total is None:
        # Should be unreachable - see check_budget_available()'s identical note.
        logger.error("record_usage_charge_missing_user", extra={"user_id": str(user_id), "model": model})
    return cost
```

---

## 6. Route-handler wiring (non-streaming paths)

`chat.py` (non-streaming branch) and `completions.py`: insert `check_budget_available`
right before `fetch_credential`, and `record_usage_charge` right after the provider
response is parsed (`timer.mark("provider_response_received")`):

```python
route = resolve_route(body.model)
check_model_policy(body.model, cache)
if route.capability != ModelCapability.CHAT:      # or the endpoint's equivalent check
    raise HttpUnsupportedRequestError(...)
await check_budget_available(session, ctx.user_id)          # NEW
credential = await fetch_credential(session, route.provider, key_provider=key_provider)
...
response = await _create_non_streaming(...)  # or openai_provider.create_completion(...)
timer.mark("provider_response_received")
await record_usage_charge(                                   # NEW
    session,
    user_id=ctx.user_id,
    model=body.model,
    prompt_tokens=response.usage.prompt_tokens,
    completion_tokens=response.usage.completion_tokens,
)
timer.mark("flush_complete")
log_gateway_request(..., status="ok", ...)
return response
```

`embeddings.py`: identical placement; the charge call passes `completion_tokens=None`
always (`EmbeddingsUsage` has no `completion_tokens` field):

```python
await check_budget_available(session, ctx.user_id)           # NEW, before fetch_credential
credential = await fetch_credential(...)
...
response = await openai_provider.create_embeddings(...)  # or vertex_provider...
timer.mark("provider_response_received")
await record_usage_charge(                                    # NEW
    session, user_id=ctx.user_id, model=body.model,
    prompt_tokens=response.usage.prompt_tokens, completion_tokens=None,
)
```

Every insertion is exactly two new lines per route (matching 1.3's own "one shared
helper, one call site per route" precedent) — no branching logic duplicated per route.
`ctx: ServiceAccountContext = Depends(require_service_account)` is already a parameter
on all three handlers; only `ctx.user_id` is new.

If a provider call fails (`ProviderCallError`/`ProviderUnsupportedRequestError`), the
existing `except` blocks raise before `record_usage_charge` is ever reached — AC-6-2 is
satisfied by construction, not by an added guard.

---

## 7. Admin API — `POST/GET/PATCH/DELETE /v1/admin/users`

Follows `api/v1/admin/service_accounts.py`'s exact pattern: `require_admin` router-level
dependency, no `org_id` param, `GatekeyError`-based errors, service-layer logic kept out
of the route module.

### 7.1 Schemas — `schemas/user.py`

```python
_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")   # AC-1-7: current_spend_usd is 422, not silently dropped

    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    budget_usd: Decimal | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank.")
        return value

    @field_validator("budget_usd")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("budget_usd must be non-negative.")
        return value


class UserUpdateRequest(BaseModel):
    """PATCH body. See ADR-4 (section 7.2) for how `budget_usd: null`
    (explicit clear -> unmetered) is distinguished from an omitted
    `budget_usd` key (leave unchanged) - AC-1-5/AC-1-6."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    budget_usd: Decimal | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank.")
        return value

    @field_validator("budget_usd")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("budget_usd must be non-negative.")
        return value


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal
    created_at: datetime
    updated_at: datetime
```

**ADR-4: PATCH's "omitted vs. explicit null" distinction uses `model_fields_set`, not a
custom sentinel.** `UserUpdateRequest.model_dump(exclude_unset=True)` in the route
handler yields a dict containing only the keys the caller actually supplied in the
request body — `{"budget_usd": null}` in the payload produces `{"budget_usd": None}` in
the dump (key present, value `None`); omitting the field entirely produces a dump
without the key at all. Passing that dict straight into a dynamic
`update(User).values(**updates)` therefore does exactly the right thing for free: a
present-but-`None` `budget_usd` clears the column to `NULL`, an absent key leaves the
column untouched. This is the standard, correct FastAPI/Pydantic pattern for this exact
problem — called out explicitly because a naive implementation (e.g. `if payload.
budget_usd is not None: ...`) silently conflates "omitted" and "explicit null," which
would directly violate AC-1-6.

### 7.2 Service — `services/users.py`

```python
class UserNotFoundError(Exception): ...


async def create_user(session: AsyncSession, *, name: str, budget_usd: Decimal | None) -> User:
    row = User(org_id=DEFAULT_ORG_ID, name=name, budget_usd=budget_usd)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_users(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.org_id == DEFAULT_ORG_ID).order_by(User.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = select(User).where(User.org_id == DEFAULT_ORG_ID, User.id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_user(session: AsyncSession, user_id: uuid.UUID, updates: dict[str, Any]) -> User | None:
    """`updates` is `UserUpdateRequest.model_dump(exclude_unset=True)` - see
    ADR-4. Empty `updates` (PATCH {}) is a legal no-op that still returns
    the current row."""
    row = await get_user(session, user_id)
    if row is None:
        return None
    for field, value in updates.items():
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool | None:
    # See design doc section 1.4 / ADR-2 for the full docstring + rationale.
    ...
```

### 7.3 Routes — `api/v1/admin/users.py`

```python
router = APIRouter(prefix="/v1/admin/users", tags=["admin", "users"], dependencies=[Depends(require_admin)])

@router.post("", response_model=UserResponse, status_code=201)
async def create_user_endpoint(payload: UserCreateRequest, session: AsyncSession = Depends(get_db_session)) -> UserResponse:
    row = await create_user(session, name=payload.name, budget_usd=payload.budget_usd)
    return UserResponse.model_validate(row)

@router.get("", response_model=list[UserResponse])
async def list_users_endpoint(session: AsyncSession = Depends(get_db_session)) -> list[UserResponse]:
    return [UserResponse.model_validate(r) for r in await list_users(session)]

@router.get("/{user_id}", response_model=UserResponse)
async def get_user_endpoint(user_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)) -> UserResponse:
    row = await get_user(session, user_id)
    if row is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    return UserResponse.model_validate(row)

@router.patch("/{user_id}", response_model=UserResponse)
async def update_user_endpoint(
    user_id: uuid.UUID, payload: UserUpdateRequest, session: AsyncSession = Depends(get_db_session)
) -> UserResponse:
    row = await update_user(session, user_id, payload.model_dump(exclude_unset=True))
    if row is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    return UserResponse.model_validate(row)

@router.delete("/{user_id}", status_code=204)
async def delete_user_endpoint(user_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)) -> Response:
    result = await delete_user(session, user_id)
    if result is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    if result is False:
        raise GatekeyError(
            f"User '{user_id}' is still referenced by one or more service-account keys "
            "and cannot be deleted.",
            code="user_in_use",
            status_code=status.HTTP_409_CONFLICT,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

Register `admin_users_router` in `main.py` alongside the other three admin routers.

---

## 8. `service_account_keys` admin API — `user_id` now required

### 8.1 `schemas/service_account_key.py` changes

```python
class ServiceAccountKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    user_id: UUID                              # NEW, required (AC-2-1)

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str: ...  # unchanged


class ServiceAccountKeyCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    user_id: UUID                              # NEW (AC-2-3)
    key_prefix: str
    secret: str
    created_at: datetime


class ServiceAccountKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    user_id: UUID                              # NEW (AC-2-3)
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None
    active: bool = True
    ...  # _compute_active unchanged
```

### 8.2 `services/service_accounts.py` changes

```python
class UserNotFoundError(Exception): ...   # or reuse services.users.UserNotFoundError

async def create_service_account(
    session: AsyncSession, name: str, user_id: uuid.UUID
) -> tuple[ServiceAccountKey, str]:
    """AC-2-2: no row is written if user_id doesn't reference an existing
    user - pre-checked via a SELECT (mirrors the product spec's own
    "no row written" requirement), not left to a bare FK-violation
    IntegrityError, so the caller (admin router) can map it to a clean 404
    with the exact recommended message shape."""
    existing_user = await get_user(session, user_id)   # services.users.get_user
    if existing_user is None:
        raise UserNotFoundError(f"No user found with id '{user_id}'.")

    secret = SECRET_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    token_part = secret[len(SECRET_PREFIX):]
    key_prefix = token_part[:KEY_PREFIX_LENGTH]

    row = ServiceAccountKey(
        org_id=DEFAULT_ORG_ID, name=name, user_id=user_id,
        key_prefix=key_prefix, secret_hash=hash_secret(secret),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, secret
```

### 8.3 `api/v1/admin/service_accounts.py` changes

```python
@router.post("", response_model=ServiceAccountKeyCreateResponse, status_code=201)
async def create_service_account_key(
    payload: ServiceAccountKeyCreateRequest, session: AsyncSession = Depends(get_db_session)
) -> ServiceAccountKeyCreateResponse:
    try:
        row, secret = await create_service_account(session, payload.name, payload.user_id)
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from None
    return ServiceAccountKeyCreateResponse(
        id=row.id, name=row.name, user_id=row.user_id, key_prefix=row.key_prefix,
        secret=secret, created_at=row.created_at,
    )
```

`list_service_account_keys`/`get_service_account_key` need no code change —
`ServiceAccountKeyResponse.model_validate(row)` picks up `user_id` automatically once
it's a declared response field (`from_attributes=True`).

---

## 9. `errors.py` addition

```python
class BudgetExhaustedError(GatekeyError):
    """The user has exhausted its per-user spend budget (Phase 1.4, US-4/US-8).

    Raised by api.v1.gateway.common.check_budget_available(). 402 Payment
    Required, not 403 - orchestrator-confirmed (product spec section 12):
    budget exhaustion is a quota/billing state, not an authorization
    decision, unlike ModelDeniedError's 403. `message` includes the user's
    name, budget, and current spend (AC-8-2) - same "caller/state input, not
    secret material" justification ModelDeniedError already uses for the
    model name.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "budget_exhausted"

    def __init__(self, *, name: str, budget_usd: Decimal, current_spend_usd: Decimal) -> None:
        super().__init__(
            f"User '{name}' has exhausted its budget of ${budget_usd:,.2f} USD "
            f"(current spend: ${current_spend_usd:,.2f} USD). "
            "Contact your administrator to increase the budget."
        )
```

Envelope shape is the identical generic `{"error": {"code", "message"}}` (AC-8-3) — no
bespoke OpenAI-shaped error body, same already-made decision `UnsupportedRequestError`
documents.

`user_in_use` (409, admin delete conflict) and the service-account-creation-user-
not-found 404 both reuse existing generic error types (`GatekeyError(..., code=...,
status_code=...)` inline and `NotFoundError`, respectively) rather than new dedicated
subclasses — matches `api/v1/admin/model_policy.py`'s existing precedent for
`unknown_model_in_policy` (a one-off `GatekeyError(...)` construction at the route,
not a new class).

---

## 10. Concurrency semantics — precise interpretation (AC-7-3)

AC-7-3's test description ("2 requests race" at a boundary where the user has "room for
exactly 1 more request") needs its exact expected assertion spelled out precisely,
because the literal design here (pre-call gate read, then a fully independent post-call
atomic charge — no lock spanning the two) does **not** guarantee "at most one of any
number of concurrently-racing requests is ever charged past the boundary." It guarantees
something narrower and explicitly weaker, which the product spec's own §5/§7 language
("some single-request overshoot... is accepted, unbounded overshoot from a race is
not") already anticipates but is worth stating with full precision:

- **What is guaranteed**: (a) the atomic `UPDATE ... RETURNING` charge write never loses
  an update under any level of concurrency (AC-7-1/7-2, unconditionally); (b) any request
  that starts its `check_budget_available()` read *after* a prior charge has already
  committed correctly sees the updated total and is blocked if it's now exhausted; (c)
  the system self-corrects immediately — there is no cache to desync, `check_budget_
  available()` always reads through to the DB, so staleness cannot compound or persist.
- **What is *not* guaranteed, and is accepted**: if *N* requests for the same user all
  call `check_budget_available()` concurrently, before any of their charges land, and
  the user had room for exactly 1 more request, **all N** may pass the gate and **all
  N** will be charged — the overshoot is bounded by the sum of those N genuinely
  concurrent in-flight requests' costs, not literally capped at "one request's worth."
  This is a direct, unavoidable consequence of the product spec's own already-accepted
  design tension (§5: cost is unknowable pre-call, so the gate can only ever ask "was
  the user already over budget," never "will this specific in-flight cohort push them
  over") — closing this fully would require a fundamentally different mechanism
  (pessimistic reservation/hold-then-settle, or a per-user serializing lock around the
  entire check+call+charge sequence), which is out of proportion for "Budget (Basic)"
  and not what's built here.
- **Flagged for QA/product-owner confirmation**: if AC-7-3's actual test asserts the
  stricter "literally at most 1 of N concurrent racers may complete past the boundary"
  property (rather than "N concurrent racers may all complete, bounded by N — not
  unbounded"), that assertion will fail against this design and requires either the test
  scoped down to match "bounded-by-concurrency-degree, not unbounded" (recommended — it's
  what the spec's own accepted-tradeoff language already implies), or a follow-up design
  change (reservation/lock) that is a materially bigger scope than this slice.

---

## 11. Non-functional requirements — explicit accounting

- **p99 gateway overhead < 150ms (Phase 1 NFR)**: this slice adds exactly two new DB
  round trips to the hot path — `check_budget_available()`'s single indexed point
  `SELECT` on `users.id` (PK), and `record_usage_charge()`'s single indexed point
  `UPDATE ... RETURNING` on the same PK. Both are simple, single-row, indexed
  operations against a local/co-located Postgres — should comfortably fit the existing
  latency budget, but per the product spec's own flag (§10), this must be verified
  under the same load test used for 1.1-1.3, not assumed. Unlike `ModelPolicyCache`
  (Phase 1.3), there is no in-process cache to avoid this cost — see `check_budget_
  available()`'s docstring for why (per-request-mutable state cannot be safely cached
  the way a config toggle can).
- **"Must not lose or double-charge requests on provider timeout/retry" (Phase 1 NFR)**:
  directly covered by AC-6/AC-7's design (§4-6, §10 above); the client-retry-dedup
  boundary is explicitly out of scope, matching 1.2's own already-made `Idempotency-Key`
  precedent (product spec §6).
- **"Atomic spend-check-and-deduct, not eventual consistency" (this is at least as strong
  a bar as Phase 2's own §2.2 NFR, per the product spec §11)**: satisfied by
  `record_usage_charge()`'s single-statement `UPDATE ... RETURNING` (never a
  read-modify-write) — see §10 above for the precise, narrower guarantee this actually
  provides under a true concurrent race at the exact boundary, which is a different
  (weaker, spec-accepted) property than "the whole check+charge sequence is one
  atomic transaction."
- **"Under 60 minutes to first request"**: preserved — a fresh single-user/single-key
  setup requires exactly one extra API call (`POST /v1/admin/users` with no `budget_usd`,
  i.e. the default/omitted value) before the existing `POST /v1/admin/service-accounts`
  call, which now requires that user's id. No budget decision is forced on a first-run
  operator.
- **Success criterion "see accurate cost/usage data"**: this slice is the *prerequisite*
  (accurate `current_spend_usd`), not the full delivery (a usage *view* is 1.5) — see
  ADR-1's monetary-precision reasoning for why "accurate" specifically required
  `NUMERIC(20,10)`, not merely "uses `Decimal`."

---

## 12. Pricing figures — sourcing instruction for backend-developer

**The architect has not invented any pricing figures below — none exist in this
repository yet.** This is a required, separately-tracked implementation task, not
optional cleanup. `providers/pricing.py` ships as a schema + empty/placeholder dict
until this is done; `tests/unit/test_pricing.py`'s completeness assertion (§2) will fail
until every entry below is filled in with real figures, and that failing test is the
correct, intended gate — do not relax it to unblock other work.

**Exact `MODEL_REGISTRY` keys needing a `PRICING_TABLE` entry** (from
`providers/model_registry.py`, current pilot list):

| Model id | Capability | Provider |
|---|---|---|
| `gpt-4o` | CHAT | openai |
| `gpt-4o-mini` | CHAT | openai |
| `text-embedding-3-small` | EMBEDDINGS | openai |
| `text-embedding-3-large` | EMBEDDINGS | openai |
| `claude-sonnet-5` | CHAT | anthropic |
| `claude-haiku-4-5-20251001` | CHAT | anthropic |
| `claude-opus-5` | CHAT | anthropic |
| `gemini-2.5-pro` | CHAT | vertex_ai |
| `gemini-2.5-flash` | CHAT | vertex_ai |
| `gemini-embedding-001` | EMBEDDINGS | vertex_ai |

**Instructions:**

1. For each row, look up the **current, standard (non-cached, non-batch)** published
   per-million-token input/output USD rate directly from the provider's own official
   pricing page (`openai.com/api/pricing`, `anthropic.com/pricing`, Google Cloud's
   Vertex AI generative AI pricing page) — not a third-party aggregator, not memory,
   not this document.
2. Record the exact date you looked it up in `as_of` (ISO format) and the URL in
   `source`, per entry — this is the only paper trail for "was this ever right, and
   when," since this table has no admin-editable correction path this slice.
3. Use the **standard tier rate**. Do not attempt to model prompt-caching discounts,
   batch-API discounts, or (for Gemini 1.5 Pro specifically, which has historically had
   a higher rate above a long-context threshold — verify against the current pricing
   page whether this still applies) long-context-tier surcharges — none of that is in
   scope this slice (product spec §9's "single-currency, token-based rate" framing).
   If a model does have a documented tiered/context-dependent rate, use the base/
   standard tier and add a one-line note in a comment next to that entry flagging the
   simplification, so it isn't silently wrong for large-context requests.
4. Every entry must be a `Decimal` constructed from a **string literal**
   (`Decimal("2.50")`, never `Decimal(2.50)`) — constructing a `Decimal` from a `float`
   literal reintroduces exactly the float-precision risk the product spec's hold-the-line
   item (§2) is trying to eliminate, even though the *type* is `Decimal`.
5. Add a unit test asserting round-trip precision through `UserResponse`/
   `ServiceAccountKeyCreateResponse` JSON serialization for at least one non-trivial
   `Decimal` value (e.g. `budget_usd = Decimal("12.3456789012")`) — Pydantic v2's
   default JSON serialization of `Decimal` has not been exercised anywhere else in this
   codebase yet; confirm it round-trips exactly before relying on it for money.

---

## 13. Forward-looking rework flags

- **Phase 2 §2.1/§2.2 (Org→Team→User, team/org budget layers)**: `User` has no auth/
  role/team fields to migrate away from (product spec §11 already confirms this). The
  atomic single-statement charge pattern already meets Phase 2's own concurrency NFR, so
  team/org-level checks can layer on top without re-deriving that guarantee. One thing
  Phase 2 *will* need to revisit: `services/budget.py`'s functions are single-user-row
  shaped (`get_budget_state`/`record_usage_charge` take one `user_id`); a team/org
  ceiling check will need either a second, analogous set of functions against a new
  `teams`/`org_budgets` table, or a generalization of `check_budget_available`/
  `record_usage_charge` to check/charge multiple levels in one pass — not decided here,
  flagged for that phase's architect.
- **Phase 2 §2.2 (rollover/reset, budget alerts)**: no period concept exists here to
  conflict with a future one; `current_spend_usd` is a pure monotonic accumulator with
  no reset primitive (product spec §9, deliberate).
- **Phase 1.5 (persisted usage log)**: several gaps this design accepts (§3.5's
  streaming-usage-unavailable case, §6's post-response charge-write failures, the
  client-retry-dedup boundary) become *reconcilable* once a persisted per-request usage
  row exists — this slice's `record_usage_charge()` return value (the computed cost) is
  already structured-logging-ready for whenever 1.5 wants to start writing it somewhere
  durable, per that phase's own stated anticipation.
- **Pricing table admin-editability** (product spec §3/§9, orchestrator-confirmed static
  this slice): if a later phase decides to make this admin-editable, `PricingEntry`'s
  record shape (not a bare tuple) and `get_pricing_entry()`'s single-lookup-point
  discipline (mirroring `model_registry.resolve_model()`) are both already structured to
  make that swap (in-code dict -> DB table) a contained change, not a redesign.

---

## 14. Task breakdown

Legend: [P] = can run in parallel with sibling [P] tasks; [D: X] = hard dependency on
task X.

### database-admin

- **DB-1**: Write and apply Alembic migration `0004_create_users_and_attribute_service_account_keys.py`
  per §1.2 (table + backfill + FK). [P] (no dependency on backend code).
- **DB-2**: Add `User` ORM model (`db/models/user.py`) per §1.3; update
  `db/models/service_account_key.py` to add `user_id`/relationship; register `User` in
  `db/models/__init__.py`. [D: DB-1] (needs the migration's exact column/FK names).

### backend-developer

- **BD-1**: `errors.py` — add `BudgetExhaustedError` (§9). [P].
- **BD-2**: `providers/pricing.py` — `PricingEntry`/`PRICING_TABLE` (empty/placeholder
  shape)/`PricingEntryMissingError`/`get_pricing_entry()` (§2). [P].
- **BD-3**: Source real pricing figures per §12's instructions and populate
  `PRICING_TABLE`; add `tests/unit/test_pricing.py`'s completeness assertions.
  [D: BD-2]. This is a hard prerequisite for any gateway integration test that asserts
  an exact charged amount — sequence it early.
- **BD-4**: `schemas/user.py` — `UserCreateRequest`/`UserUpdateRequest`/`UserResponse`
  (§7.1). [P].
- **BD-5**: `schemas/chat.py` — add `ChatCompletionStreamOptions`, `stream_options` on
  `ChatCompletionRequest`, `usage` on `ChatCompletionChunk` (§3.2). [P].
- **BD-6**: `services/users.py` — CRUD + `UserNotFoundError` + `delete_user()`'s
  ADR-2 semantics (§1.4/§7.2). [D: DB-2].
- **BD-7**: `services/budget.py` — `UserBudgetState`/`get_budget_state`/
  `is_budget_exhausted`/`compute_cost`/`record_usage_charge` (§5). [D: DB-2, BD-2].
- **BD-8**: `api/deps.py` — `ServiceAccountContext.user_id` +
  `require_service_account` populating it (§4). [D: DB-2].
- **BD-9**: `api/v1/gateway/common.py` — `check_budget_available()`/
  `record_usage_charge()` (§4); update module docstring's chain. [D: BD-1, BD-7, BD-8].
- **BD-10**: `schemas/service_account_key.py` + `services/service_accounts.py` +
  `api/v1/admin/service_accounts.py` — `user_id` required on create, 404 on unknown user,
  `user_id` on both response schemas (§8). [D: BD-6] (needs `services.users.get_user`/
  `UserNotFoundError`).
- **BD-11**: New `api/v1/admin/users.py` — the five endpoints (§7.3); register router in
  `main.py`. [D: BD-4, BD-6].
- **BD-12**: `providers/openai.py` — unconditional `stream_options.include_usage=true`
  on the outbound streaming request only (§3.3). [D: BD-5] (needs the schema field to
  exist for the passthrough to validate against).
- **BD-13**: `providers/anthropic.py` — capture `message_start`/`message_delta` usage,
  yield the terminal usage chunk (§3.3). [D: BD-5].
- **BD-14**: `providers/vertex_ai.py` — capture cumulative `usageMetadata`, yield the
  terminal usage chunk after the stream loop ends (§3.3). [D: BD-5]. Flag: verify against
  a real or recorded Vertex streaming response before considering this done — see §3.5's
  note on Vertex's usage-reporting guarantee being the least formally documented of the
  three.
- **BD-15**: `api/v1/gateway/chat.py` — wire `check_budget_available`/
  `record_usage_charge` into both the non-streaming branch and `_sse_event_stream`
  (§3.4, §6); add `stream_options`-derived `client_wants_usage` computation.
  [D: BD-9, BD-12, BD-13, BD-14].
- **BD-16**: `api/v1/gateway/completions.py` — wire `check_budget_available`/
  `record_usage_charge` into the non-streaming path (§6). [D: BD-9].
- **BD-17**: `api/v1/gateway/embeddings.py` — same, with `completion_tokens=None` (§6).
  [D: BD-9].
- **BD-18**: Tests: unit tests for `services/budget.py` (`compute_cost`'s two formulas,
  `is_budget_exhausted`'s NULL/0 distinction — AC-4-3/4-5), unit tests for
  `check_budget_available`/`record_usage_charge`'s call-site ordering in `common.py`
  (AC-4-6), unit tests for `services/users.py` (ADR-2's delete semantics, ADR-4's
  PATCH tri-state), integration tests against a real migrated DB for: the full admin
  `users` CRUD surface (AC-1-*), service-account creation with a bad `user_id` (AC-2-2),
  the migration backfill itself (AC-2-6 — apply `0004` against a DB seeded with a
  pre-1.4-shaped `service_account_keys` row and assert the backfilled `user_id`/
  unmetered default), the hard-cutoff "N completes, N+1 blocked" ordering (AC-4-2) on
  all three endpoints (AC-4-4), the N=20 concurrent-charge no-lost-update test (AC-7-2),
  the boundary-race test per §10's precise (not over-strict) assertion (AC-7-3), the
  streaming usage-capture end-to-end for all three providers including the
  `stream_options.include_usage` opt-in/opt-out forwarding behavior (§3.4), and the
  disconnect/provider-error/pricing-gap not-charged paths (AC-6-2/6-3). [D: BD-15,
  BD-16, BD-17, BD-10, BD-11].

### Parallelization summary

`DB-1` and `BD-1`/`BD-2`/`BD-4`/`BD-5` can start immediately and in parallel. `DB-2`
depends only on `DB-1`. `BD-3` (real pricing figures) depends only on `BD-2`'s shape and
should be prioritized early since several later tests need real numbers, not
placeholders, to assert exact charged amounts. `BD-6`/`BD-7`/`BD-8` depend on `DB-2` and
can proceed in parallel with each other. `BD-9` gates most of the gateway-wiring tasks
(`BD-15`/`BD-16`/`BD-17`). The three provider-streaming tasks (`BD-12`/`BD-13`/`BD-14`)
depend only on `BD-5` and can proceed fully in parallel with each other and with
`BD-6`-`BD-11`. `BD-10`/`BD-11` depend on `BD-6`. `BD-18` is last, after every
route-wiring and admin-API task lands.

### Devops / docs (flagged, not owned by this design doc's roles)

- Phase 1.7's setup-wizard flow needs a "create a user" step ahead of "create a
  service-account key," since `user_id` is now required at key-creation time — already
  noted as informational-only by the product spec (§9); repeating here so it isn't lost
  between this design and whoever picks up 1.7.
