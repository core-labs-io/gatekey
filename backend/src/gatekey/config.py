"""Application configuration.

Reads settings from environment variables (and an optional `.env` file for
local development). Fails fast at import/startup time if required secrets
are missing or malformed - in particular `GATEKEY_MASTER_KEY` must decode
to exactly 32 bytes since it's used as an AES-256 key (see
`services/encryption.py`).
"""

from __future__ import annotations

import base64
import binascii

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MASTER_KEY_BYTE_LENGTH = 32

_OIDC_FIELD_NAMES = (
    "GATEKEY_OIDC_ISSUER_URL",
    "GATEKEY_OIDC_CLIENT_ID",
    "GATEKEY_OIDC_CLIENT_SECRET",
    "GATEKEY_OIDC_REDIRECT_URI",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str

    # Phase 1.1: single shared bearer token. Phase 2 keeps it as the
    # break-glass admin credential (audit actor "system:admin_token", A4)
    # alongside org_admin SSO sessions - see `api.deps.require_admin`.
    GATEKEY_ADMIN_TOKEN: str

    # Base64-encoded 32-byte AES-256 key. Never logged, never exposed via
    # any API response.
    GATEKEY_MASTER_KEY: str

    # Single-attempt timeout (seconds) applied to the outbound validation
    # call made to a provider when a key is submitted for validation.
    GATEKEY_PROVIDER_VALIDATION_TIMEOUT_SECONDS: float = 8.0

    # Comma-separated list of ADDITIONAL origins allowed to call the API
    # from a browser (CORS), beyond GATEKEY_FRONTEND_ORIGIN below.
    #
    # Phase 2 change: the console now authenticates with a session COOKIE
    # (SSO), which requires `allow_credentials=True` - and per the Fetch
    # spec, credentialed CORS is incompatible with a wildcard origin. A "*"
    # entry is therefore IGNORED (see `cors_allowed_origins()`); only
    # explicit origins are honored. The old default behaved permissively
    # only because Phase 1 was bearer-token-only.
    GATEKEY_CORS_ALLOWED_ORIGINS: str = "*"

    # The browser frontend's origin - always included in the CORS allowlist
    # and the origin session cookies are exchanged with. Explicit origin
    # (never a wildcard) because `allow_credentials=True` - see `main.py`.
    GATEKEY_FRONTEND_ORIGIN: str = "http://localhost:3000"

    # --- Phase 2: OIDC/SSO (optional, all-or-none) ---
    # SSO stays fully optional: unset = SSO routes 404, break-glass admin
    # token remains the only auth path. If ANY of the four is set, all four
    # must be (fail-fast at startup, not a runtime 500 on first login) - see
    # `_validate_oidc_all_or_none` and design doc section 2.1.
    GATEKEY_OIDC_ISSUER_URL: str | None = None
    GATEKEY_OIDC_CLIENT_ID: str | None = None
    # Confidential-client secret - never logged, never exposed via any API
    # response (the identity read-endpoint reports only `{configured: bool}`).
    GATEKEY_OIDC_CLIENT_SECRET: str | None = None
    GATEKEY_OIDC_REDIRECT_URI: str | None = None

    # Session cookie `Secure` flag - settable false only for local http dev.
    GATEKEY_SESSION_COOKIE_SECURE: bool = True
    GATEKEY_SESSION_TTL_HOURS: int = 12

    # --- Phase 2: SMTP threshold-alert email (optional) ---
    # Unset entirely = the email notifier is a no-op (informational log at
    # startup, never a hard failure). If any SMTP value is set, HOST and
    # FROM_ADDRESS are required - see `_validate_smtp`. The notifier itself
    # is built by a later task (design doc section 6); config is owned here.
    GATEKEY_SMTP_HOST: str | None = None
    GATEKEY_SMTP_PORT: int = 587
    GATEKEY_SMTP_USERNAME: str | None = None
    GATEKEY_SMTP_PASSWORD: str | None = None
    GATEKEY_SMTP_FROM_ADDRESS: str | None = None
    GATEKEY_SMTP_USE_TLS: bool = True

    # --- Phase 3: audit source-IP capture (AC1.1/AC1.2) ---
    # Off by default: trusting a client-supplied X-Forwarded-For/X-Real-IP
    # header without a configured trusted-proxy boundary is itself a
    # spoofing risk (a caller could claim any IP it likes). Only enable this
    # behind a reverse proxy that overwrites/strips these headers on the way
    # in - see `api.deps.get_source_ip`.
    GATEKEY_TRUST_PROXY_HEADERS: bool = False

    # --- Phase 4: optional Redis-backed shared-state store (design doc
    # section 4.1/9.3) ---
    # Unset by default = `InProcessSharedStateStore` (accurate for this
    # project's actual shipped single-instance topology - see
    # `services/shared_state.py`). Same optional/pass-through posture as
    # GATEKEY_OIDC_*/GATEKEY_SMTP_* - never a hard requirement, never
    # started by plain `docker-compose up`. Set to e.g.
    # `redis://redis:6379/0` (behind `docker compose --profile cache up`,
    # devops-owned) to switch to `RedisSharedStateStore` for a genuinely
    # horizontally-scaled deployment.
    GATEKEY_REDIS_URL: str | None = None

    # --- Logging (Tier 4 ops polish) ---
    # "text" (default): human-readable lines with `extra={...}` fields
    # appended as key=value pairs. "json": one JSON object per line, for
    # log pipelines. Either way, the structured extra fields this codebase
    # attaches to log calls actually reach the output now - see
    # `observability.configure_logging`.
    GATEKEY_LOG_FORMAT: str = "text"
    GATEKEY_LOG_LEVEL: str = "INFO"

    def cors_allowed_origins(self) -> list[str]:
        """Explicit CORS origin allowlist: the frontend origin plus any
        extra configured origins. Wildcard entries are dropped - credentialed
        CORS (session cookies, Phase 2) forbids them."""
        origins = [self.GATEKEY_FRONTEND_ORIGIN.strip().rstrip("/")]
        for origin in self.GATEKEY_CORS_ALLOWED_ORIGINS.split(","):
            origin = origin.strip().rstrip("/")
            if origin and origin != "*" and origin not in origins:
                origins.append(origin)
        return origins

    def oidc_enabled(self) -> bool:
        """True iff SSO is configured (all four OIDC vars set - validated)."""
        return self.GATEKEY_OIDC_ISSUER_URL is not None

    def smtp_enabled(self) -> bool:
        return self.GATEKEY_SMTP_HOST is not None

    @model_validator(mode="after")
    def _validate_oidc_all_or_none(self) -> "Settings":
        values = {name: getattr(self, name) for name in _OIDC_FIELD_NAMES}
        set_names = [name for name, value in values.items() if value]
        if set_names and len(set_names) != len(_OIDC_FIELD_NAMES):
            missing = sorted(set(_OIDC_FIELD_NAMES) - set(set_names))
            raise ValueError(
                "OIDC/SSO configuration is all-or-none: "
                f"{', '.join(set_names)} set but {', '.join(missing)} missing. "
                "Set all four GATEKEY_OIDC_* variables or none."
            )
        return self

    @model_validator(mode="after")
    def _validate_smtp(self) -> "Settings":
        any_set = any(
            getattr(self, name)
            for name in (
                "GATEKEY_SMTP_HOST",
                "GATEKEY_SMTP_USERNAME",
                "GATEKEY_SMTP_PASSWORD",
                "GATEKEY_SMTP_FROM_ADDRESS",
            )
        )
        if any_set and not (self.GATEKEY_SMTP_HOST and self.GATEKEY_SMTP_FROM_ADDRESS):
            raise ValueError(
                "SMTP configuration requires at least GATEKEY_SMTP_HOST and "
                "GATEKEY_SMTP_FROM_ADDRESS when any GATEKEY_SMTP_* variable is set."
            )
        return self

    @field_validator("GATEKEY_MASTER_KEY")
    @classmethod
    def _validate_master_key(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "GATEKEY_MASTER_KEY is required and must be a base64-encoded "
                "32-byte value. See .env.example for how to generate one."
            )
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "GATEKEY_MASTER_KEY must be valid base64."
            ) from exc
        if len(decoded) != MASTER_KEY_BYTE_LENGTH:
            raise ValueError(
                "GATEKEY_MASTER_KEY must decode to exactly "
                f"{MASTER_KEY_BYTE_LENGTH} bytes (got {len(decoded)})."
            )
        return value

    @field_validator("GATEKEY_ADMIN_TOKEN")
    @classmethod
    def _validate_admin_token(cls, value: str) -> str:
        if not value or value.strip() == "":
            raise ValueError("GATEKEY_ADMIN_TOKEN is required.")
        return value

    def master_key_bytes(self) -> bytes:
        """Decode the configured master key to raw bytes.

        Callers must not log or otherwise persist the return value.
        """
        return base64.b64decode(self.GATEKEY_MASTER_KEY, validate=True)


def get_settings() -> Settings:
    """Construct Settings, raising immediately on misconfiguration.

    Not cached at module import time so tests can construct their own
    Settings instances with overridden environment variables.
    """
    return Settings()
