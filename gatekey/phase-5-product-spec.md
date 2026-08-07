---
title: Phase 5 — Differentiators — Buildable Spec
status: draft
last_updated: 2026-08-06
source_docs:
  - phase-5-differentiators.md
  - ui-requirements-admin.md (§6, §7, §10.3, §12)
  - 00-overview.md
  - backend/src/gatekey/db/models/audit_entry.py
  - backend/src/gatekey/db/models/content_aware_rule.py
  - backend/src/gatekey/providers/pricing.py, providers/ollama.py, providers/registry.py, providers/base.py
  - backend/src/gatekey/services/scheduler.py, services/model_policy.py, services/dlp.py
  - phase-4-product-spec.md (structural template)
author: product-owner (sub-agent)
consumed_by: architect
---

# Phase 5 — Differentiators — Buildable Spec

This translates `phase-5-differentiators.md` §5.1–§5.5 into user stories and
testable acceptance criteria. The phase doc's own "Resolved" items (5.2's
no-external-anchoring decision, 5.1's SASE-log-first default) and the
orchestrator's additional resolutions (browser extension deferred; optional
enforcement is opt-in but built now, not deferred; build order 5.2→5.4→5.5→5.3→5.1;
RBAC role set) are **not re-litigated here** — they are operationalized into
ACs. Sections below are ordered by build order (lowest integration risk
first), but AC numbers keep the phase doc's original subsection numbers
(`AC5.2.x`, `AC5.4.x`, etc.) for traceability back to `phase-5-differentiators.md`
and the UI doc, not build order.

Every genuinely new ambiguity this translation surfaced (not resolved by the
phase doc or the orchestrator's brief) is called out inline where it's
decided, and consolidated in §9 for the security-reviewer's attention.

---

## 0. Non-Negotiable Decisions Carried In (not re-decided here)

1. **5.1 Shadow AI** ships against SASE/proxy-log ingestion as the v1
   detection mechanism. A browser extension is explicitly deferred, not a
   Phase 5 deliverable. Findings surface as a detection/awareness report
   first; an opt-in enforcement mode is built in this phase (per the
   orchestrator's explicit instruction to build, not defer, it) but defaults
   off and requires explicit org opt-in.
2. **5.2 Hash-chained ledger** ships as an in-database chain only. No
   external anchoring/timestamping service integration in this phase — that
   is an explicitly deferred fast-follow gated on a real regulated-industry
   design partner asking for it.
3. **5.4 Drift detector** cost must be tracked separately from
   user-attributable usage and must not consume meaningful budget — this is
   a hard, testable NFR (see §10), not a "nice to have."
4. **5.5 Self-hosted governance** extends the real provider abstraction
   (`providers/registry.py`, `providers/base.py`) and generalizes the
   existing Phase 1 Ollama $0.00 pricing shortcut into a real configurable
   cost basis — it does not invent a parallel provider system.
5. **Build order**: 5.2, 5.4 (lowest integration risk) → 5.5 → 5.3 → 5.1
   (highest lift/uncertainty). This doc is sequenced accordingly; the
   architect/orchestrator may still resequence based on real design-partner
   demand signal per the phase doc's own success criteria.
6. **RBAC role set** (per `ui-requirements-admin.md` §1 and the codebase's
   `api/deps.py` role dependencies): **Org Admin**, **Team Lead**, **Member**,
   **Auditor**. Org-level dependencies (`require_admin`,
   `require_admin_or_auditor`, `require_role("org_admin","auditor")`) and
   team-scoped ones (`require_team_role("team_lead","member",
   org_admin_bypass=True)`) are the only RBAC primitives this spec uses —
   no new role is introduced. Every Phase 5 admin screen/endpoint below is
   assigned one of these per the precedent Phase 3 (compliance surfaces =
   Org Admin write / Auditor read) and Phase 4 (team-scoped operational
   surfaces = Org Admin + Team Lead) established.
7. **Database layer**: extend the existing Postgres schema from Phases 1–4.
   No new database vendor. `audit_entries` gets additive columns only (per
   its own module docstring's forward-compat note) — never reshaped.
   `content_aware_rules` keeps its existing `(org_id, category)` schema —
   new categories are new rows, not a schema change.

---

## 1. §5.2 Cryptographically Hash-Chained Audit Ledger (build first)

**User stories**

- As an Org Admin, I can enable a tamper-evident hash chain over the audit
  log, so any retroactive modification to a historical entry becomes
  detectable.
- As an Auditor, I can run a verification check (via the admin console or
  API) that confirms the chain is intact from genesis to present, or tells
  me exactly which entry broke it.
- As an Org Admin or Auditor, I can export the audit log with chain data
  included, so a third party can independently re-verify it offline.

**Acceptance criteria**

- AC5.2.1 — `audit_entries` gains three additive columns (migration only,
  per the table's own "additive migration" forward-compat note — no
  reshape): `chain_hash` (text, nullable until backfilled), `prev_hash`
  (text, nullable — `NULL` only at a chain's true genesis row), `chain_seq`
  (bigint, per-`org_id` monotonically increasing, unique per
  `(org_id, chain_seq)`). `chain_seq` exists because chain order must be
  unambiguous and `created_at` alone cannot guarantee strict ordering under
  concurrent writes to the same org.
- AC5.2.2 — A new org-level toggle `chain_enabled` (boolean, default
  `false`) on `compliance_settings`. Chain columns are only populated going
  forward once `chain_enabled = true`; turning it on for the first time
  computes a genesis row from the org's current audit-entry tail (see
  AC5.2.6 for backfill of full pre-existing history, which is a superset of
  this).
- AC5.2.3 — `services.audit.write_audit_entry` (the sole INSERT path per
  the table's own docstring) computes, when `chain_enabled = true`:
  `chain_hash = SHA256(prev_hash_or_empty_string || canonical_json({id,
  org_id, actor_label, action, target_type, target_id, old_value, new_value,
  source_ip, created_at}))`, where `prev_hash` is the `chain_hash` of the
  immediately preceding row for the same `org_id` (by `chain_seq`).
  **Concurrency**: the read of "current tail's `chain_hash`" and the INSERT
  of the new row must be serialized per `org_id` (e.g. `SELECT ... FOR
  UPDATE` on the tail row, or an advisory lock keyed on `org_id`) so two
  concurrent audit writes cannot both compute `prev_hash` from the same
  stale tail and fork the chain. This is a new requirement this feature
  introduces to the existing write path (see §9, judgment call #1).
- AC5.2.4 — Verification endpoint `GET /v1/admin/audit/verify` (Org Admin +
  Auditor, via `require_admin_or_auditor`): walks every row for the org in
  `chain_seq` order, recomputes each `chain_hash` from its stored fields and
  compares against the stored value, and compares each row's `prev_hash`
  against the prior row's stored `chain_hash`. Returns
  `{"status": "intact", "entries_verified": N}` or
  `{"status": "broken", "broken_at_entry_id": ..., "broken_at_chain_seq": ...,
  "expected_prev_hash": ..., "actual_prev_hash": ...}` — the failure
  response must name the specific entry (id + chain_seq), never a bare
  boolean, per the UI doc's explicit requirement (§10.3: "the failure
  message must name the entry").
- AC5.2.5 — Admin console: Security & Compliance → Audit Log tab gains the
  hash-chain integrity badge and "Verify now" button (`ui-requirements-admin.md`
  §10.3), calling AC5.2.4's endpoint synchronously. Before `chain_enabled`
  is turned on, the badge reads "Hash chain not enabled — [ Enable ]"
  instead of a verified/broken state (empty-state rule, same convention as
  the Phase 1 dashboard's tile-hiding rule).
- AC5.2.6 — Migration backfill: when `chain_enabled` is turned on for an
  org with pre-existing `audit_entries` rows, a one-time backfill pass
  computes `chain_seq`/`prev_hash`/`chain_hash` for **every existing row**,
  ordered by `(created_at, id)` (deterministic tie-break), establishing the
  org's true historical genesis at its actual first-ever row — not a fresh
  genesis at enable-time. This maximizes the feature's value (full history
  becomes verifiable, not just entries written after Phase 5 ships).
- AC5.2.7 — **Chain/purge mutual exclusivity**: enabling `chain_enabled`
  requires `compliance_settings.audit_retention_days` to be `NULL` (no
  scheduled purge configured) — the admin UI blocks enabling the chain
  while a finite retention window is configured (and vice versa: setting a
  finite `audit_retention_days` while `chain_enabled = true` is blocked),
  with an explicit inline reason each way. `run_audit_purge_if_due`
  (`services/scheduler.py`) gains a guard: it is a no-op whenever
  `chain_enabled = true` for that org, regardless of `audit_retention_days`.
  Rationale: deleting a row structurally breaks a hash chain (per
  `audit_entry.py`'s own forward-looking note); this spec resolves that by
  making the two features mutually exclusive rather than inventing
  purge-aware re-genesis bookkeeping nobody has asked for (see §9, judgment
  call #2).
- AC5.2.8 — Export (Security & Compliance → Audit Log → "Export") includes
  `chain_hash`/`prev_hash`/`chain_seq` columns in CSV/JSON output when
  `chain_enabled = true`, so a downloaded export is independently
  re-verifiable offline by a third party (matches the phase doc's stated
  "demonstrate this was produced exactly as logged" target use case).
- AC5.2.9 — No external anchoring (timestamping service) in this phase —
  carried forward as resolved, not rebuilt. `PricingEntry`-style "as of"
  citation pattern is not applicable here; this AC exists only to record
  the boundary explicitly for QA.
- AC5.2.10 — Drift alerts (§2 below) that are exported to the audit log
  write a normal `AuditEntry` row (action `"drift.alert_exported"`) and are
  therefore themselves chained — no special-casing needed.

**Deferred / explicitly out of scope for this section**

- External anchoring / third-party timestamping (resolved: fast-follow).
- Multi-org chains (single-org deployment throughout Gatekey to date; a
  per-org chain design is still correct once multi-org ships, no rework
  needed).
- Real-time chain-break alerting/polling — verification is on-demand
  ("Verify now" / API call) only, not a background watcher in this phase.
- Re-genesis bookkeeping to allow purge + chain to coexist (resolved:
  mutually exclusive instead).

---

## 2. §5.4 Provider Drift Detector (build second)

**User stories**

- As an Org Admin, a fixed, cheap canary prompt suite runs daily against
  every actively-used model, so I get an early warning if a provider
  silently changes model behavior behind a stable API/version name.
- As an Org Admin or Auditor, I can see which models have drifted, what
  metric changed and by how much, and export that alert to the audit log
  for compliance review.
- As an Org Admin, canary-run cost never shows up as user-attributable
  spend and never meaningfully affects the org's budget.

**Acceptance criteria**

- AC5.4.1 — New table `canary_prompts`: `id`, `prompt_text`, `label`
  (e.g. `"factual"`, `"creative"`, `"refusal_probe"`), `max_tokens` (small,
  capped — default 50), `enabled`. **Fixed, code-seeded set of 5 prompts in
  v1** (same "pure, hand-curated, in-code" posture as `providers/pricing.py`'s
  `PRICING_TABLE`) — admin can view them (read-only) on the Drift Detector
  tab but cannot author/edit new ones via UI in this phase (see §9,
  judgment call #4).
- AC5.4.2 — New table `canary_baselines`: `model`, `prompt_id`, established
  from the first 7 days of canary runs after a model is first seen as
  actively used (rolling average of `latency_ms`, `refusal_detected` rate,
  and a reference `output_text` for similarity comparison), `established_at`.
- AC5.4.3 — New table `canary_runs`: `id`, `model`, `prompt_id`, `run_at`,
  `output_text` (canary prompts are synthetic, org-controlled, non-user
  content — storing full output text here is explicitly fine and does not
  conflict with `usage_logs`'s "no raw prompt/response text" rule, which is
  about real user traffic), `latency_ms`, `refusal_detected` (bool),
  `similarity_score_vs_baseline` (float 0–1), `cost_usd`, `is_canary = true`
  (always).
- AC5.4.4 — Refusal detection: a keyword/regex heuristic (e.g. "I cannot
  help with", "I'm not able to", "I won't provide") — not an ML classifier,
  consistent with this codebase's existing regex-based DLP approach (see
  §9, judgment call #6).
- AC5.4.5 — Output similarity: a lightweight, deterministic, in-process
  text-similarity metric (e.g. token-level cosine/Jaccard similarity) —
  **no external embeddings-API call**, to avoid adding cost, a new provider
  dependency, and non-determinism to a feature whose own NFR is "must not
  consume meaningful budget" (see §9, judgment call #5).
- AC5.4.6 — Drift flagging is **threshold-based over a rolling window**
  (e.g. last 7 daily runs vs. baseline), not a true statistical hypothesis
  test: latency flagged if rolling average deviates >50% from baseline;
  refusal rate flagged if it rises >20 percentage points vs. baseline;
  similarity flagged if average drops below 0.7. This is an explicit
  simplification of the phase doc's "statistically significant" language —
  flagged for extra QA/security scrutiny (see §9, judgment call #4... see
  full list, item covering "statistically significant").
- AC5.4.7 — New table `drift_alerts`: `id`, `model`, `metric`
  (`"latency"|"refusal_rate"|"output_similarity"`), `baseline_value`,
  `observed_value`, `delta_pct`, `detected_at`, `status`
  (`"open"|"exported_to_audit"`). Alert text is plain-language with the
  percentage delta (ui doc §12.2: "states *what* changed... in plain
  language with the percentage delta").
- AC5.4.8 — Scheduled job `run_drift_canary_if_due` follows the **exact
  established `services/scheduler.py` `*_if_due` pattern**
  (`run_provider_key_health_check_if_due`'s in-memory `app.state` last-run
  marker + interval constant), invoked from `run_scheduler_loop` alongside
  the existing rotation/purge/health-check jobs. Interval:
  `DRIFT_CANARY_CHECK_INTERVAL_SECONDS = 24 * 60 * 60` (daily). Canaries
  only run against models with ≥1 real (non-canary) `usage_logs` request in
  the last 7 days for that org — "actively used" per this definition (see
  §9, minor judgment call).
- AC5.4.9 — **Cost separation (hard NFR, must be checkably real)**: canary
  run cost is computed via the normal `pricing.compute_cost()` /
  `compute_self_hosted_cost()` path (whichever applies to the model) and
  written **only** to `canary_runs.cost_usd`. `record_usage_charge()` is
  **never called** for a canary request — no `usage_logs` row is written,
  no team/user/org budget ceiling is touched. Acceptance test: after a
  scheduler tick runs canaries, `SELECT count(*) FROM usage_logs WHERE ...`
  referencing that request is zero, and the org's `current_spend_usd`
  figures are unchanged; canary spend is only visible via
  `SUM(canary_runs.cost_usd)`.
- AC5.4.10 — "Must not consume meaningful budget" is satisfied by the fixed
  5-prompt, low-`max_tokens` suite (AC5.4.1) — this does not mean zero real
  provider cost (a live inference call against a real BYOK provider key
  necessarily costs something); it means the canary suite's cost floor is
  small and bounded by construction, and is never charged against any
  budget ceiling (AC5.4.9). Document this distinction in the admin UI
  ("Canary suite cost this month: $X — tracked separately, never billed to
  team/user budgets").
- AC5.4.11 — Admin console Drift Detector tab (`ui-requirements-admin.md`
  §12.2) — per-model status/trend table, expandable alert detail with
  plain-language delta, "View canary history," and "Export to audit log"
  (writes an `AuditEntry` row per AC5.2.10). RBAC: Org Admin configures
  per-model canary enable/disable and thresholds; Org Admin + Auditor view
  alerts/history/export (compliance-relevant per the phase doc: "a drift
  event may be relevant to compliance review").

**Deferred / explicitly out of scope for this section**

- Admin-editable canary prompt authoring UI (fixed set only, v1).
- Embeddings-based/ML output-similarity scoring (deterministic in-process
  metric only, v1).
- True statistical significance testing (t-test/control-chart-style
  variance modeling) — threshold-based only, v1, flagged for review.
- Per-team drift views (models aren't meaningfully team-scoped for this
  purpose — org-wide only).

---

## 3. §5.5 Unified Governance for BYOK + Self-Hosted OSS Models

**User stories**

- As an Org Admin, I can register a self-hosted inference endpoint
  (vLLM, Ollama, or any OpenAI-compatible self-hosted server) with a name,
  base URL, and a GPU-hour cost basis, so it's governed under the exact
  same policy/budget/audit plane as any BYOK provider.
- As an Org Admin, I can allow/deny a self-hosted model in the org/team
  model policy exactly like any BYOK model.
- As an Org Admin, I can see estimated cost, request volume, and latency
  for each self-hosted endpoint, clearly labeled as an estimate, not an
  invoice figure.

**Acceptance criteria**

- AC5.5.1 — New table `self_hosted_providers`: `id`, `org_id`, `name`
  (unique per org, e.g. `"vllm-internal-llama3"`), `base_url`,
  `bearer_token` (encrypted with the same ciphertext/nonce/auth_tag
  envelope `provider_keys` already uses — never plaintext at rest),
  `cost_basis_per_gpu_hour` (`Numeric(10,4)`, > 0), `verified` (bool,
  default `false`), `models` (JSONB list of model-id strings this endpoint
  serves, admin-declared at registration), `created_at`, `updated_at`. A
  **new table**, not an overload of the existing `provider_keys`/`"ollama"`
  enum slot — this supports multiple independently-named self-hosted
  endpoints (matching the UI mock's "Self-Hosted Models" card showing a
  named list, e.g. `vllm-internal-llama3`), and needs no migration to the
  `provider_name_enum` Postgres type since it's a separate table (see §9,
  judgment call #9).
- AC5.5.2 — Registration/inference reuses the **existing** OpenAI-compatible
  client code in `providers/ollama.py` (`create_chat_completion`,
  `stream_chat_completion`, `OllamaValidator`) — vLLM and Ollama both expose
  an OpenAI-compatible `/v1/chat/completions` surface, so no new
  provider-client module is required; those functions are parameterized to
  accept any `base_url`/`bearer_token` pair from `self_hosted_providers`,
  not only ones literally named "ollama". (Whether to rename the module for
  clarity is an architect naming call, not a product-spec blocker.)
- AC5.5.3 — "Not verified" badge (ui doc §6) until a live health probe
  (`OllamaValidator.validate()`'s `GET /v1/models` call, reused as-is)
  succeeds; re-verification is manual (admin re-triggers from the Providers
  screen) in v1 — **not** wired into the Phase 4 5-minute
  `run_provider_key_health_check_if_due` job, which is scoped to
  `provider_keys` backup groups only. Extending continuous health polling
  to self-hosted endpoints is deferred.
- AC5.5.4 — Chat-completions only, v1 (inherits the existing Ollama-path
  constraint noted in `providers/ollama.py`'s module docstring: "Chat only —
  no `create_completion()`/`create_embeddings()` this pass"). Self-hosted
  models are not routable for `/v1/completions` or `/v1/embeddings`.
- AC5.5.5 — New process-local `SelfHostedModelRouteCache` (same
  whole-snapshot-replace pattern as `ModelPolicyCache`/`ContentAwareRuleCache`
  — one of this codebase's established conventions, applied here for
  consistency), warmed at startup from every verified `self_hosted_providers.models`
  entry and refreshed on register/edit/remove/re-verify. `resolve_route()`
  gains a fallback: when a requested model id is not a static
  `MODEL_REGISTRY` key, check this cache; if found and its owning
  `SelfHostedProvider.verified = true`, route to it. This is a real
  architectural extension of the previously all-static routing pipeline —
  flagged for architect sizing (see §9, judgment call #11).
- AC5.5.6 — Org/team model-access policy (`resolve_model_access` in
  `services/model_policy.py`) treats a self-hosted model id string exactly
  like any `MODEL_REGISTRY` key — addable/removable from org baseline and
  team-restriction allow-lists with no special-casing, and referenceable in
  a content-aware-routing category's `allowed_models` (§4 below).
- AC5.5.7 — New function `compute_self_hosted_cost(cost_basis_per_gpu_hour,
  *, wall_clock_latency_seconds) -> Decimal`, formula:
  `cost_basis_per_gpu_hour * (wall_clock_latency_seconds / 3600)`. Used
  instead of `pricing.compute_cost()` for self-hosted-model requests; the
  result is written to `usage_logs.cost_usd` the same as any other request
  (budgets, degradation, dashboards all work unmodified because the
  normalization lands in the same column). This formula is a rough proxy
  (ignores queueing delay, multi-tenant GPU sharing, cold start) — the
  phase doc specifies only "configured GPU-hour rate," not an estimation
  method, so this is an explicit v1 interim choice (see §9, judgment call
  #10). The admin UI must visibly label self-hosted cost figures as
  "estimated," distinct from BYOK providers' invoice-grade token pricing.
- AC5.5.8 — `usage_logs` gains a nullable `self_hosted_provider_id`
  (FK, `SET NULL`) alongside using `provider = "self_hosted"` (the existing
  `provider` column is a plain string, not the `provider_name_enum` — no
  enum migration needed) so self-hosted traffic is filterable/exportable in
  the Dashboard exactly like a BYOK provider's traffic. Budgets,
  degradation, rate limiting, DLP, residency, and audit logging all apply
  identically — self-hosted is just another provider in the one pipeline,
  never a bypass path.
- AC5.5.9 — Admin console: Providers screen's "Self-Hosted Models" card (ui
  doc §6) for register/edit/remove (Org Admin only). Differentiators →
  Self-Hosted Governance tab (ui doc §12, "a thin cross-link... surfaces the
  cost-normalization audit view") shows, per endpoint: total requests,
  total estimated cost, average latency, and the "estimated, not an
  invoice" disclosure (Org Admin + Auditor, read-only for Auditor). Model
  Policy → Static Allow/Deny tab's provider-grouped checklist gains a
  "Self-Hosted" group sourced from `self_hosted_providers.models`.

**Deferred / explicitly out of scope for this section**

- Real GPU-utilization telemetry integration (e.g. scraping
  nvidia-smi/Prometheus metrics from the self-hosted cluster) — v1 uses the
  latency-proxy formula only; a fast-follow if invoice-grade accuracy is
  requested.
- Multi-key/failover support for self-hosted endpoints (Phase 4's
  backup-group mechanism stays `provider_keys`-scoped in this phase).
- Auto-discovery of a self-hosted server's served-model list via its own
  `/v1/models` endpoint — v1 requires the admin to type the model id list
  manually at registration.
- Streaming usage-token accounting nuances beyond what Ollama already has
  (unchanged from Phase 1's existing unverified-streaming-usage flag in
  `providers/ollama.py` — not re-litigated here; cost accounting for
  self-hosted doesn't depend on token counts anyway, per AC5.5.7).

---

## 4. §5.3 Content-Classification-Aware Dynamic Routing

**User stories**

- As an Org Admin, I define which models are allowed for each content
  category (PII, source code, financial data, legal, general); the router
  enforces this automatically per request, without anyone maintaining a
  manual per-team allow list.
- As an Org Admin, if my org already applies its own sensitivity labels
  (e.g. via Microsoft Purview or Google DLP) to outbound requests, I can
  configure Gatekey to trust that pre-set label for a category instead of
  re-running its own classifier for that category.

**Acceptance criteria**

- AC5.3.1 — `services/dlp.py`'s scan pipeline is generalized from a single
  `pii_detected: bool` result to a `category_findings: frozenset[str]` (the
  set of triggered categories for this request), computed as:
  - `"pii"` — unchanged (existing Presidio + custom-regex engine).
  - `"financial_data"` — **new built-in patterns added to the same DLP
    regex/Presidio engine** (bank account/routing numbers, IBAN, SWIFT/BIC,
    and keyword-proximity rules like currency amounts near "revenue",
    "EBITDA", "wire transfer") — reuses the existing scan engine rather than
    building a parallel one.
  - `"source_code"` — a **new, separate lightweight heuristic** (not part
    of the DLP regex engine, since "redact" doesn't make sense for code):
    code-fence markers, brace/semicolon density, and common
    keyword-density signals (`def`, `class`, `import`, `function`, `{`,
    `};`) across common languages.
  - `"legal"` — a **new keyword/regex heuristic** (e.g. "attorney-client
    privileged", "NDA", "litigation", statute/case-citation patterns).
    Unlike `source_code`/`financial_data`, `legal` has no existing
    schema-scaffolding precedent in `content_aware_rule.py` — flagged as
    the least-grounded of the four positive categories, for extra QA
    scrutiny (see §9, judgment call #12).
  - `"general"` — **not a positive-detection category**: it is the
    default/fallback bucket for any request that triggers none of the
    above, matching the UI mock's "General → all allowed models" row. No
    classifier logic needed for it.
- AC5.3.2 — `resolve_content_classification` (services/model_policy.py) is
  generalized from a `pii_detected: bool` parameter to
  `category_findings: frozenset[str]`. For every **enabled**
  `content_aware_rules` row whose `category` is in `category_findings`, the
  effective allowed-models set for this request is the **intersection** of
  every matched category's `allowed_models`. If a request matches multiple
  enabled categories with disjoint allowed-models sets, intersection may be
  empty — in that case the request is blocked (`blocking_layer =
  "content_classification"`), same shape as the existing single-category
  block path. This ordering is unchanged: still runs *after* the static
  org/team baseline (`check_model_policy`), never re-enabling a
  statically-blocked model.
- AC5.3.3 — `content_aware_rules` gets real rows (not just persisted-but-
  inert ones) seeded for `"source_code"`, `"financial_data"`, and (new)
  `"legal"` — same `(org_id, category)` composite-PK schema, no migration
  needed beyond seeding rows an admin can then enable/configure, per the
  table's own docstring ("category is deliberately text... only the
  resolver function needs new category-handling logic").
- AC5.3.4 — Admin console: Model Policy → Content-Aware Routing tab (ui doc
  §7) — category table now functionally enforces `source_code`,
  `financial_data`, `legal` rows (previously inert), plus the existing
  `pii` row. A category with zero `allowed_models` shows the warning badge
  (already specified) meaning that category's traffic is blocked entirely
  once enabled — unchanged behavior, now real for all four categories.
- AC5.3.5 — **Sensitivity-label short-circuit** (operationalizing the
  orchestrator's framing choice — see §9 for why this framing itself is
  flagged): a new table `sensitivity_label_mappings`: `id`, `org_id`,
  `external_label` (free text, e.g. `"Microsoft Purview: Highly
  Confidential"`), `gatekey_category` (matches a `content_aware_rules.category`
  value). A request may optionally carry a pre-set label via header
  `X-Gatekey-Sensitivity-Label` (or an equivalent request body field). If
  the label value matches a configured `external_label`, that request is
  treated as already classified into `gatekey_category` **without** running
  Gatekey's own classifier for that specific category (trusting the
  enterprise's own upstream labeling) — but Gatekey's classifiers still run
  for every *other* category not covered by the pre-set label. An
  unrecognized label value is silently ignored (falls through to Gatekey's
  own classifiers for all categories) — never a hard error, so a caller
  supplying a label Gatekey doesn't yet know about is never broken.
- AC5.3.6 — Admin console: Model Policy → Content-Aware Routing tab's
  "Classification source" control (ui doc §7 mocks this as an exclusive
  radio group: Gatekey built-in / Purview / Google DLP) is reframed as
  **additive, not exclusive** — Gatekey's own classifier always runs as the
  fallback; the radio group becomes a `sensitivity_label_mappings`
  management table (add/edit/remove external-label → category mappings)
  instead of a single either/or choice. This is a UI-doc reinterpretation,
  flagged explicitly (see §9).
- AC5.3.7 — All new classifiers (`financial_data`, `source_code`, `legal`)
  are regex/keyword/heuristic-based — no new ML-serving dependency, no
  embeddings-API call, consistent with the codebase's existing DLP engine
  and the Drift Detector's own no-embeddings choice (§2, AC5.4.5) for the
  same cost/dependency/determinism reasons.
- AC5.3.8 — RBAC: Content-Aware Routing tab config and
  `sensitivity_label_mappings` management = Org Admin only (matches the
  existing static Model Policy tab's RBAC).

**Deferred / explicitly out of scope for this section**

- True ML/embedding/LLM-based content classification (regex/heuristic only,
  v1).
- Per-team content-classification overrides (org-wide only, matching the
  existing `content_aware_rules` table's documented AC4.2 no-team-override
  constraint from Phase 3).
- A live vendor API integration that calls out to Microsoft Purview's or
  Google DLP's own classification service — v1 accepts only a caller-supplied
  label as a static signal; Gatekey never calls those vendors' APIs itself.

---

## 5. §5.1 Shadow AI Discovery (build last — highest lift/uncertainty)

**User stories**

- As an Org Admin, I can ingest my org's existing SASE/proxy logs into
  Gatekey so it can flag employee usage of unsanctioned AI tools that
  bypass Gatekey entirely.
- As an Org Admin or Auditor, I can see a report of which users/teams are
  using which unsanctioned tools, how often, and when last seen.
- As a Team Lead, I can see this report scoped to my own team's members
  only.
- As an Org Admin, I can optionally enable a notification-based
  enforcement mode, off by default, requiring explicit confirmation to turn
  on.
- As an Org Admin, before enabling this feature I can review exactly what
  data it collects, retains, and who can see it.

**Acceptance criteria**

- AC5.1.1 — New table `shadow_ai_ingest_events`: `id`, `org_id`,
  `user_identifier` (text — email/username as reported by the ingesting
  tool; may not match a known Gatekey user), `matched_user_id` (nullable
  FK, `SET NULL`, populated when `user_identifier` resolves to a known
  Gatekey user by email), `destination_host` (text), `occurred_at`,
  `source` (`"sase_log"|"proxy_log"`), `raw_metadata` (JSONB, nullable —
  optional passthrough beyond the required fields). **Only rows whose
  `destination_host` matches the curated unsanctioned-AI-tool hostname list
  (AC5.1.2) are stored** — everything else in an ingested batch is dropped,
  not persisted, bounding this table's privacy/retention exposure by design
  (see §9, judgment call #17).
- AC5.1.2 — New table `known_ai_tool_hostnames`: `hostname` (e.g.
  `"api.openai.com"`, `"chat.deepseek.com"`, `"claude.ai"`,
  `"gemini.google.com"`), `tool_label` (display name), `enabled`. Hand-curated
  seed list, admin-extendable (add/remove entries) via the Shadow AI tab —
  same "curated, editable table" posture as the DLP custom-patterns table
  already in this codebase (§10.1 of the UI doc).
- AC5.1.3 — Ingestion endpoint `POST /v1/admin/shadow-ai/ingest` accepts a
  **batch of normalized events** in Gatekey's own generic schema (matching
  AC5.1.1's fields) — not any specific vendor's native SASE/proxy log
  format. A design partner's SASE/proxy tool needs its own lightweight
  transform/webhook to this contract; Gatekey does not ship vendor-specific
  adapters (Zscaler, Netskope, etc.) in v1 (see §9, judgment call #16).
  Auth: a **dedicated ingestion bearer token** (`shadow_ai_ingest_token`),
  distinct from the break-glass admin token and from regular gateway
  service-account keys — a different trust boundary (an ingestion feed
  should never be able to make inference calls, and vice versa). Generated
  via the same one-time-reveal component already used for SCIM tokens (ui
  doc §14).
- AC5.1.4 — The feature is functionally opt-in: no ingestion token exists,
  and the ingestion endpoint rejects all requests, until an Org Admin
  completes the Shadow AI tab's setup (selects a detection source and
  generates an ingestion token) — the setup flow itself is the opt-in gate;
  no separate master on/off toggle is added beyond what the UI mock already
  shows (ui doc §12.1).
- AC5.1.5 — Report endpoint `GET /v1/admin/shadow-ai/report` returns
  `(user, tool, frequency_per_week, last_seen)` rows aggregated from
  `shadow_ai_ingest_events`, grouped by `(user_identifier, destination_host)`,
  filterable by team and date range. Rows with no `matched_user_id` still
  appear (labeled "not linked to a Gatekey user"), since catching usage by
  people who've never touched Gatekey's own gateway is the whole point of
  this feature.
- AC5.1.6 — RBAC on the report: **Org Admin** — full org-wide view + all
  config. **Auditor** — full org-wide read-only view (compliance
  relevance, via `require_admin_or_auditor`). **Team Lead** — read-only,
  scoped to only their own team's `matched_user_id` members (privacy-
  conscious default; not specified by the phase doc — flagged, see §9,
  judgment call #21). **Member** — no access.
- AC5.1.7 — **Enforcement mode** — the UI mock's "Block & redirect" radio
  (ui doc §12.1) is implemented as **two mechanisms, neither of which is
  true inline network blocking** (architecturally impossible from a passive
  log-ingestion detection mechanism — Gatekey has no presence in that
  traffic's path): (a) an automated notification (email) to the flagged
  user, and optionally their Team Lead, on each detected event; (b) an
  optional outbound webhook callback fired on each detected event, which an
  org's own SASE/SOAR/automation tooling can subscribe to and use to enact
  an actual network-level block on their end. Both default **off**;
  enabling either requires the existing explicit confirm dialog (ui doc
  §12.1: "this is intrusive — are you sure?"). This is the most significant
  reinterpretation of a named UI control in this spec — flagged prominently
  (see §9, judgment call #20).
- AC5.1.8 — A "repeat violator" flag (derived, not stored) surfaces in the
  report for any `(user, tool)` pair with ≥3 events in the trailing 7 days,
  giving admins a prioritization signal without needing the enforcement
  mode enabled.
- AC5.1.9 — **Data-handling policy deliverable** (hard NFR, must be a real
  artifact, not just code behavior): a short markdown policy doc
  (`docs/policy/shadow-ai-data-handling.md`) covering: exactly what's
  collected (destination host + timestamp + user identifier — explicitly
  **never** full URLs, query strings, or request/response bodies — this
  feature scopes out payload capture entirely, connection metadata only),
  why, retention period (AC5.1.10), who can see it (AC5.1.6's role list),
  and how to disable the feature. Linked from the Shadow AI tab's "View
  policy" link (ui doc §12.1), treated with the same weight as a legal
  consent screen per the UI doc's own framing.
- AC5.1.10 — Dedicated retention window `shadow_ai_retention_days`
  (default 90, admin-configurable), **separate from** the existing
  `audit_retention_days`/`log_prompt_retention_days` config columns, given
  this is a distinct privacy-sensitive data category (network destination
  metadata about employees, not AI-gateway traffic). Purged via a new
  `run_shadow_ai_purge_if_due` job following the exact same
  `services/scheduler.py` `*_if_due` pattern as every other purge job.
- AC5.1.11 — Admin console Shadow AI tab (ui doc §12.1): detection source
  selector (SASE/proxy log ingestion only, v1 — browser extension option
  shown disabled/"coming later" per the deferred decision), enforcement
  mode controls (AC5.1.7), the report table, and the data-handling policy
  disclosure link (AC5.1.9).

**Deferred / explicitly out of scope for this section**

- Browser extension detection mechanism (explicit fallback for a future
  increment per the orchestrator's SASE-first default — not built now).
- True inline network blocking/redirection (architecturally incompatible
  with passive log ingestion — would require an inline proxy or browser
  extension sitting in the traffic path, itself deferred).
- Vendor-specific SASE/proxy log format adapters (Zscaler, Netskope,
  Palo Alto, etc.) — v1 accepts one generic normalized ingestion schema
  only; adapter-building is each design partner's own integration work.
- End-user-facing self-service visibility into their own shadow-AI flags
  beyond the notification enforcement mode (v1 is admin/auditor/Team-Lead
  facing only).

---

## 6. Explicit Scope Boundary Summary

**In scope for Phase 5 (build now):**

- In-database hash-chained audit ledger extending `audit_entries`
  additively, with a verification endpoint/UI and chain-aware export.
  Mutually exclusive with the existing audit-purge job (§1).
- Daily scheduled canary suite (fixed, cheap, code-seeded prompts) against
  actively-used models, threshold-based drift flagging on latency/refusal-
  rate/output-similarity, with cost tracked in a wholly separate table
  never touching user-attributable budget (§2).
- Self-hosted inference endpoints (vLLM/Ollama-style) registrable as a
  first-class governed provider, with a configurable GPU-hour cost basis
  normalized into the same `usage_logs.cost_usd` column BYOK providers use,
  under the identical policy/budget/DLP/audit pipeline (§3).
- Real regex/heuristic classifiers for `source_code`, `financial_data`,
  and `legal` content categories (in addition to the already-functional
  `pii`), enforced automatically via admin-defined category→allowed-model
  mappings, plus an optional pre-set sensitivity-label short-circuit signal
  (§4).
- SASE/proxy-log-based shadow AI detection with an admin-console report,
  a required data-handling policy disclosure, org-scoped and team-scoped
  RBAC read access, and an opt-in (off by default) notification/webhook-
  based enforcement mode (§5).

**Explicitly deferred / out of scope (do not build, even where the phase
doc's own "In Scope" bullets gesture at a fuller version):**

- External hash-chain anchoring (timestamping service integration) — §1.
- Admin-editable canary prompt authoring; ML/embeddings-based drift
  comparison; true statistical hypothesis testing for drift — §2.
- Real GPU-telemetry-based self-hosted cost accounting; multi-key/failover
  for self-hosted endpoints; served-model auto-discovery — §3.
- ML/LLM-based content classification; per-team content-classification
  overrides; live Purview/Google-DLP API calls — §4.
- Browser-extension shadow-AI detection; true inline network
  blocking/redirection; vendor-specific SASE adapters — §5.
- Phase 6 territory (policy-as-code plugin marketplace, internal budget
  marketplace) — explicitly out of scope per the phase doc itself.

---

## 7. Dependencies on Prior Phases

- **Phase 1** — `MODEL_REGISTRY`/routing (`resolve_route`), pricing table
  pattern (`PricingEntry`/`PRICING_TABLE`, mirrored by §3's
  `compute_self_hosted_cost`), provider validator abstraction
  (`providers/base.py`, `providers/registry.py`), request logging schema
  (`usage_logs`, extended again in §3).
- **Phase 2** — RBAC role set (Org Admin, Team Lead, Member, Auditor) and
  the `require_admin`/`require_admin_or_auditor`/`require_role`/
  `require_team_role` dependency primitives used throughout this doc's RBAC
  assignments; org/team hierarchy for §5's team-scoped Shadow AI report.
- **Phase 3** — `audit_entries` (extended additively in §1),
  `content_aware_rules` and `resolve_content_classification`/
  `ModelAccessDecision` (extended in §4), the DLP scan engine
  (`services/dlp.py`, extended with new built-in patterns in §4), the
  `services/scheduler.py` `run_scheduler_loop`/`*_if_due` job pattern
  (reused verbatim by §2's canary job and §5's shadow-AI purge job), and
  the process-local whole-snapshot-replace cache convention
  (`ModelPolicyCache`/`ContentAwareRuleCache`, reused by §3's new
  `SelfHostedModelRouteCache`).
- **Phase 4** — the provider-key health-check `*_if_due` job as the direct
  precedent for §2's drift-canary job; the Dashboard's metric-card
  extension pattern reused for §2's/§3's new admin-visible metrics; the
  existing Ollama $0.00 pricing entry and its own forward-pointing note
  ("see phase-5-differentiators.md section 5.5") that §3 directly resolves.

---

## 8. Data Model Touchpoints (for architect — a checklist, not schema design)

- `audit_entries`: add `chain_hash` (text, nullable), `prev_hash` (text,
  nullable), `chain_seq` (bigint, unique per `org_id`).
- `compliance_settings`: add `chain_enabled` (boolean, default false) —
  mutually exclusive with a non-null `audit_retention_days` (§1).
- New table `canary_prompts`: `id`, `prompt_text`, `label`, `max_tokens`,
  `enabled`.
- New table `canary_baselines`: `model`, `prompt_id`, baseline
  latency/refusal/output fields, `established_at`.
- New table `canary_runs`: `id`, `model`, `prompt_id`, `run_at`,
  `output_text`, `latency_ms`, `refusal_detected`, `similarity_score_vs_baseline`,
  `cost_usd`, `is_canary` (always true).
- New table `drift_alerts`: `id`, `model`, `metric`, `baseline_value`,
  `observed_value`, `delta_pct`, `detected_at`, `status`.
- New table `self_hosted_providers`: `id`, `org_id`, `name`, `base_url`,
  `bearer_token` (encrypted envelope), `cost_basis_per_gpu_hour`,
  `verified`, `models` (JSONB), `created_at`, `updated_at`.
- `usage_logs`: add `self_hosted_provider_id` (nullable FK, `SET NULL`) —
  `provider` column (already a plain string) takes the value
  `"self_hosted"` for these rows.
- `content_aware_rules`: seed new rows for `"source_code"`,
  `"financial_data"`, `"legal"` categories (no schema change — existing
  `(org_id, category)` composite PK already supports this).
- New table `sensitivity_label_mappings`: `id`, `org_id`, `external_label`,
  `gatekey_category`.
- New table `shadow_ai_ingest_events`: `id`, `org_id`, `user_identifier`,
  `matched_user_id` (nullable FK, `SET NULL`), `destination_host`,
  `occurred_at`, `source`, `raw_metadata` (JSONB, nullable).
- New table `known_ai_tool_hostnames`: `hostname`, `tool_label`, `enabled`.
- `compliance_settings` (or org config equivalent): add
  `shadow_ai_retention_days` (default 90).
- New credential type: `shadow_ai_ingest_token` (own trust boundary,
  distinct from service-account keys and the break-glass admin token).

---

## 9. Flagged Ambiguities (genuinely new — not re-litigating resolved items)

The phase doc's own "Resolved" items (5.2 no external anchoring, 5.1's
SASE-log default direction) and the orchestrator's explicit resolutions
(build order, RBAC role set, enforcement built-not-deferred, sensitivity-
label framing) are used as-is and are **not** included below. The following
are gaps/decisions this translation pass surfaced on its own:

1. **Hash-chain write-path concurrency.** The phase doc doesn't address
   what happens when two audit writes race for the same org's chain tail.
   **Call:** serialize the tail-read + insert per `org_id` (row lock or
   advisory lock) inside `write_audit_entry`. This is a real new
   requirement on an existing hot-ish write path — flag for
   architect/security review of lock contention under load.
2. **Hash-chain vs. audit-purge coexistence.** `audit_entry.py`'s own
   docstring flags this as an open problem for Phase 5 to solve. **Call:**
   made them mutually exclusive (enabling one blocks the other) rather than
   building purge-aware re-genesis bookkeeping. Simpler and safer for v1,
   but means an org can't have both unlimited-retention verifiability *and*
   a purge policy simultaneously — worth confirming with a real regulated-
   industry design partner before treating this as final.
3. **Hash-chain historical backfill.** Chose to retroactively compute the
   chain over **all** pre-existing rows (true historical genesis) rather
   than starting a fresh chain only from the feature's enable-date forward.
   More valuable but a heavier one-time migration — flag for a
   large-audit-table sizing check.
4. **"Statistically significant drift"** is implemented as fixed-threshold
   rolling-window comparison, not a real statistical test. The phase doc
   uses language implying more rigor than this delivers — flagged
   explicitly for QA/security, since the phase doc's own success criterion
   ("catches or would have caught at least one real provider-side model
   change") is testable either way, but the *method*'s false-positive/
   negative characteristics are unvalidated.
5. **Drift output-similarity metric** — chose a deterministic in-process
   text metric over an embeddings-API call, purely to avoid new cost/
   dependency/non-determinism given the feature's own "must not consume
   meaningful budget" NFR. Lower fidelity than an embeddings-based
   comparison would be.
6. **Drift refusal detection** — keyword/regex heuristic, same
   false-positive/negative caveat as #4/#5.
7. **"Actively used model" definition** for canary scheduling (≥1 real
   request in `usage_logs` in the last 7 days) — minor, but not specified
   by the phase doc.
8. **Canary prompts fixed, not admin-editable in v1** — a scope-narrowing
   call to avoid building a prompt-authoring UI the phase doc's "fixed test
   prompts" wording didn't ask for.
9. **New `self_hosted_providers` table instead of overloading `provider_keys`/
   `"ollama"`.** A real architectural choice: supports multiple named
   self-hosted endpoints (matching the UI mock) without a `provider_name_enum`
   migration. Alternative (extending `provider_keys` with self-hosted
   fields) was considered and rejected as messier given the enum
   constraint.
10. **Self-hosted cost-estimation formula**
    (`cost_basis_per_gpu_hour * wall_clock_latency_seconds / 3600`) — the
    phase doc specifies only "configured GPU-hour rate," not a method. This
    formula is a rough proxy that ignores queueing delay, multi-tenant GPU
    sharing, and cold-start latency. Flagged prominently since this number
    feeds real budget enforcement — must be visibly labeled "estimated" in
    the UI, never presented as invoice-grade.
11. **New `SelfHostedModelRouteCache` / dynamic routing extension.**
    Making self-hosted models first-class in a previously all-static
    (`MODEL_REGISTRY`-only) routing pipeline is a genuine architectural
    lift, not a small add. Flagged for explicit architect sizing/design
    review before implementation estimates are trusted.
12. **"Legal" content-classification category** has no existing
    schema-scaffolding precedent (unlike `pii`/`source_code`/`financial_data`,
    which were pre-seeded rows per `content_aware_rule.py`'s own docstring).
    Built anyway per the orchestrator's instruction to build "whatever the
    doc implies for legal/general categories," but it's the
    least-grounded-in-existing-code of the four positive categories —
    flagged for extra QA scrutiny on false-positive/negative rates.
13. **"General" category requires no classifier** (default/fallback
    bucket) — not stated explicitly by the phase doc, inferred from the UI
    mock's "General → all allowed models" row.
14. **Multi-category conflict resolution** (most-restrictive-wins via
    intersection of allowed-models sets across all matched enabled
    categories) — not specified by the phase doc, which only describes
    single-category resolution.
15. **Sensitivity-label UI reframing** — the UI mock (§7) shows an
    exclusive radio group ("Gatekey built-in" vs. "Purview" vs. "Google
    DLP"); this spec reframes it as an additive mapping table since
    Gatekey's own classifier must always run as fallback (per the
    orchestrator's own framing instruction, which explicitly asked this be
    flagged). Noting it here per that instruction, distinct from the
    framing decision itself (which was given, not originated, by this
    agent).
16. **Generic shadow-AI ingestion schema, no vendor adapters.** Each design
    partner's SASE/proxy tool needs its own transform to Gatekey's
    contract — reasonable scope boundary, but worth confirming a real
    design partner's tooling can produce this shape before committing.
17. **Curated hostname allowlist gates what's stored.** Only matched
    `destination_host` rows are persisted from an ingested batch; everything
    else is dropped, not retained. Deliberate privacy-by-minimization
    choice, not specified by the phase doc.
18. **Dedicated `shadow_ai_retention_days` window**, separate from existing
    audit/log-prompt retention config — inferred as necessary given this is
    a distinct privacy-sensitive data category, not specified by the phase
    doc.
19. **Dedicated shadow-AI ingestion bearer token**, its own trust boundary
    distinct from service-account keys and the break-glass admin token —
    not specified by the phase doc, inferred from this codebase's existing
    credential-separation conventions (e.g. SCIM token).
20. **"Block & redirect" reinterpreted as notification + optional webhook
    callback, not true inline network blocking.** This is the most
    significant reinterpretation of a named, already-wireframed UI control
    in this entire spec — SASE/proxy-log ingestion (the chosen v1 detection
    mechanism) is inherently passive/after-the-fact and cannot itself
    intercept live traffic. Flagged prominently for security review and for
    whoever writes the actual UI copy, since the wireframe's label may need
    updating to avoid overpromising "block" behavior Gatekey cannot deliver
    from this detection mechanism.
21. **Shadow AI RBAC scoping** (Team Lead sees only their own team's data;
    Auditor sees full org-wide data) — the phase doc and UI doc don't
    specify per-role scoping for this privacy-sensitive report; inferred
    from general least-privilege practice given the feature's own stated
    privacy sensitivity.

---

## 10. Non-Functional Requirements (testable)

- **Hash-chain integrity is real, not cosmetic.** Acceptance test: directly
  mutate one historical `audit_entries` row's `old_value` via a raw SQL
  UPDATE (bypassing the service layer, simulating tampering), then call
  `GET /v1/admin/audit/verify` — it must return `"status": "broken"` naming
  that exact entry's id and `chain_seq`.
- **Chain/purge mutual exclusivity is enforced, not just documented.**
  Acceptance test: attempt to set a finite `audit_retention_days` while
  `chain_enabled = true` (or vice versa) via the admin API — both must be
  rejected with a structured error, not silently accepted.
- **Canary cost is tracked wholly separately from user-attributable usage
  (hard NFR, explicit orchestrator must-verify).** Acceptance test: run a
  scheduler tick that fires canary requests, then assert (a) zero new
  `usage_logs` rows reference that traffic, (b) no team/user/org
  `current_spend_usd` figure changed, and (c) `canary_runs.cost_usd` sums
  to a nonzero, bounded (small) figure.
- **Canary suite cost floor is small and bounded by construction.**
  Acceptance test: with the fixed 5-prompt/low-`max_tokens` default set,
  a full daily run across N actively-used models costs less than a
  configured ceiling (e.g. under $1/day for a typical multi-model org) —
  document the actual measured figure once implemented.
- **Self-hosted cost estimate is visibly labeled as an estimate.**
  Acceptance test: the Self-Hosted Governance tab and any exported
  usage data for `provider = "self_hosted"` rows carry an explicit
  "estimated" marker distinguishing them from BYOK providers' invoice-
  grade figures.
- **Shadow AI data-handling policy exists and is linked, not just coded
  behavior (hard NFR, explicit orchestrator must-verify).** Acceptance
  test: `docs/policy/shadow-ai-data-handling.md` exists, covers collection/
  retention/access-control/opt-out per AC5.1.9, and the admin UI's "View
  policy" link resolves to it; the ingestion endpoint rejects all traffic
  until an Org Admin has completed setup (AC5.1.4).
- **Shadow AI ingestion never stores unmatched-hostname traffic.**
  Acceptance test: ingest a batch containing both known-AI-tool hosts and
  unrelated hosts; assert only the known-AI-tool rows persist in
  `shadow_ai_ingest_events`.
- **Content-classification multi-category blocking is real.** Acceptance
  test: configure `financial_data` allowed_models = `{A, B}` and `legal`
  allowed_models = `{B, C}` (both enabled); a request triggering both
  categories must only be allowed to route to `B` (the intersection); a
  request triggering both with a disjoint configuration must be blocked.

---

## 11. Success Criteria (from phase doc)

- **Validate demand before building all five** — the phase doc's own
  framing. This spec builds all five per the orchestrator's explicit
  instruction; the demand-validation exercise (design-partner conversations
  ranking 5.1–5.5) remains a real, still-open activity that should happen
  in parallel with/before general availability of each feature, not a
  gate this spec treats as satisfied.
- **At least one pilot org uses the audit ledger verification tool to
  confirm chain integrity as part of a real (or simulated) compliance
  exercise.** Acceptance: `GET /v1/admin/audit/verify` is called by a real
  pilot Auditor/Org Admin account and returns `"intact"` over a
  non-trivial entry count.
- **Drift detector catches or would have caught at least one real
  provider-side model change during the pilot window, demonstrated via the
  canary history.** Acceptance: at least one `drift_alerts` row with a
  real (not synthetic-test) `detected_at` exists for a pilot org, exported
  to the audit log and reviewed by that org's admin.

---

## 12. Open Questions to Resolve Before Building

The phase doc's inline-resolved items (5.2 external anchoring, 5.1's
SASE-log default) are not reopened. The following remain genuinely open,
carried forward from the phase doc plus this translation pass:

- **Which of the five features has the strongest pull from actual design
  partners** — still genuinely unanswered per the phase doc; this spec
  builds all five per explicit instruction, but the phase doc's own
  success criteria (validate demand) means real usage/feedback should still
  reprioritize post-build effort (bug fixes, UI polish, which gets a fast-
  follow first) even though initial build order is fixed.
- **Does the target enterprise already have SASE/proxy logging Gatekey can
  integrate with** — still unconfirmed per the phase doc; AC5.1.3's
  generic ingestion contract is designed to be integration-agnostic, but a
  real design partner's actual tooling should be checked against it before
  treating the ingestion contract as final.
- **Chain/purge mutual exclusivity (§9 #2) and the self-hosted cost formula
  (§9 #10)** — both are interim product-design calls this doc made to stay
  buildable; both should be explicitly confirmed with a real design partner
  (a regulated-industry one for the former, a cost-conscious self-hosting
  one for the latter) before being treated as permanent product decisions
  rather than v1 defaults.
