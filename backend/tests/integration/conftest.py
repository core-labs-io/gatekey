"""Shared fixtures for integration tests: a real Postgres 16 instance.

By default, spins up a throwaway `postgres:16-alpine` Docker container
(session-scoped: one container for the whole integration test run),
applies every Alembic migration against it, and tears the container down
after the session. Set `GATEKEY_TEST_DATABASE_URL` in the environment to
point at an already-running Postgres instead (e.g. in CI) - the fixture
then skips Docker entirely and just runs migrations against that URL.

Provider validators (`OpenAIValidator`/`AnthropicValidator`/
`VertexAIValidator`/`OllamaValidator`/`OpenRouterValidator`) are
monkeypatched to a canned "always valid" response by an autouse fixture so
these tests never hit real provider APIs; individual tests override this
for a single provider where they need a non-VALID outcome (see
`test_provider_keys_api.py`).
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from gatekey.config import Settings
from gatekey.main import create_app
from gatekey.providers import anthropic as anthropic_mod
from gatekey.providers import ollama as ollama_mod
from gatekey.providers import openai as openai_mod
from gatekey.providers import openrouter as openrouter_mod
from gatekey.providers import vertex_ai as vertex_mod
from gatekey.providers.base import ValidationResult, ValidationStatus

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def to_asyncpg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy `postgresql+asyncpg://` URL to a plain asyncpg DSN."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _wait_for_postgres(dsn: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            return
        except Exception as exc:  # noqa: BLE001 - retry loop, re-raised below on timeout
            last_exc = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Postgres did not become ready within {timeout_seconds}s: {last_exc}")


@pytest.fixture(scope="session")
def postgres_database_url() -> AsyncIterator[str]:
    """Yield a `postgresql+asyncpg://` URL to a running, empty Postgres 16 instance."""
    existing = os.environ.get("GATEKEY_TEST_DATABASE_URL")
    if existing:
        yield existing
        return

    port = _free_port()
    container_name = f"gatekey-test-pg-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=gatekey_test",
            "-p",
            f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    database_url = f"postgresql+asyncpg://postgres:postgres@localhost:{port}/gatekey_test"
    try:
        asyncio.run(_wait_for_postgres(to_asyncpg_dsn(database_url)))
        yield database_url
    finally:
        subprocess.run(["docker", "stop", container_name], capture_output=True)


@pytest.fixture(scope="session")
def migrated_database_url(postgres_database_url: str) -> str:
    """Apply every Alembic migration once for the whole test session."""
    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    os.environ["DATABASE_URL"] = postgres_database_url
    command.upgrade(alembic_cfg, "head")
    return postgres_database_url


@pytest.fixture
def admin_token() -> str:
    return "integration-test-admin-token"


@pytest.fixture
def master_key_bytes() -> bytes:
    return os.urandom(32)


@pytest.fixture
def auth_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest_asyncio.fixture
async def app(
    migrated_database_url: str, admin_token: str, master_key_bytes: bytes
) -> AsyncIterator[FastAPI]:
    settings = Settings(
        _env_file=None,
        DATABASE_URL=migrated_database_url,
        GATEKEY_ADMIN_TOKEN=admin_token,
        GATEKEY_MASTER_KEY=base64.b64encode(master_key_bytes).decode(),
    )
    application = create_app(settings=settings)
    yield application


@pytest_asyncio.fixture
async def client(app: FastAPI):
    """An `httpx.AsyncClient` talking to `app` over ASGI, with lifespan run.

    `httpx.ASGITransport` does not run Starlette lifespan events on its own,
    so this fixture drives `app.router.lifespan_context` explicitly - this
    also means the real `db_engine`/`db_session_factory` startup/shutdown
    code in `main.create_app` executes exactly as it would in production.
    """
    import httpx

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client


@pytest_asyncio.fixture(autouse=True)
async def _truncate_provider_keys(migrated_database_url: str) -> AsyncIterator[None]:
    """Ensure each test starts with an empty `provider_keys` table.

    `orgs` is left alone (it holds only the single seeded default org row,
    which every test relies on existing). `CASCADE` (Phase 3, migration
    `0020`): `rotation_policies.scope_provider_key_id` now FKs to this
    table, so a bare `TRUNCATE` fails with Postgres's
    `FeatureNotSupportedError` - same fix/rationale as
    `test_service_accounts_api.py`'s `_truncate_service_account_keys`.
    """
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE provider_keys CASCADE")
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def default_user_id(client, auth_headers: dict[str, str]) -> str:
    """A freshly created `User` (Phase 1.4 budget cost-center) id, for tests
    that need *a* valid `user_id` to attribute a service-account key to and
    don't care about that user's own identity/budget.

    `user_id` became a required field on `POST /v1/admin/service-accounts`
    when Phase 1.4 (Budget - Basic) landed - see
    `schemas/service_account_key.py`. This fixture exists so call sites
    don't each have to know how to stand up a user first.
    """
    response = await client.post(
        "/v1/admin/users", json={"name": "test-fixture-user"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest_asyncio.fixture
async def default_team_id(client, auth_headers: dict[str, str], default_user_id: str) -> str:
    """A team the `default_user_id` user is a member of.

    `team_id` became a required field on `POST /v1/admin/service-accounts`
    with Phase 2 (design doc section 1.7 / security review H-1), and the
    target user must hold a `TeamMembership` on that team. Unique team name
    per invocation - teams are not truncated between every test module.
    """
    import uuid as _uuid

    response = await client.post(
        "/v1/teams",
        json={"name": f"test-fixture-team-{_uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    team_id = response.json()["id"]
    response = await client.post(
        f"/v1/teams/{team_id}/members",
        json={"user_id": default_user_id, "role": "member", "budget_usd": None},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return team_id


@pytest.fixture(autouse=True)
def _default_valid_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default, every provider validator reports the credential as valid.

    Individual tests override a specific provider's `validate` again (via
    the same `monkeypatch` fixture) to exercise non-VALID outcomes.
    """

    async def _always_valid(self, secret_payload):  # noqa: ANN001, ARG001
        return ValidationResult(status=ValidationStatus.VALID)

    monkeypatch.setattr(openai_mod.OpenAIValidator, "validate", _always_valid)
    monkeypatch.setattr(anthropic_mod.AnthropicValidator, "validate", _always_valid)
    monkeypatch.setattr(vertex_mod.VertexAIValidator, "validate", _always_valid)
    monkeypatch.setattr(ollama_mod.OllamaValidator, "validate", _always_valid)
    monkeypatch.setattr(openrouter_mod.OpenRouterValidator, "validate", _always_valid)
