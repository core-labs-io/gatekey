---
title: Phase 3 — Security & Compliance Hardening
status: draft
last_updated: 2026-07-10
---

# Phase 3 — Security & Compliance Hardening

## Goal
Clear the bar that enterprise security and legal teams actually gate on before allowing wider rollout. This phase is typically the real blocker to enterprise adoption — prioritize accordingly.

## Depends On
Phase 2 (Multi-Tenant Governance) for org/team/user hierarchy and RBAC, which policy and audit features attach to.

## In Scope

### 3.1 Full Audit Trail
- Immutable-in-practice log (append-only, no update/delete via normal API) of all administrative actions: policy changes, budget changes, RBAC changes, key additions/removals, login events.
- Each entry records: actor, action, target, old value → new value, timestamp, source IP.
- Audit log is queryable and exportable (CSV/JSON) by Org Admin and Auditor roles.
- Note: this is a standard audit log, distinct from the cryptographically tamper-evident ledger planned for Phase 5 — this phase does not require hash-chaining.

### 3.2 PII / DLP Scanning
- Scan outbound prompts (and optionally inbound responses) for common PII patterns (SSNs, credit card numbers, email addresses, phone numbers) and configurable custom patterns (regex-based, e.g., internal employee ID formats). **Resolved (build vs. integrate):** integrate an existing open-source library (Presidio) rather than build pattern matching in-house. PII detection coverage/correctness (international phone formats, edge cases in SSN-like patterns, etc.) is a solved problem in mature OSS tooling — building it from scratch mainly risks under-covering cases Presidio already handles, for no real benefit. Presidio runs self-hosted (Python, fits the existing FastAPI backend per `tech-stack.md`), so this doesn't compromise the self-hosted-first, no-phone-home non-negotiable. Org-defined custom regex patterns (already in scope) layer on top of it, not instead of it.
- Configurable action per policy: **log only**, **redact and forward**, or **block entirely**.
- Redaction must be applied before the request leaves the gateway to the provider — provider never sees unredacted flagged content in "redact" mode.
- Scanning results appear in the request log for audit purposes (what was flagged, what action was taken) without necessarily storing the raw flagged content if policy says not to.

### 3.3 Data Residency Controls
- Ability to restrict, per org or per team, which provider regions/endpoints a request may be routed to (e.g., "EU team requests must only hit EU-region endpoints").
- Requests that would violate residency policy are blocked with a clear error, not silently rerouted. **Resolved (hard block vs. warn-only default):** hard block by default. An org that goes to the trouble of configuring a residency rule is explicitly trying to prevent a data flow — defaulting to warn-only would silently undermine the entire point of the feature the first time it actually matters. Warn-only remains available as an explicit, deliberate opt-down for an org still mid-migration (already reflected as a configurable per-rule setting in the admin console), but it's not what a newly-configured rule does out of the box.

### 3.4 Nested Model Policy — Content Awareness (initial version)
- Extend Phase 2's static team-level allow/deny lists with the ability to attach a policy rule based on DLP scan results from 3.2 — e.g., "if PII detected, restrict to models flagged as compliant for sensitive data" (a simple rule engine, not the full dynamic classification system planned for Phase 5).

### 3.5 SCIM (if deferred from Phase 2)
- Full SCIM 2.0 support for user/team provisioning and deprovisioning from an external IdP, if not already completed in Phase 2.

### 3.6 Compliance Documentation
- Data flow diagram and a written data handling policy (what Gatekey stores, for how long, where) suitable for a customer's security review / vendor risk assessment.
- Configurable log/prompt retention periods with automatic purge. **Resolved (default retention period):** 30 days if an org never configures one — short and safe rather than indefinite, matching the admin console's own pre-filled default (`ui-requirements-admin.md` §10.4). Audit log retention (3.1) is a separate, independently-configured setting and does not inherit this default — it's typically held longer for legal-hold reasons, which is exactly why the two are kept separable at the infra level per the non-functional requirement below.

### 3.7 Credential Rotation
- **Service-account keys (Gatekey-issued secrets)**: fully automatic rotation. An org sets a default rotation interval (e.g., every 30/60/90 days); each key can override the org default. The new secret is delivered via the existing one-time-reveal pattern (see the Phase 1 admin console spec, service-account creation flow) plus a notification (email/webhook) to whoever owns the key, so rotation is never silent.
- **Rotation timing is scheduled, not incidental.** Rather than defending "must not impact any user's working" purely with a long dual-active overlap, rotation is timed to land inside a window when the key is expected to be idle — by default, outside the key's own access-schedule window (3.8) if one is configured, or an org-wide off-hours maintenance window (e.g., 02:00 org-local time) otherwise. A **personal key** (Phase 2 §2.5) in particular is expected to see human, business-hours usage, so rotating it overnight all but eliminates the odds of an in-flight request landing mid-rotation — this is the primary mechanism satisfying "no impact on the user's work," not a long safety-net overlap. An **app/service key** may legitimately run around the clock, so this off-hours assumption is opt-in per key, not a blanket default — an app key with no defined idle window keeps the longer overlap behavior described below.
- **Grace period is now a short technical buffer, not a multi-day safety net.** Because rotation is timed to avoid active use, the old secret only needs to overlap the new one long enough to absorb a genuinely in-flight request or clock skew across gateway instances — default **a few minutes**, not 24-72h. A long-lived overlap is no longer the right default: it was originally meant to compensate for an unpredictable rotation moment, and it directly cuts against a real security concern (a compromised key staying accepted for days after "rotation" supposedly happened). Non-functional requirement below still holds — the overlap must exist, just be short.
- Manual "Rotate now" uses the same short-overlap mechanism, not an immediate revoke — immediate revoke remains a separate, distinct action for the "this key is compromised" case, where skipping any overlap at all is the correct behavior.
- **Provider keys (OpenAI/Anthropic/Vertex AI, etc.) are a different case**: Gatekey does not control issuance of these — the org holds the actual API key from the provider's own console. True *automatic* rotation would require the provider to expose a key-issuance API, which most don't in a form suitable for unattended rotation. Phase 3 therefore ships **rotation reminders + a guided manual rotation flow** for provider keys (admin pastes the new key from the provider console; Gatekey validates it, then runs the same short overlap before retiring the old one), not silent automatic rotation. Revisit true provider-key automation per-provider if/when a given provider's API supports it — do not build this as a blanket promise across all providers.

### 3.7a Local Credential Sync for CLI Use
- Solves the other half of "no manual change required": getting a rotated **personal key** (2.5) onto the machine where the user's CLI actually runs, without a persistent background process and without re-fetching on every single command.
- A small local helper (a thin wrapper the user runs instead of invoking their AI CLI directly, or a shell-init hook) is authorized **once**, interactively (e.g., a device-code-style login), storing a refresh credential in the OS keychain (Keychain / Credential Manager / Secret Service) — never a long-lived static secret sitting in a plain file.
- On each CLI invocation, the helper checks a **local cache** (key value + a `valid_until` timestamp) before doing anything else:
  - If the cache is still valid, it's used immediately — **no network call**, no added latency, no daily-repeated fetching.
  - If the cache is missing or past `valid_until`, the helper calls a lightweight "get my current active key" endpoint (authenticated via the stored refresh credential), writes the result plus a fresh `valid_until` to the cache, and proceeds.
- Gatekey sets `valid_until` on that response to just past the key's *next scheduled rotation* (e.g., "06:00 tomorrow, org-local time") — since rotation always lands off-hours (see 3.7), the cache naturally expires once per day at most, and the very first CLI invocation of the next working day is guaranteed to fetch the freshly rotated key. This is what makes "called during the first call of the day" the normal, self-arising behavior rather than something that needs separate scheduling logic.
- If a key is force-revoked out-of-band (the compromise/immediate-revoke path above), a cached-but-now-invalid secret will get a clear auth rejection from the gateway on next use — the helper treats that specific rejection as a signal to invalidate its cache and re-fetch once, transparently, rather than surfacing a raw auth error to the user.
- This helper writes to whatever file/location the user's CLI is configured to read its key from — it does not need to understand or rewrite arbitrary CLI-specific config formats beyond that one file, keeping it small and tool-agnostic in principle (though the exact file path/format is CLI-specific and needs to be confirmed per tool — see Phase 2's open question about CLI wire-protocol compatibility).
- **Resolved (cache TTL when there's no off-hours anchor):** for an app/service key, or a personal key with no access-schedule and no org off-hours window configured, there's no natural idle boundary to set `valid_until` against — the cache falls back to a fixed, conservative TTL of **1 hour**. That's short enough to pick up an out-of-band rotation reasonably quickly without a natural daily boundary to lean on, and long enough that it's still a small fraction of normal CLI usage volume, not a per-command network call. The moment a schedule or off-hours window *is* configured for that key, the cache switches to the daily/off-hours-anchored behavior described above.
- **Resolved (per-OS scope):** build the helper on a cross-platform-safe credential storage abstraction (e.g., a `keyring`-style library with OS-specific backends) from day one rather than hand-rolling one OS's keychain integration first — the credential-storage differences between Keychain/Credential Manager/Secret Service are exactly the kind of thing such libraries already abstract, so there's little saved by deferring the others. This is real client-side engineering scope regardless (per the note above), and should be scheduled as such — not estimated as "just a config file write."

### 3.8 Scheduled Access Windows
- Restrict *when* a service-account key (and, by extension, the human/app using it) may successfully authenticate to the gateway — allowed days of week, allowed hours within a day, and an org-configurable holiday calendar (specific blocked dates even if they fall on an otherwise-allowed weekday). Purpose: prevent a company-provisioned credential from being used for personal use after hours, on weekends, or on company holidays.
- Configurable at three levels with the same most-specific-wins precedence already used for nested model policy (Phase 2 §2.3): **org-wide default** → **team override** (can only narrow, same non-loosening rule as team model restrictions) → **per-service-account override**.
- A request authenticated outside its allowed window is blocked with a clear, structured error (`outside_allowed_schedule`) — never a silent failure or a generic 403 — and the block itself is logged (Phase 3.1 audit trail), not just the successful requests.
- Time zone is explicit and org-configured (not inferred from request origin), since inferring it from IP/client would be unreliable and requests may originate from server-side apps with no meaningful "user location."
- **Emergency/on-call exception**: a Team Lead or Org Admin can grant a time-boxed override (e.g., "allow this key until 06:00 tomorrow") for legitimate off-hours work (incident response, on-call) without disabling the schedule restriction entirely — this must exist from day one, not as a follow-up, since a hard off-hours block with no override path will get disabled org-wide the first time it blocks a real incident response. **Resolved (who can grant, and is a reason required):** a Team Lead may grant an override only for their own team's service accounts; an Org Admin may grant one for any team's. A reason is **required**, not optional, and is written to the audit trail — this is a deliberate bypass of a security control, so unlike a rejection reason elsewhere in this doc (optional), there's no legitimate case for granting one with no record of why.
- **Resolved (default state):** scheduled access windows default to **off** org-wide, opt-in per team/service-account — consistent with how every other traffic-shaping default in this doc (rotation, and Phase 4's caching/failover) defaults to off rather than silently changing existing behavior the moment this phase ships.

## Out of Scope for Phase 3
- Cryptographic hash-chained audit ledger (Phase 5)
- Content-classification-aware *dynamic* routing beyond the basic rule in 3.4 (full version in Phase 5)
- Caching, rate limiting, failover (Phase 4)

## Non-Functional Requirements
- DLP scanning must not add more than ~50ms p99 latency for typical prompt sizes; scanning should be able to run async/best-effort for non-blocking policies (log-only mode) without holding up the request.
- Audit log storage must be separable from application data at the infra level (e.g., different retention/backup policy) since some customers will have longer legal-hold requirements for audit data than for usage data.
- Credential rotation must never produce a window with zero valid secrets for a service account — the old secret's validity and the new secret's validity must overlap, not merely abut, even accounting for clock skew between gateway instances in a multi-instance deployment. This overlap is now deliberately short (minutes, per 3.7) rather than long — it exists to absorb clock skew and genuinely in-flight requests, not to compensate for a user who hasn't yet noticed a rotation happened.
- The local sync helper (3.7a)'s cache-validity check must be fast enough to not be a perceptible delay on the common (cache-hit) path — target: negligible compared to the CLI's own startup time, since this runs on every invocation.
- Schedule-window enforcement (3.8) must add negligible latency to the auth path (target: same order of magnitude as the existing RBAC check's <10ms budget from Phase 2) — this is a simple time-range comparison against the resolved policy, not a scan.

## Success Criteria
- A pilot org's security/compliance reviewer can complete a vendor review using only the documentation and controls shipped in this phase, without requesting features that don't exist yet.
- DLP redaction is demonstrated end-to-end: a test prompt containing synthetic PII is redacted before reaching the provider, and the redaction event is visible in the audit log.
- At least one pilot org with an actual regional compliance requirement (e.g., EU data residency) configures and validates a residency rule.
- A service-account key completes at least one full automatic rotation cycle during the pilot with zero failed requests attributable to the rotation itself (validates the short overlap actually works under real traffic, not just in a test).
- At least one pilot org configures a scheduled access window and confirms both halves of it: an off-hours request is correctly blocked, and an on-call emergency override correctly and temporarily lifts that block.
- A pilot user with the local sync helper (3.7a) configured experiences a personal-key rotation with zero manual action: their first CLI invocation on the day after rotation succeeds using the new key, with no perceptible added delay and no error, and the helper is shown to only call Gatekey once that day (cache-hit on subsequent invocations), not on every command.

## Open Questions to Resolve Before Building
Every question originally listed here is now resolved inline above (3.2, 3.3, 3.6, 3.7a, 3.8) — see those sections for each decision and rationale, including rotation interval default (off) and access-schedule default (off), both of which were already effectively decided by the surrounding text and are now stated as such rather than left as open questions. None remain open for this phase.
