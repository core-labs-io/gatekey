"""Local `{secret, valid_until}` cache file (design doc section 8.1).

`~/.gatekey-sync/cache.json` - `key value + a valid_until timestamp` (phase
doc 3.7a). Deliberately a plain JSON file via stdlib `pathlib`/`json`, not a
DB or a third-party cache library - this is a single small file read/written
on every CLI invocation, checked before anything else (the NFR is
"negligible compared to the CLI's own startup time" - a `pathlib.Path.
read_text()` + `json.loads()` comfortably clears that bar).

Deviation note: the design doc's own file-layout sketch (section 8.1)
mentions `platformdirs`-style OS-idiomatic config directories as a
deferred nice-to-have and explicitly says to use `Path.home()` for now -
followed here exactly, one directory (`~/.gatekey-sync/`) for both the
cache and the CLI's own config (`config.py` doesn't exist as a separate
module - see `cli.py`'s `_load_config`/`_save_config` for the sibling
`config.json`).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / ".gatekey-sync"
CACHE_FILE = CACHE_DIR / "cache.json"


@dataclass(frozen=True)
class CachedKey:
    secret: str
    valid_until: datetime


def _parse_valid_until(raw: str) -> datetime:
    # `datetime.fromisoformat` handles a trailing "Z" only from Python
    # 3.11+ (this package's `requires-python` floor) - no manual
    # normalization needed.
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def read_cache() -> CachedKey | None:
    """Returns `None` on ANY problem (missing file, corrupt JSON, missing
    fields) - a bad cache is always treated as a cache miss, never a crash.
    The caller (`cli.py`) reacts to `None` by re-fetching, same as a
    freshly-installed helper with no cache yet."""
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return CachedKey(secret=raw["secret"], valid_until=_parse_valid_until(raw["valid_until"]))
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError, OSError):
        return None


def is_valid(cached: CachedKey | None, *, now: datetime | None = None) -> bool:
    if cached is None:
        return False
    now = now or datetime.now(timezone.utc)
    return cached.valid_until > now


def write_cache(*, secret: str, valid_until: datetime) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"secret": secret, "valid_until": valid_until.isoformat()}), encoding="utf-8"
    )
    # Best-effort POSIX permission hardening (design doc section 8.1: "chmod
    # 0600 best-effort on POSIX - Windows ACLs are not separately hardened,
    # a known, accepted limitation"). `os.chmod` on Windows only toggles the
    # read-only bit, not real ACLs - deliberately not attempted here (would
    # be a false sense of security, not real hardening).
    if os.name == "posix":
        try:
            CACHE_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def invalidate_cache() -> None:
    """Delete the cache file, if present. Used both for a normal stale-TTL
    refresh and for the "cached-but-now-invalid" force-revoke recovery path
    (phase doc 3.7a) - see `cli.py`'s `exec` command."""
    CACHE_FILE.unlink(missing_ok=True)
