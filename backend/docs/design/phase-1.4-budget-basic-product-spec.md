---
title: Phase 1.4 — Budget (Basic) — Product Spec
status: draft
last_updated: 2026-07-17
author: product-owner
depends_on: Phase 1.1 (provider/key mgmt), 1.2 (gateway core), 1.3 (model policy) — all built, 288 tests passing
---

# Phase 1.4 — Budget (Basic) — Product Spec

Source requirement: `gatekey/phase-1-core-gateway.md` section 1.4:

> - One flat spend budget per user, defined in currency (e.g., USD).
> - Hard cutoff: requests blocked once a user's budget is exhausted, with a clear error message.
> - Cost computed per request using provider's published token pricing, normalized to a common currency.

This document converts that into buildable/testable user stories and acceptance criteria, resolves one real ambiguity in the source requirement (no human-user identity exists yet), and draws explicit lines against 1.5, 1.6, 1.7, and Phase 2.

---

## 1. Resolved Ambiguity: the "User" entity

**Problem:** the requirement says "budget per user," but Phase 1.2 deliberately built service-account keys as distinct from human user identity (no login exists; `ServiceAccountContext` carries only `org_id` / `service_account_id` / `name` — see `api/deps.py`). Human login/SSO is explicitly Phase 2 (2.1).

**Resolution:** introduce a minimal `User` entity in this slice:

- `id`, `org_id`, `name` (free-text label, not unique, not an email/login), `budget_usd` (nullable), `current_spend_usd`, `created_at`/`updated_at`.
- **Not an authentication principal.** No password, no session, no login endpoint, no token type. It exists solely so a service-account key can be attributed to a budget owner — a "cost center," not an identity.
- Every `ServiceAccountKey` gets a required `user_id` FK. Multiple keys may point at the same `User` (e.g., one person's several internal apps all draw from one budget pool) — this is intentional, not a bug: the requirement is "budget per user," not "budget per key."

This is a deliberate, minimal stand-in — not a guess at Phase 2's real identity model. Phase 2 (2.1) adds Org→Team→User hierarchy, RBAC roles, SSO/SCIM. Phase 1's `User` row has **zero** fields Phase 2 would need to remove or restructure (no role, no team, no auth fields) — Phase 2 only needs to *add* a team-membership table and role column, and additively extend budget enforcement to two more levels (team, org) on top of this same per-user row. Strict subset, confirmed against Phase 2 §2.1/§2.2.

### Migration of pre-existing service-account keys (built in 1.2, before this concept existed)

**Decision: auto-create a default user and backfill.** Not manual reassignment.

Rationale, weighed against Phase 1's own non-negotiables:
- **"Don't break existing pilot deployments":** manual reassignment would mean every existing service-account key stops being usable for budget purposes (or the migration must reject/pause traffic) until an admin acts. That is a regression a shipped pilot deployment did not sign up for.
- **"Under 60 minutes to first request" (this applies to *new* deployments too):** the default user must not silently impose a budget that blocks traffic the moment the migration runs. So the default user's `budget_usd` is `NULL` (see below) — unmetered, not `$0`.
- Migration steps: (1) create `users` table; (2) insert one default user per org (name: `"Unassigned (pre-1.4 legacy keys)"`, `budget_usd = NULL`); (3) add `service_account_keys.user_id` as nullable, backfill every existing row to the default user's id, then alter the column to `NOT NULL` + add the FK constraint. This is the standard safe pattern for adding a required FK to a populated table (no downtime, no window where the column is enforced-but-unpopulated).

`budget_usd = NULL` means **unmetered** (no cap enforced), not "$0 blocked." This deliberately mirrors an existing precedent in this codebase: `ModelPolicySnapshot`'s `"unconfigured"` mode is permissive-by-default (`services/model_policy.py`), not deny-by-default. Using the same "absence of config = permissive" convention here keeps the codebase's semantics consistent and avoids inventing a second meaning for "not set." An admin who wants to actually cap the default/legacy user does so with one `PATCH`.

`budget_usd = 0` (explicit zero, distinct from `NULL`) is a valid, different state: it means "this user may spend nothing" and blocks their very first request. This distinction (AC-1-6, AC-4-5) must not be collapsed.

---

## 2. Data model (spec-level, not literal DDL — architect owns exact column types/migration file)

**New table `users`:**
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `org_id` | UUID FK → `orgs.id`, `ON DELETE CASCADE` | matches `provider_key`/`service_account_key` pattern; scoped to `DEFAULT_ORG_ID` this phase, same as every other 1.1–1.3 table |
| `name` | text, not null | free-text label; **not** unique, **not** an email/login field this phase |
| `budget_usd` | fixed-point decimal (NUMERIC), nullable | `NULL` = unmetered (see §1) |
| `current_spend_usd` | fixed-point decimal (NUMERIC), not null, default 0 | monotonically increases; see §6 for why there is no "reset" endpoint |
| `created_at` / `updated_at` | timestamptz | |

Index: `ix_users_org_id`.

**Modify `service_account_keys`:**
- add `user_id` UUID, FK → `users.id`, `ON DELETE RESTRICT`, `NOT NULL` (after backfill — see migration above).
- `ON DELETE RESTRICT`, not `CASCADE`: deleting a `User` must never silently kill a live app's credential. See AC-2-5.
- index `ix_service_account_keys_user_id`.

**Monetary type requirement:** `budget_usd`, `current_spend_usd`, and every pricing-table rate MUST use a fixed-point decimal type (Postgres `NUMERIC`, Python `Decimal`) end to end — never IEEE-754 `float` — anywhere on the charge path. Floats accumulate rounding error across thousands of summed per-request charges; over a pilot's lifetime that drift could push `current_spend_usd` measurably away from real spend in either direction, undermining the "accurate cost/usage data" success criterion. This is a hold-the-line item, not a suggestion.

---

## 3. Pricing table prerequisite

Cost computation requires input+output USD-per-token pricing for every model in `providers/model_registry.py`'s `MODEL_REGISTRY`.

**Shape (spec-level):** a new static, in-code module (e.g. `providers/pricing.py`), mirroring `model_registry.py`'s existing "pure module, zero I/O, hand-curated dict at import time" pattern — not a DB table. Reasons:
- 1.6's admin capability list is explicit: "add/edit/remove provider keys; add/remove users, set per-user budget; set org-wide model allowlist/denylist; view usage dashboard." Editing model prices is **not** on that list.
- Pricing figures are platform-maintained data that must track what the model registry itself supports, exactly like `MODEL_REGISTRY` is hand-curated, not admin-editable.

Entry shape: `input_price_per_million_usd: Decimal`, `output_price_per_million_usd: Decimal | None` (`None` only for `ModelCapability.EMBEDDINGS` routes, which have no completion tokens — `EmbeddingsUsage` has no `completion_tokens` field at all, per `schemas/chat.py`).

**Completeness invariant (hard requirement, not a nice-to-have):** every key in `MODEL_REGISTRY` MUST have a corresponding pricing entry, and every `CHAT`-capability entry MUST have a non-`None` `output_price_per_million_usd`. A model that is routable but unpriced must never silently cost `$0` — that is a budget-bypass bug, not a graceful degradation. Enforce this two ways: (a) a build-time/test-time assertion that `PRICING_TABLE.keys() == MODEL_REGISTRY.keys()` and every chat entry has both prices set; (b) a defensive runtime guard that treats a missing entry as an internal error (loud log + 500), never as an implicit `$0` charge.

**Do not invent pricing figures.** Sourcing correct, currently-accurate, dated provider pricing (OpenAI, Anthropic, Vertex AI, for the exact pilot models in `MODEL_REGISTRY`) is the architect/backend-developer's job, and the module should document an as-of date/citation per entry since providers change list prices. This spec defines only the schema shape and the completeness invariant.

**Flagged, not decided:** making this table admin-editable (so a self-hosted operator can correct stale prices without a code deploy) is a reasonable future ask, but it isn't requested by 1.6's capability list and is out of scope this slice. Flagging because stale hardcoded prices are a real, silent correctness risk over time (under/over-charging as providers reprice) — worth an explicit "yes, static in-code is fine for the pilot" or "no, we want this admin-editable now" call from the orchestrator/user rather than me guessing further.

---

## 4. Request-pipeline integration

Existing chain (`api/v1/gateway/common.py`, established 1.2/1.3): `resolve_route()` → `check_model_policy()` → endpoint-specific capability/provider check → `fetch_credential()` → provider call.

**New chain:**

```
resolve_route()
  -> check_model_policy()
  -> [endpoint-specific capability/provider check]
  -> check_budget_available()      [NEW — pre-call gate]
  -> fetch_credential()
  -> [provider call, streaming or not]
  -> record_usage_charge()         [NEW — post-call, only on confirmed success]
```

`check_budget_available()` is placed **before** `fetch_credential()`, for the same reason `check_model_policy()` is: reject cheaply before paying the decryption cost of a credential fetch. Unlike `check_model_policy()`, it is **not** zero-I/O — `current_spend_usd`/`budget_usd` are per-user mutable state that changes on every charged request, so (unlike the org-wide policy snapshot) it is not a candidate for `ModelPolicyCache`'s in-process-cache pattern; it must read through to the DB every time. It is still cheaper than `fetch_credential()` (a single indexed point lookup vs. decrypt), so the ordering still saves work on the reject path.

`ServiceAccountContext` (`api/deps.py`) currently exposes `org_id` / `service_account_id` / `name` only. It must gain a `user_id: uuid.UUID` field, populated from the now-required `ServiceAccountKey.user_id` column in `require_service_account()`'s existing lookup — no new query, just reading one more column off the row already fetched.

`record_usage_charge()` must be called at every place a route handler currently has confirmed, complete usage data:
- `/v1/chat/completions` non-streaming: after the provider response (with `usage`) is received, before/at building `ChatCompletionResponse`.
- `/v1/chat/completions` streaming: after the terminal chunk carrying `usage` is produced by the provider-translation generator — see §6 for what happens if the stream never reaches that point.
- `/v1/completions` (legacy, non-streaming only — streaming is already rejected 400 by existing 1.2 behavior): same as chat non-streaming.
- `/v1/embeddings`: after the provider response (with `usage`, no `completion_tokens`) is received.

---

## 5. Resolved design tension: "hard cutoff before any provider call" vs. "cost from actual usage, not pre-call estimate"

These two requirement bullets are in tension and the source doc doesn't reconcile them — I'm resolving it explicitly here, not skating past it:

- We cannot know a specific request's exact cost before the provider responds (no pre-call estimation allowed).
- Therefore the pre-call "hard cutoff" can only be: **is this user already at or over budget from *previous* requests?** — not "will *this* request push them over."
- Consequence (accepted, documented behavior, not a bug): a user may finish one single request that pushes `current_spend_usd` past `budget_usd`. The cutoff guarantees the **next** request after that is blocked, not that spend never exceeds the ceiling by more than one in-flight request's cost. This mirrors how most real postpaid-metering systems behave (cloud spend caps included).
- QA must test the "N+1"-th request is blocked, not that the exact crossing request is blocked (AC-4-2).

Gate condition: block if `budget_usd IS NOT NULL AND current_spend_usd >= budget_usd`. (`>=`, not `>` — "exhausted" means fully used.)

---

## 6. Charging semantics: idempotency, atomicity, failure handling

- Charging happens **exactly once**, gated by a single code path per request lifecycle (not duplicated across success/error branches) — this is an implementation-discipline requirement, testable by AC-6-1.
- **Never charge without confirmed actual usage.** A request that errors before the provider responds (denied model, no credential, provider 4xx/5xx/timeout, gateway-side validation failure) is never charged.
- **Streaming, partial/aborted:** if a stream terminates (client disconnect, provider error mid-stream) before the terminal usage-bearing chunk is received, the request is **not charged**. We deliberately fail toward *not charging* rather than estimating from partial output — consistent with the "actual usage, not pre-call estimate" principle applying just as much to a post-call estimate. Flagged, accepted limitation: a small amount of real provider spend on aborted streams will not appear in Gatekey's tracked total in this slice; full reconciliation is out of scope (would need persisted per-request state, which is 1.5).
- **Charge-write failure after a response was already (partially) delivered to the caller** (e.g., DB unavailable at the moment `record_usage_charge()` runs, after streaming bytes are already on the wire): the response is not rolled back (impossible for a stream already in flight); the accounting failure is logged loudly server-side (not swallowed), and is an accepted best-effort gap this slice does not retry/reconcile. Flagged forward as a reason 1.5's persisted usage log should make this class of gap reconcilable later.
- **Cross-retry (client-level) dedup is explicitly out of scope**, consistent with 1.2's already-made decision that `Idempotency-Key` is plumb-through-only with "no caching/dedup logic in this phase" (`api/v1/gateway/common.py` module docstring). "Idempotent accounting" in 1.4 means (a) one request execution charges at most once and (b) no charge without a successful response — not that Gatekey can recognize two separate client HTTP calls sharing the same `Idempotency-Key` as "the same logical request already paid for." I'm resolving this by inference from 1.2's documented precedent, not silently narrowing the requirement — flagging the boundary explicitly here in case that's not actually what's wanted.
- **Atomicity under concurrency:** the charge write MUST be a single atomic `UPDATE ... SET current_spend_usd = current_spend_usd + :cost WHERE id = :user_id RETURNING current_spend_usd` (or equivalent single-statement form) — never a read-then-write in application code. This mirrors the atomic-upsert pattern already established in this codebase (`services/provider_keys.add_or_replace_key`, `services/model_policy.set_policy` — both single `INSERT ... ON CONFLICT ... DO UPDATE` statements specifically to prevent interleaving under concurrent writers). Two concurrent requests from the same user must both be reflected, never lost to a lost-update race. This is at least as strong as Phase 2's own NFR ("atomic spend-check-and-deduct, not eventual consistency," §2.2 NFR) so Phase 2 can build team/org-level atomic checks on top without re-deriving this guarantee for the user layer.

---

## 7. Error class

New `BudgetExhaustedError` in `errors.py`, held to the same bar as `ModelDeniedError`:
- Own error `code` (e.g. `"budget_exhausted"`), not a generic 403/500.
- `message` built from non-secret, caller-relevant data (user name, budget, current spend) — same "caller input/state, not secret material" justification `ModelDeniedError` and `ModelNotFoundError` already use for including the model name.
- Example: `"User 'checkout-service' has exhausted its budget of $50.00 USD (current spend: $50.00 USD). Contact your administrator to increase the budget."`

**Flagged, not decided:** status code. `ModelDeniedError` uses 403 (a policy denial). Budget exhaustion is conceptually a quota/billing state, not an authorization decision — HTTP 402 Payment Required is the semantically precise code for "out of quota, needs the account topped up," and I recommend it. But this is a judgment call that deviates from the one existing precedent in this codebase (403), so I'm flagging it explicitly rather than picking silently — orchestrator/user, please confirm 402 vs. 403 before the architect locks the schema.

---

## 8. User Stories & Acceptance Criteria

### US-1 — Admin can create and manage budget-owning users (backend API only, no login)
As an org admin, I can create, list, view, and delete `User` records and set/update their USD budget via API, with no UI (1.6) and no login/auth capability of the User itself.

- **AC-1-1:** `POST /v1/admin/users` (gated by the existing `require_admin` dependency, same trust boundary as every other Phase 1 admin route) accepts `{name: str, budget_usd: Decimal | null}`; `budget_usd` is optional and defaults to `NULL` (unmetered) if omitted.
- **AC-1-2:** Response includes `id`, `name`, `budget_usd`, `current_spend_usd` (starts at `0`), `created_at`.
- **AC-1-3:** `GET /v1/admin/users` lists every user for the default org.
- **AC-1-4:** `GET /v1/admin/users/{id}` returns one user; `404` (structured `NotFoundError`, matching existing convention) if not found.
- **AC-1-5:** `PATCH /v1/admin/users/{id}` accepts partial `{name?, budget_usd?}`; `budget_usd` may be set to `NULL` (revert to unmetered) or any non-negative decimal, including `0`. `404` if not found.
- **AC-1-6:** `budget_usd = 0` and `budget_usd = NULL` are distinct, both persisted and returned exactly as submitted (no silent coercion of one to the other).
- **AC-1-7:** `current_spend_usd` is never a settable field on create or `PATCH` (request schema uses `extra="forbid"`, matching this codebase's existing admin-schema convention — see `schemas/provider_key.py`/`schemas/chat.py` docstring note) — attempting to set it is a `422`, not a silent no-op.
- **AC-1-8:** `DELETE /v1/admin/users/{id}` returns `204` and hard-deletes the row if no active (non-revoked) `ServiceAccountKey` references it.
- **AC-1-9:** `DELETE /v1/admin/users/{id}` returns `409` (structured error, e.g. `user_in_use`) if one or more active service-account keys still reference the user — the FK is `ON DELETE RESTRICT`; the service layer catches the resulting `IntegrityError`/pre-checks and raises a clean structured conflict, never a raw DB error or generic 500.
- **AC-1-10:** `DELETE /v1/admin/users/{id}` returns `404` if the id doesn't exist.
- **AC-1-11:** No endpoint under this story issues, validates, or accepts any credential/password/session for a `User` — there is no such capability to test against, by design.

### US-2 — Service-account keys are attributed to a budget-owning user
- **AC-2-1:** `POST /v1/admin/service-accounts` (existing 1.2 endpoint) request schema gains a required `user_id: uuid.UUID` field.
- **AC-2-2:** Creating a service-account key with a `user_id` that doesn't reference an existing user returns `404` (structured, e.g. `"No user found with id '<id>'."`), no row is written.
- **AC-2-3:** A successfully created key's `user_id` is stored and returned in `ServiceAccountKeyResponse`.
- **AC-2-4:** Multiple service-account keys may share the same `user_id`; all their charged usage accrues to that one user's `current_spend_usd`.
- **AC-2-5:** There is no endpoint to reassign an existing key's `user_id` in this slice — to move a live credential to a different budget owner, an admin revokes the old key and issues a new one under the correct `user_id`. (See §9 for why this is deliberately deferred, not an oversight.)
- **AC-2-6 (migration):** after the migration runs against a database with pre-existing (1.2-era) service-account keys, every existing key has a non-null `user_id` pointing at an auto-created default user with `budget_usd = NULL`, and continues to authenticate/route exactly as before — zero manual admin action required for existing pilot traffic to keep working.

### US-3 — Per-model USD pricing table exists as a cost-computation prerequisite
- **AC-3-1:** Every key in `MODEL_REGISTRY` (`providers/model_registry.py`) has a corresponding pricing entry.
- **AC-3-2:** Every `CHAT`-capability entry has both a non-null input and non-null output price; every `EMBEDDINGS`-capability entry has a non-null input price and an explicitly-`None` output price (never a missing/omitted field).
- **AC-3-3:** A test asserting `PRICING_TABLE.keys() == MODEL_REGISTRY.keys()` (plus the AC-3-2 shape check) exists and fails the build if a model is added to the registry without a matching pricing entry — this must not be discoverable only at request time.
- **AC-3-4:** All rates are fixed-point `Decimal`, never `float`.
- **AC-3-5:** No pricing figures in the delivered table are invented/placeholder values by the product-owner spec — actual figures, sourced and dated, are the architect/backend-developer's deliverable against this schema.

### US-4 — Hard cutoff blocks requests for an already-exhausted user, before any provider call
- **AC-4-1:** For a user with `budget_usd IS NOT NULL AND current_spend_usd >= budget_usd`, a gateway request is rejected with `BudgetExhaustedError` (§7) **before** `fetch_credential()` runs — no provider credential is decrypted, no outbound provider call is made.
- **AC-4-2:** The specific request whose actual cost first pushes `current_spend_usd` to/past `budget_usd` is **not** itself blocked (cost unknowable pre-call, §5) — it completes and is charged; the **next** request from that user is what gets blocked. Test asserts this exact "N completes, N+1 is blocked" ordering, not "N is blocked."
- **AC-4-3:** For a user with `budget_usd IS NULL` (unmetered), no pre-call gate is applied regardless of `current_spend_usd`'s value — the request proceeds.
- **AC-4-4:** This check runs identically on `/v1/chat/completions` (streaming and non-streaming), `/v1/completions`, and `/v1/embeddings` — no route can bypass it.
- **AC-4-5:** A user created/updated with `budget_usd = 0` is blocked on their very first request (`0 >= 0`), distinct from `budget_usd = NULL` which is never blocked by this check alone.
- **AC-4-6:** The check happens after `check_model_policy()` and any endpoint capability check, and before `fetch_credential()` — a request denied by model policy or an unsupported capability never reaches the budget check (ordering test against `common.py`'s chain).

### US-5 — Cost is computed from actual provider-reported usage only
- **AC-5-1:** For chat/completions, cost = `prompt_tokens * input_price_per_million_usd / 1_000_000 + completion_tokens * output_price_per_million_usd / 1_000_000`, using the exact `ChatCompletionUsage` values the provider returned (never a pre-call estimate derived from prompt length/tokenizer guesswork).
- **AC-5-2:** For embeddings, cost = `prompt_tokens * input_price_per_million_usd / 1_000_000` only (no output-token term — `EmbeddingsUsage` has no `completion_tokens`).
- **AC-5-3:** Cost computed at $0 for a genuinely zero-usage response is a legitimate value, distinct from "no pricing entry found" (§3, an internal error) — these two zero-cost-looking states must not be conflated in code.
- **AC-5-4:** A model resolved successfully by `resolve_route()` but missing from the pricing table never silently charges `$0` (see AC-3-3/§3) — it is a logged internal error state.

### US-6 — Idempotent accounting: never double-charge, never charge a failed/no-response request
- **AC-6-1:** `record_usage_charge()` is invoked from exactly one call site per request outcome path (success), never from more than one branch that could both fire for the same request.
- **AC-6-2:** A request that fails before a provider response (model denied, model not found, provider not configured, budget exhausted, request-validation error, provider upstream error/timeout) results in zero change to `current_spend_usd`.
- **AC-6-3:** A streaming request whose connection is aborted before the terminal usage-bearing chunk is produced results in zero change to `current_spend_usd` (§6 — deliberate fail-toward-not-charging).
- **AC-6-4:** Re-running the exact same successful request twice (two independent HTTP calls, not a client-level retry of one call) charges twice — this is correct, expected behavior (two real provider calls, two real costs), and must not be treated as a "double charge" bug. (Distinguishes AC-6 from client-retry dedup, which is explicitly out of scope — §6.)

### US-7 — Atomicity under concurrent requests from the same user
- **AC-7-1:** The charge write is a single atomic `UPDATE ... SET current_spend_usd = current_spend_usd + :cost ... RETURNING current_spend_usd` statement (or DB-enforced equivalent) — no read-modify-write round trip in application code.
- **AC-7-2:** A test issuing N concurrent charged requests for the same user (e.g., N=20, each costing a known fixed amount) asserts the final `current_spend_usd` equals exactly `N * cost` — no lost updates.
- **AC-7-3:** A test issuing concurrent requests that straddle the budget boundary (user has room for exactly 1 more request, 2 requests race) asserts at most one of the two is charged past the point where the other would have been blocked had it gone second — i.e., the pre-call gate (AC-4-1) and the atomic charge together must not allow unbounded overshoot under concurrency; some single-request overshoot per §5 is accepted, unbounded overshoot from a race is not.

### US-8 — Structured, high-quality budget-exhausted error
- **AC-8-1:** `BudgetExhaustedError` is its own `GatekeyError` subclass with a dedicated `code` (not reused from `ModelDeniedError` or a generic 403/500).
- **AC-8-2:** `message` includes the user's name, their budget, and their current spend, formatted as currency — no secret material, matching the existing "caller input, safe to log" bar (`ModelDeniedError`'s docstring rationale).
- **AC-8-3:** Response envelope shape is identical to every other `GatekeyError` (`{"error": {"code", "message"}}`) — the gateway does not get a bespoke OpenAI-shaped error body for this case either, consistent with the already-made decision documented on `UnsupportedRequestError`.
- **AC-8-4:** Status code: see §7 flagged decision (402 recommended, pending confirmation).

---

## 9. Explicit scope boundaries — out of scope this slice

- **No persisted per-request usage log table.** `current_spend_usd` on `User` is the only state tracked; no per-request row, no historical detail — that is 1.5's schema.
- **No admin console UI.** Only the backend API surface in US-1 exists; 1.6's UI consumes it later.
- **No deployment/docker/setup-wizard work.** That's 1.7. Note for that phase (informational only, not built here): the setup wizard's flow will need a "create a user" step ahead of the existing "create a service-account key" step, since `user_id` is now required at key-creation time.
- **No budget rollover, no period boundaries (monthly/quarterly), no roll-over-vs-reset policy.** `current_spend_usd` accumulates indefinitely; there is no automatic reset. All of this is Phase 2 §2.2.
- **No team-level or org-level budget.** Exactly one budget layer exists: per-user. Phase 2 §2.2 adds team/org ceilings and the "team total ≤ sum of members" constraint on top — nothing here needs to be torn out to support that.
- **No RBAC / Team Lead role.** The admin API in US-1 is gated by the same single shared `require_admin` token as every other Phase 1 admin endpoint (per 1.6: "single org admin role only"). No delegated administration.
- **No budget alerts/notifications** (soft threshold emails/webhooks) — Phase 2 §2.2.
- **No cost-normalization "show your work" audit API.** Phase 2 §2.2's "admin can see how a raw provider cost was converted" is a Phase 2 deliverable; this slice only needs correct arithmetic, not an audit endpoint over it (though `record_usage_charge()`'s inputs should be structured-logged, dovetailing with 1.5's anticipated schema, per 1.5's own note that its schema "should anticipate" future fields).
- **No reassignment of an existing service-account key's `user_id`** (AC-2-5) — deferred; a real "move this credential's budget owner" primitive belongs with Phase 2's budget reassignment machinery (§2.2), which needs the audit trail Phase 2 §2.4 builds (who changed what, old→new) — building a one-off, unaudited version of it here would likely need to be redone, not extended, once Phase 2 lands.
- **No manual "reset spend" or "top-up" admin endpoint.** An admin unblocks an exhausted user only by raising `budget_usd` (which is always sufficient, since the gate is `current_spend_usd >= budget_usd`). Deliberately not adding a separate reset primitive avoids inventing ad hoc period semantics that would duplicate (and likely conflict with) Phase 2's real rollover/reset model.
- **No multi-currency support.** Everything is USD; the pricing table is USD-denominated so no normalization step is needed yet. Phase 2 §2.2's "single currency unit regardless of provider" concern is about normalizing different *pricing shapes* (per-token vs. per-character vs. per-request), not currencies — Phase 1's pricing table only needs to hold token-based rates since every current pilot model is token-priced; its shape should not, however, hard-code an assumption that blocks a future per-character/per-request rate shape (flag to architect: keep the pricing entry an extensible record, not a bare two-`Decimal` tuple).
- **No admin-editable pricing** (§3) — flagged as a judgment call above, not silently decided.

---

## 10. Non-functional requirements carried forward

- **p99 added latency, target <150ms** (Phase 1 NFR, unchanged — not loosened). This slice adds two DB round trips to the hot path that weren't there before: the budget-gate read (AC-4-1, a single indexed point lookup) and the atomic charge write (AC-7-1, a single indexed point update). Unlike `check_model_policy()`, these cannot be served from an in-process cache (§4) because the data changes on every request. Flagging to the architect as a latency-budget consideration, not a requested relaxation of the 150ms target: both operations should be simple indexed point queries against the `users` PK, which should comfortably fit the existing budget on a local/co-located Postgres, but this needs to be verified under the same load test used to validate 1.1–1.3's latency, not assumed.
- **"Must not lose or double-charge requests on provider timeout/retry"** (Phase 1 NFR) — directly covered by US-6/US-7 above; not weakened, and the client-retry-dedup boundary is explicitly scoped out (§6), not silently dropped.
- **Success criterion "see accurate cost/usage data"** — this slice is a prerequisite for that criterion (accurate `current_spend_usd`), not the full delivery of it (the "usage view" itself is 1.5).
- **Success criterion "under 60 minutes to first request"** — preserved by the `budget_usd = NULL` default (§1): a fresh deployment's first user/key can be created with zero budget-related decisions and get to a first proxied request immediately; setting a real budget is a follow-up action, not a blocker.

---

## 11. Forward-compatibility check against Phase 2 §2.1/§2.2

Confirmed this spec is a strict subset Phase 2 can extend, not something it has to tear out:
- `User` has no auth/role/team fields to migrate away from when Phase 2 adds SSO, RBAC, and Org→Team→User (§2.1).
- Exactly one budget layer (user) exists; Phase 2 adds team and org layers *on top*, plus the "team total ≤ sum of members" assignment-time constraint (§2.2) — nothing here assumes there is only ever one layer, it just doesn't build the other two.
- The atomic-single-statement charge pattern (§6/AC-7-1) already meets Phase 2's own concurrency NFR ("atomic spend-check-and-deduct, not eventual consistency," §2.2), so Phase 2's team/org-level checks can be layered on without re-deriving that guarantee.
- No period/rollover concept exists to conflict with Phase 2's configurable period boundaries and rollover-vs-reset policy (§2.2) — Phase 2 is free to add it without unwinding anything here.
- Currency handling is trivially single-currency (§9) and doesn't block Phase 2's normalization-auditability feature.

---

## 12. Items flagged back for orchestrator/user decision (not guessed)

1. **`BudgetExhaustedError` status code: 402 vs. 403** (§7/AC-8-4). Recommend 402 (semantically correct for quota exhaustion); flagging because it breaks precedent with `ModelDeniedError`'s 403.
2. **Should the pricing table be admin-editable via API, or static in-code** (§3)? Recommend static in-code this slice (matches `MODEL_REGISTRY`'s precedent and 1.6's admin capability list, which doesn't mention pricing). Flagging because stale hardcoded prices are a real silent-correctness risk as providers reprice over time.

## 13. Open Questions from `phase-1-core-gateway.md` — relevance check

The top-level Phase 1 open questions (provider priority, embeddings/vision in v1, self-hosted-only vs. hosted sandbox) are already effectively resolved by 1.1–1.3's shipped code (three providers — openai/anthropic/vertex_ai — and embeddings are both already live in `MODEL_REGISTRY`) and none require a new decision specific to 1.4. No action needed here.
