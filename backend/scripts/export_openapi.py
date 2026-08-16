"""Regenerate the committed OpenAPI document (Tier 4 ops/DX polish).

    python scripts/export_openapi.py

Writes `docs/api/openapi.json` (relative to backend/). CI regenerates it
and fails on drift, so the committed file is always current - API consumers
can read/codegen against it without running the server. Uses throwaway
config values: building the schema never touches the database or network.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from gatekey.config import Settings
from gatekey.main import create_app


def build_openapi() -> dict:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="schema-export-only",
        GATEKEY_MASTER_KEY=base64.b64encode(b"\x00" * 32).decode(),
    )
    app = create_app(settings)
    return app.openapi()


def main() -> None:
    out_path = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    schema = build_openapi()
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
