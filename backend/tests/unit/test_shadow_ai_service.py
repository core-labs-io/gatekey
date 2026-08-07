"""Unit tests for `services/shadow_ai.py`'s pure, DB-free logic (Phase 5 -
Differentiators, 5.1 Shadow AI Discovery): the AC5.1.1 hostname-match
data-minimization gate (`partition_events_by_hostname_match`) and the
AC5.1.8 "repeat violator" threshold predicate (`is_repeat_violator`).

The DB-touching orchestration (`ingest_events`'s actual persistence,
`get_shadow_ai_report`'s aggregation/team-scoping, the ingest-token
auth-boundary proof, the purge job) needs a real Postgres to exercise
meaningfully - covered by the integration suite, same split this codebase
already uses for `services/self_hosted_providers.py`
(`tests/unit/test_self_hosted_providers.py` vs.
`tests/integration/test_self_hosted_providers_api.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from gatekey.services.shadow_ai import (
    ShadowAiIngestEventInput,
    is_repeat_violator,
    partition_events_by_hostname_match,
    shadow_ai_ingest_token_matches,
)

_NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _event(host: str, user: str = "someone@example.com") -> ShadowAiIngestEventInput:
    return ShadowAiIngestEventInput(
        user_identifier=user,
        destination_host=host,
        occurred_at=_NOW,
        source="sase_log",
    )


# ---------------------------------------------------------------------------
# partition_events_by_hostname_match (AC5.1.1's data-minimization gate)
# ---------------------------------------------------------------------------


def test_mixed_batch_only_matched_hosts_survive() -> None:
    enabled = frozenset({"chat.openai.com", "claude.ai"})
    events = [
        _event("chat.openai.com"),
        _event("not-an-ai-tool.example.com"),
        _event("claude.ai"),
        _event("also-unrelated.example.net"),
    ]
    matched, dropped = partition_events_by_hostname_match(events, enabled)
    assert [e.destination_host for e in matched] == ["chat.openai.com", "claude.ai"]
    assert dropped == 2


def test_empty_batch_yields_nothing() -> None:
    matched, dropped = partition_events_by_hostname_match([], frozenset({"claude.ai"}))
    assert matched == []
    assert dropped == 0


def test_no_hostnames_enabled_drops_everything() -> None:
    events = [_event("chat.openai.com"), _event("claude.ai")]
    matched, dropped = partition_events_by_hostname_match(events, frozenset())
    assert matched == []
    assert dropped == 2


def test_all_hosts_matched_drops_nothing() -> None:
    enabled = frozenset({"chat.openai.com", "claude.ai"})
    events = [_event("chat.openai.com"), _event("claude.ai")]
    matched, dropped = partition_events_by_hostname_match(events, enabled)
    assert len(matched) == 2
    assert dropped == 0


def test_hostname_match_is_exact_not_substring_or_suffix() -> None:
    """A caller-controlled `destination_host` must never be treated as a
    pattern - `evil-chat.openai.com.attacker.example` must NOT match just
    because it contains `chat.openai.com` as a substring, and a bare
    subdomain relationship (`sub.claude.ai`) must not match `claude.ai`
    either unless it is itself an explicitly enabled row."""
    enabled = frozenset({"chat.openai.com"})
    events = [
        _event("evil-chat.openai.com.attacker.example"),
        _event("sub.chat.openai.com"),
        _event("CHAT.OPENAI.COM"),  # case-sensitive - different string
    ]
    matched, dropped = partition_events_by_hostname_match(events, enabled)
    assert matched == []
    assert dropped == 3


def test_disabled_hostname_is_treated_as_unmatched() -> None:
    """`enabled_hostnames` is expected to already be pre-filtered to
    `enabled = true` rows only (`_load_enabled_hostnames`'s job) - this test
    documents that this function itself has no separate "is it enabled"
    concept, it only ever sees the already-enabled set."""
    enabled = frozenset()  # simulates every known hostname currently disabled
    matched, dropped = partition_events_by_hostname_match([_event("chat.openai.com")], enabled)
    assert matched == []
    assert dropped == 1


# ---------------------------------------------------------------------------
# is_repeat_violator (AC5.1.8's fixed "trailing 7 days, >= 3 events" threshold)
# ---------------------------------------------------------------------------


def test_below_threshold_is_not_a_repeat_violator() -> None:
    assert is_repeat_violator(0) is False
    assert is_repeat_violator(1) is False
    assert is_repeat_violator(2) is False


def test_at_threshold_is_a_repeat_violator() -> None:
    assert is_repeat_violator(3) is True


def test_above_threshold_is_a_repeat_violator() -> None:
    assert is_repeat_violator(4) is True
    assert is_repeat_violator(100) is True


# ---------------------------------------------------------------------------
# shadow_ai_ingest_token_matches - fail-closed defaults (AC5.1.4), mirrors
# `test_scim_service.py`'s `scim_token_matches` coverage for the sibling
# mechanism.
# ---------------------------------------------------------------------------


def test_ingest_token_never_matches_when_no_config_row_exists() -> None:
    assert shadow_ai_ingest_token_matches(None, "gk_sai_anything") is False


def test_ingest_token_never_matches_when_no_token_generated_yet() -> None:
    from types import SimpleNamespace

    config = SimpleNamespace(ingest_token_hash=None)
    assert shadow_ai_ingest_token_matches(config, "gk_sai_anything") is False


def test_ingest_token_matches_correct_token() -> None:
    from types import SimpleNamespace

    from gatekey.services.service_accounts import hash_secret

    token = "gk_sai_correct-token-value"
    config = SimpleNamespace(ingest_token_hash=hash_secret(token))
    assert shadow_ai_ingest_token_matches(config, token) is True


def test_ingest_token_rejects_wrong_token() -> None:
    from types import SimpleNamespace

    from gatekey.services.service_accounts import hash_secret

    config = SimpleNamespace(ingest_token_hash=hash_secret("gk_sai_correct-token-value"))
    assert shadow_ai_ingest_token_matches(config, "gk_sai_wrong-token-value") is False
