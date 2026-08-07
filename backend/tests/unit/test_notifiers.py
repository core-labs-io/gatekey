"""Unit tests for `services/notifiers.py` (Phase 2, BD-18) - the pure,
no-DB pieces: threshold-crossing transition logic, Slack-vs-generic payload
shape, and per-channel dispatcher isolation."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from gatekey.services.notifiers import (
    NotifierDispatcher,
    NotifyRecipient,
    ThresholdAlertEvent,
    build_webhook_payload,
    crossed_thresholds,
    is_slack_webhook,
)


def _cross(old: str, new: str, ceiling: str | None, *, e80: bool = True, e100: bool = True):
    return crossed_thresholds(
        old_total=Decimal(old),
        new_total=Decimal(new),
        ceiling=Decimal(ceiling) if ceiling is not None else None,
        alert_80_enabled=e80,
        alert_100_enabled=e100,
    )


# --- crossed_thresholds: false -> true transition only -----------------------


def test_crossing_80_fires_once():
    assert _cross("79", "81", "100") == [80]


def test_already_over_80_does_not_refire():
    assert _cross("81", "85", "100") == []


def test_crossing_both_thresholds_in_one_charge_fires_both():
    assert _cross("50", "120", "100") == [80, 100]


def test_crossing_100_only():
    assert _cross("90", "100", "100") == [100]  # exact landing counts (<=)


def test_landing_exactly_on_80_counts_as_crossed():
    assert _cross("79.999", "80", "100") == [80]


def test_starting_exactly_on_80_does_not_refire():
    assert _cross("80", "99", "100") == []


def test_unmetered_ceiling_never_fires():
    assert _cross("0", "1000000", None) == []


def test_zero_ceiling_never_fires():
    assert _cross("0", "5", "0") == []


def test_disabled_thresholds_are_skipped():
    assert _cross("50", "120", "100", e80=False) == [100]
    assert _cross("50", "120", "100", e100=False) == [80]
    assert _cross("50", "120", "100", e80=False, e100=False) == []


# --- payload shape -----------------------------------------------------------


def _event(pct: int = 80) -> ThresholdAlertEvent:
    return ThresholdAlertEvent(
        team_id=uuid.UUID("00000000-0000-0000-0000-000000000042"),
        team_name="ml-research",
        threshold_pct=pct,  # type: ignore[arg-type]
        current_spend_usd=Decimal("81.50"),
        budget_ceiling_usd=Decimal("100"),
        currency="USD",
        recipients=[NotifyRecipient(name="Lead", email="lead@example.com")],
    )


def test_slack_url_detection():
    assert is_slack_webhook("https://hooks.slack.com/services/T00/B00/xyz")
    assert not is_slack_webhook("https://example.com/hooks.slack.com/nope")
    assert not is_slack_webhook("https://internal.example.com/webhook")


def test_slack_payload_is_text_only():
    payload = build_webhook_payload(_event(), "https://hooks.slack.com/services/T00/B00/x")
    assert set(payload) == {"text"}
    assert "ml-research" in payload["text"]
    assert "80%" in payload["text"]


def test_generic_payload_shape():
    payload = build_webhook_payload(_event(100), "https://example.com/webhook")
    assert payload["event"] == "budget_threshold_crossed"
    assert payload["team_id"] == "00000000-0000-0000-0000-000000000042"
    assert payload["team_name"] == "ml-research"
    assert payload["threshold_pct"] == 100
    # Decimals serialize as strings - no float precision loss.
    assert payload["current_spend_usd"] == "81.50"
    assert payload["budget_ceiling_usd"] == "100"
    assert payload["currency"] == "USD"
    # Recipients are deliberately NOT in the webhook body.
    assert "recipients" not in payload


# --- dispatcher isolation ----------------------------------------------------


@pytest.mark.asyncio
async def test_one_channel_failing_never_blocks_the_next():
    delivered = []

    class _Exploding:
        async def send(self, event):
            raise RuntimeError("boom")

    class _Recording:
        async def send(self, event):
            delivered.append(event.threshold_pct)

    dispatcher = NotifierDispatcher([_Exploding(), _Recording()])
    await dispatcher.dispatch(_event())  # must not raise
    assert delivered == [80]
