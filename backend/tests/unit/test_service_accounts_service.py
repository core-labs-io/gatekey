"""Unit tests for services/service_accounts.py - the parts that don't need a real DB.

DB-backed CRUD (list/get/revoke-idempotency/hash-based-lookup against a real
unique index) is covered by `tests/integration/test_service_accounts_api.py`
against a real Postgres, same split as `test_provider_keys_service.py` /
`test_provider_keys_api.py`. This file covers:
  - the plaintext secret format and entropy sanity (`create_service_account`
    against a minimal fake session that doesn't need a real DB round trip),
  - `key_prefix` derivation matching the DB column's `String(12)` limit, and
  - `hash_secret`'s determinism/correctness.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.services.service_accounts import (
    KEY_PREFIX_LENGTH,
    SECRET_PREFIX,
    create_service_account,
    hash_secret,
)

_TEST_USER_ID = uuid.uuid4()


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal stand-in for `AsyncSession` sufficient for `create_service_account`.

    Doesn't touch a real database - `add` just stashes the row, `commit`/
    `refresh` are no-ops, `execute` returns a canned "user exists" result
    (Phase 1.4: `create_service_account` now pre-checks `user_id` via
    `services.users.get_user`). Good enough to exercise the secret-generation
    and hashing logic in this module without needing Postgres.
    """

    def __init__(self, *, user_exists: bool = True) -> None:
        self.added: list[ServiceAccountKey] = []
        self._user_exists = user_exists

    def add(self, row: ServiceAccountKey) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        return None

    async def refresh(self, row: ServiceAccountKey) -> None:
        return None

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        return _FakeResult(_TEST_USER_ID if self._user_exists else None)


@pytest.mark.asyncio
async def test_create_service_account_secret_has_expected_prefix():
    row, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    assert secret.startswith(SECRET_PREFIX)


@pytest.mark.asyncio
async def test_create_service_account_secret_has_sufficient_entropy_length():
    # secrets.token_urlsafe(32) encodes 32 bytes (256 bits) as base64url;
    # the encoded string is at least 32 chars (it's actually ~43), so a
    # sanity floor well below that catches any accidental truncation while
    # not being brittle to the exact encoding length.
    _, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    token_part = secret[len(SECRET_PREFIX) :]
    assert len(token_part) >= 32


@pytest.mark.asyncio
async def test_create_service_account_secrets_are_unique_across_calls():
    secrets_seen = set()
    for _ in range(20):
        _, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
        secrets_seen.add(secret)
    assert len(secrets_seen) == 20


@pytest.mark.asyncio
async def test_create_service_account_key_prefix_matches_column_limit():
    row, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    assert len(row.key_prefix) == KEY_PREFIX_LENGTH
    # Must fit the DB column's String(12) limit exactly.
    assert KEY_PREFIX_LENGTH == 12


@pytest.mark.asyncio
async def test_create_service_account_key_prefix_is_first_chars_after_secret_prefix():
    row, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    token_part = secret[len(SECRET_PREFIX) :]
    assert row.key_prefix == token_part[:KEY_PREFIX_LENGTH]


@pytest.mark.asyncio
async def test_create_service_account_never_stores_plaintext_secret():
    row, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    # No attribute on the row holds the plaintext secret anywhere.
    assert secret.encode("utf-8") != row.secret_hash
    assert not hasattr(row, "secret")


@pytest.mark.asyncio
async def test_create_service_account_secret_hash_matches_sha256_of_secret():
    row, secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    assert row.secret_hash == hashlib.sha256(secret.encode("utf-8")).digest()


@pytest.mark.asyncio
async def test_create_service_account_scopes_row_to_default_org():
    row, _secret = await create_service_account(_FakeSession(), "my-app", _TEST_USER_ID)
    assert row.org_id == DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_create_service_account_raises_user_not_found_for_unknown_user():
    from gatekey.services.service_accounts import UserNotFoundError

    session = _FakeSession(user_exists=False)
    with pytest.raises(UserNotFoundError):
        await create_service_account(session, "my-app", uuid.uuid4())
    assert session.added == []


def test_hash_secret_is_deterministic():
    assert hash_secret("gk_sk_abc123") == hash_secret("gk_sk_abc123")


def test_hash_secret_differs_for_different_input():
    assert hash_secret("gk_sk_abc123") != hash_secret("gk_sk_abc124")


def test_hash_secret_matches_raw_sha256():
    assert hash_secret("gk_sk_hello") == hashlib.sha256(b"gk_sk_hello").digest()


def test_hash_secret_returns_32_bytes():
    assert len(hash_secret("gk_sk_anything")) == 32
