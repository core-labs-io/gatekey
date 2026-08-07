"""Unit tests for `services/audit_chain.py` (Phase 5 - Differentiators, 5.2
Hash-Chained Audit Ledger). Covers the pure hash-chain math
(`build_chain_payload`/`compute_chain_hash`) and `verify_chain`'s
recompute-and-compare walk against a fake session (no real DB) - per
AC5.2.3/AC5.2.4.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from gatekey.services.audit_chain import (
    build_chain_payload,
    compute_chain_hash,
    verify_chain,
)

_ORG_ID = uuid.uuid4()


def _payload_kwargs(**overrides: Any) -> dict[str, Any]:
    base = dict(
        entry_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        org_id=_ORG_ID,
        actor_label="Ada Lovelace <ada@example.com>",
        action="team.create",
        target_type="team",
        target_id="t1",
        old_value=None,
        new_value={"name": "Platform"},
        source_ip="203.0.113.5",
        created_at=datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return base


# --- build_chain_payload / compute_chain_hash - pure math -------------------


def test_build_chain_payload_is_deterministic() -> None:
    payload_1 = build_chain_payload(**_payload_kwargs())
    payload_2 = build_chain_payload(**_payload_kwargs())
    assert payload_1 == payload_2


def test_compute_chain_hash_is_deterministic_given_same_prev_hash() -> None:
    fields = _payload_kwargs()
    hash_1 = compute_chain_hash("", **fields)
    hash_2 = compute_chain_hash("", **fields)
    assert hash_1 == hash_2
    # SHA-256 hex digest shape.
    assert len(hash_1) == 64
    int(hash_1, 16)  # raises ValueError if not valid hex


def test_compute_chain_hash_changes_when_prev_hash_changes() -> None:
    fields = _payload_kwargs()
    genesis_hash = compute_chain_hash("", **fields)
    linked_hash = compute_chain_hash("some-prior-hash", **fields)
    assert genesis_hash != linked_hash


@pytest.mark.parametrize(
    "field,override",
    [
        ("actor_label", "Someone Else <mallory@example.com>"),
        ("action", "team.delete"),
        ("target_type", "user"),
        ("target_id", "t2"),
        ("old_value", {"tampered": True}),
        ("new_value", {"name": "Tampered"}),
        ("source_ip", "198.51.100.9"),
        ("created_at", datetime(2099, 1, 1, tzinfo=timezone.utc)),
    ],
)
def test_compute_chain_hash_changes_when_any_hashed_field_changes(
    field: str, override: Any
) -> None:
    """AC5.2.3: every field in the canonical payload participates in the
    hash - tampering ANY one of them (the exact NFR scenario: mutate
    `old_value` via raw SQL) must change the resulting hash."""
    baseline = compute_chain_hash("", **_payload_kwargs())
    tampered = compute_chain_hash("", **_payload_kwargs(**{field: override}))
    assert baseline != tampered


def test_compute_chain_hash_source_ip_normalizes_ipaddress_object_same_as_str() -> None:
    """`source_ip` is a plain `str` at write-time but an `ipaddress.IPv4Address`
    once read back from the DB's `INET` column - both must hash identically,
    or a freshly-written row would never verify against itself."""
    import ipaddress

    as_str = compute_chain_hash("", **_payload_kwargs(source_ip="203.0.113.5"))
    as_ip_object = compute_chain_hash(
        "", **_payload_kwargs(source_ip=ipaddress.IPv4Address("203.0.113.5"))
    )
    assert as_str == as_ip_object


# --- verify_chain - recompute-and-compare walk, fake session ---------------


@dataclass
class _FakeAuditRow:
    id: uuid.UUID
    org_id: uuid.UUID
    actor_label: str
    action: str
    target_type: str
    target_id: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    source_ip: str | None
    created_at: datetime
    chain_hash: str | None = None
    prev_hash: str | None = None
    chain_seq: int | None = None


class _FakeScalars:
    def __init__(self, rows: list[_FakeAuditRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeAuditRow]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[_FakeAuditRow]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeVerifySession:
    """Returns a fixed, pre-ordered list of `_FakeAuditRow`s regardless of
    the query - `verify_chain` only ever issues one SELECT."""

    def __init__(self, rows: list[_FakeAuditRow]) -> None:
        self._rows = rows

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        return _FakeResult(self._rows)


def _build_valid_chain(n: int) -> list[_FakeAuditRow]:
    """A genuinely valid, self-consistent chain of `n` rows - genesis
    `prev_hash=None`, each subsequent row's `prev_hash` = the prior row's
    `chain_hash`, every `chain_hash` a real recomputation."""
    rows: list[_FakeAuditRow] = []
    tail_hash: str | None = None
    for i in range(1, n + 1):
        fields = _payload_kwargs(
            entry_id=uuid.uuid4(),
            target_id=f"t{i}",
            created_at=datetime(2026, 8, i, tzinfo=timezone.utc),
        )
        prev_hash_for_hash = tail_hash if tail_hash is not None else ""
        chain_hash = compute_chain_hash(prev_hash_for_hash, **fields)
        rows.append(
            _FakeAuditRow(
                id=fields["entry_id"],
                org_id=fields["org_id"],
                actor_label=fields["actor_label"],
                action=fields["action"],
                target_type=fields["target_type"],
                target_id=fields["target_id"],
                old_value=fields["old_value"],
                new_value=fields["new_value"],
                source_ip=fields["source_ip"],
                created_at=fields["created_at"],
                chain_hash=chain_hash,
                prev_hash=tail_hash,
                chain_seq=i,
            )
        )
        tail_hash = chain_hash
    return rows


@pytest.mark.asyncio
async def test_verify_chain_intact_for_a_genuinely_valid_chain() -> None:
    rows = _build_valid_chain(5)
    session = _FakeVerifySession(rows)
    result = await verify_chain(session, _ORG_ID)  # type: ignore[arg-type]
    assert result.status == "intact"
    assert result.entries_verified == 5


@pytest.mark.asyncio
async def test_verify_chain_empty_chain_is_vacuously_intact() -> None:
    session = _FakeVerifySession([])
    result = await verify_chain(session, _ORG_ID)  # type: ignore[arg-type]
    assert result.status == "intact"
    assert result.entries_verified == 0


@pytest.mark.asyncio
async def test_verify_chain_detects_tampered_old_value_and_names_the_entry() -> None:
    """The exact NFR scenario (design doc section 9.1): a historical row's
    `old_value` is mutated directly (simulating a raw SQL UPDATE) without
    recomputing `chain_hash` - `verify_chain` must report `broken` and name
    that exact entry's id + chain_seq."""
    rows = _build_valid_chain(5)
    tampered_row = rows[2]  # chain_seq == 3
    tampered_row.old_value = {"tampered": "yes"}

    session = _FakeVerifySession(rows)
    result = await verify_chain(session, _ORG_ID)  # type: ignore[arg-type]

    assert result.status == "broken"
    assert result.broken_at_entry_id == str(tampered_row.id)
    assert result.broken_at_chain_seq == 3


@pytest.mark.asyncio
async def test_verify_chain_detects_broken_link_even_when_row_content_is_self_consistent() -> None:
    """A row that's been re-forged to be internally self-consistent (its
    `chain_hash` DOES correctly recompute from its own stored fields AND its
    own now-bogus `prev_hash`) but whose `prev_hash` no longer points at the
    actual prior row's real `chain_hash` - only the second (linkage) check
    catches this; the first (recomputation) check alone would pass."""
    rows = _build_valid_chain(4)
    forged_prev_hash = "0" * 64
    forged_fields = _payload_kwargs(
        entry_id=rows[2].id,
        target_id=rows[2].target_id,
        created_at=rows[2].created_at,
    )
    forged_chain_hash = compute_chain_hash(forged_prev_hash, **forged_fields)
    rows[2].prev_hash = forged_prev_hash
    rows[2].chain_hash = forged_chain_hash

    session = _FakeVerifySession(rows)
    result = await verify_chain(session, _ORG_ID)  # type: ignore[arg-type]

    assert result.status == "broken"
    assert result.broken_at_chain_seq == 3
