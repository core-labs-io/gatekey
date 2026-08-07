"""gatekey-sync - local credential-sync helper for Gatekey personal keys.

See `docs/design/phase-3-security-compliance-design.md` section 8 (backend)
and `gatekey/phase-3-security-compliance.md` section 3.7a (product
narrative) for the full design. This package is intentionally small: a
device-code login (`auth.py`), a `{secret, valid_until}` JSON cache file
(`cache.py`), a thin HTTP client against the backend's device-auth +
current-key endpoints (`client.py`), and an `argparse` CLI surface
(`cli.py`).
"""

from __future__ import annotations

__version__ = "0.1.0"
