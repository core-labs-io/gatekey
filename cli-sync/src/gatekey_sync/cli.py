"""`gatekey-sync` CLI entrypoint (design doc section 8.1, phase doc 3.7a).

Subcommands:
  - `login [--base-url URL]` - one-time device-code login (`auth.login`).
  - `configure (--env-var NAME | --write-file PATH [--template TPL])
    [--base-url URL]` - persists where a fetched key gets written, so
    `exec`/`get-key` don't need it repeated on every call. "This helper
    writes to whatever file/location the user's CLI is configured to read
    its key from" (phase doc 3.7a) - `configure` is that one-time mapping
    step, deliberately tool-agnostic (an env var name, or an arbitrary file
    path + a `{secret}` template), never a hardcoded target.
  - `get-key [--base-url URL]` - prints the current (cache-hit or freshly
    fetched) plaintext key to stdout, for scripting
    (`export X=$(gatekey-sync get-key)`).
  - `exec [--env-var NAME | --write-file PATH [--template TPL]]
    [--base-url URL] -- <command...>` - the thin wrapper phase doc 3.7a
    describes: "a thin wrapper the user runs instead of invoking their AI
    CLI directly". Checks the local cache first (no network call on a
    cache hit - the NFR this whole design exists to satisfy), injects the
    key, runs the wrapped command, and forwards its exit code.

Uses stdlib `argparse` (ladder rung 3 - a handful of subcommands doesn't
need a third-party CLI framework). The `--` separator between this tool's
own flags and the wrapped command is handled by hand (splitting `sys.argv`
before argparse ever sees it) since `argparse.REMAINDER` interacts poorly
with flags that come before it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from gatekey_sync import auth, cache
from gatekey_sync.client import AuthRejectedError, CurrentKey, GatekeySyncClient, GatekeySyncError

CONFIG_FILE = cache.CACHE_DIR / "config.json"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_FILE_TEMPLATE = "{secret}"


@dataclass(frozen=True)
class InjectTarget:
    mode: str  # "env" | "file"
    var: str | None = None
    path: str | None = None
    template: str = DEFAULT_FILE_TEMPLATE


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(config: dict) -> None:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url
    return _load_config().get("base_url", DEFAULT_BASE_URL)


def _resolve_inject(args: argparse.Namespace) -> InjectTarget:
    if getattr(args, "env_var", None):
        return InjectTarget(mode="env", var=args.env_var)
    if getattr(args, "write_file", None):
        return InjectTarget(mode="file", path=args.write_file, template=args.template)
    configured = _load_config().get("inject")
    if configured:
        return InjectTarget(**configured)
    print(
        "No injection target configured. Pass --env-var NAME or --write-file PATH, "
        "or run `gatekey-sync configure` once to set a default.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _refresh_key(base_url: str) -> CurrentKey:
    """Cache miss/stale path: fetch a fresh key via the stored refresh
    credential, write the result to the cache. Also the recovery path when
    the CACHED key is later found to be invalid (see `_run_exec` below)."""
    refresh_credential = auth.get_refresh_credential()
    if refresh_credential is None:
        print("Not logged in. Run `gatekey-sync login` first.", file=sys.stderr)
        raise SystemExit(1)

    client = GatekeySyncClient(base_url)
    try:
        current = client.fetch_current_key(refresh_credential)
    except AuthRejectedError:
        # The refresh credential itself was rejected (revoked out of band -
        # e.g. SCIM deactivation, or an admin revoking CLI access). Nothing
        # left to retry with - clear the dead credential/cache and tell the
        # user to re-authorize (design doc section 8.2, phase doc 3.7a's
        # revocation-recovery framing, applied to the refresh credential
        # itself rather than the derived personal key).
        auth.clear_refresh_credential()
        cache.invalidate_cache()
        print(
            "Gatekey rejected the stored login. Run `gatekey-sync login` again.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except GatekeySyncError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    cache.write_cache(secret=current.secret, valid_until=current.valid_until)
    return current


def _resolve_key(base_url: str) -> str:
    """Cache-first resolution (phase doc 3.7a's core mechanic): a valid
    cache entry is used immediately with NO network call; only a
    missing/expired cache triggers a fetch."""
    cached = cache.read_cache()
    if cache.is_valid(cached):
        assert cached is not None  # narrows for type checkers
        return cached.secret
    return _refresh_key(base_url).secret


def _write_injection(inject: InjectTarget, secret: str) -> dict | None:
    """Returns an `env` override dict for `subprocess.run` (env mode), or
    `None` (file mode - the target file itself was written; the wrapped
    process inherits the parent's environment unchanged)."""
    if inject.mode == "env":
        env = os.environ.copy()
        env[inject.var] = secret
        return env
    path = Path(inject.path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inject.template.format(secret=secret), encoding="utf-8")
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return None


def _run_wrapped(command: list[str], env: dict | None) -> int:
    if not command:
        print("No command given after `--`. Usage: gatekey-sync exec ... -- <command...>", file=sys.stderr)
        raise SystemExit(2)
    result = subprocess.run(command, env=env)
    return result.returncode


def _cmd_login(args: argparse.Namespace) -> None:
    base_url = args.base_url or _load_config().get("base_url", DEFAULT_BASE_URL)
    auth.login(base_url)
    config = _load_config()
    config["base_url"] = base_url
    _save_config(config)


def _cmd_configure(args: argparse.Namespace) -> None:
    config = _load_config()
    if args.base_url:
        config["base_url"] = args.base_url
    if args.env_var:
        config["inject"] = {"mode": "env", "var": args.env_var}
    elif args.write_file:
        config["inject"] = {"mode": "file", "path": args.write_file, "template": args.template}
    _save_config(config)
    print(f"Saved configuration to {CONFIG_FILE}")


def _cmd_get_key(args: argparse.Namespace) -> None:
    base_url = _resolve_base_url(args)
    print(_resolve_key(base_url))


def _cmd_exec(args: argparse.Namespace) -> None:
    base_url = _resolve_base_url(args)
    inject = _resolve_inject(args)

    cached = cache.read_cache()
    used_cache = cache.is_valid(cached)
    secret = cached.secret if used_cache else _refresh_key(base_url).secret

    env = _write_injection(inject, secret)
    exit_code = _run_wrapped(args.command, env)

    if exit_code != 0 and used_cache:
        # ponytail: a nonzero exit is an imprecise, generic-enough proxy
        # for "the wrapped CLI's own auth call against Gatekey rejected the
        # cached key" (phase doc 3.7a's cached-but-now-invalid case) - the
        # helper is tool-agnostic and cannot generically parse the wrapped
        # command's own error output, so exit-code is the best available
        # generic signal. Bounded to exactly one retry: a false positive
        # (the wrapped command failed for an unrelated reason) costs one
        # extra fetch + one extra re-run, never a loop. A freshly-fetched
        # key that still fails is NOT retried again - that's a real error,
        # surfaced via the wrapped command's own (second) exit code.
        # Upgrade path: an opt-out flag if some wrapped tool's routine
        # nonzero exits make this too eager in practice.
        cache.invalidate_cache()
        secret = _refresh_key(base_url).secret
        env = _write_injection(inject, secret)
        exit_code = _run_wrapped(args.command, env)

    raise SystemExit(exit_code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gatekey-sync")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    login_parser = subparsers.add_parser("login", help="One-time device-code login.")
    login_parser.add_argument("--base-url", default=None)
    login_parser.set_defaults(func=_cmd_login)

    configure_parser = subparsers.add_parser(
        "configure", help="Set the default injection target and/or base URL."
    )
    configure_parser.add_argument("--base-url", default=None)
    configure_parser.add_argument("--env-var", default=None, help="Env var name to inject the key as.")
    configure_parser.add_argument("--write-file", default=None, help="File path to write the key to.")
    configure_parser.add_argument("--template", default=DEFAULT_FILE_TEMPLATE)
    configure_parser.set_defaults(func=_cmd_configure)

    get_key_parser = subparsers.add_parser("get-key", help="Print the current key to stdout.")
    get_key_parser.add_argument("--base-url", default=None)
    get_key_parser.set_defaults(func=_cmd_get_key)

    exec_parser = subparsers.add_parser(
        "exec", help="Run a command with the current key injected (env var or file)."
    )
    exec_parser.add_argument("--base-url", default=None)
    exec_parser.add_argument("--env-var", default=None)
    exec_parser.add_argument("--write-file", default=None)
    exec_parser.add_argument("--template", default=DEFAULT_FILE_TEMPLATE)
    exec_parser.set_defaults(func=_cmd_exec)

    return parser


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    # Split this tool's own flags from the wrapped command at a literal
    # "--" (module docstring: `argparse.REMAINDER` alone mishandles flags
    # that precede it).
    if "--" in raw_argv:
        sep = raw_argv.index("--")
        own_argv, command = raw_argv[:sep], raw_argv[sep + 1 :]
    else:
        own_argv, command = raw_argv, []

    parser = _build_parser()
    args = parser.parse_args(own_argv)
    args.command = command
    args.func(args)


if __name__ == "__main__":
    main()
