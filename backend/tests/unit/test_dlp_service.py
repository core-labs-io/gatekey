"""Unit tests for `services/dlp.py` (Phase 3, BD-1/BD-7).

Redaction-correctness tests exercise the REAL Presidio `AnalyzerEngine`
(`build_analyzer_engine()` is process-wide `@lru_cache`d - see that
function's docstring - so this pays the spaCy load cost once for the whole
test session, not once per test). No database is touched anywhere in this
module - `requires_sync_scan`/`resolve_builtin_action`/`overall_action_taken`
are pure, and the scan/redact tests only need the in-process engine.
"""

from __future__ import annotations

import pytest

from gatekey.db.models.dlp_policy import DlpAction
from gatekey.services.dlp import (
    CustomPatternConfig,
    DlpFinding,
    DlpPolicyConfig,
    InboundScanningNotImplementedError,
    build_analyzer_engine,
    has_any_scanning_enabled,
    overall_action_taken,
    requires_sync_scan,
    resolve_builtin_action,
    scan_texts,
    validate_scan_inbound_responses,
)

# ---------------------------------------------------------------------------
# Pure logic - no Presidio, no DB.
# ---------------------------------------------------------------------------


def test_resolve_builtin_action_team_override_wins_when_present() -> None:
    assert resolve_builtin_action(DlpAction.LOG, DlpAction.BLOCK) == DlpAction.BLOCK


def test_resolve_builtin_action_falls_back_to_org_default_when_no_override() -> None:
    assert resolve_builtin_action(DlpAction.REDACT, None) == DlpAction.REDACT


def test_has_any_scanning_enabled_false_when_nothing_configured() -> None:
    policy = DlpPolicyConfig(
        detectors_enabled={"ssn": False, "credit_card": False, "email": False, "phone": False},
        default_action=DlpAction.LOG,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )
    assert has_any_scanning_enabled(policy, []) is False


def test_has_any_scanning_enabled_true_with_one_detector() -> None:
    policy = DlpPolicyConfig(
        detectors_enabled={"ssn": True, "credit_card": False, "email": False, "phone": False},
        default_action=DlpAction.LOG,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )
    assert has_any_scanning_enabled(policy, []) is True


def test_has_any_scanning_enabled_true_with_only_a_custom_pattern() -> None:
    policy = DlpPolicyConfig(
        detectors_enabled={"ssn": False, "credit_card": False, "email": False, "phone": False},
        default_action=DlpAction.LOG,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )
    patterns = [CustomPatternConfig(name="x", pattern="x", action=DlpAction.LOG)]
    assert has_any_scanning_enabled(policy, patterns) is True


@pytest.mark.parametrize(
    "effective_action,custom_actions,content_aware,expected",
    [
        (DlpAction.LOG, [], False, False),  # AC2.6: pure log-only -> async
        (DlpAction.REDACT, [], False, True),  # AC2.8: redact -> sync
        (DlpAction.BLOCK, [], False, True),  # AC2.8: block -> sync
        (DlpAction.LOG, [DlpAction.BLOCK], False, True),  # a custom pattern can force sync
        (DlpAction.LOG, [], True, True),  # AC2.9/AC5.3.2: an enabled content-aware category forces sync
    ],
)
def test_requires_sync_scan(effective_action, custom_actions, content_aware, expected) -> None:
    patterns = [CustomPatternConfig(name="p", pattern="p", action=a) for a in custom_actions]
    assert (
        requires_sync_scan(
            effective_builtin_action=effective_action,
            custom_patterns=patterns,
            content_aware_classification_enabled=content_aware,
        )
        is expected
    )


def test_validate_scan_inbound_responses_accepts_false() -> None:
    validate_scan_inbound_responses(False)  # no-op, must not raise


def test_validate_scan_inbound_responses_rejects_true() -> None:
    """Security review finding 4: response-scanning was never implemented -
    the toggle must not silently accept `true`."""
    with pytest.raises(InboundScanningNotImplementedError) as excinfo:
        validate_scan_inbound_responses(True)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "inbound_scanning_not_implemented"


def test_presidio_regex_timeout_is_bounded_not_the_60s_default() -> None:
    """Security review finding 3: a custom DLP pattern is caller-supplied
    regex run synchronously on the <50ms p99 request path (AC2.10) - Presidio's
    own `regex`-library match timeout must be bounded well below the library
    default (60s), not left unset. `services.dlp` sets this as an import-time
    side effect (see the module docstring next to `REGEX_TIMEOUT_SECONDS`
    there) - this test just confirms it actually landed on the presidio
    modules that use it, not that it timed out (a full ReDoS proof isn't
    practical/needed here)."""
    import presidio_analyzer.analyzer_engine as presidio_analyzer_engine_module
    import presidio_analyzer.pattern_recognizer as presidio_pattern_recognizer_module

    assert presidio_pattern_recognizer_module.REGEX_TIMEOUT_SECONDS == 2
    assert presidio_analyzer_engine_module.REGEX_TIMEOUT_SECONDS == 2


def test_overall_action_taken_empty_findings_is_log() -> None:
    assert overall_action_taken([]) == DlpAction.LOG


def test_overall_action_taken_is_most_restrictive_of_mixed_findings() -> None:
    findings = [
        DlpFinding(name="EMAIL_ADDRESS", action=DlpAction.LOG),
        DlpFinding(name="US_SSN", action=DlpAction.REDACT),
    ]
    assert overall_action_taken(findings) == DlpAction.REDACT
    findings.append(DlpFinding(name="custom:x", action=DlpAction.BLOCK))
    assert overall_action_taken(findings) == DlpAction.BLOCK


# ---------------------------------------------------------------------------
# Redaction correctness - real Presidio engine, one per detector type + a
# custom pattern. Non-canonical/non-blacklisted sample values are used
# deliberately (e.g. Presidio's `UsSsnRecognizer` explicitly invalidates the
# textbook placeholder "123-45-6789" - see `invalidate_result`).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    return build_analyzer_engine()


def _all_detectors_policy(*, default_action: DlpAction) -> DlpPolicyConfig:
    return DlpPolicyConfig(
        detectors_enabled={"ssn": True, "credit_card": True, "email": True, "phone": True},
        default_action=default_action,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )


async def test_scan_texts_ssn_is_detected_and_redacted(engine) -> None:
    outcome = await scan_texts(
        engine,
        ["my SSN is 234-56-7890 today"],
        policy=_all_detectors_policy(default_action=DlpAction.REDACT),
        custom_patterns=[],
        team_override=None,
    )
    assert outcome.ran is True
    assert any(f.name == "US_SSN" for f in outcome.findings)
    assert outcome.redacted_texts is not None
    assert "234-56-7890" not in outcome.redacted_texts[0]
    assert "[REDACTED]" in outcome.redacted_texts[0]


async def test_scan_texts_credit_card_is_detected_and_redacted(engine) -> None:
    outcome = await scan_texts(
        engine,
        ["my card number is 4111111111111111 ok"],
        policy=_all_detectors_policy(default_action=DlpAction.REDACT),
        custom_patterns=[],
        team_override=None,
    )
    assert any(f.name == "CREDIT_CARD" for f in outcome.findings)
    assert "4111111111111111" not in outcome.redacted_texts[0]


async def test_scan_texts_email_is_detected_and_redacted(engine) -> None:
    outcome = await scan_texts(
        engine,
        ["reach me at jane.doe@example.com please"],
        policy=_all_detectors_policy(default_action=DlpAction.REDACT),
        custom_patterns=[],
        team_override=None,
    )
    assert any(f.name == "EMAIL_ADDRESS" for f in outcome.findings)
    assert "jane.doe@example.com" not in outcome.redacted_texts[0]


async def test_scan_texts_phone_is_detected_and_redacted(engine) -> None:
    outcome = await scan_texts(
        engine,
        ["call me at 415-555-0132 tomorrow"],
        policy=_all_detectors_policy(default_action=DlpAction.REDACT),
        custom_patterns=[],
        team_override=None,
    )
    assert any(f.name == "PHONE_NUMBER" for f in outcome.findings)
    assert "415-555-0132" not in outcome.redacted_texts[0]


async def test_scan_texts_custom_pattern_uses_its_own_action_not_org_default(engine) -> None:
    """AC2.4: a custom pattern's action is independent of the org default -
    here the org default is `log` (would never redact/block a built-in
    finding) but the custom pattern is `block`."""
    policy = DlpPolicyConfig(
        detectors_enabled={"ssn": False, "credit_card": False, "email": False, "phone": False},
        default_action=DlpAction.LOG,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )
    patterns = [CustomPatternConfig(name="project-codename", pattern=r"\bProjectX\b", action=DlpAction.BLOCK)]
    outcome = await scan_texts(
        engine, ["this mentions ProjectX explicitly"], policy=policy, custom_patterns=patterns, team_override=None
    )
    assert outcome.blocked is True
    assert any(f.name == "custom:project-codename" and f.action == DlpAction.BLOCK for f in outcome.findings)


async def test_scan_texts_log_action_never_redacts(engine) -> None:
    """AC2.5: `log` records a finding but never mutates the text."""
    outcome = await scan_texts(
        engine,
        ["contact jane.doe@example.com"],
        policy=_all_detectors_policy(default_action=DlpAction.LOG),
        custom_patterns=[],
        team_override=None,
    )
    assert any(f.name == "EMAIL_ADDRESS" for f in outcome.findings)
    assert outcome.redacted_texts is None  # nothing was redacted
    assert outcome.blocked is False


async def test_scan_texts_team_override_upgrades_action_to_block(engine) -> None:
    """AC2.4: a team override replaces the org default for built-in
    findings only."""
    outcome = await scan_texts(
        engine,
        ["contact jane.doe@example.com"],
        policy=_all_detectors_policy(default_action=DlpAction.LOG),
        custom_patterns=[],
        team_override=DlpAction.BLOCK,
    )
    assert outcome.blocked is True


async def test_scan_texts_no_detectors_or_patterns_is_a_fast_noop(engine) -> None:
    policy = DlpPolicyConfig(
        detectors_enabled={"ssn": False, "credit_card": False, "email": False, "phone": False},
        default_action=DlpAction.LOG,
        store_raw_flagged_content=False,
        scan_inbound_responses=False,
    )
    outcome = await scan_texts(engine, ["nothing to see here"], policy=policy, custom_patterns=[], team_override=None)
    assert outcome.ran is False
    assert outcome.findings == []
    assert outcome.pii_detected is False
