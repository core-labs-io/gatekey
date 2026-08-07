---
title: Phase 1.5 (Logging & Observability - Basic) / Phase 1.6 (Admin Console - Minimal, remaining endpoints) — Design
status: accepted
author: architect
last_updated: 2026-07-22
---

# Phase 1.5 / 1.6 — Design

Scope: a persisted per-request `UsageLog` table, wiring it into every gateway
route handler's success and failure paths, an admin usage-summary endpoint
(`GET /v1/admin/usage/summary`), and the `User` admin CRUD surface
(`/v1/admin/users`) that Phase 1.4's design doc already fully specified in
its section 7 (implemented here without re-litigating that design).

This builds directly on Phase 1.4 (`docs/design/phase-1.4-budget-basic-design.md`
- the `User`/budget machinery, `check_budget_available` -> `fetch_credential`
-> provider call -> `record_usage_charge` chain in `common.py`) and Phase 1.2/1.3
(`log_gateway_request`'s existing structured-log-only instrumentation, which
this phase's persisted table is explicitly *not* a replacement for - both
continue to exist side by side: `log_gateway_request` for
correlation/latency debugging, `UsageLog` for durable accounting/dashboard
queries).

---

## 1. `usage_logs` schema (migration `0005`)

See `gatekey.db.models.usage_log.UsageLog` for the full column-by-column
rationale. Summary of the two decisions worth calling out explicitly:

- **`user_id`/`service_account_key_id` are nullable with `ON DELETE SET
  NULL`, not `RESTRICT`.** Every other Phase 1 FK from a "live, in-use"
  table (e.g. `service_account_keys.user_id`) uses `RESTRICT` specifically
  so a delete can't silently orphan something still in active use. This
  table inverts that: a `UsageLog` row is a *historical* record - it must
  outlive the user/key that generated it, not block their deletion. Losing
  the FK on delete (falling back to `NULL`) is the correct behavior, not a
  gap; the `request_id`/`model`/`cost_usd`/etc. columns remain fully intact
  either way.
- **`model` stores the raw caller-requested string**, not a validated
  `MODEL_REGISTRY` key. A denied/unknown-model request still gets a usage
  row (see section 3) and its `model` value is exactly what the caller
  asked for - useful for "what are people actually trying to request that
  we're blocking" observability, which a NULL or a validated-only column
  would lose.

---

## 2. Usage-summary aggregation (`services/usage_logs.get_usage_summary`)

Single set of `GROUP BY` queries against the `(org_id, created_at)` index -
no N+1 per-row Python aggregation, matching the same performance discipline
`services/model_policy.py`/`services/budget.py` already apply to their own
hot/warm paths. Four queries: totals (spend/count/avg latency/error count),
spend-by-day, spend-by-model, spend-by-user (joined against the *current*
`users` row for `name`/`budget_usd`, not a point-in-time snapshot - a user's
displayed budget in a historical range always reflects their budget *today*,
matching the dashboard's live "Budget" column in the UI spec, not a
"what was their budget on that day" audit view, which is out of scope this
phase).

Response shape matches `gatekey/phase-1-admin-console-ui-requirements.md`
section 11's documented mock shape byte-for-byte (`total_spend_usd`,
`request_count`, `avg_latency_ms`, `error_rate`, `spend_by_day`,
`spend_by_model`, `spend_by_user`) so the frontend's Dashboard screen needed
zero field renaming once this endpoint existed.

`GET /v1/admin/usage/summary?range=24h|7d|30d` or `?range=custom&start=...&end=...`
- matches the UI spec's time-range selector exactly (§7.3).

---

## 3. Where `record_usage_log()` is called — and where it deliberately is not

**Called on every terminal outcome from `check_budget_available()` onward**:
a successful response, `BudgetExhaustedError`, `ProviderNotConfiguredError`
(raised by `fetch_credential()`), `ProviderUpstreamError`, and (streaming
only) `client_disconnected`/`usage_unavailable`/`charge_failed`. Each
gateway route handler wraps its body in a `try/except GatekeyError` that
persists a failed-outcome row (status = the error's `code`, no cost, no
token counts) before re-raising the exact same exception unchanged - the
caller's HTTP response is byte-for-byte identical to before this phase;
persistence is purely an added side effect, never a behavior change to the
response itself.

**Deliberately NOT called for `ModelNotFoundError`, `ModelDeniedError`, or
an endpoint's own capability-mismatch `UnsupportedRequestError`** (e.g. a
chat-incapable model against `/v1/chat/completions`). These three checks
are documented, in `api/v1/gateway/common.py`'s module docstring, as
*zero-I/O, reject-cheaply-before-any-database-work* checks - deliberately
ordered before `check_budget_available()`/`fetch_credential()` specifically
so a request that's going to be denied regardless never pays a DB round
trip. Adding a `UsageLog` write to that path would directly undo that
existing, tested architectural property (`tests/unit/test_gateway_chat.py`'s
`test_chat_completion_denied_model_*` tests assert *zero* DB access via an
exploding-session-sentinel fixture specifically to lock this down) for the
sake of persisting rows that are, in practice, the cheapest and least
interesting to reconstruct after the fact (they're still visible in the
existing structured `gatekey_error` log line every `GatekeyError` response
already emits via `errors.register_exception_handlers` - just not in the
`UsageLog` table). This is a deliberate scope line, not an oversight -
flagged here explicitly per the instruction to surface this class of
tradeoff rather than making the call silently.

**Best-effort, never raises**: `record_usage_log()` catches and logs its
own exceptions rather than letting a logging failure turn an otherwise-good
response into a 500 - the same "accounting is best-effort once bytes are
at risk" principle Phase 1.4's design doc already applies to
`record_usage_charge()` failures on the streaming path.

---

## 4. `services/users.py` / `api/v1/admin/users.py`

Implemented exactly per `docs/design/phase-1.4-budget-basic-design.md`
section 7 (schemas, service layer, routes, ADR-2's delete semantics, ADR-4's
PATCH tri-state) - no changes to that design. See that document for the full
rationale; this document does not repeat it.

---

## 5. Non-functional notes

- The `UsageLog` write is an additional DB round trip on every gateway
  request (success or failure) beyond what Phase 1.4 already added
  (`check_budget_available`'s read, `record_usage_charge`'s write) - three
  total DB round trips on a charged request's hot path. All three are
  single-row, indexed operations; per Phase 1.4's own flag, this should be
  verified under load rather than assumed, same caveat carried forward
  here, not newly introduced.
- No prompt/response body is ever logged (Phase 1.5's explicit scope
  boundary) - only routing/usage metadata. The schema doesn't preclude
  adding a body column later (Phase 3, with redaction controls), per the
  product requirement's own "schema should anticipate adding it" note.
