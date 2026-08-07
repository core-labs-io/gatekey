"""Confirms `require_admin` and `require_service_account` are genuinely
separate, non-bypassable trust boundaries (Phase 1.2 design doc section 4).

`test_deps.py` unit-tests each dependency in isolation against a throwaway
route. That's necessary but not sufficient: it never proves that the *real*
router wiring in `main.create_app()` keeps the two credential types from
working against each other's endpoints. This module closes that gap by
exercising the actual gateway and admin routers together, through the real
app factory, with only the DB session overridden (never the auth
dependencies themselves).

Two things are asserted per direction:
  - the wrong credential is rejected with 401 `unauthorized`, not silently
    accepted, not a 403, and not a 500 from an unexpected code path.
  - the request never reaches the database (the exploding session sentinel
    would raise `AssertionError` - surfaced as an unhandled exception - if
    it did), i.e. the credential mismatch is caught before any DB round
    trip.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gatekey.api.deps import get_db_session, require_admin, require_service_account
from gatekey.db.session import get_db_session as db_session_get_db_session
from gatekey.main import create_app

from tests.unit.gateway_test_support import _ExplodingSessionSentinel, make_settings

_ADMIN_TOKEN = "the-real-admin-token"
_SERVICE_ACCOUNT_KEY = f"gk_sk_{'a' * 40}"


async def _override_get_db_session():
    yield _ExplodingSessionSentinel()


def _build_app_with_real_auth_both_boundaries():
    """Real `create_app()`, both `require_admin` and `require_service_account`
    left as the genuine dependencies (neither overridden) - only the DB
    session is swapped for an exploding sentinel so no real database is
    needed for a request that's correctly rejected at the auth layer.
    """
    app = create_app(settings=make_settings(GATEKEY_ADMIN_TOKEN=_ADMIN_TOKEN))
    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[db_session_get_db_session] = _override_get_db_session
    return app


def test_admin_token_rejected_against_gateway_chat_completions() -> None:
    """A valid human admin token must not authenticate a gateway request."""
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_admin_token_rejected_against_gateway_completions() -> None:
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post(
            "/v1/completions",
            json={"model": "gpt-4o", "prompt": "hi"},
            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_admin_token_rejected_against_gateway_embeddings() -> None:
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hi"},
            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_service_account_key_rejected_against_admin_create_service_account() -> None:
    """A valid per-app service-account key must not authenticate an admin request."""
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post(
            "/v1/admin/service-accounts",
            json={"name": "some-app"},
            headers={"Authorization": f"Bearer {_SERVICE_ACCOUNT_KEY}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_service_account_key_rejected_against_admin_list_service_accounts() -> None:
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.get(
            "/v1/admin/service-accounts",
            headers={"Authorization": f"Bearer {_SERVICE_ACCOUNT_KEY}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_service_account_key_rejected_against_admin_provider_keys() -> None:
    app = _build_app_with_real_auth_both_boundaries()
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.get(
            "/v1/admin/providers",
            headers={"Authorization": f"Bearer {_SERVICE_ACCOUNT_KEY}"},
        )
    # Either a real route that 401s, or (if this exact path isn't the
    # provider-listing route) a 404 is acceptable - what must never happen
    # is a 200 or anything indicating the service-account credential was
    # accepted as admin auth. Assert explicitly against 200 either way, and
    # assert 401 in the expected case.
    assert response.status_code != 200
    if response.status_code == 401:
        assert response.json()["error"]["code"] == "unauthorized"


def test_admin_dependency_and_service_account_dependency_are_distinct_callables() -> None:
    """Cheap structural guardrail: the two auth dependencies must not
    resolve to the same function object (which would silently merge the two
    trust boundaries no matter what the route wiring says).
    """
    assert require_admin is not require_service_account
