"""Unit tests for `services/residency.py` (Phase 3, BD-3/BD-7).

All pure/synchronous/zero-I/O - no database, mirrors `test_model_policy_
service.py`'s posture toward `ModelPolicyCache`/`resolve_model_access`.
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.providers.model_registry import ModelRoute, ModelCapability
from gatekey.services.residency import (
    ResidencyDecision,
    ResidencyRuleCache,
    ResidencyRuleSnapshot,
    coarsen_gcp_location,
    resolve_model_region,
    resolve_residency,
)

# --- resolve_model_region -----------------------------------------------------


def test_resolve_model_region_openai_and_anthropic_are_static_us() -> None:
    route = ModelRoute(provider="openai", capability=ModelCapability.CHAT, native_model_id="gpt-4o")
    assert resolve_model_region(route, None) == "us"
    route = ModelRoute(provider="anthropic", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, None) == "us"


def test_resolve_model_region_openrouter_is_always_unknown() -> None:
    route = ModelRoute(provider="openrouter", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"anything": "here"}) is None


def test_resolve_model_region_vertex_ai_coarsens_location() -> None:
    route = ModelRoute(provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"location": "us-central1"}) == "us"
    assert resolve_model_region(route, {"location": "europe-west4"}) == "eu"
    assert resolve_model_region(route, {"location": "asia-southeast1"}) == "apac"


def test_resolve_model_region_vertex_ai_no_key_configured_is_unknown() -> None:
    route = ModelRoute(provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, None) is None


def test_resolve_model_region_vertex_ai_unrecognized_location_is_unknown_not_a_guess() -> None:
    route = ModelRoute(provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"location": "me-west1"}) is None


def test_resolve_model_region_ollama_reads_admin_settable_region_verbatim() -> None:
    route = ModelRoute(provider="ollama", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"region": "eu"}) == "eu"


def test_resolve_model_region_ollama_unset_region_is_unknown_by_default() -> None:
    """Ratified #5: a residency rule blocks self-hosted traffic by default
    until an admin explicitly tags its region."""
    route = ModelRoute(provider="ollama", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"base_url": "http://localhost:11434"}) is None
    assert resolve_model_region(route, None) is None


def test_resolve_model_region_ollama_rejects_a_region_outside_supported_set() -> None:
    route = ModelRoute(provider="ollama", capability=ModelCapability.CHAT, native_model_id="x")
    assert resolve_model_region(route, {"region": "mars"}) is None


@pytest.mark.parametrize(
    "location,expected",
    [
        ("us-central1", "us"),
        ("northamerica-northeast1", "us"),
        ("southamerica-east1", "us"),
        ("europe-west4", "eu"),
        ("asia-southeast1", "apac"),
        ("australia-southeast1", "apac"),
        ("me-west1", None),
        ("totally-unknown-1", None),
    ],
)
def test_coarsen_gcp_location(location: str, expected: str | None) -> None:
    assert coarsen_gcp_location(location) == expected


# --- ResidencyRuleCache --------------------------------------------------------


def test_cache_defaults_to_no_rules() -> None:
    cache = ResidencyRuleCache()
    assert cache.get_org_rule() is None
    assert cache.get_team_rule(uuid.uuid4()) is None


def test_cache_set_all_replaces_whole_snapshot() -> None:
    cache = ResidencyRuleCache()
    team_a = uuid.uuid4()
    org_rule = ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")
    cache.set_all(org_rule, {team_a: ResidencyRuleSnapshot(allowed_regions=frozenset({"eu"}), violation_behavior="warn")})
    assert cache.get_org_rule() == org_rule
    assert cache.get_team_rule(team_a).allowed_regions == frozenset({"eu"})

    # A second set_all with no team rules fully replaces (not merges).
    cache.set_all(None, {})
    assert cache.get_org_rule() is None
    assert cache.get_team_rule(team_a) is None


def test_cache_set_team_rule_keeps_other_teams_and_can_remove() -> None:
    cache = ResidencyRuleCache()
    team_a, team_b = uuid.uuid4(), uuid.uuid4()
    snap = ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")
    cache.set_team_rule(team_a, snap)
    cache.set_team_rule(team_b, snap)
    assert cache.get_team_rule(team_a) == snap
    cache.set_team_rule(team_a, None)  # removal
    assert cache.get_team_rule(team_a) is None
    assert cache.get_team_rule(team_b) == snap  # untouched


# --- resolve_residency ----------------------------------------------------------


def test_resolve_residency_no_rule_anywhere_is_unrestricted() -> None:
    decision = resolve_residency("us", cache=ResidencyRuleCache(), team_id=None)
    assert decision == ResidencyDecision(allowed=True, violated=False, behavior=None, region="us")


def test_resolve_residency_region_within_org_allowlist_passes() -> None:
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us", "eu"}), violation_behavior="hard_block")
    )
    decision = resolve_residency("eu", cache=cache, team_id=None)
    assert decision.allowed is True
    assert decision.violated is False


def test_resolve_residency_hard_block_rejects_out_of_allowlist_region() -> None:
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")
    )
    decision = resolve_residency("apac", cache=cache, team_id=None)
    assert decision.allowed is False
    assert decision.violated is True
    assert decision.behavior == "hard_block"


def test_resolve_residency_warn_behavior_allows_but_flags_violation() -> None:
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="warn")
    )
    decision = resolve_residency("apac", cache=cache, team_id=None)
    assert decision.allowed is True  # warn never blocks
    assert decision.violated is True  # but it IS a violation, for the audit trail


def test_resolve_residency_unknown_region_always_violates_an_active_rule() -> None:
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")
    )
    decision = resolve_residency(None, cache=cache, team_id=None)
    assert decision.allowed is False
    assert decision.violated is True


def test_resolve_residency_team_rule_narrower_than_org_still_enforces() -> None:
    """A team rule that is narrower than the org rule still blocks a region
    the org rule alone would have allowed - both layers are checked
    cumulatively on every read (security review fix), not "team rule only,
    trusted forever because write-time narrowing validated it once")."""
    team_id = uuid.uuid4()
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us", "eu"}), violation_behavior="hard_block"),
        team_rules={team_id: ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")},
    )
    # "eu" is allowed by the org rule but NOT by this team's narrower rule.
    decision = resolve_residency("eu", cache=cache, team_id=team_id)
    assert decision.allowed is False


def test_resolve_residency_falls_back_to_org_rule_when_team_has_none() -> None:
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")
    )
    decision = resolve_residency("us", cache=cache, team_id=uuid.uuid4())
    assert decision.allowed is True


# --- Regression: org-rule tightening after a team narrowed under the OLD org
# rule must still be enforced (security review finding - see resolve_
# residency's docstring for the full staleness-bug narrative) ---------------


def test_resolve_residency_org_rule_tightened_after_team_narrowed_still_blocks() -> None:
    """Reproduces the exact bug the innermost-only model missed: org
    originally allows [us, eu]; a team narrows to [eu] (valid at that
    time - a subset of the THEN-current org rule); the org is later
    tightened to [us] only, WITHOUT the team's row ever being touched or
    re-validated. Under the old innermost-only resolution, only the team's
    [eu] row would be consulted and the request would silently pass
    forever. Under the fixed cumulative model, the org's now-violated [us]
    rule is also checked and correctly blocks the request even though the
    team's own (now-stale) rule would have allowed it."""
    team_id = uuid.uuid4()
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us", "eu"}), violation_behavior="hard_block"),
        team_rules={team_id: ResidencyRuleSnapshot(allowed_regions=frozenset({"eu"}), violation_behavior="hard_block")},
    )
    # Sanity check: at this point, "eu" through the team is fine (team rule
    # passes, and the org rule - not yet tightened - also still allows "eu").
    assert resolve_residency("eu", cache=cache, team_id=team_id).allowed is True

    # The org rule is tightened to "us" only - simulating an admin PUT that
    # never touches (and has no reason to touch) the team's existing row.
    cache.set_org_rule(ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block"))

    # The team's own rule ([eu]) still, in isolation, "allows" eu - but the
    # org's now-tightened rule does not, and that must still be enforced.
    decision = resolve_residency("eu", cache=cache, team_id=team_id)
    assert decision.allowed is False
    assert decision.violated is True
    assert decision.behavior == "hard_block"

    # A region satisfying BOTH the (now-tightened) org rule and the team's
    # rule is not possible here (team only allows eu, org only allows us) -
    # confirms this isn't a "team rule ignored" regression, both are real.
    assert resolve_residency("us", cache=cache, team_id=team_id).allowed is False  # team disallows "us"


def test_resolve_residency_hard_block_outranks_warn_regardless_of_which_layer_has_it() -> None:
    """A `hard_block` violation on EITHER layer always outranks a `warn`
    violation on the other - checking cumulatively must never let a
    less-severe layer's outcome silently mask a more severe one."""
    team_id = uuid.uuid4()

    # org=warn, team=hard_block -> overall hard_block.
    cache = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="warn"),
        team_rules={team_id: ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block")},
    )
    decision = resolve_residency("eu", cache=cache, team_id=team_id)
    assert decision.allowed is False
    assert decision.behavior == "hard_block"

    # org=hard_block, team=warn -> overall still hard_block (order-independent).
    cache2 = ResidencyRuleCache(
        org_rule=ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="hard_block"),
        team_rules={team_id: ResidencyRuleSnapshot(allowed_regions=frozenset({"us"}), violation_behavior="warn")},
    )
    decision2 = resolve_residency("eu", cache=cache2, team_id=team_id)
    assert decision2.allowed is False
    assert decision2.behavior == "hard_block"
