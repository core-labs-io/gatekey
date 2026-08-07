# Security Review — Custom Model Registry (CMR-13)

Scope reviewed (direct code reading, not agent self-reports): `backend/src/gatekey/services/custom_models.py`,
`services/self_hosted_providers.py`, `api/v1/admin/custom_models.py`, `api/v1/gateway/{common,chat,embeddings,completions}.py`,
`api/v1/model_access.py`, `db/models/custom_model.py`, `alembic/versions/0044_create_custom_models.py`,
`schemas/custom_model.py`, `main.py`'s CMR wiring, `services/model_policy.py`/`api/v1/admin/model_policy.py`/`api/v1/teams.py`
widening, and the RBAC-hiding logic in `frontend/app/providers/page.tsx`. Independently ran the full existing test suite
and wrote two throwaway repro scripts (deleted after use) to verify a concurrency claim against a real Postgres instance
rather than trusting code reading alone.

## §8.1 Mandatory flag list — pass/fail

1. **Shadowing (highest severity).** PASS. `resolve_route()` (`api/v1/gateway/common.py:151-232`) tries the static
   registry unconditionally first; the custom-model fallback is checked before self-hosted but this ordering is
   immaterial since the collision guards keep the three keyspaces disjoint. `_log_custom_model_shadowing()`
   (`main.py:327-371`) genuinely logs at `ERROR` (confirmed by reading the code, not the docstring — `logger.error(...)`
   at line 362) naming org + custom-model id, and is exercised by a real integration test
   (`tests/integration/test_custom_model_startup_wiring.py`) that inserts rows directly via the ORM, drives the real
   `main.py` lifespan via `app.router.lifespan_context(app)` against a real Postgres instance, and monkeypatches
   `main.logger.error` as a spy (not `caplog`, documented as unreliable here due to Alembic's `disable_existing_loggers`).
   `shadowed_by_registry` is computed fresh per response (`is_shadowed_by_registry()`, a plain `name in MODEL_REGISTRY`
   check, never persisted/cached). No auto-remediation code path exists anywhere.

2. **Bidirectional collision guard, both directions, independently tested.** PASS.
   `test_bidirectional_collision_custom_model_rejects_self_hosted_name` and
   `test_bidirectional_collision_self_hosted_rejects_custom_model_name` cover both directions non-concurrently;
   `test_custom_model_collision_race_condition.py` covers the concurrent case (see deadlock finding below for a
   related but distinct issue).

3. **`resolve_route()` keyword-argument hazard.** PASS. `chat.py:514-515` calls `resolve_route(body.model,
   self_hosted_cache=self_hosted_cache, custom_model_cache=custom_model_cache)` — both keyword. `embeddings.py:159`
   calls `resolve_route(body.model, custom_model_cache=custom_model_cache)` — keyword, correctly omitting
   `self_hosted_cache` (embeddings stays self-hosted-free per AC5.5.4). `completions.py:172` calls
   `resolve_route(body.model)` with no cache args at all, structurally enforcing "custom models never route at
   `/v1/completions`".

4. **`custom_model_id` as sole cost discriminator.** PASS. `chat.py:823` and `embeddings.py:308` both branch on
   `route.custom_model_id is not None`, never `route.provider`, and both branches are mutually exclusive with the
   self-hosted branch by construction.

5. **Embeddings-provider guard enforced at write time.** PASS. `_validate_custom_model_write()` rejects
   `capability=embeddings` for any provider outside `{"openai", "vertex_ai"}` before any DB write.

6. **Verify endpoint never charges budget or writes `usage_logs`.** PASS. `verify_custom_model()` calls only
   `get_decrypted_provider_credential()` and the raw per-provider client function — no `check_budget_available`,
   `record_usage_charge`, or `usage_logs` write anywhere in the module.

7. **No credential leakage.** PASS. `verify_custom_model()`'s error-message construction is via
   `provider_call_error_from_response`/`provider_call_error_from_exception`, which build fixed, templated messages
   — never the raw response body or the credential object. No credential interpolation found in any error path
   across the provider client modules.

8. **RBAC boundary.** PASS, independently re-verified by running the tests: Team Lead/Member get 401/403 on every
   endpoint; Auditor gets 200 on GET, 403 on POST/PUT/DELETE/verify. Frontend hiding is defense-in-depth only,
   backed by real server-side checks.

## Independent verification performed

- Ran the full targeted CMR test surface directly (99 tests across 7 files), all passing.
- Ran the full existing suite: 817 passed / 1 skipped (unit), 289 passed / 7 skipped (integration) — zero failures,
  matching the CMR-14 baseline.
- Independently re-verified the CMR-12/CMR-14 collision-race fix is sound, not just passing its own test: both
  `_lock_org_settings_for_model_name_guard` implementations (in `custom_models.py` and `self_hosted_providers.py`)
  take `SELECT ... FOR UPDATE` on the same single `org_settings` row before their respective collision SELECTs —
  since both sides acquire only one lock (the same row), there is no two-lock deadlock risk *between these two
  modules specifically*.

## Finding — genuine deadlock between the new lock and the pre-existing `org_settings.py` PUT endpoint

While specifically chasing the deadlock-risk question this review was tasked to check carefully, a real deadlock was
found — not within the custom-model/self-hosted pairing itself, but between that pairing and a third, pre-existing
endpoint that locks the same `org_settings` row in the opposite order relative to the audit-chain lock:

- `api/v1/admin/custom_models.py`'s POST/PUT handlers call `write_audit_entry()` (which, when Phase 5.2's
  `compliance_settings.chain_enabled=True`, takes `SELECT ... FOR UPDATE` on `compliance_settings`) **before**
  calling `register_custom_model()`/`edit_custom_model()` (which locks `org_settings`). Lock order:
  **compliance_settings → org_settings**.
- `api/v1/admin/org_settings.py`'s pre-existing `PUT` handler (`put_org_settings_endpoint`) calls
  `set_org_budget_ceiling()` (locks `org_settings`) **first**, then `write_audit_entry()` (locks
  `compliance_settings`) second. Lock order: **org_settings → compliance_settings**.

This is the classic opposite-order two-lock deadlock, reproduced directly against a real Postgres instance:

```
DeadlockDetectedError: deadlock detected
DETAIL: Process 65 waits for ShareLock on transaction 738; blocked by process 66.
Process 66 waits for ShareLock on transaction 737; blocked by process 65.
```

Concretely: with an org's hash-chained audit ledger enabled (a real, shipped Phase 5.2 feature aimed at exactly this
product's compliance-conscious customer base), an Org Admin calling `PUT /v1/admin/org-settings` (e.g. changing the
budget ceiling) concurrently with another Org Admin calling `POST /v1/admin/custom-models` (or `PUT .../{id}` when
name/provider/capability/price fields are touched) has a real, reproducible chance of one of the two requests
failing with an unhandled `DeadlockDetectedError` (surfacing as a raw 500, not a clean 4xx) instead of succeeding.

This is **not new to CMR** — the identical deadlock reproduces between `org_settings.py`'s PUT and the pre-existing
Phase 5.5 `self_hosted_providers.py`'s POST (same audit-then-lock ordering). CMR-14's fix correctly made
`custom_models.py` and `self_hosted_providers.py` internally consistent with each other, but both inherited an
ordering that was already inconsistent with `org_settings.py`, and CMR extends the set of endpoints exposed to this
pre-existing bug from one (self-hosted) to two (self-hosted + custom models).

This is **not** a violation of any of the four standing non-negotiables (no plaintext key exposure, no RBAC bypass,
no budget-check bypass, encryption is fine) — it's a reliability/availability defect: Postgres's deadlock detector
guarantees no corruption or hang, one transaction is cleanly aborted and the other proceeds, and a client retry
would succeed. But it is a real, 100%-reproducible bug directly in the mechanism this review was tasked to
scrutinize, triggered by ordinary concurrent admin usage in exactly the deployment configuration (multi-admin org,
audit chaining enabled) this product targets.

**Recommended fix:** make lock acquisition order consistent everywhere a transaction needs both a
`compliance_settings` lock (via `write_audit_entry` under chaining) and an `org_settings`/`self_hosted_providers`/
`custom_models`-adjacent lock. Simplest concrete fix: reorder `api/v1/admin/org_settings.py::put_org_settings_endpoint`
to call `write_audit_entry()` **before** `set_org_budget_ceiling()`, matching the ordering convention
`custom_models.py`/`self_hosted_providers.py` already use. A more robust fix is a documented codebase-wide
convention ("always acquire the `compliance_settings` audit-chain lock, when applicable, before any other admin-config
row lock in the same transaction") plus an audit of any other admin endpoint that both writes an audit entry and
separately locks a config row (e.g. `team_budget.py`'s `_lock_team`-based endpoints), since this is a systemic
pattern, not something confined to these three files.

## Other items checked, no issue found

- **No new credential storage** — confirmed directly in migration `0044` and `db/models/custom_model.py`: no
  `ciphertext`/`nonce`/`auth_tag` column exists on `custom_models` at all.
- **`$0`/negative pricing** — hard-blocked at three independent layers: Pydantic `Field(gt=0)`, the app-layer
  `_validate_custom_model_write()` check, and the DB `CHECK` constraints in migration `0044`. No edit-path bypasses
  this — `edit_custom_model()` always re-validates the full effective post-edit value set whenever a price field
  changes.
- **Capability/output-price completeness invariant** — enforced at both the service layer and the DB
  (`chk_custom_models_capability_output_price`).
- **Model Access end-user view (QA's CMR-12 fix)** — re-verified directly: `api/v1/model_access.py` unions
  `MODEL_REGISTRY.keys() | custom_model_cache.known_model_ids() | self_hosted_cache.known_model_ids()`, both
  `known_model_ids()` calls only ever return `verified=true` rows by construction.
- **Degradation-path `resolve_route(candidate_model)` gap** (`chat.py:644`) — confirmed real, but explicitly
  documented, pre-existing accepted limitation shared identically by self-hosted models (technical design doc §11).
  Fails closed. Non-blocking.
- **`usage_logs.custom_model_id`-less design** — confirmed deliberate and disclosed (design doc §2.6).

## Sign-off

**FAIL — one blocking finding, must be fixed before this feature is marked done.**

**Blocking (must fix now):**
1. Cross-endpoint lock-ordering deadlock between `api/v1/admin/org_settings.py`'s `PUT` handler and
   `api/v1/admin/custom_models.py`'s POST/PUT handlers (and, pre-existing, `api/v1/admin/self_hosted_providers.py`'s
   POST/PUT) when `compliance_settings.chain_enabled=True`. Reproduced directly against real Postgres. Fix: make
   audit-chain-lock vs. org-config-lock acquisition order consistent across all three admin routers.

**Non-blocking, tracked follow-ups (not a re-review gate):**
1. The pre-existing scope of the deadlock above (it already existed between `org_settings.py` and
   `self_hosted_providers.py` before this feature) — worth a broader one-time audit of every admin endpoint that
   both writes an audit entry and locks a config row, not just the three touched by this fix.
2. `chat.py`'s degradation-target `resolve_route(candidate_model)` call still doesn't thread
   `custom_model_cache`/`self_hosted_cache` through, so a downgrade policy configured to fall back to a custom or
   self-hosted model name will fail rather than degrade. Already explicitly disclosed as an accepted, shared
   limitation in the technical design doc's Known Limitations (§11); not introduced or worsened by this feature.

Everything else — the shadowing detection/disclosure mechanism, the bidirectional collision guard, the
`ModelRoute.custom_model_id` discriminator discipline, the embeddings-provider write-time guard, the verify
endpoint's budget/usage-log isolation and credential-leak-free error handling, RBAC enforcement, and the complete
absence of any new credential storage — passes direct, independent verification.

## CMR-14 resolution (post-review fix, verified)

The blocking deadlock finding is fixed. `api/v1/admin/org_settings.py::put_org_settings_endpoint` now calls
`write_audit_entry()` before `set_org_budget_ceiling()`, matching `custom_models.py`/`self_hosted_providers.py`'s
ordering. While fixing it, the implementer audited every `with_for_update` call site in the codebase (9 files) for
the same conflict and found the fix would otherwise have introduced a *new* deadlock between `team_budget.py`'s
`set_team_budget_ceiling` (locks `org_settings` then `teams`) and several `teams.py`/`scim.py` endpoints that
previously locked `teams` before auditing — all reordered to audit-first for global consistency. Each fix was
validated by reverting it, reproducing a real `DeadlockDetectedError` against Postgres, then restoring the fix and
confirming it passes. Two new regression tests
(`backend/tests/integration/test_lock_ordering_deadlock_regression.py`) cover both the mandated fix and the broader
systemic one. Full suite re-run clean: 817 passed/1 skipped (unit), 291 passed/7 skipped (integration), zero
failures.

**Final verdict: PASS.** No blocking findings remain.
