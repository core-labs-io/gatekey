"""Fix 2 (originally a QA finding): the "team pool" rate-limit counter must
be shared across the team's users, not an independent copy per user.

AC4.2.9 (phase-4-product-spec.md) is explicit: "A team with limit 100 rpm
and a user with limit 50 rpm can send up to 150 rpm total (50 to personal
pool, 100 SHARED team pool)". AC4.2.6's own worked example says the same
thing even more directly: "the Redis key `rate_limit:team:123:provider:
openai:window:1m` holds a GLOBAL count" - i.e. one counter for the whole
team, incremented by every member.

`services.rate_limit._pool_rate_limit_key()` used to build
`f"rate_limit:v1:pool:{team_id}:{user_id}:requests:1m"` - keyed by BOTH
team_id AND user_id - so a team configured with a 100 rpm "shared" limit
never actually capped the team's aggregate traffic at 100 rpm at all: each
distinct user under that team got their OWN independent 100 rpm allowance,
so a team with N active users could actually send up to N * 100 rpm before
the team-level rule ever tripped for any of them.

Fixed: `_pool_rate_limit_key` now branches on the pool rule's own
`scope_type` - a TEAM-scoped rule's key is `team_id`-only (a real shared
counter, incremented by every member); an ORG_DEFAULT_PER_USER-scoped rule
(a genuinely different, explicitly per-user default - not a shared pool)
keeps its previous per-user key unchanged. This test was originally added
by QA as `xfail(strict=True)` to document the defect; it now asserts the
CORRECT (fixed) behavior directly, with no xfail marker.
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.services.rate_limit import check_and_consume_rate_limit
from gatekey.services.shared_state import InProcessSharedStateStore


def _team_rule(*, requests_per_min: int, team_id: uuid.UUID) -> RateLimitRule:
    return RateLimitRule(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope_type=RateLimitScopeType.TEAM,
        scope_team_id=team_id,
        scope_user_id=None,
        requests_per_min=requests_per_min,
        tokens_per_min=None,
        on_limit=RateLimitOnLimit.REJECT,
        max_queue_wait_seconds=5,
    )


@pytest.mark.asyncio
async def test_team_pool_limit_is_shared_across_different_users_in_the_team() -> None:
    """A team rule of requests_per_min=2 caps the TEAM's combined traffic at
    2 requests/minute total, regardless of how many distinct users in the
    team send them - not a private per-user copy of the limit."""
    store = InProcessSharedStateStore()
    team_id = uuid.uuid4()
    team_rule = _team_rule(requests_per_min=2, team_id=team_id)

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    # user_a alone consumes the team's entire 2-request budget.
    first = await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=team_rule, user_rule=None, team_id=team_id, user_id=user_a
    )
    second = await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=team_rule, user_rule=None, team_id=team_id, user_id=user_a
    )
    assert first.allowed is True
    assert second.allowed is True

    # A DIFFERENT user in the SAME team is now rejected - the team's shared
    # pool is already exhausted.
    third_different_user = await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=team_rule, user_rule=None, team_id=team_id, user_id=user_b
    )
    assert third_different_user.allowed is False, (
        "team pool must be shared across all users in the team, not "
        "per-user - see module docstring"
    )


@pytest.mark.asyncio
async def test_org_default_per_user_pool_stays_independent_per_user() -> None:
    """The ORG_DEFAULT_PER_USER scope is explicitly a per-user default (not
    a shared pool) - unlike the TEAM scope above, two different users must
    each get their own independent counter under this scope, unchanged by
    Fix 2."""
    store = InProcessSharedStateStore()
    org_rule = RateLimitRule(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER,
        scope_team_id=None,
        scope_user_id=None,
        requests_per_min=1,
        tokens_per_min=None,
        on_limit=RateLimitOnLimit.REJECT,
        max_queue_wait_seconds=5,
    )
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    first = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=None, user_rule=None, team_id=None, user_id=user_a
    )
    assert first.allowed is True

    # A different user's OWN org-default-per-user counter is untouched.
    second = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=None, user_rule=None, team_id=None, user_id=user_b
    )
    assert second.allowed is True
