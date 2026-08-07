"""Unit tests for `services/drift_detector.py`'s pure functions (Phase 5 -
Differentiators, 5.4 Provider Drift Detector): refusal detection (AC5.4.4),
output similarity (AC5.4.5), and fixed-threshold drift flagging (AC5.4.6).

The DB-touching orchestration (`run_canary_suite_for_org`, `establish_
baseline_if_ready`, `flag_drift`) and the cost-separation NFR need a real
Postgres + mocked provider HTTP transport to exercise meaningfully -
covered by QA's integration pass, not here (same split this codebase
already uses for `run_due_rotations`/hash-chain backfill).
"""

from __future__ import annotations

from decimal import Decimal

from gatekey.services.drift_detector import (
    compute_similarity,
    detect_refusal,
    latency_drift_delta_pct,
    refusal_rate_drift_delta_pp,
    similarity_drift_delta_pct,
)

# --- detect_refusal (AC5.4.4) ------------------------------------------------


def test_detect_refusal_true_for_common_refusal_phrasing() -> None:
    assert detect_refusal("I'm sorry, but I cannot help with that request.") is True
    assert detect_refusal("I can't provide instructions for that.") is True
    assert detect_refusal("I am not able to assist with this.") is True


def test_detect_refusal_false_for_a_normal_answer() -> None:
    assert detect_refusal("The capital of France is Paris.") is False
    assert detect_refusal("15 multiplied by 7 is 105.") is False


def test_detect_refusal_is_case_insensitive() -> None:
    assert detect_refusal("I CANNOT HELP WITH THAT.") is True


# --- compute_similarity (AC5.4.5) -------------------------------------------


def test_compute_similarity_identical_text_is_one() -> None:
    text = "The capital of France is Paris."
    assert compute_similarity(text, text) == Decimal("1.0")


def test_compute_similarity_completely_disjoint_text_is_zero() -> None:
    assert compute_similarity("apple banana cherry", "xylophone zeppelin quasar") == Decimal("0.0")


def test_compute_similarity_partial_overlap_is_between_zero_and_one() -> None:
    score = compute_similarity("the quick brown fox", "the slow brown dog")
    assert Decimal("0") < score < Decimal("1")


def test_compute_similarity_both_empty_is_trivially_one() -> None:
    assert compute_similarity("", "") == Decimal("1.0")


def test_compute_similarity_one_empty_is_zero() -> None:
    assert compute_similarity("", "some content") == Decimal("0.0")


def test_compute_similarity_is_symmetric() -> None:
    a, b = "the quick brown fox", "the slow brown dog"
    assert compute_similarity(a, b) == compute_similarity(b, a)


def test_compute_similarity_ignores_case_and_punctuation() -> None:
    assert compute_similarity("Hello, World!", "hello world") == Decimal("1.0")


# --- drift threshold flagging (AC5.4.6) -------------------------------------


def test_latency_drift_flagged_above_fifty_percent_increase() -> None:
    # 100ms baseline -> 160ms observed = 60% increase, over the 50% threshold.
    delta = latency_drift_delta_pct(Decimal("100"), Decimal("160"))
    assert delta is not None
    assert delta == Decimal("60.00")


def test_latency_drift_flagged_for_a_fifty_percent_speedup_too() -> None:
    # A dramatic speedup is still a behavior change worth surfacing.
    delta = latency_drift_delta_pct(Decimal("200"), Decimal("80"))
    assert delta is not None
    assert delta == Decimal("-60.00")


def test_latency_drift_not_flagged_within_fifty_percent() -> None:
    assert latency_drift_delta_pct(Decimal("100"), Decimal("140")) is None


def test_latency_drift_not_flagged_at_exactly_the_threshold_boundary() -> None:
    # Exactly 50% deviation is not ">50%".
    assert latency_drift_delta_pct(Decimal("100"), Decimal("150")) is None


def test_latency_drift_handles_zero_baseline_without_dividing_by_zero() -> None:
    assert latency_drift_delta_pct(Decimal("0"), Decimal("100")) is None


def test_refusal_rate_drift_flagged_above_twenty_point_increase() -> None:
    delta = refusal_rate_drift_delta_pp(Decimal("0.05"), Decimal("0.30"))
    assert delta is not None
    assert delta == Decimal("25.00")


def test_refusal_rate_drift_not_flagged_for_a_decrease() -> None:
    # AC5.4.6 only names a RISE as the drift signal.
    assert refusal_rate_drift_delta_pp(Decimal("0.50"), Decimal("0.10")) is None


def test_refusal_rate_drift_not_flagged_within_twenty_points() -> None:
    assert refusal_rate_drift_delta_pp(Decimal("0.05"), Decimal("0.20")) is None


def test_similarity_drift_flagged_below_point_seven() -> None:
    delta = similarity_drift_delta_pct(Decimal("0.65"))
    assert delta is not None
    assert delta == Decimal("-35.00")


def test_similarity_drift_not_flagged_at_or_above_point_seven() -> None:
    assert similarity_drift_delta_pct(Decimal("0.70")) is None
    assert similarity_drift_delta_pct(Decimal("0.95")) is None
