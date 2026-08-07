"""Unit tests for api/deps.py - require_admin / require_service_account auth dependencies."""

from __future__ import annotations

import base64
import os
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gatekey.api import deps as deps_module
from gatekey.api.deps import (
    AdminContext,
    GatewayCallerContext,
    ServiceAccountContext,
    get_db_session,
    get_key_provider,
    get_validator_registry,
    require_admin,
    require_gateway_credential,
    require_service_account,
)
from gatekey.config import Settings
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.errors import register_exception_handlers
from gatekey.providers.registry import SUPPORTED_PROVIDERS
from gatekey.services.encryption import EnvKeyProvider
from gatekey.services.sessions import SessionContext


def _make_app(admin_token: str) -> FastAPI:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN=admin_token,
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )
    app = FastAPI()
    app.state.settings = settings
    register_exception_handlers(app)

    # Phase 2: `require_admin` gained a DB-session dependency for its
    # org_admin-session fallback. Overridden to the exploding sentinel: the
    # break-glass path, and every no-cookie rejection path, must never touch
    # the DB (`try_get_session_context` returns before any query when no
    # session cookie is present).
    async def _override_get_db_session():
        yield _ExplodingSessionSentinel()

    app.dependency_overrides[get_db_session] = _override_get_db_session

    @app.get("/protected", dependencies=[Depends(require_admin)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/admin-context")
    async def admin_context(ctx: AdminContext = Depends(require_admin)) -> dict:
        return {
            "actor_user_id": str(ctx.actor_user_id) if ctx.actor_user_id else None,
            "actor_label": ctx.actor_label,
            "org_id": str(ctx.org_id),
        }

    return app


def test_require_admin_accepts_correct_token():
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Bearer correct-token"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_require_admin_rejects_wrong_token():
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "wrong-token" not in response.text
    assert "correct-token" not in response.text


def test_require_admin_rejects_missing_header():
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 401


def test_require_admin_rejects_non_bearer_scheme():
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Basic correct-token"})
    assert response.status_code == 401


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_get_validator_registry_covers_all_supported_providers():
    settings = _settings(GATEKEY_PROVIDER_VALIDATION_TIMEOUT_SECONDS=3.5)
    registry = get_validator_registry(settings)
    assert set(registry.keys()) == set(SUPPORTED_PROVIDERS)


def test_get_key_provider_returns_configured_master_key_bytes():
    settings = _settings()
    key_provider = get_key_provider(settings)
    assert isinstance(key_provider, EnvKeyProvider)
    assert key_provider.get_key() == settings.master_key_bytes()


# --- require_service_account -------------------------------------------------


class _ExplodingSessionSentinel:
    """Session stand-in that errors on any attribute access.

    Used to prove a rejection path in `require_service_account` returns
    before ever touching the database (mirrors the identical pattern in
    `test_provider_keys_service.py`).
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed on a rejected request")


def _make_service_account_app(*, lookup_result: ServiceAccountKey | None) -> FastAPI:
    """Build an app with a route gated by `require_service_account`.

    `get_active_service_account_by_hash` is monkeypatched at the module
    level `require_service_account` calls it through (`gatekey.api.deps`),
    so no real database is needed - it always returns `lookup_result`
    regardless of the hash computed from the submitted token. `get_db_session`
    is overridden to yield an exploding sentinel so any code path that
    *does* try to touch the DB (rather than going through the monkeypatched
    lookup) fails loudly.
    """

    async def _fake_lookup(session, secret_hash):  # noqa: ANN001, ARG001
        return lookup_result

    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )
    app = FastAPI()
    app.state.settings = settings
    register_exception_handlers(app)

    async def _override_get_db_session():
        yield _ExplodingSessionSentinel()

    app.dependency_overrides[get_db_session] = _override_get_db_session

    original_lookup = deps_module.get_active_service_account_by_hash
    deps_module.get_active_service_account_by_hash = _fake_lookup  # type: ignore[assignment]
    app.state._original_lookup = original_lookup  # keep a reference for restoration in tests

    @app.get("/gateway-protected", dependencies=[Depends(require_service_account)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/gateway-context")
    async def context(ctx: ServiceAccountContext = Depends(require_service_account)) -> dict:
        return {
            "org_id": str(ctx.org_id),
            "service_account_id": str(ctx.service_account_id),
            "name": ctx.name,
        }

    return app


@pytest.fixture(autouse=True)
def _restore_deps_module_lookup():
    """Restore `deps_module.get_active_service_account_by_hash` after each test.

    `_make_service_account_app` monkeypatches this module attribute
    directly (rather than via the `monkeypatch` fixture) since it needs to
    be patched before route registration in a couple of tests; this
    autouse fixture guarantees it's never left patched across tests.
    """
    from gatekey.services.service_accounts import (
        get_active_service_account_by_hash as real_lookup,
    )

    yield
    deps_module.get_active_service_account_by_hash = real_lookup


def _make_row(*, org_id=None, revoked=False) -> ServiceAccountKey:
    row = ServiceAccountKey(
        org_id=org_id or uuid.uuid4(),
        name="billing-service",
        key_prefix="abcdefghijkl",
        secret_hash=b"\x00" * 32,
    )
    row.id = uuid.uuid4()
    if revoked:
        import datetime

        row.revoked_at = datetime.datetime.now(datetime.timezone.utc)
    return row


def test_require_service_account_accepts_valid_key_and_returns_context():
    org_id = uuid.uuid4()
    row = _make_row(org_id=org_id)
    app = _make_service_account_app(lookup_result=row)
    client = TestClient(app)

    response = client.get(
        "/gateway-context", headers={"Authorization": f"Bearer gk_sk_{'a' * 40}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == str(org_id)
    assert body["service_account_id"] == str(row.id)
    assert body["name"] == "billing-service"


def test_require_service_account_rejects_when_no_active_match_found():
    # Covers both "never existed" and "revoked" - the lookup function
    # itself returns None for both, and require_service_account must not
    # distinguish them in the response.
    app = _make_service_account_app(lookup_result=None)
    client = TestClient(app)

    response = client.get(
        "/gateway-protected", headers={"Authorization": f"Bearer gk_sk_{'a' * 40}"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "gk_sk_" not in response.text


def test_require_service_account_rejects_garbage_token_without_touching_db():
    app = _make_service_account_app(lookup_result=None)
    client = TestClient(app)

    response = client.get(
        "/gateway-protected", headers={"Authorization": "Bearer not-a-service-account-token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_require_service_account_rejects_missing_header():
    app = _make_service_account_app(lookup_result=None)
    client = TestClient(app)

    response = client.get("/gateway-protected")
    assert response.status_code == 401


def test_require_service_account_rejects_non_bearer_scheme():
    app = _make_service_account_app(lookup_result=None)
    client = TestClient(app)

    response = client.get(
        "/gateway-protected", headers={"Authorization": f"Basic gk_sk_{'a' * 40}"}
    )
    assert response.status_code == 401


def test_require_service_account_never_leaks_submitted_token_on_success():
    org_id = uuid.uuid4()
    row = _make_row(org_id=org_id)
    app = _make_service_account_app(lookup_result=row)
    client = TestClient(app)
    submitted = f"gk_sk_{'b' * 40}"

    response = client.get("/gateway-context", headers={"Authorization": f"Bearer {submitted}"})
    assert submitted not in response.text


# --- Phase 2: require_admin org_admin-session fallback (design doc 2.3) ------


def _session_ctx(org_role):
    return SessionContext(
        session_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_role=org_role,
        display_label="Jane Admin <jane@example.com>",
    )


def test_require_admin_break_glass_token_returns_system_sentinel_context():
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/admin-context", headers={"Authorization": "Bearer correct-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["actor_user_id"] is None
    assert body["actor_label"] == "system:admin_token"


def test_require_admin_accepts_org_admin_session(monkeypatch: pytest.MonkeyPatch):
    ctx = _session_ctx("org_admin")

    async def _fake_try_get_session_context(request, session):  # noqa: ANN001, ARG001
        return ctx

    monkeypatch.setattr(deps_module, "try_get_session_context", _fake_try_get_session_context)
    app = _make_app("correct-token")
    client = TestClient(app)
    # No bearer token at all - session cookie path only.
    response = client.get("/admin-context")
    assert response.status_code == 200
    body = response.json()
    assert body["actor_user_id"] == str(ctx.user_id)
    assert body["actor_label"] == ctx.display_label
    assert body["org_id"] == str(ctx.org_id)


@pytest.mark.parametrize("org_role", [None, "auditor"])
def test_require_admin_rejects_non_org_admin_session(
    monkeypatch: pytest.MonkeyPatch, org_role
):
    ctx = _session_ctx(org_role)

    async def _fake_try_get_session_context(request, session):  # noqa: ANN001, ARG001
        return ctx

    monkeypatch.setattr(deps_module, "try_get_session_context", _fake_try_get_session_context)
    app = _make_app("correct-token")
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --- Phase 2: require_gateway_credential (design doc 2.5) --------------------


def _make_personal_key_row(*, org_id=None, team_id=None) -> PersonalApiKey:
    row = PersonalApiKey(
        org_id=org_id or uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        team_id=team_id or uuid.uuid4(),
        name="jane-laptop",
        key_prefix="abcdefghijkl",
        secret_hash=b"\x00" * 32,
    )
    row.id = uuid.uuid4()
    return row


def _make_gateway_credential_app(
    *,
    service_account_result: ServiceAccountKey | None = None,
    personal_key_result: PersonalApiKey | None = None,
) -> FastAPI:
    """App with a route gated by `require_gateway_credential`; both
    per-prefix DB lookups are monkeypatched at the module level the
    dependency calls them through, same pattern as
    `_make_service_account_app`."""

    async def _fake_sa_lookup(session, secret_hash):  # noqa: ANN001, ARG001
        return service_account_result

    async def _fake_pk_lookup(session, secret_hash):  # noqa: ANN001, ARG001
        return personal_key_result

    app = _make_app("test-token")
    deps_module.get_active_service_account_by_hash = _fake_sa_lookup  # type: ignore[assignment]
    deps_module.get_active_personal_key_by_hash = _fake_pk_lookup  # type: ignore[assignment]

    @app.get("/gateway-caller")
    async def caller(ctx: GatewayCallerContext = Depends(require_gateway_credential)) -> dict:
        return {
            "org_id": str(ctx.org_id),
            "credential_id": str(ctx.credential_id),
            "credential_type": ctx.credential_type,
            "user_id": str(ctx.user_id),
            "team_id": str(ctx.team_id) if ctx.team_id else None,
            "name": ctx.name,
        }

    return app


@pytest.fixture(autouse=True)
def _restore_personal_key_lookup():
    from gatekey.services.personal_keys import get_active_personal_key_by_hash as real_lookup

    yield
    deps_module.get_active_personal_key_by_hash = real_lookup


def test_require_gateway_credential_dispatches_gk_sk_to_service_account():
    row = _make_row()
    row.user_id = uuid.uuid4()
    row.team_id = None
    app = _make_gateway_credential_app(service_account_result=row)
    client = TestClient(app)
    response = client.get("/gateway-caller", headers={"Authorization": f"Bearer gk_sk_{'a' * 40}"})
    assert response.status_code == 200
    body = response.json()
    assert body["credential_type"] == "service_account"
    assert body["credential_id"] == str(row.id)
    assert body["team_id"] is None


def test_require_gateway_credential_dispatches_gk_pk_to_personal_key():
    team_id = uuid.uuid4()
    row = _make_personal_key_row(team_id=team_id)
    app = _make_gateway_credential_app(personal_key_result=row)
    client = TestClient(app)
    response = client.get("/gateway-caller", headers={"Authorization": f"Bearer gk_pk_{'a' * 40}"})
    assert response.status_code == 200
    body = response.json()
    assert body["credential_type"] == "personal"
    assert body["credential_id"] == str(row.id)
    assert body["user_id"] == str(row.owner_user_id)
    assert body["team_id"] == str(team_id)


def test_require_gateway_credential_rejects_unmatched_personal_key():
    # Covers revoked, expired, and never-existed alike - the lookup returns
    # None for all three and the response must not distinguish them.
    app = _make_gateway_credential_app(personal_key_result=None)
    client = TestClient(app)
    response = client.get("/gateway-caller", headers={"Authorization": f"Bearer gk_pk_{'a' * 40}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "gk_pk_" not in response.text


def test_require_gateway_credential_rejects_unknown_prefix_without_touching_db():
    app = _make_gateway_credential_app()
    client = TestClient(app)
    response = client.get(
        "/gateway-caller", headers={"Authorization": "Bearer not-a-gateway-token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --- Phase 2 follow-up: break-glass bearer on require_role/require_team_role -


def _make_rbac_app() -> FastAPI:
    """`_make_app` plus routes gated by the require_role/require_team_role
    factories - break-glass must drive these too (locked decision #1/A4)."""
    app = _make_app("correct-token")

    @app.get("/org-only")
    async def org_only(
        ctx: SessionContext = Depends(deps_module.require_role("org_admin")),
    ) -> dict:
        return {
            "user_id": str(ctx.user_id) if ctx.user_id else None,
            "label": ctx.display_label,
            "org_role": ctx.org_role,
        }

    @app.get("/teams/{team_id}/lead-only")
    async def lead_only(
        team_ctx=Depends(deps_module.require_team_role("team_lead")),
    ) -> dict:
        return {"role": team_ctx.role, "via_bypass": team_ctx.via_bypass}

    return app


def test_break_glass_bearer_passes_require_role_org_admin():
    app = _make_rbac_app()
    client = TestClient(app)
    response = client.get("/org-only", headers={"Authorization": "Bearer correct-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] is None
    assert body["label"] == "system:admin_token"
    assert body["org_role"] == "org_admin"


def test_break_glass_bearer_passes_require_team_role_via_org_admin_bypass():
    app = _make_rbac_app()
    client = TestClient(app)
    response = client.get(
        f"/teams/{uuid.uuid4()}/lead-only", headers={"Authorization": "Bearer correct-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"role": "org_admin", "via_bypass": True}


def test_wrong_bearer_and_no_cookie_still_401_on_rbac_factories():
    app = _make_rbac_app()
    client = TestClient(app)
    for path in ("/org-only", f"/teams/{uuid.uuid4()}/lead-only"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer wrong-token"})
        assert response.status_code == 401
        assert "correct-token" not in response.text


def test_member_session_still_403_on_rbac_factories(monkeypatch: pytest.MonkeyPatch):
    member_ctx = _session_ctx(None)

    async def _fake_try_get_session_context(request, session):  # noqa: ANN001, ARG001
        return member_ctx

    async def _fake_get_team_membership(session, *, team_id, user_id):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(deps_module, "try_get_session_context", _fake_try_get_session_context)
    monkeypatch.setattr(deps_module, "_get_team_membership", _fake_get_team_membership)
    app = _make_rbac_app()
    client = TestClient(app)
    for path in ("/org-only", f"/teams/{uuid.uuid4()}/lead-only"):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
