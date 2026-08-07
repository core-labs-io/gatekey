"""`KnownAiToolHostname` - a curated allowlist of hostnames the shadow-AI
detector matches ingested connection events against (Phase 5 -
Differentiators, 5.1 Shadow AI Discovery).

See `gatekey/phase-5-technical-design.md` sections 2.5/4.2 for the full
design rationale. `services.shadow_ai.ingest_events` (backend-developer
task) persists a `ShadowAiIngestEvent` row only when the submitted event's
`destination_host` matches an `enabled = true` row here - every other event
is silently dropped (AC5.1.1's privacy-by-minimization gate).

Not org-scoped - a single, platform-wide curated list (matches this
codebase's existing single-default-org posture for features that haven't
needed multi-org scoping yet). Seeded with a starter allowlist by
`alembic/versions/0042_create_shadow_ai_tables.py`; an Org Admin can
add/remove entries afterward via `PUT /v1/admin/shadow-ai/known-hostnames`
(backend-developer task) - the migration seed is a sane v1 starting point,
not an admin-immutable list.

`hostname` is the primary key (not a surrogate `id`) - at most one row per
hostname string.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0042_create_shadow_ai_tables.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class KnownAiToolHostname(Base):
    __tablename__ = "known_ai_tool_hostnames"

    hostname: Mapped[str] = mapped_column(Text, primary_key=True)
    tool_label: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
