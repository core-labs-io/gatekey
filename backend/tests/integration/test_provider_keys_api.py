"""Integration tests for the provider-key admin API against a real Postgres.

See `conftest.py` for the Postgres/Docker/migration/lifespan/validator-mock
plumbing these tests build on.
"""

from __future__ import annotations

import json

import asyncpg
import httpx
import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.providers.anthropic import AnthropicValidator
from gatekey.providers.base import ValidationResult, ValidationStatus
from gatekey.providers.openai import OpenAIValidator
from gatekey.services.encryption import EnvKeyProvider, build_aad, decrypt_secret

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


async def _fetch_row(database_url: str, provider: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT ciphertext, nonce, auth_tag, metadata, validated_at, created_at, updated_at "
            "FROM provider_keys WHERE org_id = $1 AND provider = $2",
            DEFAULT_ORG_ID,
            provider,
        )
    finally:
        await conn.close()


async def _row_count(database_url: str, provider: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM provider_keys WHERE org_id = $1 AND provider = $2",
            DEFAULT_ORG_ID,
            provider,
        )
    finally:
        await conn.close()


def _decrypt(row: asyncpg.Record, provider: str, master_key_bytes: bytes) -> object:
    plaintext = decrypt_secret(
        bytes(row["ciphertext"]),
        nonce=bytes(row["nonce"]),
        auth_tag=bytes(row["auth_tag"]),
        aad=build_aad(str(DEFAULT_ORG_ID), provider),
        key_provider=EnvKeyProvider(master_key_bytes),
    )
    return json.loads(plaintext)


async def test_add_key_success_persists_encrypted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    submitted_key = "sk-integration-test-plaintext-marker"

    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": submitted_key},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openai"
    assert body["configured"] is True
    assert "ciphertext" not in body
    assert "nonce" not in body
    assert "auth_tag" not in body
    assert submitted_key not in response.text

    row = await _fetch_row(migrated_database_url, "openai")
    assert row is not None
    ciphertext = bytes(row["ciphertext"])
    assert isinstance(ciphertext, bytes) and len(ciphertext) > 0
    # Not stored as (or containing) plaintext.
    assert submitted_key.encode("utf-8") not in ciphertext

    decrypted = _decrypt(row, "openai", master_key_bytes)
    assert decrypted == submitted_key


async def test_invalid_key_nothing_persisted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _invalid(self, secret_payload):  # noqa: ANN001, ARG001
        return ValidationResult(status=ValidationStatus.INVALID_KEY, detail="Provider said no.")

    monkeypatch.setattr(OpenAIValidator, "validate", _invalid)

    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-should-not-be-saved"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_key"
    assert "sk-should-not-be-saved" not in response.text

    assert await _row_count(migrated_database_url, "openai") == 0

    get_response = await client.get("/v1/admin/providers/openai", headers=auth_headers)
    assert get_response.status_code == 404


async def test_provider_unreachable_maps_to_502(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unreachable(self, secret_payload):  # noqa: ANN001, ARG001
        return ValidationResult(status=ValidationStatus.PROVIDER_UNREACHABLE, detail="Timed out.")

    monkeypatch.setattr(AnthropicValidator, "validate", _unreachable)

    response = await client.put(
        "/v1/admin/providers/anthropic/key",
        json={"api_key": "sk-whatever"},
        headers=auth_headers,
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unreachable"


async def test_unknown_validation_error_maps_to_500(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unknown(self, secret_payload):  # noqa: ANN001, ARG001
        return ValidationResult(status=ValidationStatus.UNKNOWN_ERROR, detail="Something odd.")

    monkeypatch.setattr(OpenAIValidator, "validate", _unknown)

    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-whatever"},
        headers=auth_headers,
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "unknown_error"


async def test_upsert_twice_replaces_not_duplicates(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    first = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-first-value"},
        headers=auth_headers,
    )
    assert first.status_code == 200
    first_created_at = first.json()["created_at"]

    second = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-second-value"},
        headers=auth_headers,
    )
    assert second.status_code == 200
    second_body = second.json()

    # Same row (created_at preserved by the DB, not a new insert), not a
    # second row.
    assert second_body["created_at"] == first_created_at
    assert await _row_count(migrated_database_url, "openai") == 1

    list_response = await client.get("/v1/admin/providers", headers=auth_headers)
    openai_entries = [entry for entry in list_response.json() if entry["provider"] == "openai"]
    assert len(openai_entries) == 1

    row = await _fetch_row(migrated_database_url, "openai")
    assert _decrypt(row, "openai", master_key_bytes) == "sk-second-value"


async def test_delete_then_get_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    put_response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-to-be-deleted"},
        headers=auth_headers,
    )
    assert put_response.status_code == 200

    delete_response = await client.delete("/v1/admin/providers/openai", headers=auth_headers)
    assert delete_response.status_code == 204

    get_response = await client.get("/v1/admin/providers/openai", headers=auth_headers)
    assert get_response.status_code == 404

    # Deleting again (nothing left to delete) is also a 404, not a 500/204.
    redelete_response = await client.delete("/v1/admin/providers/openai", headers=auth_headers)
    assert redelete_response.status_code == 404


async def test_list_never_includes_secret_fields_in_raw_response(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    openai_secret = "sk-openai-super-secret-marker"
    anthropic_secret = "sk-anthropic-super-secret-marker"

    for provider, key in (("openai", openai_secret), ("anthropic", anthropic_secret)):
        response = await client.put(
            f"/v1/admin/providers/{provider}/key",
            json={"api_key": key},
            headers=auth_headers,
        )
        assert response.status_code == 200

    list_response = await client.get("/v1/admin/providers", headers=auth_headers)
    assert list_response.status_code == 200
    raw_text = list_response.text

    for forbidden in ("ciphertext", "nonce", "auth_tag", openai_secret, anthropic_secret):
        assert forbidden not in raw_text

    body = list_response.json()
    assert {entry["provider"] for entry in body} == {"openai", "anthropic"}
    for entry in body:
        assert set(entry.keys()) == {
            "provider",
            "configured",
            "validated_at",
            "created_at",
            "updated_at",
            "metadata",
        }


async def test_add_ollama_key_with_bearer_token_persists_encrypted_and_base_url_metadata(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    submitted_bearer = "ollama-integration-bearer-marker"

    response = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://localhost:11434", "bearer_token": submitted_bearer},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "ollama"
    assert body["metadata"] == {"base_url": "http://localhost:11434"}
    assert submitted_bearer not in response.text

    row = await _fetch_row(migrated_database_url, "ollama")
    assert row is not None
    ciphertext = bytes(row["ciphertext"])
    assert isinstance(ciphertext, bytes) and len(ciphertext) > 0
    assert submitted_bearer.encode("utf-8") not in ciphertext

    decrypted = _decrypt(row, "ollama", master_key_bytes)
    assert decrypted == submitted_bearer


async def test_add_ollama_key_with_no_bearer_token_still_persists_non_empty_ciphertext(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    """AC-B2-5: an Ollama key saved with no bearer_token must still produce
    real, non-empty ciphertext/nonce/auth_tag - the encrypt step is never
    skipped, never a NULL-equivalent placeholder."""
    response = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://localhost:11434"},
        headers=auth_headers,
    )

    assert response.status_code == 200

    row = await _fetch_row(migrated_database_url, "ollama")
    assert row is not None
    ciphertext, nonce, auth_tag = bytes(row["ciphertext"]), bytes(row["nonce"]), bytes(row["auth_tag"])
    assert len(ciphertext) > 0
    assert len(nonce) > 0
    assert len(auth_tag) > 0

    decrypted = _decrypt(row, "ollama", master_key_bytes)
    assert decrypted == ""


async def test_replace_ollama_key_with_new_base_url_updates_metadata_not_stale(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    """An admin editing/replacing an Ollama key with a different base_url
    must have the new value reflected everywhere - the PUT response, a
    subsequent GET, and the persisted row - never a stale base_url left
    over from the first save. Exercises the same `INSERT ... ON CONFLICT
    DO UPDATE` path as `test_upsert_twice_replaces_not_duplicates`, but for
    Ollama's `key_metadata` column specifically (the openai/anthropic case
    has no metadata to go stale)."""
    first = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://localhost:11434", "bearer_token": "tok-first"},
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json()["metadata"] == {"base_url": "http://localhost:11434"}

    second = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://new-ollama-host:22222", "bearer_token": "tok-second"},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json()["metadata"] == {"base_url": "http://new-ollama-host:22222"}

    # A fresh GET (simulating a newly-fetched credential/response, not a
    # cached one from the PUT response object) must also see the new value.
    get_response = await client.get("/v1/admin/providers/ollama", headers=auth_headers)
    assert get_response.status_code == 200
    assert get_response.json()["metadata"] == {"base_url": "http://new-ollama-host:22222"}

    # Still exactly one row (upsert, not a duplicate insert).
    assert await _row_count(migrated_database_url, "ollama") == 1

    row = await _fetch_row(migrated_database_url, "ollama")
    assert row is not None
    assert json.loads(row["metadata"]) == {"base_url": "http://new-ollama-host:22222"}
    assert _decrypt(row, "ollama", master_key_bytes) == "tok-second"


async def test_add_openrouter_key_success_persists_encrypted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    submitted_key = "sk-or-integration-test-plaintext-marker"

    response = await client.put(
        "/v1/admin/providers/openrouter/key",
        json={"api_key": submitted_key},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openrouter"
    assert submitted_key not in response.text

    row = await _fetch_row(migrated_database_url, "openrouter")
    assert row is not None
    decrypted = _decrypt(row, "openrouter", master_key_bytes)
    assert decrypted == submitted_key


async def test_concurrent_put_race_does_not_corrupt_state(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    """Two concurrent PUTs for the same provider must not corrupt the row.

    The atomic `INSERT ... ON CONFLICT DO UPDATE` (see
    `services/provider_keys.py`) means Postgres serializes the two
    statements: one fully wins. A read-then-write upsert could instead
    leave a row with e.g. the ciphertext from one call paired with the
    nonce/auth_tag from the other - `decrypt_secret` below would fail an
    AES-GCM authentication check on any such mismatch, since the nonce is
    fresh per call.
    """
    import asyncio

    async def _put(value: str) -> httpx.Response:
        return await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": value},
            headers=auth_headers,
        )

    responses = await asyncio.gather(_put("sk-race-value-a"), _put("sk-race-value-b"))
    assert all(response.status_code == 200 for response in responses)

    assert await _row_count(migrated_database_url, "openai") == 1

    row = await _fetch_row(migrated_database_url, "openai")
    assert row is not None
    # Decrypts cleanly (would raise DecryptionError on any mixed/corrupted
    # ciphertext+nonce+auth_tag combination) and matches exactly one of the
    # two concurrently-submitted values, not a garbled mix of both.
    decrypted = _decrypt(row, "openai", master_key_bytes)
    assert decrypted in ("sk-race-value-a", "sk-race-value-b")
