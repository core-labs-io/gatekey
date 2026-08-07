"""Admin endpoints for provider key management (Phase 1.1, section 1.6).

All four endpoints require `require_admin` (the Phase 1.1 single-shared-
admin-token stub - see `api/deps.py`). None of these endpoints accept an
`org_id` - see `constants.DEFAULT_ORG_ID` for why this slice only ever
operates against the single seeded default org.

Phase 4 (Reliability & Cost Efficiency, multi-key/failover) note
------------------------------------------------------------------
`PUT .../{provider}/key` now accepts an optional `label` (see
`schemas/provider_key.py`), so an org can add a genuine SECOND key for a
provider instead of always overwriting the single `"Default"`-labeled row
(AC4.1.1/AC4.1.2). `DELETE .../{provider}` deletes EVERY key for that
provider (every label) - unchanged, documented "remove this provider
entirely" behavior (see `services.provider_keys.delete_key`'s docstring).
`DELETE .../{provider}/keys/{key_id}` below is the new surgical,
one-key-at-a-time disambiguator a multi-key org needs instead (`services.
provider_keys.delete_key_by_id`, previously implemented but never wired to
a route).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_key_provider, get_validator_registry, require_admin
from gatekey.db.models.provider_key import ProviderName
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.providers.base import ProviderValidator
from gatekey.schemas.provider_key import (
    AnthropicKeyRequest,
    OllamaKeyRequest,
    OpenAIKeyRequest,
    OpenRouterKeyRequest,
    ProviderKeyResponse,
    VertexAIKeyRequest,
)
from gatekey.services.encryption import KeyProvider
from gatekey.services.provider_keys import (
    InvalidProviderKeyError,
    ProviderUnreachableError,
    ProviderValidationUnknownError,
    add_or_replace_key,
    delete_key,
    delete_key_by_id,
    get_key,
    list_keys,
)

router = APIRouter(
    prefix="/v1/admin/providers",
    tags=["admin", "providers"],
    dependencies=[Depends(require_admin)],
)

# Which request schema to validate the raw JSON body against, keyed by the
# provider path param. Request shape genuinely differs per provider (see
# `schemas/provider_key.py`), so the body can't be a single static Pydantic
# param on the route function the way FastAPI usually does it.
_REQUEST_SCHEMAS: dict[ProviderName, type[BaseModel]] = {
    ProviderName.OPENAI: OpenAIKeyRequest,
    ProviderName.ANTHROPIC: AnthropicKeyRequest,
    ProviderName.VERTEX_AI: VertexAIKeyRequest,
    ProviderName.OLLAMA: OllamaKeyRequest,
    ProviderName.OPENROUTER: OpenRouterKeyRequest,
}


@router.put("/{provider}/key", response_model=ProviderKeyResponse)
async def put_provider_key(
    provider: ProviderName,
    payload: dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_db_session),
    validator_registry: dict[str, ProviderValidator] = Depends(get_validator_registry),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> ProviderKeyResponse:
    """Validate then save (insert or replace) the key for `provider`.

    422 `invalid_key` if the provider rejects the credential, 502
    `provider_unreachable` if the provider couldn't be reached to validate,
    500 `unknown_error` for anything else that prevented validation from
    completing. No database write happens in any of those cases.
    """
    schema_cls = _REQUEST_SCHEMAS[provider]
    try:
        request_model = schema_cls.model_validate(payload)
    except PydanticValidationError as exc:
        # `include_input=False` is pydantic v2's supported way to suppress
        # the "input" key that `.errors()` otherwise attaches to every error
        # dict (keyed independently of the field name) - without this, a
        # too-long api_key or a service_account_json submitted as the wrong
        # type gets its raw secret value echoed straight back into the 422
        # response. See also the defense-in-depth `redact_json_safe()` pass
        # in `errors.py`'s `RequestValidationError` handler.
        raise RequestValidationError(exc.errors(include_input=False)) from None

    full_payload = request_model.model_dump()
    # `label` (Phase 4, AC4.1.1/AC4.1.2) is routing/CRUD metadata, not
    # secret material or provider-specific config - pull it out before
    # handing the rest to `_serialize_secret_payload`/`_build_key_metadata`
    # (via `add_or_replace_key`'s `secret_payload` param) and the live
    # provider `validate()` call, neither of which expects it as a key.
    label = full_payload.pop("label")
    secret_payload = full_payload

    try:
        provider_key = await add_or_replace_key(
            session,
            provider.value,
            secret_payload,
            label=label,
            validator_registry=validator_registry,
            key_provider=key_provider,
        )
    except InvalidProviderKeyError as exc:
        raise GatekeyError(exc.message, code="invalid_key", status_code=422) from None
    except ProviderUnreachableError as exc:
        raise GatekeyError(exc.message, code="provider_unreachable", status_code=502) from None
    except ProviderValidationUnknownError as exc:
        raise GatekeyError(exc.message, code="unknown_error", status_code=500) from None

    return ProviderKeyResponse.model_validate(provider_key)


@router.get("", response_model=list[ProviderKeyResponse])
async def list_provider_keys(
    session: AsyncSession = Depends(get_db_session),
) -> list[ProviderKeyResponse]:
    """List every configured provider key (safe fields only) for the default org."""
    rows = await list_keys(session)
    return [ProviderKeyResponse.model_validate(row) for row in rows]


@router.get("/{provider}", response_model=ProviderKeyResponse)
async def get_provider_key(
    provider: ProviderName,
    session: AsyncSession = Depends(get_db_session),
) -> ProviderKeyResponse:
    """Fetch the configured key (safe fields only) for `provider`. 404 if not configured."""
    row = await get_key(session, provider.value)
    if row is None:
        raise NotFoundError(f"No key configured for provider '{provider.value}'.")
    return ProviderKeyResponse.model_validate(row)


@router.delete("/{provider}", status_code=204)
async def delete_provider_key(
    provider: ProviderName,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete EVERY key configured for `provider` (every label). 404 if
    none configured. For a multi-key org, prefer `DELETE
    /v1/admin/providers/{provider}/keys/{key_id}` below to remove one
    specific key instead of the whole provider."""
    deleted = await delete_key(session, provider.value)
    if not deleted:
        raise NotFoundError(f"No key configured for provider '{provider.value}'.")
    return Response(status_code=204)


@router.delete("/{provider}/keys/{key_id}", status_code=204)
async def delete_provider_key_by_id(
    provider: ProviderName,
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete exactly one key (by id) for `provider`, leaving any other
    labeled key for that provider untouched - the multi-key-safe
    counterpart of `DELETE /v1/admin/providers/{provider}` above. 404 if no
    such key exists (either a bad id, or an id that belongs to a different
    provider)."""
    deleted = await delete_key_by_id(session, provider.value, key_id)
    if not deleted:
        raise NotFoundError(f"No key found with id '{key_id}' for provider '{provider.value}'.")
    return Response(status_code=204)
