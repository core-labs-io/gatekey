# Known limitations

Gatekey errs on the side of disclosing what has *not* been verified, not
just what has. This page is organized by what you're trying to do, not by
when the code was built. Items closed by later work are removed (see
`CHANGELOG.md` for history).

## Deploying and operating

- **The console has no UI for granting org-wide roles.** `PATCH
  /v1/admin/users/{id}/org-role` exists and is audited, but making a user
  an Org Admin or Auditor is curl-only for now (see [sso.md](sso.md)).
  Team-level roles are fully manageable in the UI.
- **The admin credential is env-var-only by design.** There is deliberately
  no API that persists a new admin token (an unauthenticated "set admin
  token" endpoint would itself be a privilege-escalation hole) — the
  console's first-run screen only covers "connect your first provider";
  signing in with the already-provisioned `GATEKEY_ADMIN_TOKEN` is the de
  facto first step.
- **`cli-sync`'s device-code pending-auth state is in-process,
  single-worker only** — a login in flight when the backend restarts, or a
  multi-replica deployment routing the `/start` and `/poll` calls to
  different workers, will fail. Fine for the shipped single-container
  docker-compose deployment; needs a DB-backed store before horizontal
  scaling.
- **Budget period rollover is lazy/touch-based, not scheduled** (there is
  no cron/worker container). A boundary crossing is applied the next time
  *anything* touches the team: a gateway request, or just opening the
  team's page. A fully dormant team's numbers won't visibly roll over at
  midnight on the boundary — they roll over on next touch, computed
  correctly for however many periods elapsed.
- **Performance targets are designed-for, not load-tested.** The RBAC/
  policy-resolution overhead target (<10ms) adds only in-process lookups on
  the hot path but has not been verified under real concurrency; the
  synchronous DLP scan (<50ms p99 target) measured ~10–19ms warm on the
  build machine only; Redis cache-lookup overhead measured ~30–36ms in a
  Docker Desktop/WSL2 sandbox where the round-trip itself dominated —
  re-measure against your own Redis topology before treating that NFR as
  unmet.

## Budgets, pricing, and cost accuracy

- **Provider pricing figures** (`backend/src/gatekey/providers/pricing.py`)
  are standard published rates as documented at implementation time and
  could not be re-verified live before shipping — **confirm them against
  each provider's current pricing page before relying on budget
  enforcement**, and update the table (a code change + redeploy) if a
  provider has repriced. Every entry records its `as_of` date and `source`
  URL for exactly this reason.
- **Budget enforcement is check-before-call**: the gate runs before the
  provider call and the charge is recorded after it, so truly simultaneous
  in-flight requests can briefly overshoot a member's budget, bounded by
  whatever is concurrently in flight at the cutoff moment. The counter
  itself is updated atomically, so the recorded total is exact.
  (Assignment-time ceiling allocation, by contrast, is fully serialized
  under a row lock and cannot over-allocate.)
- **`rollover` compounds unspent budget indefinitely by design**: a
  member's unused allowance is added onto their budget for the next period
  and keeps compounding if left unspent. This is the documented consequence
  of opting into rollover — it is why `reset` is the default — not a bug.
  Rollover credits are deliberately not re-checked against the team
  ceiling.
- **Costs are USD-only.** The org currency setting and FX columns exist for
  forward compatibility, but every rate is `1` and every cost is USD.
- **Ollama-routed requests are priced at $0.00** — there is no per-token
  invoice for a self-hosted target to charge against. Do not treat Ollama's
  dashboard spend figure as your true cost of running those models.
  Streaming Ollama token accounting also depends on Ollama's
  OpenAI-compatible layer honoring `stream_options.include_usage`, which
  was not verified against a live instance.
- **Self-hosted endpoint cost is an estimate** — configured GPU-hour rate ×
  wall-clock request latency, ignoring queueing, multi-tenant GPU sharing,
  and cold starts. It is labeled "estimated" everywhere it appears and is
  not invoice-grade the way BYOK token pricing is.
- **Vertex AI (Gemini) streaming usage accuracy** is the least formally
  guaranteed of the providers' streaming-usage contracts — verify against a
  real Vertex streaming response if precise per-request billing accuracy
  for Gemini streaming matters to you.

## SSO, provisioning, and alerting

- **Only Keycloak has been exercised as a live SSO IdP.** The OIDC flow is
  standard and provider-agnostic, but no Okta/Azure AD/Google Workspace
  round-trip was possible in the build environment. See
  [sso.md](sso.md).
- **SCIM has not been exercised against a real IdP's live SCIM client** —
  endpoint shapes follow the SCIM 2.0 RFC and are covered by integration
  tests against a real Postgres, but no live provisioning round-trip has
  been run.
- **Email threshold alerts are implemented but unverified-live.** The SMTP
  notifier is built and unit-tested but has never delivered to a real
  mailbox. Test against your own relay before relying on it; webhook
  delivery *is* the verified alert path.
- **Automatic service-account key rotation has no propagation path to the
  consuming app.** Rotation mints a new secret, keeps the previous one
  valid for a short overlap window, and notifies — but unlike personal keys
  (which have `cli-sync`'s "fetch my current key" mechanism), nothing
  updates a server-side app's stored `gk_sk_...` secret. Either disable
  automatic rotation for keys whose consuming app you can't update in
  time, or treat the overlap window as your update deadline.

## DLP and content classification

- **Presidio's PII detection coverage is exactly what its four
  pattern-based recognizers (SSN, credit card, email, phone) plus your own
  custom regex patterns provide** — not independently audited for
  false-negative/false-positive rates. Validate against representative
  traffic before relying on it for a real compliance requirement.
- **The `source_code`, `financial_data`, and `legal` classifiers are
  regex/keyword heuristics**, not ML/embeddings-based — same validation
  caveat as above. Gatekey does not call out to Microsoft Purview's or
  Google DLP's classification APIs; the sensitivity-label mapping only
  trusts a caller-supplied label string your own tooling has already
  computed (and it only affects routing category assignment — the
  underlying DLP redaction/block scan always runs regardless).
- **Inbound (provider response) DLP scanning is not implemented.** The
  `scan_inbound_responses` toggle exists in the schema/API for forward
  compatibility but is rejected (422) if set to `true`. Only outbound
  (prompt) scanning is functional.
- **Prompt-log retention is enforced and always-on, with no "never purge"
  option** (unlike audit retention, which defaults to never). `usage_logs`
  and `dlp_scan_results` rows (including any raw flagged PII substrings, if
  that opt-in is enabled) are hard-deleted after 30 days by default. Raise
  `log_prompt_retention_days` on the Compliance Settings screen before that
  window elapses if you need longer — there is no undo after a purge. See
  `backend/docs/compliance/data-handling-policy.md` §3.2.
- **Residency and access-schedule narrowing is validated at write time and
  re-checked cumulatively at read time** (org AND team AND, for schedules,
  per-key layers all evaluated on every request). If you're auditing this
  area, verify against `services/residency.py`'s `resolve_residency` and
  `services/access_schedules.py`'s `resolve_access_schedule_decision`
  directly, not this note alone.

## Rate limiting, caching, and failover

- **Redis is required for rate limiting, caching, and shared state** —
  deployments without it continue to work minus those features (failover
  does not depend on Redis). Enable with `docker compose --profile cache
  up` **and** `GATEKEY_REDIS_URL=redis://redis:6379/0` in `.env` — the
  profile alone does nothing.
- **A token-only rate-limit rule has unbounded burst overshoot.** The
  atomic per-minute concurrency-safety bound only holds when
  `requests_per_min` is also configured on the same rule; set one alongside
  any `tokens_per_min` rule. This is a cost-shaping gap, not a budget
  bypass — the hard budget cutoff always applies.
- **Rate-limit queue depth (for queue-and-retry rules) is not tracked or
  exposed** — the live path polls in-process rather than using a persisted,
  inspectable queue.
- **Caching is exact-match only** (no semantic/near-duplicate caching).
  `POST /v1/admin/cache/clear` performs a real delete — functionally
  equivalent to the design's "soft clear", just not reversible.
- **Provider health checks are active synthetic checks**, not
  passive/traffic-derived — a scheduled job makes a real test request to
  each provider (using the key's real decrypted credential) every 5
  minutes.
- **A degradation policy targeting a custom or self-hosted model name fails
  rather than degrades** — the degradation-target resolution path doesn't
  thread the custom-model/self-hosted caches through yet. Fails closed
  (clean error, never a misroute).

## Audit trail and tamper evidence

- **Audit append-only is application-level discipline**, not yet a
  database-level guarantee: service code only ever inserts audit rows, but
  there is no DB trigger blocking `UPDATE`/`DELETE` from a direct DB
  connection. The hash chain (below) is the tamper-*evidence* layer.
- **The hash-chained ledger cannot detect a deleted tail.** Tampering with
  or reordering an existing entry is always caught by `GET
  /v1/admin/audit/verify`, but someone with raw database write access who
  deletes the most-recent N entries outright leaves a chain that still
  verifies as internally consistent. Don't treat an "intact" result as
  proof nothing was ever deleted from the tail. This is disclosed in the
  admin console too (tooltip on the integrity badge).
- **No external anchoring/timestamping integration** — the chain is
  in-database only in this release.
- **Hash chain and finite audit retention are mutually exclusive** — full
  historical verifiability or automatic purging, not both (deleting an
  entry structurally breaks a chain). Enforced at both the DB and API
  layer, and in the UI.

## Self-hosted and custom models

- **Self-hosted models support chat completions only** — not
  `/v1/completions` or `/v1/embeddings`.
- **Custom model registry keeps you in the driver's seat, deliberately:**
  no auto-discovery from a provider's list-models API (the admin types the
  native model id by hand), no org-vs-team scoping, no bulk import, no
  tiered pricing, no scheduled re-verification, no price-staleness
  detection — one flat model at a time with a manually maintained rate.
  Ollama is out of scope here (register those models under a Self-Hosted
  endpoint instead).
- **A custom model shadowed by a later Gatekey registry update is flagged,
  never auto-fixed**: the static registry entry always wins at request
  time, an `ERROR` log fires at startup, and the console shows a
  "Shadowed by registry update" badge — the admin renames or removes it
  manually.

## Shadow AI discovery

- **Detection is via SASE/proxy-log ingestion only** — no browser extension
  in this release, and no true inline network blocking: "enforcement mode"
  is a notification email and/or an outbound webhook your own SASE/SOAR
  tooling can act on, not Gatekey intercepting traffic (architecturally
  impossible from passive log ingestion).

## Drift detector

- **Drift flagging is threshold-based** (fixed percentage deviations
  against a rolling 7-run baseline), not a statistical hypothesis test;
  refusal detection and output-similarity are deterministic
  keyword/text-metric heuristics, not ML. The canary suite is a fixed,
  code-seeded set of 5 prompts, not admin-authorable. Thresholds are
  global; only per-model enable/disable is configurable.

## Project maturity

- **No CI pipeline, published container images, or production deployment
  guide yet** — these are the next planned tranche of work. Today's
  deployment path is `git clone` + docker-compose, and upgrades are
  `git pull && docker compose up --build`.
- **Feature prioritization has not yet been validated with real design
  partners** — the five differentiating features (hash-chained ledger,
  drift detector, self-hosted governance, content-aware routing, shadow AI
  discovery) were all built ahead of that signal. Feedback from a real
  pilot is genuinely wanted; see [CONTRIBUTING.md](../CONTRIBUTING.md).
