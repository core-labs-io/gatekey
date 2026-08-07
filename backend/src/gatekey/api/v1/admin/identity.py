"""Identity & Access admin endpoints (Phase 2, BD-19) - design doc section
5.9 / ADR-8: read-only this phase.

SSO config is env-derived (`GATEKEY_OIDC_*`), never DB-backed and never
writable here. The client secret is reported strictly as
`{configured: bool}` - the value itself never appears in any response, log,
or error message. `test-connection` performs a live discovery-document
fetch against the configured issuer and reports one of three structured
outcomes, mirroring the provider-key validation pattern
(`providers.base.ValidationResult`): `ok` / `unreachable` /
`invalid_response`.
"""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gatekey.api.deps import get_provider_http_client, get_settings_dep, require_role
from gatekey.config import Settings
from gatekey.errors import OidcUnavailableError
from gatekey.services.oidc import fetch_discovery_document
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/identity", tags=["admin", "identity"])


class ClientSecretStatus(BaseModel):
    configured: bool


class SsoConfigResponse(BaseModel):
    enabled: bool
    issuer_url: str | None
    client_id: str | None
    redirect_uri: str | None
    client_secret: ClientSecretStatus


class SsoTestConnectionResponse(BaseModel):
    """`detail` is availability info only - never echoes IdP response
    bodies (same hygiene as `services.oidc`)."""

    status: Literal["ok", "unreachable", "invalid_response", "not_configured"]
    detail: str


@router.get("/sso-config", response_model=SsoConfigResponse)
async def get_sso_config_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    settings: Settings = Depends(get_settings_dep),
) -> SsoConfigResponse:
    return SsoConfigResponse(
        enabled=settings.oidc_enabled(),
        issuer_url=settings.GATEKEY_OIDC_ISSUER_URL,
        client_id=settings.GATEKEY_OIDC_CLIENT_ID,
        redirect_uri=settings.GATEKEY_OIDC_REDIRECT_URI,
        client_secret=ClientSecretStatus(
            configured=settings.GATEKEY_OIDC_CLIENT_SECRET is not None
        ),
    )


@router.post("/sso-config/test-connection", response_model=SsoTestConnectionResponse)
async def test_sso_connection_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    settings: Settings = Depends(get_settings_dep),
    http_client: httpx.AsyncClient = Depends(get_provider_http_client),
) -> SsoTestConnectionResponse:
    """Live discovery fetch via `services.oidc.fetch_discovery_document`
    (the exact code path the real login flow uses, so a passing test means
    login's first hop works too)."""
    if not settings.oidc_enabled():
        return SsoTestConnectionResponse(
            status="not_configured",
            detail="SSO is not configured (GATEKEY_OIDC_* environment variables unset).",
        )
    assert settings.GATEKEY_OIDC_ISSUER_URL is not None
    try:
        document = await fetch_discovery_document(http_client, settings.GATEKEY_OIDC_ISSUER_URL)
    except OidcUnavailableError:
        return SsoTestConnectionResponse(
            status="unreachable",
            detail="The configured issuer's discovery document could not be fetched.",
        )
    required = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
    missing = [field for field in required if not document.get(field)]
    if missing:
        return SsoTestConnectionResponse(
            status="invalid_response",
            detail=(
                "The issuer responded, but its discovery document is missing "
                f"required fields: {', '.join(missing)}."
            ),
        )
    return SsoTestConnectionResponse(
        status="ok", detail="Discovery document fetched and validated."
    )
