"""Unit tests for `services.users.resolve_or_create_sso_user` (Phase 3,
BD-21, design doc section 6.3) - the SSO-callback identity-reconciliation
fix: a SCIM-provisioned `User` row (or pre-Phase-2 legacy row) with a
matching email and no `sso_subject` yet is claimed on first SSO login,
rather than a duplicate row being created.
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.user import User
from gatekey.services.users import resolve_or_create_sso_user


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Returns the next canned `scalar_one_or_none()` result per `execute()`
    call, in order - one call per `select(User).where(...)` the function
    under test issues. `commit`/`refresh`/`add` are no-ops beyond recording."""

    def __init__(self, canned_returns: list):
        self._canned = list(canned_returns)
        self.added: list[User] = []
        self.commit_count = 0

    def add(self, row: User) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commit_count += 1

    async def refresh(self, row: User) -> None:
        return None

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        return _FakeResult(self._canned.pop(0))


def _existing_user(**overrides) -> User:
    defaults = dict(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        name="Alice",
        sso_subject="sub-123",
        sso_email="alice@example.com",
        org_role=None,
        budget_usd=None,
    )
    defaults.update(overrides)
    return User(**defaults)


# --- case 1: matched by sso_subject ------------------------------------------


@pytest.mark.asyncio
async def test_matched_by_sso_subject_returns_existing_row_unchanged():
    existing = _existing_user()
    session = _FakeSession([existing])

    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub="sub-123", email="alice@example.com", name="Alice"
    )

    assert user is existing
    assert session.commit_count == 0  # no email drift, no write needed
    assert session.added == []


@pytest.mark.asyncio
async def test_matched_by_sso_subject_refreshes_display_only_email():
    existing = _existing_user(sso_email="old@example.com")
    session = _FakeSession([existing])

    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub="sub-123", email="new@example.com", name="Alice"
    )

    assert user.sso_email == "new@example.com"
    assert session.commit_count == 1


# --- case 2: SCIM-identity reconciliation (the design doc 6.3 fix) ----------


@pytest.mark.asyncio
async def test_no_sso_subject_match_claims_scim_provisioned_row_by_email():
    scim_row = _existing_user(sso_subject=None, sso_email="bob@example.com")
    # First execute(): sso_subject lookup -> no match. Second execute():
    # email + sso_subject IS NULL lookup -> the SCIM-provisioned row.
    session = _FakeSession([None, scim_row])

    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub="new-sub-456", email="bob@example.com", name="Bob"
    )

    assert user is scim_row
    assert user.sso_subject == "new-sub-456"  # backfilled, not a new row
    assert session.commit_count == 1
    assert session.added == []  # no duplicate User row created


@pytest.mark.asyncio
async def test_no_email_asserted_skips_reconciliation_lookup_entirely():
    """No IdP-asserted email at all -> only one lookup attempt (by
    sso_subject); falls straight through to row creation without a second,
    meaningless email-based query."""
    session = _FakeSession([None])

    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub="sub-789", email=None, name="Carol"
    )

    assert user.sso_subject == "sub-789"
    assert len(session.added) == 1


# --- case 3: neither match -> create a new row (Phase 2's existing path) ----


@pytest.mark.asyncio
async def test_no_match_at_all_creates_new_user():
    session = _FakeSession([None, None])

    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub="brand-new-sub", email="dave@example.com", name="Dave"
    )

    assert len(session.added) == 1
    assert session.added[0] is user
    assert user.sso_subject == "brand-new-sub"
    assert user.sso_email == "dave@example.com"
    assert user.org_role is None
    assert user.budget_usd is None  # A6: flat budget stays unused for SSO users
