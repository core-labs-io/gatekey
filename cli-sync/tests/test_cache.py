"""Minimal self-check for `gatekey_sync.cache` (design doc section 8.1's own
`tests/test_cache.py`) - cache-hit/miss/expiry logic, no fixtures/mocking
framework. Redirects `CACHE_DIR`/`CACHE_FILE` to a temp directory per test
(monkeypatch) rather than touching the real `~/.gatekey-sync/`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gatekey_sync import cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "CACHE_FILE", tmp_path / "cache.json")


def test_read_cache_missing_file_returns_none() -> None:
    assert cache.read_cache() is None


def test_write_then_read_round_trips() -> None:
    valid_until = datetime.now(timezone.utc) + timedelta(hours=1)
    cache.write_cache(secret="gk_pk_abc", valid_until=valid_until)
    result = cache.read_cache()
    assert result is not None
    assert result.secret == "gk_pk_abc"
    assert result.valid_until == valid_until


def test_is_valid_true_for_future_valid_until() -> None:
    now = datetime.now(timezone.utc)
    cache.write_cache(secret="s", valid_until=now + timedelta(hours=1))
    assert cache.is_valid(cache.read_cache(), now=now) is True


def test_is_valid_false_for_past_valid_until() -> None:
    now = datetime.now(timezone.utc)
    cache.write_cache(secret="s", valid_until=now - timedelta(seconds=1))
    assert cache.is_valid(cache.read_cache(), now=now) is False


def test_is_valid_false_for_none() -> None:
    assert cache.is_valid(None) is False


def test_corrupt_cache_file_is_treated_as_a_miss() -> None:
    cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache.CACHE_FILE.write_text("not json", encoding="utf-8")
    assert cache.read_cache() is None


def test_missing_field_is_treated_as_a_miss() -> None:
    cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache.CACHE_FILE.write_text('{"secret": "s"}', encoding="utf-8")
    assert cache.read_cache() is None


def test_invalidate_cache_removes_file() -> None:
    cache.write_cache(secret="s", valid_until=datetime.now(timezone.utc) + timedelta(hours=1))
    assert cache.CACHE_FILE.exists()
    cache.invalidate_cache()
    assert not cache.CACHE_FILE.exists()


def test_invalidate_cache_is_idempotent_when_already_absent() -> None:
    cache.invalidate_cache()  # no file exists yet - must not raise
    cache.invalidate_cache()
