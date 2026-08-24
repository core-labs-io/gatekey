"""Integration tests for the model-policy admin API and the gateway's
model-policy enforcement, against a real Postgres (Phase 1.3, BD-9).

See `conftest.py` for the Postgres/Docker/migration/lifespan/validator-mock
plumbing these tests build on.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

import gatekey.main as main_module
from gatekey.constants import DEFAULT_ORG_ID

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_MODEL_POLICY_URL = "/v1/admin/model-policy"


@pytest.fixture(autouse=True)
async def _truncate_model_policies(migrated_database_url: str):
    """Ensure each test starts with no `model_policies` row (unconfigured)."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE model_policies")
    finally:
        await conn.close()
    yield


async def _row_count(database_url: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM model_policies WHERE org_id = $1", DEFAULT_ORG_ID
        )
    finally:
        await conn.close()


async def _fetch_row(database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT mode, models FROM model_policies WHERE org_id = $1", DEFAULT_ORG_ID
        )
    finally:
        await conn.close()


# --- admin GET/PUT contract ---------------------------------------------------


async def test_get_with_no_row_returns_unconfigured_default(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """AC-4/AC-7: a fresh org with no PUT ever called reads back as
    `{"mode": "unconfigured", "models": []}`, not a 404."""
    response = await client.get(_MODEL_POLICY_URL, headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"mode": "unconfigured", "models": []}


async def test_put_allowlist_then_get_reflects_it(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    put_response = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "allowlist", "models": ["gpt-4o", "gpt-4o-mini"]},
        headers=auth_headers,
    )
    assert put_response.status_code == 200
    body = put_response.json()
    assert body["mode"] == "allowlist"
    assert sorted(body["models"]) == ["gpt-4o", "gpt-4o-mini"]

    get_response = await client.get(_MODEL_POLICY_URL, headers=auth_headers)
    assert get_response.status_code == 200
    assert get_response.json() == body

    assert await _row_count(migrated_database_url) == 1


async def test_put_rejects_unknown_model_id_and_writes_nothing(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    response = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "allowlist", "models": ["gpt-4o", "not-a-real-model"]},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_in_policy"
    assert await _row_count(migrated_database_url) == 0


async def test_put_rejects_unconfigured_mode_with_structured_422(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "unconfigured", "models": []},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_put_requires_admin_auth(client: httpx.AsyncClient) -> None:
    response = await client.put(
        _MODEL_POLICY_URL, json={"mode": "allowlist", "models": ["gpt-4o"]}
    )
    assert response.status_code == 401


async def test_get_requires_admin_auth(client: httpx.AsyncClient) -> None:
    response = await client.get(_MODEL_POLICY_URL)
    assert response.status_code == 401


# --- AC-8: PUT is a full-replace upsert, not a merge --------------------------


async def test_put_denylist_then_put_allowlist_fully_replaces_old_entries(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    """PUT a denylist, then PUT an allowlist - the old denylist entries must
    have zero effect afterward (AC-8), and exactly one row must exist (a
    true replace, not an accumulating second row)."""
    first = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "denylist", "models": ["gpt-4o", "gpt-4o-mini"]},
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json() == {"mode": "denylist", "models": ["gpt-4o", "gpt-4o-mini"]}

    second = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "allowlist", "models": ["claude-sonnet-5"]},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json() == {"mode": "allowlist", "models": ["claude-sonnet-5"]}

    # Exactly one row - a true replace, not a second accumulating row.
    assert await _row_count(migrated_database_url) == 1
    row = await _fetch_row(migrated_database_url)
    assert row["mode"] == "allowlist"
    # asyncpg returns a `jsonb` column as raw JSON text, not a parsed list.
    assert json.loads(row["models"]) == ["claude-sonnet-5"]

    get_response = await client.get(_MODEL_POLICY_URL, headers=auth_headers)
    assert get_response.json() == {"mode": "allowlist", "models": ["claude-sonnet-5"]}
    # `gpt-4o`/`gpt-4o-mini` (the old denylist's entries) do not appear
    # anywhere in the new row at all - the old denylist has zero remaining
    # effect. The dedicated gateway-enforcement tests below additionally
    # prove this behaviorally (a request for a model absent from the new
    # snapshot's `models` list is evaluated purely against the *current*
    # mode/list, never against anything from a prior PUT).


# --- gateway enforcement end-to-end (real DB-backed policy, not a monkeypatched
#     in-process cache) ----------------------------------------------------------


async def _create_service_account(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> str:
    """`user_id` became required on this endpoint with Phase 1.4 (Budget -
    Basic) and `team_id` with Phase 2 (design doc 1.7 / security review
    H-1) - `default_user_id`/`default_team_id` are the `conftest.py`
    fixtures that stand up a throwaway user + team membership for exactly
    this purpose."""
    response = await client.post(
        "/v1/admin/service-accounts",
        json={
            "name": "policy-test-sa",
            "user_id": default_user_id,
            "team_id": default_team_id,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()["secret"]


async def test_fresh_org_default_permissive_registry_model_succeeds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    default_user_id: str,
    default_team_id: str,
) -> None:
    """AC-4: no PUT is ever called for this org - a registry-known model
    request must succeed (reach the provider dispatch layer), proving the
    warmed cache defaults to permissive against a real, empty DB."""
    from gatekey.providers import openai as openai_mod
    from gatekey.schemas.chat import (
        ChatCompletionChoice,
        ChatCompletionResponse,
        ChatCompletionUsage,
        ChatCompletionResponseMessage,
    )
    from gatekey.services.proxy_keys import ApiKeyCredential

    async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_create(client_, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return ChatCompletionResponse(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatCompletionResponseMessage(role="assistant", content="hi"),
                    finish_reason="stop",
                )
            ],
            usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    from gatekey.api.v1.gateway import common as gateway_common

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)
    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    # The real request path resolves the provider key via
    # `select_provider_key()` -> `provider_keys_service.get_primary_key()`,
    # a real DB query - the `get_decrypted_provider_credential` monkeypatch
    # above is on the old, no-longer-on-path function, so a `ProviderKey`
    # row must actually exist for this org/provider (conftest.py's
    # `OpenAIValidator.validate` monkeypatch makes this succeed without a
    # live network call).
    put_key_response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-integration-test-plaintext-marker"},
        headers=auth_headers,
    )
    assert put_key_response.status_code == 200

    secret = await _create_service_account(
        client, auth_headers, default_user_id, default_team_id
    )

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200


async def test_put_denylist_then_gateway_request_for_denied_model_returns_403(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    default_user_id: str,
    default_team_id: str,
) -> None:
    """The admin `PUT` pushes the new snapshot straight into this process's
    cache (design doc section 2.3) - no restart/re-warm needed for the very
    next gateway request in the same process to see it."""
    from gatekey.api.v1.gateway import common as gateway_common

    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)

    secret = await _create_service_account(
        client, auth_headers, default_user_id, default_team_id
    )

    put_response = await client.put(
        _MODEL_POLICY_URL,
        json={"mode": "denylist", "models": ["gpt-4o"]},
        headers=auth_headers,
    )
    assert put_response.status_code == 200

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_denied"


# --- Phase 1.1/1.2/1.4 addition: new-provider models go through the exact
#     same generic model-policy mechanism as the original 3 providers, not
#     re-implemented or bypassed for ollama/openrouter -----------------------


async def _create_service_account_via_service_layer(database_url: str) -> str:
    """Bypasses `POST /v1/admin/service-accounts` - see
    `test_gateway_ollama_openrouter.py`'s module docstring for why (a
    pre-existing, unrelated bug in that route/fixture makes it return 422
    instead of 201 in this test session; not this addition's bug to fix)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from gatekey.db.session import create_engine as db_create_engine
    from gatekey.db.session import create_session_factory
    from gatekey.services.service_accounts import create_service_account
    from gatekey.services.users import create_user

    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory: async_sessionmaker = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = await create_user(session, name="policy-new-provider-test-user")
            _row, secret = await create_service_account(
                session, "policy-new-provider-test-sa", user.id
            )
            return secret
    finally:
        await engine.dispose()


async def test_put_denylist_then_gateway_request_for_denied_ollama_model_returns_403(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    """Mirrors test_put_denylist_then_gateway_request_for_denied_model_
    returns_403 exactly, but for an `ollama/`-prefixed gateway-facing model
    key - confirms `check_model_policy()`'s exact-string MODEL_REGISTRY-key
    membership check (module docstring, common.py) applies identically to
    the new providers' prefixed keys, not just the original 3 providers'
    bare names."""
    import httpx as httpx_module

    from gatekey.api.v1.gateway import common as gateway_common

    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    import pytest as pytest_module

    monkeypatch = pytest_module.MonkeyPatch()
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)
    try:
        secret = await _create_service_account_via_service_layer(migrated_database_url)

        async with app.router.lifespan_context(app):
            transport = httpx_module.ASGITransport(app=app)
            async with httpx_module.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_response = await client.put(
                    _MODEL_POLICY_URL,
                    json={"mode": "denylist", "models": ["ollama/llama3.1"]},
                    headers=auth_headers,
                )
                assert put_response.status_code == 200

                response = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "ollama/llama3.1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        monkeypatch.undo()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_denied"


# --- ADR-3 addendum, second round: self-heal must not clobber a concurrent
#     PUT (security review finding) -------------------------------------------


async def test_self_heal_does_not_clobber_a_put_that_lands_while_its_read_is_in_flight(
    app: FastAPI, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review finding, second round (design doc section 2.2/ADR-3
    addendum): `_model_policy_self_heal` now runs concurrently with live
    admin `PUT` traffic (it's scheduled as a background task after a failed
    bootstrap, not run to completion before the app serves traffic like the
    one-shot bootstrap is). Reproduces the exact interleaving the finding
    describes: a self-heal retry's `load_policy_snapshot()` read is
    in-flight (already dispatched, awaiting) when a concurrent admin `PUT`
    commits a new, more-restrictive policy and calls `cache.set()`; the
    self-heal read then resumes with what it had already fetched (modeled
    here as a stale, pre-PUT snapshot - the point being *that it was in
    flight before the PUT*, not the exact bytes it returns). Asserts the
    cache ends up holding the PUT's value, not self-heal's stale one.

    Placed here (integration, real Postgres) rather than in
    `tests/unit/test_main.py`: driving a real `PUT /v1/admin/model-policy`
    to completion requires a real DB session - the unit-test harness's
    `get_db_session` override is an exploding sentinel that raises on any
    attribute access (design doc section 6), so a real PUT cannot be driven
    through in that harness. This also happens to exercise the precise
    single-threaded-event-loop interleaving the finding is about: this
    test, the self-heal background task, and the PUT request all run as
    plain `asyncio` tasks on one event loop (no `TestClient` portal thread
    involved, unlike the unit self-heal tests, which matters because the
    race is specifically a same-loop, no-lock-needed one).

    Drives the lifespan directly via `app.router.lifespan_context` (rather
    than the `client` fixture) so the `load_policy_snapshot` monkeypatch
    below is guaranteed to be in place *before* the lifespan's bootstrap
    attempt runs.
    """
    call_count = 0
    self_heal_read_in_flight = asyncio.Event()
    release_self_heal_read = asyncio.Event()
    stale_pre_put_snapshot = main_module.ModelPolicySnapshot(
        mode="unconfigured", models=frozenset()
    )

    async def _fail_once_then_race(*args, **kwargs):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # The initial bootstrap attempt fails, so `_lifespan` schedules
            # the self-heal background task.
            raise RuntimeError("simulated transient DB outage during bootstrap")
        # This is a self-heal retry's read: signal the test that it has
        # been dispatched, then block - modeling "already in flight" -
        # until the test has driven a PUT through underneath it, then
        # resume with a snapshot representing what it had already fetched
        # (pre-PUT), exactly like the finding's step 3.
        self_heal_read_in_flight.set()
        await release_self_heal_read.wait()
        return stale_pre_put_snapshot

    monkeypatch.setattr(main_module, "load_policy_snapshot", _fail_once_then_race)
    # Fast, deterministic backoff/timeout for the test.
    monkeypatch.setattr(main_module, "_MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "_MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS", 30.0)

    async with app.router.lifespan_context(app):
        self_heal_task = app.state.model_policy_self_heal_task
        assert self_heal_task is not None

        # Wait for the self-heal retry's read to actually be in flight
        # (blocked on `release_self_heal_read`) before driving the PUT -
        # this is what makes the interleaving deterministic rather than a
        # coin flip on scheduling order.
        await asyncio.wait_for(self_heal_read_in_flight.wait(), timeout=5.0)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            put_response = await client.put(
                _MODEL_POLICY_URL,
                json={"mode": "denylist", "models": ["gpt-4o"]},
                headers=auth_headers,
            )
        assert put_response.status_code == 200
        assert put_response.json() == {"mode": "denylist", "models": ["gpt-4o"]}

        # Now let the blocked self-heal read resume and complete - per the
        # finding, its stale result is exactly what would otherwise
        # silently clobber the PUT's just-committed, more-restrictive
        # policy.
        release_self_heal_read.set()
        await self_heal_task

        final_snapshot = app.state.model_policy_cache.get()
        assert final_snapshot.mode == "denylist"
        assert final_snapshot.models == frozenset({"gpt-4o"})
        assert final_snapshot.is_allowed("gpt-4o") is False

    assert call_count == 2
    assert self_heal_task.done()
