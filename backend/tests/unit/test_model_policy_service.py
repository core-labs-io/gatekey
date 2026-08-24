"""Unit tests for `services/model_policy.py`'s pure, in-memory pieces
(Phase 1.3, BD-9).

`ModelPolicySnapshot`/`ModelPolicyCache`/`check_model_policy()` are all
pure/synchronous/zero-I/O by design (AC-3a) - these tests exercise them
directly, with no app/DB/HTTP involved. DB-backed `load_policy_snapshot`/
`get_policy`/`set_policy` are covered by the integration tests (a real
Postgres is required to exercise the upsert statement meaningfully) and by
the admin-route unit tests via monkeypatching.
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.api.v1.gateway.common import check_content_classification, check_model_policy
from gatekey.errors import ModelDeniedError
from gatekey.services.model_policy import (
    ContentAwareRuleCache,
    ContentAwareRuleSnapshot,
    MemberModelPolicyCache,
    ModelAccessDecision,
    ModelPolicyCache,
    ModelPolicySnapshot,
    TeamModelPolicyCache,
    UnknownModelInPolicyError,
    resolve_content_classification,
    resolve_model_access,
)

# --- ModelPolicySnapshot.is_allowed() ----------------------------------------


def test_unconfigured_snapshot_is_permissive() -> None:
    """AC-4: no policy ever set -> every model is allowed."""
    snapshot = ModelPolicySnapshot(mode="unconfigured", models=frozenset())
    assert snapshot.is_allowed("gpt-4o") is True
    assert snapshot.is_allowed("anything-at-all") is True


def test_default_constructed_snapshot_is_unconfigured_and_permissive() -> None:
    snapshot = ModelPolicySnapshot(mode="unconfigured")
    assert snapshot.models == frozenset()
    assert snapshot.is_allowed("gpt-4o") is True


def test_empty_allowlist_denies_everything() -> None:
    """AC-5: an allowlist with zero entries permits nothing."""
    snapshot = ModelPolicySnapshot(mode="allowlist", models=frozenset())
    assert snapshot.is_allowed("gpt-4o") is False
    assert snapshot.is_allowed("claude-sonnet-5") is False


def test_empty_denylist_denies_nothing() -> None:
    """AC-6: a denylist with zero entries permits everything."""
    snapshot = ModelPolicySnapshot(mode="denylist", models=frozenset())
    assert snapshot.is_allowed("gpt-4o") is True
    assert snapshot.is_allowed("claude-sonnet-5") is True


def test_allowlist_permits_only_listed_models() -> None:
    snapshot = ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))
    assert snapshot.is_allowed("gpt-4o") is True
    assert snapshot.is_allowed("gpt-4o-mini") is False


def test_denylist_denies_only_listed_models() -> None:
    snapshot = ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
    assert snapshot.is_allowed("gpt-4o") is False
    assert snapshot.is_allowed("gpt-4o-mini") is True


@pytest.mark.parametrize("variant", ["GPT-4o", " gpt-4o", "gpt-4o ", "Gpt-4O"])
def test_allowlist_membership_is_exact_no_case_or_whitespace_normalization(variant: str) -> None:
    """Section 3.1's adversarial case, at the pure-snapshot level: an
    allowlist containing "gpt-4o" must not match a case/whitespace variant.
    (The full bypass-proof story additionally relies on `resolve_route()`
    rejecting any such variant before this check ever runs - see the
    gateway-route-level test for the end-to-end 404-not-403-not-200
    behavior.)
    """
    snapshot = ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))
    assert snapshot.is_allowed(variant) is False


# --- ModelPolicyCache ---------------------------------------------------------


def test_cache_defaults_to_unconfigured_permissive_snapshot() -> None:
    cache = ModelPolicyCache()
    snapshot = cache.get()
    assert snapshot.mode == "unconfigured"
    assert snapshot.is_allowed("gpt-4o") is True


def test_cache_can_be_constructed_with_an_initial_snapshot() -> None:
    initial = ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
    cache = ModelPolicyCache(initial=initial)
    assert cache.get() is initial


def test_cache_set_replaces_the_snapshot_wholesale() -> None:
    cache = ModelPolicyCache()
    assert cache.get().mode == "unconfigured"

    new_snapshot = ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))
    cache.set(new_snapshot)

    assert cache.get() is new_snapshot
    assert cache.get().mode == "allowlist"


# --- ModelPolicyCache generation counter / CAS (security review finding,
# second round, design doc section 2.2/ADR-3 addendum) -----------------------


def test_cache_starts_at_generation_zero() -> None:
    cache = ModelPolicyCache()
    assert cache.get_generation() == 0


def test_cache_set_bumps_the_generation_and_returns_it() -> None:
    cache = ModelPolicyCache()
    new_snapshot = ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))

    returned = cache.set(new_snapshot)

    assert cache.get_generation() == 1
    assert returned == 1

    cache.set(ModelPolicySnapshot(mode="denylist", models=frozenset()))
    assert cache.get_generation() == 2


def test_set_if_current_applies_and_bumps_generation_when_generation_matches() -> None:
    cache = ModelPolicyCache()
    expected_generation = cache.get_generation()
    new_snapshot = ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))

    applied = cache.set_if_current(new_snapshot, expected_generation)

    assert applied is True
    assert cache.get() is new_snapshot
    assert cache.get_generation() == expected_generation + 1


def test_set_if_current_is_a_noop_when_generation_is_stale() -> None:
    """The race this exists to close: something else (e.g. an admin `PUT`
    via `set()`) already wrote a newer snapshot after the caller captured
    its `expected_generation` - `set_if_current()` must leave the cache
    untouched and report it was not applied, rather than clobbering the
    newer write with a stale one."""
    cache = ModelPolicyCache()
    stale_expected_generation = cache.get_generation()

    # Someone else's write lands first (e.g. a concurrent admin PUT).
    winning_snapshot = ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
    cache.set(winning_snapshot)

    # A stale caller (e.g. self-heal, holding the pre-PUT generation) now
    # tries to apply an older/different result.
    stale_snapshot = ModelPolicySnapshot(mode="unconfigured", models=frozenset())
    applied = cache.set_if_current(stale_snapshot, stale_expected_generation)

    assert applied is False
    assert cache.get() is winning_snapshot  # untouched by the superseded caller
    assert cache.get_generation() == 1  # only the winning set() counted


# --- check_model_policy() -----------------------------------------------------


def test_check_model_policy_allows_when_permitted() -> None:
    cache = ModelPolicyCache(ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"})))
    check_model_policy("gpt-4o", cache)  # must not raise


def test_check_model_policy_raises_model_denied_error_when_denied() -> None:
    cache = ModelPolicyCache(ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"})))
    with pytest.raises(ModelDeniedError) as exc_info:
        check_model_policy("gpt-4o", cache)
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "model_denied"
    assert "gpt-4o" in exc_info.value.message


def test_check_model_policy_default_unconfigured_cache_allows_any_registry_model() -> None:
    cache = ModelPolicyCache()
    check_model_policy("gpt-4o", cache)  # must not raise
    check_model_policy("claude-sonnet-5", cache)  # must not raise


# --- UnknownModelInPolicyError -------------------------------------------------


def test_unknown_model_in_policy_error_message_lists_offending_models_sorted() -> None:
    error = UnknownModelInPolicyError(["zzz-fake-model", "aaa-fake-model"])
    assert error.unknown_models == ["zzz-fake-model", "aaa-fake-model"]
    assert "aaa-fake-model" in error.message
    assert "zzz-fake-model" in error.message
    # Sorted in the message regardless of input order.
    assert error.message.index("aaa-fake-model") < error.message.index("zzz-fake-model")


# --- Phase 2 (BD-12): TeamModelPolicyCache / resolve_model_access -------------


def _permissive_org_cache() -> ModelPolicyCache:
    return ModelPolicyCache()  # unconfigured -> permissive


def test_team_cache_get_returns_none_for_unknown_team() -> None:
    assert TeamModelPolicyCache().get(uuid.uuid4()) is None


def test_team_cache_set_all_replaces_whole_snapshot() -> None:
    cache = TeamModelPolicyCache()
    team_a, team_b = uuid.uuid4(), uuid.uuid4()
    cache.set_all({team_a: frozenset({"gpt-4o"})})
    cache.set_all({team_b: frozenset({"claude-sonnet-5"})})
    assert cache.get(team_a) is None  # full replace, not merge
    assert cache.get(team_b) == frozenset({"claude-sonnet-5"})


def test_team_cache_set_team_updates_one_entry_keeping_others() -> None:
    cache = TeamModelPolicyCache()
    team_a, team_b = uuid.uuid4(), uuid.uuid4()
    cache.set_all({team_a: frozenset({"gpt-4o"})})
    cache.set_team(team_b, frozenset({"gpt-4o-mini"}))
    assert cache.get(team_a) == frozenset({"gpt-4o"})
    assert cache.get(team_b) == frozenset({"gpt-4o-mini"})


def test_resolve_model_access_org_denial_wins_and_names_org_layer() -> None:
    """Layering: the org baseline is checked first - even a team overlay
    that would allow the model cannot re-enable an org-denied one (AC3.2's
    structural guarantee)."""
    org_cache = ModelPolicyCache(
        ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
    )
    team_id = uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o"})})
    decision = resolve_model_access(
        "gpt-4o", org_cache=org_cache, team_cache=team_cache, team_id=team_id
    )
    assert decision == ModelAccessDecision(allowed=False, blocking_layer="org")


def test_resolve_model_access_team_overlay_narrows_org_baseline() -> None:
    team_id = uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o-mini"})})
    denied = resolve_model_access(
        "gpt-4o", org_cache=_permissive_org_cache(), team_cache=team_cache, team_id=team_id
    )
    assert denied == ModelAccessDecision(allowed=False, blocking_layer="team")
    allowed = resolve_model_access(
        "gpt-4o-mini",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
    )
    assert allowed == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_resolve_model_access_no_restriction_row_means_org_baseline_only() -> None:
    """Absence of a team entry = no further restriction (design doc 1.3)."""
    decision = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=TeamModelPolicyCache(),
        team_id=uuid.uuid4(),
    )
    assert decision.allowed is True


def test_resolve_model_access_legacy_none_team_id_skips_team_layer() -> None:
    """team_id=None (legacy flat path) never consults the team overlay,
    even if overlays exist for other teams."""
    team_cache = TeamModelPolicyCache({uuid.uuid4(): frozenset()})
    decision = resolve_model_access(
        "gpt-4o", org_cache=_permissive_org_cache(), team_cache=team_cache, team_id=None
    )
    assert decision.allowed is True


def test_check_model_policy_team_denial_is_403_model_denied_with_team_layer() -> None:
    """BD-13: same code/status as an org denial; only the message names the
    blocking layer."""
    team_id = uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o-mini"})})
    with pytest.raises(ModelDeniedError) as exc_info:
        check_model_policy("gpt-4o", _permissive_org_cache(), team_cache, team_id)
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "model_denied"
    assert exc_info.value.blocking_layer == "team"
    assert "team restriction" in exc_info.value.message


def test_check_model_policy_org_denial_message_names_org_policy() -> None:
    cache = ModelPolicyCache(ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"})))
    with pytest.raises(ModelDeniedError) as exc_info:
        check_model_policy("gpt-4o", cache)
    assert exc_info.value.blocking_layer == "org"
    assert "org policy" in exc_info.value.message


# --- Per-team-member narrowing overlay (third layer) -------------------------


def test_member_cache_get_returns_none_for_unknown_member() -> None:
    assert MemberModelPolicyCache().get(uuid.uuid4(), uuid.uuid4()) is None


def test_member_cache_set_all_replaces_whole_snapshot() -> None:
    cache = MemberModelPolicyCache()
    team_a, user_a = uuid.uuid4(), uuid.uuid4()
    team_b, user_b = uuid.uuid4(), uuid.uuid4()
    cache.set_all({(team_a, user_a): frozenset({"gpt-4o"})})
    cache.set_all({(team_b, user_b): frozenset({"claude-sonnet-5"})})
    assert cache.get(team_a, user_a) is None  # full replace, not merge
    assert cache.get(team_b, user_b) == frozenset({"claude-sonnet-5"})


def test_member_cache_set_member_updates_one_entry_keeping_others() -> None:
    cache = MemberModelPolicyCache()
    team_id, user_a, user_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.set_all({(team_id, user_a): frozenset({"gpt-4o"})})
    cache.set_member(team_id, user_b, frozenset({"gpt-4o-mini"}))
    assert cache.get(team_id, user_a) == frozenset({"gpt-4o"})
    assert cache.get(team_id, user_b) == frozenset({"gpt-4o-mini"})


def test_resolve_model_access_member_overlay_narrows_team_baseline() -> None:
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o", "gpt-4o-mini"})})
    member_cache = MemberModelPolicyCache({(team_id, user_id): frozenset({"gpt-4o-mini"})})
    denied = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=member_cache,
        user_id=user_id,
    )
    assert denied == ModelAccessDecision(allowed=False, blocking_layer="member")
    allowed = resolve_model_access(
        "gpt-4o-mini",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=member_cache,
        user_id=user_id,
    )
    assert allowed == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_resolve_model_access_team_denial_wins_over_member_overlay() -> None:
    """Layering one level further: a member overlay that would allow the
    model cannot re-enable a team-denied one - the team layer is checked
    first and short-circuits before the member layer is ever consulted."""
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o-mini"})})
    member_cache = MemberModelPolicyCache({(team_id, user_id): frozenset({"gpt-4o"})})
    decision = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=member_cache,
        user_id=user_id,
    )
    assert decision == ModelAccessDecision(allowed=False, blocking_layer="team")


def test_resolve_model_access_no_member_restriction_row_means_team_baseline_only() -> None:
    """Absence of a member entry = no further restriction beyond the team's
    own effective set."""
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o"})})
    decision = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=MemberModelPolicyCache(),
        user_id=user_id,
    )
    assert decision.allowed is True


def test_resolve_model_access_member_layer_skipped_when_cache_or_user_id_missing() -> None:
    """`member_cache=None`/`user_id=None` (a caller not yet updated to pass
    them) preserves byte-for-byte pre-member-layer behavior - never denies
    on a layer it wasn't given enough to evaluate."""
    team_id = uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o"})})
    decision = resolve_model_access(
        "gpt-4o", org_cache=_permissive_org_cache(), team_cache=team_cache, team_id=team_id
    )
    assert decision.allowed is True


def test_resolve_model_access_legacy_none_team_id_skips_member_layer_too() -> None:
    """`team_id=None` skips the team AND member layers - a member overlay is
    meaningless outside a team context, same as the team layer itself."""
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    member_cache = MemberModelPolicyCache({(team_id, user_id): frozenset()})
    decision = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=TeamModelPolicyCache(),
        team_id=None,
        member_cache=member_cache,
        user_id=user_id,
    )
    assert decision.allowed is True


def test_check_model_policy_member_denial_is_403_model_denied_with_member_layer() -> None:
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o", "gpt-4o-mini"})})
    member_cache = MemberModelPolicyCache({(team_id, user_id): frozenset({"gpt-4o-mini"})})
    with pytest.raises(ModelDeniedError) as exc_info:
        check_model_policy(
            "gpt-4o", _permissive_org_cache(), team_cache, team_id, member_cache, user_id
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "model_denied"
    assert exc_info.value.blocking_layer == "member"
    assert "team lead" in exc_info.value.message


def test_resolve_model_access_member_empty_list_blocks_every_team_model() -> None:
    """QA gap (Member Model Assignment review, item 3b): an EMPTY `models`
    list for a `(team_id, user_id)` entry is a real, intentional lockout -
    "this member can use NOTHING" - and must be distinguished from the
    ABSENCE of an entry (`None`, "no further restriction beyond the team's
    own effective set"). Both are exercised side-by-side here so a
    regression that ever conflated `frozenset()` with "no restriction"
    (e.g. an `is not None` check accidentally becoming truthiness) fails
    loudly."""
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o", "gpt-4o-mini"})})

    locked_out_cache = MemberModelPolicyCache({(team_id, user_id): frozenset()})
    for model in ("gpt-4o", "gpt-4o-mini"):
        decision = resolve_model_access(
            model,
            org_cache=_permissive_org_cache(),
            team_cache=team_cache,
            team_id=team_id,
            member_cache=locked_out_cache,
            user_id=user_id,
        )
        assert decision == ModelAccessDecision(allowed=False, blocking_layer="member")

    # Contrast: no row at all for this (team_id, user_id) -> team baseline
    # applies unchanged, both models allowed.
    no_row_cache = MemberModelPolicyCache()
    for model in ("gpt-4o", "gpt-4o-mini"):
        decision = resolve_model_access(
            model,
            org_cache=_permissive_org_cache(),
            team_cache=team_cache,
            team_id=team_id,
            member_cache=no_row_cache,
            user_id=user_id,
        )
        assert decision == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_resolve_model_access_stale_wider_member_restriction_cannot_over_permit_past_a_tightened_team() -> None:
    """QA gap (item 3a): if a member's cached restriction is WIDER than the
    team's CURRENT (already-tightened) restriction - e.g. the member row was
    written before the team was narrowed, and hasn't itself been re-written
    since - the team layer (checked first, unconditionally, at read time)
    still independently blocks any model the team no longer allows. The
    member layer can only ever narrow further, never widen past whatever the
    team layer already decided - so a stale, over-broad member entry cannot
    silently over-permit. This is the read-time counterpart to
    `test_resolve_model_access_team_denial_wins_over_member_overlay` above,
    phrased explicitly for the "team was tightened after the member row was
    written" scenario called out in the QA review."""
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    # Team was narrowed AFTER the member restriction below was written -
    # the member row still lists "gpt-4o", which the team no longer allows.
    team_cache = TeamModelPolicyCache({team_id: frozenset({"gpt-4o-mini"})})
    stale_member_cache = MemberModelPolicyCache(
        {(team_id, user_id): frozenset({"gpt-4o", "gpt-4o-mini"})}
    )
    decision = resolve_model_access(
        "gpt-4o",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=stale_member_cache,
        user_id=user_id,
    )
    assert decision == ModelAccessDecision(allowed=False, blocking_layer="team")
    # The still-permitted model keeps working normally.
    decision_ok = resolve_model_access(
        "gpt-4o-mini",
        org_cache=_permissive_org_cache(),
        team_cache=team_cache,
        team_id=team_id,
        member_cache=stale_member_cache,
        user_id=user_id,
    )
    assert decision_ok == ModelAccessDecision(allowed=True, blocking_layer=None)


# --- Phase 3 (BD-5): ContentAwareRuleCache / resolve_content_classification --


def test_content_aware_cache_get_returns_none_for_unconfigured_category() -> None:
    assert ContentAwareRuleCache().get("pii") is None


def test_content_aware_cache_set_all_replaces_whole_snapshot() -> None:
    cache = ContentAwareRuleCache()
    cache.set_all({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"gpt-4o"}))})
    cache.set_all({"source_code": ContentAwareRuleSnapshot(enabled=False, allowed_models=frozenset())})
    assert cache.get("pii") is None  # full replace, not merge
    assert cache.get("source_code") is not None


def test_content_aware_cache_set_category_updates_one_entry_keeping_others() -> None:
    cache = ContentAwareRuleCache()
    cache.set_all({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"gpt-4o"}))})
    cache.set_category("financial_data", ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset()))
    assert cache.get("pii").enabled is True
    assert cache.get("financial_data").enabled is True


def test_resolve_content_classification_no_categories_triggered_is_always_allowed() -> None:
    cache = ContentAwareRuleCache({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset())})
    decision = resolve_content_classification("gpt-4o", cache=cache, category_findings=frozenset())
    assert decision == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_resolve_content_classification_disabled_rule_never_restricts_even_with_pii() -> None:
    cache = ContentAwareRuleCache({"pii": ContentAwareRuleSnapshot(enabled=False, allowed_models=frozenset())})
    decision = resolve_content_classification("gpt-4o", cache=cache, category_findings=frozenset({"pii"}))
    assert decision.allowed is True


def test_resolve_content_classification_no_rule_configured_never_restricts() -> None:
    decision = resolve_content_classification(
        "gpt-4o", cache=ContentAwareRuleCache(), category_findings=frozenset({"pii"})
    )
    assert decision.allowed is True


def test_resolve_content_classification_pii_plus_enabled_rule_restricts_to_allowed_set() -> None:
    cache = ContentAwareRuleCache(
        {"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"claude-sonnet-5"}))}
    )
    denied = resolve_content_classification("gpt-4o", cache=cache, category_findings=frozenset({"pii"}))
    assert denied == ModelAccessDecision(allowed=False, blocking_layer="content_classification")
    allowed = resolve_content_classification(
        "claude-sonnet-5", cache=cache, category_findings=frozenset({"pii"})
    )
    assert allowed == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_resolve_content_classification_empty_allowed_models_blocks_everything() -> None:
    """AC4.4: a triggered category with zero allowed models blocks all
    traffic in that category - real enforcement, not just a UI warning."""
    cache = ContentAwareRuleCache({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset())})
    decision = resolve_content_classification("gpt-4o", cache=cache, category_findings=frozenset({"pii"}))
    assert decision.allowed is False
    assert decision.blocking_layer == "content_classification"


def test_resolve_content_classification_multi_category_intersection_narrows_to_shared_model() -> None:
    """AC5.3.2's exact scenario: category A allows {X, Y}, category B allows
    {Y, Z}, both enabled and triggered -> only Y is allowed."""
    cache = ContentAwareRuleCache(
        {
            "financial_data": ContentAwareRuleSnapshot(
                enabled=True, allowed_models=frozenset({"model-x", "model-y"})
            ),
            "legal": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"model-y", "model-z"})),
        }
    )
    category_findings = frozenset({"financial_data", "legal"})
    assert resolve_content_classification(
        "model-y", cache=cache, category_findings=category_findings
    ) == ModelAccessDecision(allowed=True, blocking_layer=None)
    assert resolve_content_classification(
        "model-x", cache=cache, category_findings=category_findings
    ) == ModelAccessDecision(allowed=False, blocking_layer="content_classification")
    assert resolve_content_classification(
        "model-z", cache=cache, category_findings=category_findings
    ) == ModelAccessDecision(allowed=False, blocking_layer="content_classification")


def test_resolve_content_classification_multi_category_disjoint_sets_blocks_everything() -> None:
    """AC5.3.2: disjoint allowed-models sets across matched enabled
    categories -> empty intersection -> blocked."""
    cache = ContentAwareRuleCache(
        {
            "financial_data": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"model-x"})),
            "legal": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"model-z"})),
        }
    )
    decision = resolve_content_classification(
        "model-x", cache=cache, category_findings=frozenset({"financial_data", "legal"})
    )
    assert decision == ModelAccessDecision(allowed=False, blocking_layer="content_classification")


def test_resolve_content_classification_multi_category_only_triggered_categories_considered() -> None:
    """A category that has an enabled rule but was NOT triggered this
    request must not narrow the result - only categories actually present
    in `category_findings` participate in the intersection."""
    cache = ContentAwareRuleCache(
        {
            "pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset({"model-x"})),
            "legal": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset()),
        }
    )
    # Only "pii" triggered - "legal" (which would otherwise block
    # everything, given its empty allowed_models) is irrelevant here.
    decision = resolve_content_classification("model-x", cache=cache, category_findings=frozenset({"pii"}))
    assert decision == ModelAccessDecision(allowed=True, blocking_layer=None)


def test_check_content_classification_raises_model_denied_with_content_classification_layer() -> None:
    cache = ContentAwareRuleCache({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset())})
    with pytest.raises(ModelDeniedError) as exc_info:
        check_content_classification("gpt-4o", cache, category_findings=frozenset({"pii"}))
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "model_denied"
    assert exc_info.value.blocking_layer == "content_classification"


def test_check_content_classification_allows_when_no_categories_triggered() -> None:
    cache = ContentAwareRuleCache({"pii": ContentAwareRuleSnapshot(enabled=True, allowed_models=frozenset())})
    check_content_classification("gpt-4o", cache, category_findings=frozenset())  # must not raise
