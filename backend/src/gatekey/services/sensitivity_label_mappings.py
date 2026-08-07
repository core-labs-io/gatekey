"""CRUD + request-time lookup for `sensitivity_label_mappings` (Phase 5 -
Differentiators, 5.3 Content-Classification-Aware Routing, AC5.3.5/AC5.3.8).

See `gatekey.db.models.sensitivity_label_mapping.SensitivityLabelMapping`
and `gatekey/phase-5-technical-design.md` section 2.4 for the full design
rationale, in particular "Why `sensitivity_label_mappings` gets no dedicated
`*Cache` class" - `list_sensitivity_label_mappings` is a fresh, cheap
per-request read used directly by `api.v1.gateway.common.run_dlp_scan`, not
a warmed process-local cache.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.sensitivity_label_mapping import SensitivityLabelMapping
from gatekey.errors import GatekeyError


class DuplicateSensitivityLabelMappingError(GatekeyError):
    """`(org_id, external_label)` must be unique - mirrors `services.dlp.
    DuplicateCustomPatternNameError`'s shape. `external_label` is caller
    input, not secret material - safe in `message`."""

    status_code = 409
    code = "sensitivity_label_mapping_conflict"

    def __init__(self, external_label: str) -> None:
        super().__init__(
            f"A sensitivity-label mapping for external label '{external_label}' already exists."
        )
        self.external_label = external_label


async def list_sensitivity_label_mappings(session: AsyncSession) -> list[SensitivityLabelMapping]:
    """Fresh per-request read (AC5.3.5) - used both by the admin `GET` and
    by `api.v1.gateway.common.run_dlp_scan`'s sensitivity-label
    short-circuit lookup. Deliberately not cached - see module docstring."""
    stmt = (
        select(SensitivityLabelMapping)
        .where(SensitivityLabelMapping.org_id == DEFAULT_ORG_ID)
        .order_by(SensitivityLabelMapping.external_label)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_sensitivity_label_mapping(
    session: AsyncSession, mapping_id: uuid.UUID
) -> SensitivityLabelMapping | None:
    row = await session.get(SensitivityLabelMapping, mapping_id)
    if row is None or row.org_id != DEFAULT_ORG_ID:
        return None
    return row


async def resolve_pretrusted_categories(session: AsyncSession, label: str | None) -> frozenset[str]:
    """AC5.3.5: 0 or 1 `gatekey_category` for the given `X-Gatekey-
    Sensitivity-Label` header value. An unrecognized/absent label resolves
    to an empty set - never a hard error, always falls through to Gatekey's
    own classifiers for every category (see `api.v1.gateway.common.
    run_dlp_scan`'s docstring)."""
    if not label:
        return frozenset()
    mappings = await list_sensitivity_label_mappings(session)
    return frozenset(m.gatekey_category for m in mappings if m.external_label == label)


async def create_sensitivity_label_mapping(
    session: AsyncSession, *, external_label: str, gatekey_category: str
) -> SensitivityLabelMapping:
    """Flushes (populating `row.id`), does NOT commit - mirrors `services.
    dlp.create_custom_pattern`'s shape so the route layer can write the
    `sensitivity_label_mapping.create` audit entry (which needs the
    now-flushed `row.id` as its `target_id`) in the SAME transaction before
    committing once."""
    row = SensitivityLabelMapping(
        org_id=DEFAULT_ORG_ID, external_label=external_label, gatekey_category=gatekey_category
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise DuplicateSensitivityLabelMappingError(external_label) from None
    return row


async def update_sensitivity_label_mapping(
    session: AsyncSession, mapping_id: uuid.UUID, *, external_label: str, gatekey_category: str
) -> SensitivityLabelMapping | None:
    row = await get_sensitivity_label_mapping(session, mapping_id)
    if row is None:
        return None
    row.external_label = external_label
    row.gatekey_category = gatekey_category
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise DuplicateSensitivityLabelMappingError(external_label) from None
    return row


async def delete_sensitivity_label_mapping(session: AsyncSession, mapping_id: uuid.UUID) -> bool:
    row = await get_sensitivity_label_mapping(session, mapping_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True
