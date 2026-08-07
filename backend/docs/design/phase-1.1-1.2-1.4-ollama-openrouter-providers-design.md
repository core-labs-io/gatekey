---
title: Phase 1.1/1.2/1.4 — Add Ollama & OpenRouter Providers — Architecture Design
status: accepted
author: architect
last_updated: 2026-07-28
---

> **Post-ship correction (2026-07-28, live verification):** this doc's
> `OpenRouterValidator` code sample below validates against
> `GET /api/v1/models`. Live testing against OpenRouter's real API (not
> mocks) found that endpoint is public/unauthenticated and returns `200`
> for any request, including no `Authorization` header at all - it never
> actually checks the submitted key. The shipped code now validates
> against `GET /api/v1/auth/key` instead, which correctly requires a valid
> key. See `providers/openrouter.py`'s module docstring for the full
> explanation. This design doc's code sample is left as-written below for
> historical accuracy (it reflects what was actually designed and built at
> the time) rather than edited to match after the fact - the shipped
> source file is the current source of truth, not this sample.

# Add Ollama & OpenRouter Providers — Design

Source of truth for scope/ACs: `docs/design/phase-1.1-1.2-1.4-ollama-openrouter-providers.md`
(product-owner spec, §1–§12). This document designs against that spec's acceptance
criteria and does not re-litigate them. Its §11 "Flagged" items are now **settled,
orchestrator-confirmed inputs** to this design, not open questions:

1. **Model key naming**: gateway-facing `MODEL_REGISTRY` keys are `{provider}/`-prefixed
   (`ollama/llama3.1`, `openrouter/openai/gpt-4o-mini`); `native_model_id` stays
   unprefixed (`llama3.1`, `openai/gpt-4o-mini`). See ADR-1 (§8) for the traceable record.
2. **OpenRouter entry count**: 1 entry (`openrouter/openai/gpt-4o-mini`) is an
   acceptable, correct deliverable — do not fabricate additional entries.
3. **Ollama placeholder bearer token**: the literal `"ollama"`, one named constant
   (`_OLLAMA_PLACEHOLDER_BEARER_TOKEN`, home: `providers/ollama.py` module level — see §3).
4. **`stream_options.include_usage` on Ollama**: implemented identically to
   `providers/openai.py` (always injected outbound); flagged in-code as unverified
   against a live Ollama instance, not a design blocker.
5. **First-run setup wizard**: stays scoped to the original 3 providers. Zero code
   change to `frontend/app/setup/page.tsx`.
6. **UI-requirements doc staleness**: out of this design's scope (docs-writer's job).

---

## 1. Migration plan — `0006_add_ollama_openrouter_providers.py`

### 1.1 Exact migration

```python
"""add ollama and openrouter to the provider_name enum

Phase 1.1/1.2/1.4 addition (Ollama & OpenRouter providers). See
`gatekey.db.models.provider_key.ProviderName` for the ORM side and
`docs/design/phase-1.1-1.2-1.4-ollama-openrouter-providers-design.md`
section 1 for the full rationale (transactional-DDL safety, downgrade
limitation).

No table DDL, no data backfill - no existing row references either new
value. `ALTER TYPE ... ADD VALUE IF NOT EXISTS` is used (not a plain
ADD VALUE) so this migration is safe to re-run against a database where it
was already partially/fully applied out-of-band.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROVIDER_ENUM_NAME = "provider_name"
NEW_VALUES = ("ollama", "openrouter")


def upgrade() -> None:
    for value in NEW_VALUES:
        # Postgres 12+ permits ALTER TYPE ... ADD VALUE inside a transaction
        # block, as long as the new value is not *used* (compared, cast, or
        # inserted) within that same transaction - this migration only adds
        # the values, it never uses them, so it is safe under Alembic's
        # default transactional-DDL wrapping. See design doc section 1.2 for
        # the concrete verification database-admin must run before this is
        # considered done, not merely assumed safe.
        op.execute(f"ALTER TYPE {PROVIDER_ENUM_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no native "DROP VALUE FROM enum" primitive - removing an
    # enum value requires rebuilding the type (CREATE new type, ALTER every
    # column using it, DROP old type), which is destructive if any row still
    # references the value and out of proportion for this addition. This is
    # an honest, documented hard limitation, not a silent no-op: downgrading
    # past this revision is NOT SUPPORTED. If this must ever be reversed,
    # do it as a hand-written, reviewed one-off migration at that time, not
    # by trusting this function.
    raise NotImplementedError(
        "0006 cannot be downgraded: Postgres has no DROP VALUE for enum "
        "types. See this migration's module docstring / design doc section "
        "1.1 - a real reversal requires a hand-written type-rebuild "
        "migration, not implemented here."
    )
```

**Why `raise NotImplementedError` and not a no-op `pass`**: mirrors AC-C1-4's explicit
instruction ("where a true reversal is genuinely not possible, the migration's docstring
says so explicitly rather than pretending"). A silent `pass` would let `alembic downgrade`
report success while leaving the enum values in place — actively misleading. Raising makes
the limitation impossible to miss at the moment someone actually tries it.

### 1.2 Verification plan (AC-C1-3 / US-G9) — concrete steps for database-admin

This is "verify, don't assume," executed as follows, against a real `postgres:16-alpine`
container (not sqlite, not mocked):

1. Start a fresh `postgres:16-alpine` container (or reuse whatever the existing
   `0001`–`0005` migration-test fixture already uses — check `tests/` for the existing
   Postgres test-container precedent before standing up a new one).
2. Run `alembic upgrade head` from a clean/empty database (applies `0001`–`0006` in one
   invocation). Confirm it exits 0 with no error.
3. In a **new** connection/transaction (i.e., not the one Alembic's `context.begin_
   transaction()` used and already committed by the time step 2 finished — see note
   below), run:
   ```sql
   INSERT INTO orgs (id, name) VALUES ('00000000-0000-0000-0000-000000000099', 'test')
   ON CONFLICT DO NOTHING;
   INSERT INTO provider_keys (id, org_id, provider, ciphertext, nonce, auth_tag)
   VALUES (gen_random_uuid(), '00000000-0000-0000-0000-000000000001', 'ollama',
           '\x00'::bytea, '\x00'::bytea, '\x00'::bytea);
   ```
   (or the SQLAlchemy/ORM equivalent — the point is a real `INSERT ... provider = 'ollama'`
   against the just-migrated schema). Confirm it succeeds, then roll back or delete the
   test row.
4. Also run `alembic upgrade head` a second time starting from a DB already at `0005`
   (i.e., `0006` applied alone, in its own single-migration transaction) to cover the
   incremental-deploy case (existing production DB getting this one new migration), not
   just the fresh-clone-run-everything-at-once case. Same INSERT check as step 3.
5. Document the result (pass/fail, Postgres version used) as a comment in the migration
   test itself (US-G9) — this is the permanent record that "should be safe" became
   "verified," per AC-C1-3.

**Why a new connection/transaction in step 3 matters**: if the INSERT were attempted
inside the *same* still-open transaction that ran the `ALTER TYPE ADD VALUE`, Postgres
raises `unsafe use of new value of enum type "provider_name"`. Alembic's `env.py` (this
repo's `alembic/env.py`, confirmed) wraps the whole `run_migrations()` call in one
`context.begin_transaction()` block that commits when `alembic upgrade` exits
successfully — so a fresh connection opened *after* the `alembic upgrade head` process
has fully exited is guaranteed to see the new enum values as already committed. Step 3
must be a genuinely separate process/connection, not a follow-on statement appended
inside the same Alembic run.

### 1.3 `ProviderName` enum (database-admin, `db/models/provider_key.py`)

```python
class ProviderName(str, enum.Enum):
    """Matches `providers.registry.SUPPORTED_PROVIDERS` exactly."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    VERTEX_AI = "vertex_ai"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
```

No other change to this file — `provider_name_enum` (the `PGEnum(..., create_type=False)`
wrapper) automatically reflects the new members; DDL ownership stays exclusively with the
Alembic migration per this file's existing documented convention.

---

## 2. Credential plumbing — `services/proxy_keys.py`, `services/provider_keys.py`

### 2.1 `OllamaCredential` (new, `services/proxy_keys.py`)

```python
_API_KEY_PROVIDERS = ("openai", "anthropic", "openrouter")     # AC-B1-1: openrouter added
_SERVICE_ACCOUNT_PROVIDERS = ("vertex_ai",)
_BASE_URL_BEARER_PROVIDERS = ("ollama",)                        # NEW, AC-B2-2


@dataclass(frozen=True, repr=False)
class OllamaCredential(ProviderCredential):
    """Decrypted base-url-plus-optional-bearer credential (ollama only).

    `bearer_token` is never `None` - an empty string means "not configured"
    (AC-B2-1/AC-B2-3), matching the one-representation discipline
    `OllamaKeyRequest` already enforces at the API layer (schemas/
    provider_key.py). `base_url` is not itself secret (sourced from the
    non-secret `key_metadata` column, same pattern as
    `ServiceAccountCredential.project_id`/`.location`), but the whole
    object still gets the inherited redacted `__repr__`/`__str__` for
    consistency - no per-field secrecy special-casing.
    """

    provider: str
    base_url: str
    bearer_token: str
```

`get_decrypted_provider_credential()` gains a third dispatch branch, inserted after the
existing `_SERVICE_ACCOUNT_PROVIDERS` branch and before the trailing
`UnsupportedProviderCredentialError`:

```python
        if provider in _BASE_URL_BEARER_PROVIDERS:
            bearer_token = json.loads(plaintext)
            if not isinstance(bearer_token, str):
                raise TypeError("decoded ollama bearer_token credential was not a string")
            metadata = row.key_metadata
            return OllamaCredential(
                provider=provider,
                base_url=metadata["base_url"],
                bearer_token=bearer_token,
            )
```

This sits inside the existing `try/except (json.JSONDecodeError, TypeError, KeyError):
raise CredentialDecodeError() from None` block unchanged — a missing `base_url` key in
`key_metadata` (should be unreachable given `_build_key_metadata`'s ollama branch below
always writes it) degrades to the same safe, non-leaking `CredentialDecodeError` as every
other malformed-credential case in this function.

### 2.2 `services/provider_keys.py` — `_serialize_secret_payload` / `_build_key_metadata`

```python
def _serialize_secret_payload(provider: str, secret_payload: dict[str, Any]) -> bytes:
    if provider in ("openai", "anthropic", "openrouter"):     # AC-B1-2
        return json.dumps(secret_payload["api_key"]).encode("utf-8")
    if provider == "vertex_ai":
        return json.dumps(secret_payload["service_account_json"]).encode("utf-8")
    if provider == "ollama":                                    # AC-B2-3
        # Always produces a serializable value, never skips the encrypt
        # step - satisfies ciphertext/nonce/auth_tag's NOT NULL constraint
        # even when the admin left bearer_token blank (schemas/
        # provider_key.py's OllamaKeyRequest already normalizes "" -> None
        # before this is called, so `.get("bearer_token") or ""` covers
        # both "field omitted" and "field explicitly None").
        return json.dumps(secret_payload.get("bearer_token") or "").encode("utf-8")
    raise ValueError(f"Unknown provider: {provider!r}")


def _build_key_metadata(provider: str, secret_payload: dict[str, Any]) -> dict[str, Any]:
    if provider == "vertex_ai":
        return {
            "project_id": secret_payload["project_id"],
            "location": secret_payload["location"],
        }
    if provider == "ollama":                                    # AC-B2-4
        return {"base_url": secret_payload["base_url"]}
    return {}
```

No change to `add_or_replace_key()` itself — it already calls both functions generically
by `provider` string, so the new branches are picked up with zero call-site changes.

---

## 3. `providers/ollama.py` (new)

```python
"""Ollama provider key validation and inference calls - self-hosted,
admin-configured base_url (Phase 1 addition, US-A1/US-A2).

`secret_payload` shape: `{"base_url": str, "bearer_token": str | None}`.

Structural deviation from every other provider module (openai/anthropic/
vertex_ai): there is deliberately no module-level `*_CHAT_COMPLETIONS_URL`
constant (AC-A1-2). The URL is built at call time from
`credential.base_url`, since Ollama's endpoint is admin-configured and
varies per deployment, unlike every other provider's fixed endpoint. Do
not "fix" this back to a module constant during review.

Chat only - no `create_completion()`/`create_embeddings()` this pass
(AC-A1-4), matching MODEL_REGISTRY having zero non-chat Ollama entries.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from gatekey.providers.base import (
    ProviderValidator,
    ValidationResult,
    ValidationStatus,
    map_http_status,
    map_httpx_exception,
    provider_call_error_from_exception,
    provider_call_error_from_response,
)
from gatekey.schemas.chat import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse

if TYPE_CHECKING:
    from gatekey.services.proxy_keys import OllamaCredential

_PROVIDER_NAME = "Ollama"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0

# Fixed placeholder Bearer token sent when the admin has configured no
# bearer_token (AC-A1-3/AC-A2-1 - orchestrator-confirmed literal, settled
# item #3). Ollama's OpenAI-compat layer requires *a* Authorization header
# be present but never validates its value; "ollama" matches Ollama's own
# community convention (commonly documented as OPENAI_API_KEY=ollama in
# their own examples). Single named constant - referenced by both
# OllamaValidator and the inference functions below, never re-typed.
_OLLAMA_PLACEHOLDER_BEARER_TOKEN = "ollama"


def _resolve_bearer_token(bearer_token: str) -> str:
    return bearer_token or _OLLAMA_PLACEHOLDER_BEARER_TOKEN


class OllamaValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        base_url = secret_payload.get("base_url")
        if not base_url or not isinstance(base_url, str):
            return ValidationResult(              # AC-A2-3
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed secret payload: expected non-empty 'base_url' string.",
            )
        bearer_token = secret_payload.get("bearer_token") or ""
        headers = {"Authorization": f"Bearer {_resolve_bearer_token(bearer_token)}"}
        url = f"{base_url.rstrip('/')}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)   # AC-A2-2: unreachable -> PROVIDER_UNREACHABLE
        return map_http_status(response, _PROVIDER_NAME)


def _auth_headers(credential: "OllamaCredential") -> dict[str, str]:
    return {"Authorization": f"Bearer {_resolve_bearer_token(credential.bearer_token)}"}


def _chat_completions_url(credential: "OllamaCredential") -> str:
    # AC-A1-2 - built at call time, never a module constant.
    return f"{credential.base_url.rstrip('/')}/v1/chat/completions"


def _chat_request_body(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    # Copied verbatim from providers.openai._chat_request_body (not
    # imported) - keeps this module independently readable/editable, same
    # convention as anthropic.py/vertex_ai.py not sharing this helper either.
    body: dict[str, Any] = {
        "model": native_model_id,
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.stop is not None:
        body["stop"] = request.stop
    if request.n is not None:
        body["n"] = request.n
    return body


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: "OllamaCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming passthrough against `{base_url}/v1/chat/completions`.

    Mirrors `providers.openai.create_chat_completion` exactly except URL
    construction (AC-A1-2) and credential type. Raises
    `providers.base.ProviderCallError` on network failure or non-2xx
    (AC-A1-5).
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            _chat_completions_url(credential),
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return ChatCompletionResponse.model_validate(response.json())


async def stream_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: "OllamaCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncIterator[ChatCompletionChunk]:
    """Streaming counterpart - mirrors `providers.openai.stream_chat_completion`'s
    SSE `data: {...}` parsing loop exactly (AC-A1-6).

    FLAG (orchestrator item #4, settled as "flag, don't block"): whether
    Ollama's OpenAI-compat streaming layer actually honors
    `stream_options.include_usage=true` the way real OpenAI does (i.e.
    emits a terminal usage-bearing chunk) is UNVERIFIED against a live
    Ollama instance as of this writing. If it does not, streaming Ollama
    requests silently lose token-count usage logging (cost accounting is
    unaffected either way - Ollama pricing is $0, see providers/pricing.py).
    backend-developer: confirm against a real local Ollama instance before
    treating this as done; do not remove this comment without that
    confirmation.
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    try:
        async with client.stream(
            "POST",
            _chat_completions_url(credential),
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise provider_call_error_from_response(response, _PROVIDER_NAME)
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                yield ChatCompletionChunk.model_validate(json.loads(payload))
    except httpx.HTTPError as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
```

`create_completion()` / `create_embeddings()` are **not defined** in this module (AC-A1-4)
— not stubs, not placeholders, simply absent.

**AC-A1-7 regression test** (US-G1): two `OllamaCredential`s differing only in `base_url`
must produce two different outbound `_chat_completions_url(...)` values for the same
`create_chat_completion` call — this is the one test shape no existing provider module
needs, since none of the other three vary base URL by credential.

---

## 4. `providers/openrouter.py` (new)

```python
"""OpenRouter provider key validation and inference calls - direct
OpenAI-shape passthrough against a fixed hosted endpoint (Phase 1
addition, US-A3).

`secret_payload` shape: `{"api_key": str}` - identical to openai.py.

Mirrors `providers/openai.py` exactly except: fixed OpenRouter URLs
(base URL is fixed, unlike Ollama's - AC-A3-1), and no
`create_completion()`/`create_embeddings()` this pass (AC-A3-3), matching
MODEL_REGISTRY having only ModelCapability.CHAT OpenRouter entries.

Not implemented this pass (AC-A3-2): optional attribution headers
(`HTTP-Referer`, `X-OpenRouter-Title`) - deferred, not a correctness gap.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from gatekey.providers.base import (
    ProviderValidator,
    ValidationResult,
    ValidationStatus,
    map_http_status,
    map_httpx_exception,
    provider_call_error_from_exception,
    provider_call_error_from_response,
)
from gatekey.schemas.chat import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse

if TYPE_CHECKING:
    from gatekey.services.proxy_keys import ApiKeyCredential

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_PROVIDER_NAME = "OpenRouter"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0


class OpenRouterValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        api_key = secret_payload.get("api_key")
        if not api_key or not isinstance(api_key, str):
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed secret payload: expected non-empty 'api_key' string.",
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(OPENROUTER_MODELS_URL, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)
        return map_http_status(response, _PROVIDER_NAME)


def _auth_headers(credential: "ApiKeyCredential") -> dict[str, str]:
    return {"Authorization": f"Bearer {credential.api_key}"}


def _chat_request_body(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    # Copied verbatim from providers.openai._chat_request_body.
    body: dict[str, Any] = {
        "model": native_model_id,
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.stop is not None:
        body["stop"] = request.stop
    if request.n is not None:
        body["n"] = request.n
    return body


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: "ApiKeyCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming passthrough - identical structure to
    `providers.openai.create_chat_completion`, `OPENROUTER_CHAT_COMPLETIONS_URL`
    substituted for OpenAI's fixed URL."""
    body = _chat_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            OPENROUTER_CHAT_COMPLETIONS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return ChatCompletionResponse.model_validate(response.json())


async def stream_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: "ApiKeyCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncIterator[ChatCompletionChunk]:
    """Streaming passthrough - identical structure to
    `providers.openai.stream_chat_completion`, including the unconditional
    outbound `stream_options: {"include_usage": true}` for Budget's (1.4)
    accounting. Not flagged as unverified (unlike Ollama, item #4) -
    OpenRouter is a direct OpenAI-shape proxy over a hosted, stable
    endpoint, the same class of target `providers/openai.py`'s already-
    proven implementation targets."""
    body = _chat_request_body(native_model_id, request)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    try:
        async with client.stream(
            "POST",
            OPENROUTER_CHAT_COMPLETIONS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise provider_call_error_from_response(response, _PROVIDER_NAME)
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                yield ChatCompletionChunk.model_validate(json.loads(payload))
    except httpx.HTTPError as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
```

Credential type: reuses the existing `ApiKeyCredential` unchanged (US-B1) — no new
dataclass needed for OpenRouter, unlike Ollama.

---

## 5. Registry wiring — `providers/registry.py`

```python
SUPPORTED_PROVIDERS = ("openai", "anthropic", "vertex_ai", "ollama", "openrouter")


def build_validator_registry(timeout_seconds: float = 8.0) -> dict[str, ProviderValidator]:
    return {
        "openai": OpenAIValidator(timeout_seconds=timeout_seconds),
        "anthropic": AnthropicValidator(timeout_seconds=timeout_seconds),
        "vertex_ai": VertexAIValidator(timeout_seconds=timeout_seconds),
        "ollama": OllamaValidator(timeout_seconds=timeout_seconds),
        "openrouter": OpenRouterValidator(timeout_seconds=timeout_seconds),
    }
```

Plus the corresponding `from gatekey.providers.ollama import OllamaValidator` /
`from gatekey.providers.openrouter import OpenRouterValidator` imports and the module
docstring's provider-identifier list update.

**AC-A4-3 resolved, confirmed by direct code read**: `api/deps.py`'s
`get_validator_registry()` calls `build_validator_registry(...)` directly with no
separate hardcoded provider list — `registry.py` is confirmed the only place
`SUPPORTED_PROVIDERS`-equivalent enumeration lives on the backend. The only other two
enumerations in the entire codebase are both frontend, both accounted for in §7 below
(`frontend/app/providers/page.tsx`'s local `PROVIDERS` const, which **does** need updating,
and `frontend/app/setup/page.tsx`'s local `PROVIDERS` const, which deliberately does
**not**, per settled item #5).

---

## 6. Schemas / Admin API

### 6.1 `schemas/provider_key.py`

```python
_MAX_BASE_URL_LENGTH = 2048  # generous bound for a scheme+host[:port][/path] string


class OpenRouterKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/openrouter/key`. Identical
    shape to `OpenAIKeyRequest` (AC-D1-1)."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=_MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_LENGTH)

    @field_validator("api_key")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank.")
        return value


class OllamaKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/ollama/key`."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=_MAX_BASE_URL_LENGTH)
    bearer_token: str | None = Field(default=None, max_length=_MAX_API_KEY_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("base_url must not be blank.")
        # AC-D2-2: minimal scheme sanity check only, generic to any
        # admin-configured endpoint URL - not Ollama-specific format
        # validation. Consistent with this module's stated "minimal sanity
        # bounds, not provider-specific format validation" philosophy.
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://.")
        return value

    @field_validator("bearer_token")
    @classmethod
    def _normalize_blank_to_none(cls, value: str | None) -> str | None:
        # AC-D2-1: an empty/whitespace-only bearer_token normalizes to
        # None - exactly one representation of "not configured", never a
        # distinct "empty but present" state.
        if value is not None and not value.strip():
            return None
        return value
```

### 6.2 `api/v1/admin/providers.py`

```python
from gatekey.schemas.provider_key import (
    AnthropicKeyRequest,
    OllamaKeyRequest,          # NEW
    OpenAIKeyRequest,
    OpenRouterKeyRequest,      # NEW
    ProviderKeyResponse,
    VertexAIKeyRequest,
)

_REQUEST_SCHEMAS: dict[ProviderName, type[BaseModel]] = {
    ProviderName.OPENAI: OpenAIKeyRequest,
    ProviderName.ANTHROPIC: AnthropicKeyRequest,
    ProviderName.VERTEX_AI: VertexAIKeyRequest,
    ProviderName.OLLAMA: OllamaKeyRequest,          # NEW
    ProviderName.OPENROUTER: OpenRouterKeyRequest,  # NEW
}
```

Zero other route-code changes (AC-D3-1) — `PUT/GET/DELETE /v1/admin/providers/{provider}`
and `GET /v1/admin/providers` already dispatch generically on `provider: ProviderName`
(the path param's type annotation), and the existing `InvalidProviderKeyError → 422` /
`ProviderUnreachableError → 502` / `ProviderValidationUnknownError → 500` mapping applies
unchanged (AC-D3-2). `ProviderKeyResponse.metadata` for a saved Ollama key returns exactly
`{"base_url": "..."}` — `bearer_token` is never in `key_metadata`, so it can never leak
through this response model (AC-D3-3), by the same "no field exists that could hold
secret material" structural guarantee this schema module's docstring already documents.

**Real, code-level dependency** (important for sequencing, §9): this file (`api/v1/admin/
providers.py`) is the **one and only** backend file that imports `ProviderName` directly
(`from gatekey.db.models.provider_key import ProviderName`) and uses it both as a
dict key and as a FastAPI path-param type (which drives the enum-based 404/422 on an
unrecognized `{provider}` path segment). This two-line `_REQUEST_SCHEMAS` addition
**cannot be imported, let alone tested**, until `ProviderName.OLLAMA`/`.OPENROUTER` exist
— i.e., until §1.3's database-admin task lands. Every other backend module touched by
this addition (`providers/ollama.py`, `providers/openrouter.py`, `services/proxy_keys.py`,
`services/provider_keys.py`, `providers/registry.py`, `providers/model_registry.py`,
`providers/pricing.py`, and `schemas/provider_key.py`'s two new request classes
themselves) operate on plain `provider: str` values or standalone Pydantic classes and
have **no Python-level import dependency** on the enum — they can be written and unit-
tested fully in parallel with §1's database-admin work. Only this one file's wiring, and
any integration test that exercises `PUT /v1/admin/providers/ollama/key` end-to-end
against a real router, has the hard dependency. See §9 for the full sequencing plan.

---

## 7. Model registry & pricing

### 7.1 `providers/model_registry.py` additions

```python
    # --- Ollama - chat only (self-hosted; Ollama's OpenAI-compat layer has
    # no embeddings endpoint). Example tags only, functional only if the
    # admin's Ollama instance has actually pulled that exact model tag - an
    # unpulled model fails at Ollama, surfaced as ProviderCallError (a real,
    # expected failure mode, not a Gatekey bug). Gateway-facing keys are
    # `ollama/`-prefixed per ADR-1 (section 8) - native_model_id stays the
    # bare tag actually sent to Ollama.
    # NOT built this pass: dynamic per-org model discovery
    # (GET {base_url}/api/tags) - deliberate, explicit follow-up (see
    # section 10, forward-looking flags).
    "ollama/llama3.1": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="llama3.1"
    ),
    "ollama/mistral": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="mistral"
    ),
    "ollama/qwen2.5": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="qwen2.5"
    ),
    # --- OpenRouter - chat only, small curated allowlist (not a mirror of
    # OpenRouter's full multi-hundred-model catalog, matching this
    # registry's existing stated philosophy). native_model_id uses
    # OpenRouter's own `vendor/model` slug convention verbatim; only the
    # gateway-facing key gets the `openrouter/` prefix (ADR-1).
    "openrouter/openai/gpt-4o-mini": ModelRoute(
        provider="openrouter", capability=ModelCapability.CHAT, native_model_id="openai/gpt-4o-mini"
    ),
```

Zero `ModelCapability.EMBEDDINGS` entries for either provider (AC-E1-4, AC-A3-3's
symmetry).

### 7.2 `providers/pricing.py` additions

```python
    # --- Ollama - chat, self-hosted: $0.00 is not a real cost basis. See
    # module docstring addendum below and phase-5-differentiators.md
    # section 5.5 ("Unified Governance for BYOK + Self-Hosted OSS Models")
    # for the eventual real cost-basis model - this table is NOT a preview
    # of that design.
    "ollama/llama3.1": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    "ollama/mistral": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    "ollama/qwen2.5": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    # --- OpenRouter - chat, no-markup pass-through of the underlying
    # model's own per-token price. A separate ~5.5% fee applies only to
    # OpenRouter credit purchases at the account level and is out of scope
    # for per-request cost accounting (Gatekey has no visibility into an
    # org's credit-purchase transactions) - do not add a markup here.
    "openrouter/openai/gpt-4o-mini": PricingEntry(
        input_price_per_million_usd=Decimal("0.15"),
        output_price_per_million_usd=Decimal("0.60"),
        as_of="<backend-developer: fill with live-verified date>",
        source="<backend-developer: fill with live-verified OpenRouter pricing-page URL>",
    ),
```

**The architect has not fabricated the OpenRouter citation URL/date above** — mirroring
`phase-1.4-budget-basic-design.md` §12's own precedent, the `$0.15`/`$0.60` *figures*
come from the already-confirmed product spec (AC-E4-1: "matches direct OpenAI pricing,
corroborating the no-markup claim"), but `as_of`/`source` must be filled by
backend-developer from a live check of OpenRouter's own model page for
`openai/gpt-4o-mini` before this entry ships — do not invent a URL to unblock the
completeness test; a placeholder value here should fail
`tests/unit/test_pricing.py`'s completeness assertion by design if this is skipped
(extend that test, if needed, to assert `source` is non-placeholder-shaped for every
entry, mirroring how it already distinguishes Ollama's explanatory-string `source` from
every other provider's URL-shaped one, per AC-E3-2/US-G7).

Both module-level sourcing-comment blocks required by AC-E3-3 (Ollama: (a) $0 ≠ real
infra cost, (b) Phase 5 §5.5 is the eventual real answer, (c) this is not a preview of
that design) and AC-E4-2 (OpenRouter: no markup on tokens, confirmed; the ~5.5%
credit-purchase fee is a separate, out-of-scope concern) are backend-developer's to write
verbatim against those ACs — not reproduced a second time here to avoid drift between two
copies of the same required text; the spec (§5, US-E3/US-E4) is the single source of
truth for the exact required content.

---

## 8. ADR-1 — gateway-facing model key naming convention

- **Decision**: prefix both new providers' gateway-facing `MODEL_REGISTRY` keys with
  `{provider}/` (`ollama/llama3.1`, `ollama/mistral`, `ollama/qwen2.5`,
  `openrouter/openai/gpt-4o-mini`). `native_model_id` — the string actually sent to the
  provider — stays unprefixed (`llama3.1`, `openai/gpt-4o-mini`).
- **Alternatives considered**:
  1. *Bare tags/slugs as gateway-facing keys* (`llama3.1`, `openai/gpt-4o-mini`). No
     literal string collision exists today against the current 10-entry registry, so this
     would technically work. Rejected: OpenRouter's own native slug format
     (`vendor/model`) would place `openai/gpt-4o-mini` (routes to OpenRouter) directly
     next to the pre-existing bare `gpt-4o-mini` key (routes straight to OpenAI) in the
     same flat dict — same vendor name visible in both, different route, different price,
     different failure mode. An admin or API caller skimming model names has no structural
     signal these are different routes; only careful reading of the full string prevents a
     costly mistake (e.g. assuming a straight-to-OpenAI SLA/rate-limit when the request
     actually goes through OpenRouter). No collision today does not mean no confusion.
  2. *Prefix only when a real collision exists, leave everything else bare.* Rejected as
     an inconsistent convention — a future third OpenRouter model with a slug that *does*
     collide would then need a different naming rule than the ones added today, which is
     worse for long-term registry readability than one uniform rule applied now, while the
     registry is still small enough that the cost of establishing the convention is low.
- **Why the chosen option**: establishes a forward-compatible, uniform rule (every
  non-"direct" provider's gateway-facing key is `{provider}/`-prefixed) before the
  registry grows past 10 entries, at the cost of a more verbose string
  (`openrouter/openai/gpt-4o-mini`) for the one OpenRouter entry that ships this pass.
  This is a genuine, acknowledged public-API-surface change (the `model` field value
  callers must pass) for *newly added* models only — no existing model key changes, so it
  does not violate the "OpenAI-compatible API surface maintained across phases" hold-the-
  line item (no existing integration's `model` value stops working).
- **Traceability**: this was flagged back to the orchestrator by product-owner (spec §11
  item #1) as a genuine judgment call rather than decided silently; the orchestrator
  confirmed this exact convention before this design was written (see this document's
  header). Recorded here so the reasoning survives past the conversation that produced it.

---

## 9. Non-functional requirements — explicit accounting

- **p99 gateway overhead < 150ms (Phase 1 NFR)**: unaffected. This addition adds dict
  entries to `MODEL_REGISTRY`/`PRICING_TABLE` and two new provider modules; it introduces
  zero new hot-path DB calls and reuses the exact same `resolve_route → check_model_policy
  → check_budget_available → fetch_credential → provider call → record_usage_charge`
  chain Phase 1.2/1.3/1.4 already built, load-tested, and did not change here. The one
  variable this design cannot control is a self-hosted Ollama target's own responsiveness
  (outside Gatekey's process) — not a Gatekey-side latency regression.
- **OpenAI-compatible API surface maintained across phases**: strictly additive. No
  existing model key, request-schema field, or response shape changes for the pre-existing
  3 providers; the existing bare `gpt-4o-mini` key still routes to OpenAI unchanged — see
  ADR-1 for why the new `openrouter/openai/gpt-4o-mini` key cannot be mistaken for it.
- **No plaintext provider keys at rest or in logs**: `OllamaCredential.bearer_token`
  undergoes the identical envelope-encryption-at-rest and redacted-`__repr__`/`__str__`
  treatment every other credential type gets (§2.1); `base_url` is non-secret and stored
  in the existing `key_metadata` JSONB column, the same precedent Vertex AI's
  `project_id`/`location` already established — not a new exception to the hold-the-line
  item.
- **Self-hosted first, no mandatory phone-home**: unaffected — Ollama is itself a
  self-hosted target; this addition introduces no new outbound telemetry.
- **Docs sufficient for self-deploy**: Component H (US-H1/H2) covers this; out of this
  design document's scope (docs-writer executes it) but flagged here as a real ship
  blocker per the non-negotiables, not optional polish.
- **Under-60-minutes setup**: preserved explicitly by keeping the first-run wizard scoped
  to the original 3 providers (settled item #5) — `frontend/app/setup/page.tsx` requires
  zero code change.

---

## 10. Forward-looking rework flags

- **Phase 5 §5.5 (self-hosted cost-basis / BYOK + OSS governance)**: Ollama's `$0.00`
  pricing here is explicitly *not* a preview of that eventual design (AC-E3-3). When that
  phase lands, `PricingEntry`'s record shape (not a bare tuple — already established in
  the 1.4 budget design for exactly this reason) already accommodates a future
  GPU-hour-rate or similar field as an addition, not a schema rewrite.
- **Dynamic Ollama model discovery** (`GET {base_url}/api/tags`): deliberately not built,
  comment-only follow-up (AC-E1-3). Flagging explicitly for whichever future phase picks
  this up: it is a materially different mechanism than today's static in-code
  `MODEL_REGISTRY` dict (per-org, live, cached discovery vs. a hand-curated allowlist
  shared across all orgs) — not a small extension of the current registry shape.
- **OpenRouter attribution headers** (`HTTP-Referer`, `X-OpenRouter-Title`): deferred
  (AC-A3-2). If added later, the shared `_chat_request_body`-style helper would need to
  become provider-parameterized for *headers* (currently only `_auth_headers` varies per
  provider) — a contained change, not a redesign, but not free either since no current
  provider module varies its non-auth headers.
- **Multi-key-per-provider (Phase 2+)**: unaffected by this addition — the same
  `UNIQUE(org_id, provider)` constraint applies identically to `ollama`/`openrouter` as to
  the original 3, so a future Phase 2 multi-key change touches these two providers exactly
  the same way it touches every other provider; nothing here special-cases them in a way
  that would need undoing.

---

## 11. Task breakdown

Legend: `[P]` = can run in parallel with sibling `[P]` tasks; `[D: X]` = hard dependency on
task X.

### database-admin

- **DB-1**: Write and apply Alembic migration `0006_add_ollama_openrouter_providers.py`
  per §1.1. `[P]` (no dependency on other in-flight work).
- **DB-2**: Verify per §1.2's concrete steps (fresh-DB run, incremental-from-`0005` run,
  post-commit INSERT using both new enum values in a new transaction; record pass/fail as
  a comment in the migration test, US-G9). `[D: DB-1]`.
- **DB-3**: Add `OLLAMA`/`OPENROUTER` to `db/models/provider_key.py`'s `ProviderName`
  enum per §1.3. Should land in the same PR/commit as DB-1 for review coherence (both
  define "the two new provider identifiers"), though it has no separate migration
  dependency of its own. `[P]` with DB-1, but treat as landing together.

### backend-developer

- **BD-1**: `providers/ollama.py` — full module per §3 (validator, `create_chat_
  completion`, `stream_chat_completion`, `_OLLAMA_PLACEHOLDER_BEARER_TOKEN`). `[P]` — no
  DB/enum dependency (operates on `provider: str` / a forward-referenced credential type).
- **BD-2**: `providers/openrouter.py` — full module per §4. `[P]`.
- **BD-3**: `services/proxy_keys.py` — `OllamaCredential` + `_BASE_URL_BEARER_PROVIDERS`
  dispatch branch + `"openrouter"` added to `_API_KEY_PROVIDERS` per §2.1. `[P]` — string-
  keyed dispatch, no enum import.
- **BD-4**: `services/provider_keys.py` — `_serialize_secret_payload`/`_build_key_
  metadata` new branches per §2.2. `[P]`.
- **BD-5**: `providers/registry.py` — `SUPPORTED_PROVIDERS` + `build_validator_registry`
  entries per §5. `[P]` at the code level (no import-time dependency on the enum), but
  should be authored against the exact same two identifiers DB-3 defines — coordinate,
  don't diverge.
- **BD-6**: `schemas/provider_key.py` — `OllamaKeyRequest`/`OpenRouterKeyRequest` per
  §6.1. `[P]` — standalone Pydantic classes, no `ProviderName` import.
- **BD-7**: `api/v1/admin/providers.py` — `_REQUEST_SCHEMAS` dict gains the two new
  entries per §6.2. `[D: DB-3, BD-6]` — **real, hard dependency**: this file imports
  `ProviderName` directly; it cannot import successfully until DB-3 lands (see §6.2's
  "real, code-level dependency" note).
- **BD-8**: `providers/model_registry.py` — 4 new `MODEL_REGISTRY` entries per §7.1.
  `[P]`.
- **BD-9**: `providers/pricing.py` — 4 new `PRICING_TABLE` entries per §7.2, including
  sourcing the real OpenRouter `as_of`/`source` citation live (do not fabricate).
  `[D: BD-8]` (the existing `_validate_completeness()` import-time check will fail the
  moment `MODEL_REGISTRY` gains entries with no matching pricing row — sequence pricing
  right after registry, same precedent as 1.4's `BD-3`).
- **BD-10**: Tests (US-G1–G8, excluding G9 which is DB-2's job) — unit tests for both new
  provider modules (including the AC-A1-7 base-URL-varies-by-credential regression test),
  `registry.py`'s `SUPPORTED_PROVIDERS` length assertion updated to 5, `schemas/
  provider_key.py` validation tests, `proxy_keys.py`/`provider_keys.py` new-branch tests
  (including the AC-B2-5 always-encrypted-even-when-blank test), pricing/registry
  completeness tests, and the two stubbed-server gateway integration tests (US-G8).
  `[D: BD-1, BD-2, BD-3, BD-4, BD-5, BD-6, BD-7, BD-8, BD-9]` — last, after every
  implementation task it exercises has landed.

### frontend-developer

- **FE-1**: `frontend/src/lib/api.ts` — `ProviderName` type extended to the 5-provider
  union, `PROVIDER_LABELS` gains `"Ollama"`/`"OpenRouter"`, `putProviderKey`'s body union
  type gains `{ base_url: string; bearer_token?: string | null }`, `MODELS_BY_PROVIDER`
  gains `ollama: ["ollama/llama3.1", "ollama/mistral", "ollama/qwen2.5"]` and
  `openrouter: ["openrouter/openai/gpt-4o-mini"]`. `[P]` — needs only the API contract
  (provider identifier strings, request field names, response shape) already fixed in
  §1.3/§6/§7.1 of this document; no live backend or DB required.
- **FE-2**: `frontend/src/components/ProviderKeyForm.tsx` — add an `provider === "ollama"`
  branch rendering `base_url` (required text input, placeholder `http://localhost:11434`)
  + `bearer_token` (optional, masked input, helper text "only needed if your Ollama
  instance sits behind an authenticating reverse proxy" per AC-F1-3); extract the
  API-key-field placeholder into a small `apiKeyPlaceholder(provider)` helper (`"sk-..."`
  for openai, `"sk-ant-..."` for anthropic, a generic placeholder for openrouter/any
  future bearer-key provider) so OpenRouter's key field — which falls into the existing
  generic `else` branch automatically, needing no new branch (AC-F1-4) — doesn't
  cosmetically show Anthropic's placeholder text. `handleSave()` gains an `else if
  (provider === "ollama")` branch calling `putProviderKey(provider, { base_url, bearer_
  token })` (backend already normalizes a blank `bearer_token` to `None`, per §6.1 — no
  frontend-side `|| null` special-casing required). `[D: FE-1]` (needs the extended type/
  labels).
- **FE-3**: `frontend/app/providers/page.tsx` — extend the local `PROVIDERS` const to
  `["openai", "anthropic", "vertex_ai", "ollama", "openrouter"]` (AC-F1-2). The rest of
  the page (card rendering, modal wiring, remove-confirmation) is fully generic over this
  array already — no other line changes needed. `[D: FE-1]`.
- **FE-4** (no-code confirmation task): `frontend/app/setup/page.tsx`'s own separate,
  locally-scoped `PROVIDERS` const stays exactly `["openai", "anthropic", "vertex_ai"]`
  (settled item #5 / AC-F1-5). Recorded as an explicit task so it is a deliberate
  "confirmed untouched," not an oversight discovered later.

### qa-engineer

- Own US-G1–G9 execution/CI-wiring once BD-10 and DB-2 land (unit + integration suite,
  migration verification harness). No new QA-owned design decisions beyond what §11 above
  already specifies — this row exists for completeness of the role list, not because new
  test-strategy design is needed beyond the product spec's Component G.

### Parallelization summary

`DB-1`/`DB-3` (migration + enum, same identifiers, land together) block only `BD-7` (the
one file that imports `ProviderName`) and any integration test exercising the real admin
router end-to-end. Every other backend task (`BD-1`–`BD-6`, `BD-8`) is genuinely `[P]`
from hour zero — none of them import `ProviderName` or touch the database. `BD-9` waits
only on `BD-8` (registry completeness). `BD-10` (tests) is last. Frontend's `FE-1`–`FE-4`
can **all start immediately and fully in parallel with database-admin's and backend-
developer's entire workstream** — they depend only on the already-fixed API contract in
this document (provider name strings, request field names `base_url`/`bearer_token`,
unchanged `ProviderKeyResponse` shape), never on live backend code or a live database.
The only two hard cross-role gates in the whole slice are: (1) `BD-7` needs `DB-3`
merged, and (2) `BD-10`/qa-engineer's integration tests need essentially everything else
merged first.
