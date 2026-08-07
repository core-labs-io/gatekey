"""Minimal self-check for `gatekey_sync.cli`'s non-trivial branches: the
"--"-argv split (`main`) and the cache-hit-then-wrapped-command-fails retry
path (`_cmd_exec`) - no real subprocess or network call is ever made (both
are monkeypatched).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gatekey_sync import cache, cli


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")


# --- argv "--" splitting -------------------------------------------------


def test_main_splits_own_flags_from_wrapped_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _fake_get_key(args) -> None:
        captured["command"] = args.command
        captured["env_var"] = args.env_var

    monkeypatch.setattr(cli, "_cmd_exec", _fake_get_key)
    cli.main(["exec", "--env-var", "OPENAI_API_KEY", "--", "mycli", "chat", "hello world"])
    assert captured["command"] == ["mycli", "chat", "hello world"]
    assert captured["env_var"] == "OPENAI_API_KEY"


def test_main_with_no_separator_yields_empty_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(cli, "_cmd_get_key", lambda args: captured.setdefault("ran", True))
    cli.main(["get-key"])
    assert captured["ran"] is True


# --- exec: cache-hit-then-failure retry path (phase doc 3.7a) ------------


def _write_valid_cache(secret: str) -> None:
    cache.write_cache(secret=secret, valid_until=datetime.now(timezone.utc) + timedelta(hours=1))


def test_exec_uses_cache_without_a_network_call_on_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_valid_cache("gk_pk_cached")
    monkeypatch.setattr(
        cli,
        "_refresh_key",
        lambda base_url: (_ for _ in ()).throw(AssertionError("must not fetch on a cache hit")),
    )
    ran_with: dict = {}
    monkeypatch.setattr(cli, "_run_wrapped", lambda command, env: ran_with.update(env=env) or 0)

    args = cli._build_parser().parse_args(["exec", "--env-var", "X"])
    args.command = ["mycli"]
    with pytest.raises(SystemExit) as exc_info:
        cli._cmd_exec(args)
    assert exc_info.value.code == 0
    assert ran_with["env"]["X"] == "gk_pk_cached"


def test_exec_retries_once_after_cache_hit_then_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_valid_cache("gk_pk_stale")
    fetch_calls: list[str] = []

    class _Fresh:
        secret = "gk_pk_fresh"

    def _fake_refresh(base_url: str):
        fetch_calls.append(base_url)
        return _Fresh()

    monkeypatch.setattr(cli, "_refresh_key", _fake_refresh)

    run_calls: list[dict] = []

    def _fake_run(command, env):
        run_calls.append(env)
        # First run (stale cached key) fails; the retry (fresh key) succeeds.
        return 1 if len(run_calls) == 1 else 0

    monkeypatch.setattr(cli, "_run_wrapped", _fake_run)

    args = cli._build_parser().parse_args(["exec", "--env-var", "X"])
    args.command = ["mycli"]
    with pytest.raises(SystemExit) as exc_info:
        cli._cmd_exec(args)

    assert exc_info.value.code == 0  # the retry succeeded
    assert len(fetch_calls) == 1  # exactly one re-fetch, never a loop
    assert run_calls[0]["X"] == "gk_pk_stale"
    assert run_calls[1]["X"] == "gk_pk_fresh"


def test_exec_does_not_retry_a_freshly_fetched_key_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # No cache at all -> the first fetch is NOT a "cache hit", so a failure
    # must propagate directly, never trigger the one-retry path again.
    class _Fresh:
        secret = "gk_pk_fresh"

    fetch_calls: list[str] = []

    def _fake_refresh(base_url: str):
        fetch_calls.append(base_url)
        return _Fresh()

    monkeypatch.setattr(cli, "_refresh_key", _fake_refresh)
    monkeypatch.setattr(cli, "_run_wrapped", lambda command, env: 1)

    args = cli._build_parser().parse_args(["exec", "--env-var", "X"])
    args.command = ["mycli"]
    with pytest.raises(SystemExit) as exc_info:
        cli._cmd_exec(args)

    assert exc_info.value.code == 1
    assert len(fetch_calls) == 1  # never retried a freshly-fetched failure
