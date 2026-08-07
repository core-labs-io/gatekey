"""Shared types for provider key validation.

Each concrete provider validator (`providers/openai.py`,
`providers/anthropic.py`, `providers/vertex_ai.py`) implements
`ProviderValidator.validate()` by making a single, cheap, side-effect-free
API call to confirm a submitted credential actually works, without ever
persisting or logging the raw secret.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


class ValidationStatus(str, Enum):
    VALID = "valid"
    INVALID_KEY = "invalid_key"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a provider credential.

    `detail` must be safe to log and safe to return to an API caller: it
    must never contain the raw key/token/service-account material. Prefer
    short, generic messages (e.g. "received HTTP 401 from provider") over
    echoing raw provider response bodies, which may themselves reflect
    request headers back.
    """

    status: ValidationStatus
    detail: str | None = None
    provider_metadata: dict[str, Any] | None = None

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.VALID


class ProviderValidator(ABC):
    """Validates a provider credential by making a live test call."""

    @abstractmethod
    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        """Validate `secret_payload` against the live provider API.

        `secret_payload` shape is provider-specific (see each concrete
        validator's module docstring). Implementations must apply a single
        bounded-timeout attempt with no retries (retries belong to a higher
        layer, if ever, since this is a synchronous "test this key now"
        call on the admin key-entry path).
        """
        raise NotImplementedError


def map_httpx_exception(exc: Exception, provider_name: str) -> ValidationResult:
    """Map an exception raised while calling a provider's HTTP API to a `ValidationResult`.

    Shared by every HTTP-based validator (openai, anthropic, vertex_ai) so
    the network-error-to-status mapping only lives in one place.
    `provider_name` is used only for the human-readable `detail` string
    (e.g. "OpenAI", "Anthropic", "Vertex AI") - it must never itself be or
    contain secret material, which is always true for a static provider
    display name.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail=f"Timed out contacting {provider_name}.",
        )
    if isinstance(exc, httpx.ConnectError):
        return ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail=f"Could not connect to {provider_name}.",
        )
    if isinstance(exc, httpx.HTTPError):
        return ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail=f"Network error contacting {provider_name}.",
        )
    return ValidationResult(
        status=ValidationStatus.UNKNOWN_ERROR,
        detail=f"Unexpected error while validating {provider_name} key.",
    )


def map_http_status(response: httpx.Response, provider_name: str) -> ValidationResult:
    """Map a completed HTTP response from a provider's API to a `ValidationResult`.

    Shared by every HTTP-based validator (openai, anthropic, vertex_ai) so
    the status-code-to-status mapping only lives in one place. `provider_name`
    is used only for the human-readable `detail` string - see
    `map_httpx_exception` for the same caveat on what's safe to pass there.
    """
    if response.status_code == 200:
        return ValidationResult(status=ValidationStatus.VALID)
    if response.status_code in (401, 403):
        return ValidationResult(
            status=ValidationStatus.INVALID_KEY,
            detail=f"{provider_name} rejected the key (HTTP {response.status_code}).",
        )
    if response.status_code >= 500:
        return ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail=f"{provider_name} returned a server error (HTTP {response.status_code}).",
        )
    return ValidationResult(
        status=ValidationStatus.UNKNOWN_ERROR,
        detail=f"Unexpected response from {provider_name} (HTTP {response.status_code}).",
    )


# ---------------------------------------------------------------------------
# Phase 1.2 (BD-7a/b/c): inference-call errors.
#
# Deliberately distinct from the `ValidationResult`/`ValidationStatus`
# machinery above, which belongs to Phase 1.1's admin "test this key on
# entry" flow. These are raised by the `create_*`/`stream_*` inference
# methods in `providers/openai.py`, `providers/anthropic.py`, and
# `providers/vertex_ai.py` and are meant to be caught and translated to an
# HTTP response by the route-handler layer (BD-9) - nothing in this module
# catches or handles them itself.
# ---------------------------------------------------------------------------


class ProviderInferenceError(Exception):
    """Base class for errors raised while making an actual inference call
    to a provider (as opposed to the Phase 1.1 key-validation path)."""


class UnsupportedRequestError(ProviderInferenceError):
    """Raised when a request shape can't be translated for a provider's
    inference API in the current phase's translation contract.

    Example: `n > 1` for Anthropic/Vertex AI, where 1.2's translation
    contract maps exactly one gateway request to exactly one upstream
    response. The message is caller input describing an unsupported
    request shape, not secret material, so it's safe to log/return -
    route handlers should map this to HTTP 400 with a code such as
    `"unsupported_request"`.
    """


class ProviderCallError(ProviderInferenceError):
    """The upstream provider returned an error, or was unreachable, during
    an inference call.

    The inference-call analogue of `ValidationStatus.PROVIDER_UNREACHABLE`
    from the key-validation path. `message` must be safe to log/return:
    like `map_http_status` above, never echo a raw provider response body,
    which could itself reflect request content (including message text)
    back to the caller. `status_code` carries the upstream HTTP status
    code when known (`None` for network-level failures), for the route
    layer to decide how to map it (e.g. passthrough 429, 502 for 5xx).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def provider_call_error_from_response(response: httpx.Response, provider_name: str) -> ProviderCallError:
    """Build a `ProviderCallError` from a completed non-2xx HTTP response
    received during an inference call. Shared by every HTTP-based provider
    inference method so the status-code-to-error mapping only lives in one
    place. See `map_http_status` for the same "no raw body in the message"
    caveat.
    """
    return ProviderCallError(
        f"{provider_name} returned HTTP {response.status_code} during inference.",
        status_code=response.status_code,
    )


def provider_call_error_from_exception(exc: Exception, provider_name: str) -> ProviderCallError:
    """Build a `ProviderCallError` from an exception raised while calling a
    provider's HTTP API during an inference call. Shared by every
    HTTP-based provider inference method. See `map_httpx_exception` for the
    validation-path equivalent.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderCallError(f"Timed out contacting {provider_name} during inference.")
    if isinstance(exc, httpx.HTTPError):
        return ProviderCallError(f"Network error contacting {provider_name} during inference.")
    return ProviderCallError(f"Unexpected error calling {provider_name} during inference.")
