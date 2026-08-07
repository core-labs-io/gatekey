"""Shared SQLAlchemy declarative base for all ORM models.

Every model in `gatekey.db.models` must inherit from `Base` so that:

- Alembic's `target_metadata` (see `alembic/env.py`) can discover every
  table via `Base.metadata` for autogenerate diffs.
- A single `MetaData` instance is shared across the whole app - required
  for cross-table foreign keys (e.g. `ProviderKey.org_id` -> `orgs.id`) to
  resolve correctly.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
