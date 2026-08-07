"""Hardening pass item 2: QA audit of every `on_conflict_do_update(...).
returning(...)` call site in the codebase, looking for the exact same
identity-map staleness defect already found and fixed (this same session)
in `services.residency.set_org_residency_rule`/`set_team_residency_rule`
and `services.dlp.set_dlp_policy`/`set_team_dlp_override` (see those
functions' docstrings for the full SQLAlchemy 2.0 `populate_existing`
mechanism). Two more genuinely-triggered, enforcement-impacting instances
were found:

  - `services.model_policy.set_team_model_policy` (triggered by
    `api/v1/teams.py::put_model_restrictions_endpoint`'s same-session
    pre-read) - feeds `TeamModelPolicyCache`, read by `resolve_model_
    access()` on every gateway request.
  - `services.model_policy.set_content_aware_rule` (triggered by
    `api/v1/admin/content_aware_rules.py::put_content_aware_rules_
    endpoint`'s same-session pre-read) - feeds `ContentAwareRuleCache`,
    read by `resolve_content_classification()` on every gateway request.
  - `services.access_schedules.set_org_access_schedule`/`set_team_access_
    schedule`/`set_service_account_access_schedule` (each triggered by its
    own PUT route's same-session pre-read for its own audit entry) - feed
    `AccessScheduleCache`, read by `resolve_access_schedule_decision()` via
    `check_access_schedule()` on every gateway request. Only the org-level
    scope is exercised end-to-end here (the identical fix - and the
    identical bug shape - applies to all three; see `services.access_
    schedules.set_org_access_schedule`'s docstring for the shared
    mechanism and `set_team_access_schedule`/`set_service_account_access_
    schedule`'s own docstrings for their own trigger sites).

Each test below follows the same three-step shape as `test_policy_write_
cache_invalidation_e2e.py` (minus the Redis dependency - these three
in-process caches need no Redis to reproduce): populate a PERMISSIVE policy
via the real admin PUT (creating the row), issue a gateway request that
gets PAST the layer under test (proving the permissive state really is
wired up), then TIGHTEN the same policy via a SECOND real admin PUT (the
one that pre-reads the now-existing row into the same session, exactly the
trigger condition), and assert the VERY NEXT gateway request is evaluated
against the NEW, tighter value - not a stale value re-armed from the
first, pre-tightening row by SQLAlchemy's identity map.

Under the old code (no `populate_existing`), the second PUT's `RETURNING`
would hand back the STALE (permissive) ORM object already sitting in the
session's identity map from the first PUT's `INSERT` (still resident in
that PUT's post-commit session state until the request ends - the `PUT`
handler pre-reads the row for `old_value` BEFORE calling the service
function in every case here, so as soon as a row already exists the
pre-read hydrates the identity map with it before the upsert runs), so the
in-process cache would still hold the PERMISSIVE snapshot and the following
gateway request would incorrectly be ALLOWED where it must now be DENIED.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_SSN = "234-56-7890"  # Presidio's UsSsnRecognizer invalidates "123-45-6789".


@pytest.fixture(autouse=True)
async def _truncate_shared_singleton_tables(migrated_database_url: str):
    """`model_policies`/`content_aware_rules`/`dlp_policies`/`access_
    schedules` (plus their team/service-account overlay tables) are
    process-wide singletons per org/team/key, never truncated between test
    files by default (this whole session shares one Postgres instance) -
    every test below writes an org-wide (or team-scoped, but team ids are
    fresh per test via `default_team_id`) row via a real admin PUT, which
    would otherwise leak into whichever test runs next. One combined
    fixture (not three) since all three are autouse and would otherwise all
    run before every test in this file regardless.

    Truncated both BEFORE and AFTER each test - mirroring `test_access_
    schedule_gateway.py`'s/`test_content_classification_gateway.py`'s own
    fixtures of this shape (see their docstrings): an org-wide `access_
    schedules` row this file's own access-schedule test leaves behind
    (deliberately narrowed to exclude TODAY's weekday) would otherwise
    silently start blocking every OTHER test file's gateway requests that
    happen to run later in this same pytest session/Postgres instance -
    exactly the false-failure class this before-and-after truncation
    exists to prevent."""

    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE model_policies, team_model_policies, content_aware_rules, "
                "dlp_policies, dlp_custom_patterns, team_dlp_action_overrides, dlp_scan_results, "
                "access_schedules, holiday_dates, emergency_overrides CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "populate-existing-fix-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


# ---------------------------------------------------------------------------
# Fix 1a: services.model_policy.set_team_model_policy
# ---------------------------------------------------------------------------


async def test_team_model_policy_tightening_denies_next_gateway_request(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """Team restriction narrowed from {gpt-4o, gpt-4o-mini} to {gpt-4o}
    (removing gpt-4o-mini) via TWO real PUTs to `/v1/teams/{team_id}/
    model-restrictions` (org-wide `model_policies` is left unconfigured -
    permissive by default - so the team overlay alone is what's tightened).
    The second PUT is the one that matters: `put_model_restrictions_
    endpoint` pre-reads the row the first PUT just created (for its audit
    `old_value`) into the same session before `set_team_model_policy`'s
    upsert runs - the exact trigger condition."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    permissive = await client.put(
        f"/v1/teams/{default_team_id}/model-restrictions",
        json={"models": ["gpt-4o", "gpt-4o-mini"]},
        headers=auth_headers,
    )
    assert permissive.status_code == 200, permissive.text
    assert permissive.json()["team_restriction"] == ["gpt-4o", "gpt-4o-mini"]

    # Sanity check under the permissive restriction: gpt-4o-mini passes the
    # model-policy layer and fails LATER for an unrelated reason (no
    # provider key configured) - proving THIS layer specifically allowed it.
    allowed_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert allowed_resp.json()["error"]["code"] == "provider_not_configured", allowed_resp.text

    # Tighten: remove gpt-4o-mini. This PUT is the one whose same-session
    # pre-read (of the row the first PUT created) triggers the identity-map
    # staleness bug if `populate_existing` were missing.
    tightened = await client.put(
        f"/v1/teams/{default_team_id}/model-restrictions",
        json={"models": ["gpt-4o"]},
        headers=auth_headers,
    )
    assert tightened.status_code == 200, tightened.text
    assert tightened.json()["team_restriction"] == ["gpt-4o"]

    denied_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert denied_resp.status_code == 403, denied_resp.text
    body = denied_resp.json()
    assert body["error"]["code"] == "model_denied"
    assert "team" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Fix 1b: services.model_policy.set_content_aware_rule
# ---------------------------------------------------------------------------


async def test_content_aware_rule_tightening_denies_next_gateway_request(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """The 'pii' category's `allowed_models` narrowed from {gpt-4o,
    gpt-4o-mini} to {gpt-4o} (removing gpt-4o-mini) via TWO real PUTs to
    `/v1/admin/content-aware-rules`. The second PUT is the trigger:
    `put_content_aware_rules_endpoint` pre-reads every current rule row
    (including the one the first PUT just created) for its per-category
    audit `old_value` before `set_content_aware_rule`'s upsert runs."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    dlp_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "log"},
        headers=auth_headers,
    )
    assert dlp_resp.status_code == 200, dlp_resp.text

    permissive = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "pii", "enabled": True, "allowed_models": ["gpt-4o", "gpt-4o-mini"]}]},
        headers=auth_headers,
    )
    assert permissive.status_code == 200, permissive.text

    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": f"my SSN is {_SSN} today"}],
    }
    # Sanity check: gpt-4o-mini is in the 'pii' category's allowlist, so it
    # passes the content-classification layer and fails LATER (no provider
    # key configured) - proving THIS layer specifically allowed it.
    allowed_resp = await client.post(
        "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
    )
    assert allowed_resp.json()["error"]["code"] == "provider_not_configured", allowed_resp.text

    # Tighten: remove gpt-4o-mini from the 'pii' category's allowlist. This
    # PUT's same-session pre-read (of the row the first PUT created) is the
    # trigger condition for the identity-map staleness bug.
    tightened = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "pii", "enabled": True, "allowed_models": ["gpt-4o"]}]},
        headers=auth_headers,
    )
    assert tightened.status_code == 200, tightened.text

    denied_resp = await client.post(
        "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
    )
    assert denied_resp.status_code == 403, denied_resp.text
    denied_body = denied_resp.json()
    assert denied_body["error"]["code"] == "model_denied"
    assert "content classification" in denied_body["error"]["message"]


# ---------------------------------------------------------------------------
# Fix 2: services.access_schedules.set_org_access_schedule (org scope - the
# identical fix/bug shape also applies to the team and service-account
# scopes, see module docstring above).
# ---------------------------------------------------------------------------


async def test_org_access_schedule_tightening_denies_next_gateway_request(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """The org-wide schedule narrowed from "every day" (permissive) to
    excluding TODAY's weekday entirely via TWO real PUTs to `/v1/admin/
    access-schedule`. The second PUT is the trigger: `put_org_access_
    schedule_endpoint` pre-reads the row the first PUT just created (for its
    audit `old_value`) into the same session before `set_org_access_
    schedule`'s upsert runs. Day-based narrowing (not hour-of-day) is used
    so the assertion is never flaky against wall-clock time, mirroring
    `test_access_schedule_gateway.py`'s existing convention."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    permissive = await client.put(
        "/v1/admin/access-schedule",
        json={"enabled": True, "allowed_days": [1, 2, 3, 4, 5, 6, 7]},
        headers=auth_headers,
    )
    assert permissive.status_code == 200, permissive.text

    # Sanity check: every day is allowed, so today passes the schedule layer
    # and fails LATER (no provider key configured) - proving THIS layer
    # specifically allowed it.
    allowed_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert allowed_resp.json()["error"]["code"] == "provider_not_configured", allowed_resp.text

    # Tighten: exclude today's weekday entirely. This PUT's same-session
    # pre-read (of the row the first PUT created) is the trigger condition
    # for the identity-map staleness bug.
    today_weekday = datetime.now(timezone.utc).isoweekday()
    allowed_days = [d for d in range(1, 8) if d != today_weekday]
    tightened = await client.put(
        "/v1/admin/access-schedule",
        json={"enabled": True, "allowed_days": allowed_days},
        headers=auth_headers,
    )
    assert tightened.status_code == 200, tightened.text

    denied_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert denied_resp.status_code == 403, denied_resp.text
    assert denied_resp.json()["error"]["code"] == "outside_allowed_schedule"
