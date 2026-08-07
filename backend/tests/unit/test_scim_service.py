"""Unit tests for services/scim.py - the parts that don't need a real DB.

DB-backed CRUD (create/list/get against a real unique index, full router
round-trips) is left to `tests/integration` (out of scope here - see module
docstring precedent in `test_service_accounts_service.py`). This file
covers:
  - `create_scim_user` never sets `org_role` (AC5.3/AC5.8, structural).
  - `parse_simple_eq_filter` / `parse_user_patch_active` /
    `parse_group_patch_member_ops` - the scoped SCIM-payload parsing (AC5.1).
  - `scim_token_matches` - the constant-time bearer-token check (AC5.7).
  - `revoke_scim_deactivated_user_credentials` - the deactivation cascade
    revokes exactly the right credential set, one audit entry per revoked
    credential, and is idempotent on a second call.
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.api.deps import AdminContext
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.scim_config import ScimConfig
from gatekey.services.scim import (
    ScimError,
    create_scim_user,
    parse_group_patch_member_ops,
    parse_simple_eq_filter,
    parse_user_patch_active,
    revoke_scim_deactivated_user_credentials,
    scim_token_matches,
)
from gatekey.services.service_accounts import hash_secret

_TEST_USER_ID = uuid.uuid4()
_ACTOR = AdminContext(actor_user_id=None, actor_label="system:scim", org_id=DEFAULT_ORG_ID)


class _FakeResult:
    def __init__(self, items: list):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _FakeSession:
    """Minimal stand-in for `AsyncSession`. `execute()` returns the next
    canned result in sequence (one per `UPDATE ... RETURNING` statement),
    `add()`/`flush()` just record calls - good enough to exercise
    `revoke_scim_deactivated_user_credentials`'s cascade logic and
    `create_scim_user`'s row construction without touching Postgres.

    Phase 5 (5.2 Hash-Chained Audit Ledger): `write_audit_entry` now reads
    `compliance_settings.chain_enabled` (`services.compliance_settings.
    get_effective_compliance_settings`) via its own `session.execute()` call
    before every INSERT - special-cased here to always answer "no row"
    (ADR-2 default, chain disabled) WITHOUT consuming from `_canned`, so
    every existing test's canned-results queue (sized for exactly the
    `UPDATE ... RETURNING` statements it expects) stays unchanged."""

    def __init__(self, canned_returns: list[list] | None = None) -> None:
        self.added: list = []
        self._canned = list(canned_returns or [])

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        if "COMPLIANCE_SETTINGS" in str(stmt).upper():
            return _FakeResult([])
        return _FakeResult(self._canned.pop(0))


# --- create_scim_user: AC5.3/AC5.8 structural org_role guarantee ------------


@pytest.mark.asyncio
async def test_create_scim_user_never_sets_org_role():
    session = _FakeSession()
    user = await create_scim_user(
        session, user_name="alice@example.com", display_name="Alice", external_id="ext-1"
    )
    assert user.org_role is None


@pytest.mark.asyncio
async def test_create_scim_user_maps_fields():
    session = _FakeSession()
    user = await create_scim_user(
        session, user_name="alice@example.com", display_name="Alice", external_id="ext-1"
    )
    assert user.sso_email == "alice@example.com"
    assert user.name == "Alice"
    assert user.scim_external_id == "ext-1"
    assert user.org_id == DEFAULT_ORG_ID
    assert user.budget_usd is None


@pytest.mark.asyncio
async def test_create_scim_user_has_no_signature_path_to_set_org_role():
    """Structural (AC5.8), not a runtime check: `create_scim_user` simply
    has no parameter through which an org-role-shaped SCIM attribute could
    ever reach the `User` row, regardless of what the caller passes."""
    import inspect

    params = inspect.signature(create_scim_user).parameters
    assert "org_role" not in params
    assert "role" not in params


# --- filter parsing (AC5.1's scoped subset) ----------------------------------


def test_parse_simple_eq_filter_none_when_absent():
    assert parse_simple_eq_filter(None, allowed_attributes={"username"}) is None


def test_parse_simple_eq_filter_matches_username():
    result = parse_simple_eq_filter('userName eq "alice@example.com"', allowed_attributes={"username", "externalid"})
    assert result == ("username", "alice@example.com")


def test_parse_simple_eq_filter_rejects_disallowed_attribute():
    with pytest.raises(ScimError) as exc_info:
        parse_simple_eq_filter('emails eq "x"', allowed_attributes={"username"})
    assert exc_info.value.status_code == 400
    assert exc_info.value.scim_type == "invalidFilter"


def test_parse_simple_eq_filter_rejects_unsupported_grammar():
    with pytest.raises(ScimError):
        parse_simple_eq_filter('userName sw "alice"', allowed_attributes={"username"})


# --- PATCH operation parsing (AC5.1's scoped subset) -------------------------


def test_parse_user_patch_active_replace_with_path():
    ops = [{"op": "replace", "path": "active", "value": False}]
    assert parse_user_patch_active(ops) is False


def test_parse_user_patch_active_replace_pathless_value_dict():
    # Some IdPs (e.g. Azure AD) send a path-less op with a value object.
    ops = [{"op": "replace", "value": {"active": False}}]
    assert parse_user_patch_active(ops) is False


def test_parse_user_patch_active_no_recognized_op_returns_none():
    ops = [{"op": "replace", "path": "displayName", "value": "New Name"}]
    assert parse_user_patch_active(ops) is None


def test_parse_group_patch_member_ops_add():
    user_id = uuid.uuid4()
    ops = [{"op": "add", "path": "members", "value": [{"value": str(user_id)}]}]
    add_ids, remove_ids = parse_group_patch_member_ops(ops)
    assert add_ids == [user_id]
    assert remove_ids == []


def test_parse_group_patch_member_ops_remove_via_filtered_path():
    user_id = uuid.uuid4()
    ops = [{"op": "remove", "path": f'members[value eq "{user_id}"]'}]
    add_ids, remove_ids = parse_group_patch_member_ops(ops)
    assert add_ids == []
    assert remove_ids == [user_id]


def test_parse_group_patch_member_ops_remove_via_value_array():
    user_id = uuid.uuid4()
    ops = [{"op": "remove", "path": "members", "value": [{"value": str(user_id)}]}]
    _add_ids, remove_ids = parse_group_patch_member_ops(ops)
    assert remove_ids == [user_id]


# --- scim_token_matches: constant-time bearer-token check (AC5.7) -----------


def _config(*, enabled: bool, token: str | None) -> ScimConfig:
    row = ScimConfig(org_id=DEFAULT_ORG_ID, enabled=enabled)
    row.bearer_token_hash = hash_secret(token) if token else None
    return row


def test_scim_token_matches_correct_token():
    config = _config(enabled=True, token="gk_scim_abc123")
    assert scim_token_matches(config, "gk_scim_abc123") is True


def test_scim_token_matches_wrong_token():
    config = _config(enabled=True, token="gk_scim_abc123")
    assert scim_token_matches(config, "gk_scim_wrong") is False


def test_scim_token_matches_disabled_config_never_matches():
    config = _config(enabled=False, token="gk_scim_abc123")
    assert scim_token_matches(config, "gk_scim_abc123") is False


def test_scim_token_matches_no_token_generated_yet():
    config = _config(enabled=True, token=None)
    assert scim_token_matches(config, "anything") is False


def test_scim_token_matches_none_config():
    assert scim_token_matches(None, "anything") is False


# --- revoke_scim_deactivated_user_credentials: the deactivation cascade -----


@pytest.mark.asyncio
async def test_cascade_revokes_expected_credential_types_and_audits_each():
    personal_key_id = uuid.uuid4()
    session_id_1 = uuid.uuid4()
    session_id_2 = uuid.uuid4()
    cli_id = uuid.uuid4()
    session = _FakeSession(
        [
            [personal_key_id],  # personal_api_keys
            [],  # service_account_keys (none team-attributed for this user)
            [session_id_1, session_id_2],  # sessions
            [cli_id],  # cli_refresh_credentials
        ]
    )

    actions = await revoke_scim_deactivated_user_credentials(session, _TEST_USER_ID, actor=_ACTOR)

    assert actions == [
        "personal_key.revoke",
        "session.revoke",
        "session.revoke",
        "cli_refresh_credential.revoke",
    ]
    # One AuditEntry per revoked credential (4 total), each with the
    # "system:scim" sentinel actor.
    assert len(session.added) == 4
    assert {entry.actor_label for entry in session.added} == {"system:scim"}
    assert {entry.action for entry in session.added} == {
        "personal_key.revoke",
        "session.revoke",
        "cli_refresh_credential.revoke",
    }


@pytest.mark.asyncio
async def test_cascade_revokes_team_attributed_service_account_keys():
    sa_key_id = uuid.uuid4()
    session = _FakeSession([[], [sa_key_id], [], []])

    actions = await revoke_scim_deactivated_user_credentials(session, _TEST_USER_ID, actor=_ACTOR)

    assert actions == ["service_account_key.revoke"]
    assert len(session.added) == 1
    assert session.added[0].action == "service_account_key.revoke"


@pytest.mark.asyncio
async def test_cascade_is_idempotent_on_second_call():
    """A second SCIM deactivation push (or duplicate `PATCH active:false`)
    must never double-revoke or double-audit - the underlying `UPDATE ...
    WHERE revoked_at IS NULL` finds zero rows the second time."""
    session = _FakeSession([[], [], [], []])

    actions = await revoke_scim_deactivated_user_credentials(session, _TEST_USER_ID, actor=_ACTOR)

    assert actions == []
    assert session.added == []
