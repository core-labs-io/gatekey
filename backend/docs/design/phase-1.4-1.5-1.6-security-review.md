---
title: Security Review — Phase 1.4 (Budget), 1.5 (Logging), 1.6 (Admin Console + Frontend)
status: signed-off, with flagged follow-ups
author: security-reviewer
last_updated: 2026-07-26
---

# Security Review — Budget, Usage Logging, Admin Users API, Admin Console UI

Scope reviewed: everything added in this slice —
`services/budget.py`, `providers/pricing.py`, `services/users.py` +
`api/v1/admin/users.py`, `services/usage_logs.py` + `api/v1/admin/usage.py`,
budget/usage-log wiring in `api/v1/gateway/{chat,completions,embeddings}.py`
and `api/v1/gateway/common.py`, streaming usage-capture changes in
`providers/{openai,anthropic,vertex_ai}.py`, `schemas/service_account_key.py`'s
`user_id` addition, CORS middleware, and the entire `frontend/` app.

## Findings — fixed during this review

1. **Missing CORS configuration (fixed).** The backend had no
   `CORSMiddleware`, which would have silently blocked every browser
   `fetch()` call the new admin console makes cross-origin (frontend on
   `:3000`, backend on `:8000`). Added `config.Settings.
   GATEKEY_CORS_ALLOWED_ORIGINS` (default `"*"`) + `CORSMiddleware` in
   `main.py`. Verified safe as configured: this API is Bearer-token
   authenticated (`Authorization` header), never cookie-authenticated, and
   `allow_credentials=False` is set — a permissive origin allowlist carries
   no CSRF/credential-leak risk the way it would for a cookie-authenticated
   API. Confirmed no code path anywhere in this codebase relies on
   cookie-based auth.
2. **mypy type-narrowing gap (fixed, defense-in-depth only, not
   exploitable).** `check_budget_available()` passed a `Decimal | None`
   where `BudgetExhaustedError.__init__` expects `Decimal`; added an
   explicit `assert state.budget_usd is not None` at the one call site
   where `is_budget_exhausted()` has already guaranteed that. Not a runtime
   bug (the None case is provably unreachable there), but worth locking
   down so a future refactor can't silently reintroduce a real bug.

## Verified — no new issues found

- **No plaintext secrets at rest or in logs (cross-phase non-negotiable),
  still holds.** `record_usage_log()`'s persisted `UsageLog` row never
  receives provider keys, service-account secrets, or prompt/response
  bodies — only routing/usage metadata (model string, token counts, cost,
  latency, status code). Verified every `record_usage_log(...)` call site
  in `chat.py`/`completions.py`/`embeddings.py` passes only non-secret
  fields. `errors.py`'s `_REDACTED_FIELD_NAMES` didn't need new entries —
  none of this slice's new request/response fields (`budget_usd`,
  `current_spend_usd`, `user_id`, usage-summary fields) are secret-shaped.
- **Admin-only endpoints correctly gated.** `api/v1/admin/users.py` and
  `api/v1/admin/usage.py` both declare `dependencies=[Depends(require_admin)]`
  at the router level, matching every other Phase 1 admin router — no
  endpoint added in this slice is reachable by a service-account credential
  or unauthenticated.
- **`BudgetExhaustedError`'s message** (user name, budget, current spend)
  is caller/state input, not secret material — same bar `ModelDeniedError`
  already established; consistent, not a new exposure.
- **`ServiceAccountKeyCreateRequest.user_id`** being newly required does not
  weaken auth: it's validated against a real, admin-scoped `User` row
  (404 `UserNotFoundError` on an unknown id) before any row is written — no
  way to attribute a key to an arbitrary/unowned budget entity.
- **Streaming usage-capture changes** (`providers/openai.py`'s
  unconditional `stream_options.include_usage=true`,
  `providers/anthropic.py`/`providers/vertex_ai.py`'s terminal usage chunk)
  add no new outbound data — they parse/forward token counts the providers
  were always willing to report; no new fields sent *to* providers beyond
  the OpenAI-only `stream_options` flag, which carries no secret material.
- **Frontend token handling.** The admin token is stored in
  `localStorage` (`frontend/src/lib/api.ts`) and sent only as an
  `Authorization: Bearer` header, never logged (`grep`-verified no
  `console.log`/`console.error` of token/secret values anywhere in
  `frontend/src` or `frontend/app`), never embedded in a URL. This matches
  the UI spec's own stated auth model (a single shared secret typed into a
  browser, not a session-cookie system) — standard SPA-token-storage risk
  profile for that model, not a regression from a stronger scheme Phase 1
  could have had instead (there is no user-account/session system yet to
  compare against; that's Phase 2).
- **Service-account secret / provider key one-time-reveal UI**
  (`ServiceAccounts` create flow, `ProviderKeyForm`) matches the UI spec's
  "shown once, never again" requirement: the secret is never re-fetchable
  from any list/get endpoint (verified server-side:
  `ServiceAccountKeyResponse` has no `secret`/`secret_hash` field at all —
  pre-existing Phase 1.2 guarantee, unchanged by this slice), and the
  step-2 modal cannot be backdrop/Esc-dismissed (`Modal`'s `onClose={null}`
  path in `ServiceAccounts`'s `CreateFlow`).

## Flagged, not blocking — needs the deploying operator's attention

1. **Pricing table figures need re-verification against live provider
   pricing pages before production budget enforcement is trusted** — the
   build environment this slice was produced in had no live web access.
   Every `providers/pricing.py` entry is dated (`as_of`) and cited
   (`source`) specifically so this is checkable, not silently assumed
   correct. See `README.md`'s "Known limitations" section.
2. **`docker-compose up` was not executed end-to-end in this review** —
   Docker Desktop was unavailable in the environment at the time of this
   review (service stopped, could not be started without elevated
   permissions). Compose file syntax was validated (`docker compose
   config`); a live run should be performed before this is considered
   production-deployable. See `README.md`'s "Known limitations" section
   for the same note.
3. **Vertex AI streaming usage accuracy** is the least formally guaranteed
   of the three providers' streaming-usage contracts per Google's own
   documentation — an under- or over-count here is a billing-accuracy risk
   (not a security vulnerability), flagged for the same reason Phase 1.4's
   original design doc flagged it.

## Sign-off

This slice is approved to ship as Phase 1's remaining budget/logging/admin/
frontend work, contingent on the deploying operator (a) re-verifying
provider pricing before relying on budget enforcement for real spend
control, and (b) personally exercising a `docker-compose up` pass on their
own machine before pilot traffic. Neither blocks correctness of the code
itself; both are external-verification steps this review environment could
not complete.
