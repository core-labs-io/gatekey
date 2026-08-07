"""AES-256-GCM envelope encryption for provider secret material.

Design
------
`KeyProvider` is a narrow seam for where the raw 32-byte AES key comes from.
`EnvKeyProvider` is the Phase 1.1 implementation (reads from `Settings`).
A later phase can add a `VaultKeyProvider` / `KmsKeyProvider` etc. without
touching the encrypt/decrypt functions below.

Storage shape
-------------
`ProviderKey` (database-admin's model) stores three separate columns:
`ciphertext`, `nonce`, `auth_tag`. `AESGCM.encrypt()` from `cryptography`
returns ciphertext with the 16-byte GCM tag appended; `encrypt_secret()`
splits that apart so callers can persist the three pieces independently.
`decrypt_secret()` reassembles them before calling `AESGCM.decrypt()`.

Associated data (AAD)
----------------------
Callers must pass AAD of the form `f"{org_id}:{provider}"` (see
`build_aad()`). Binding ciphertext to org+provider means a ciphertext
row can never be decrypted successfully if it's copied to a different
org_id/provider column - tampering with either is detected as an
authentication failure, same as tampering with the ciphertext itself.

Secret hygiene
--------------
No function in this module ever includes plaintext key material in a log
message or exception message. `DecryptionError` messages are static/generic
- callers must not append `str(exc)` from the underlying `cryptography`
exception to anything user-facing or logged, since pyca's InvalidTag
carries no plaintext but we still keep messages generic defensively and to
avoid leaking ciphertext/nonce bytes via repr().
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LENGTH_BYTES = 12
AUTH_TAG_LENGTH_BYTES = 16
KEY_LENGTH_BYTES = 32


class EncryptionError(Exception):
    """Base class for encryption/decryption failures. Never carries plaintext."""


class DecryptionError(EncryptionError):
    """Raised when decryption fails: wrong key, wrong AAD, or tampered data.

    Deliberately generic - does not distinguish *why* the tag check failed,
    since that distinction is not useful to callers and over-specific error
    messages can leak information to an attacker probing for valid
    ciphertexts.
    """

    def __init__(self) -> None:
        super().__init__("Failed to decrypt secret: authentication check failed.")


class KeyProvider(ABC):
    """Abstraction over where the raw AES-256 master key comes from."""

    @abstractmethod
    def get_key(self) -> bytes:
        """Return the raw 32-byte AES key. Must never be logged by callers."""
        raise NotImplementedError


class EnvKeyProvider(KeyProvider):
    """Reads the master key from application Settings (env var / .env).

    Phase 1.1 implementation. A pluggable KMS backend (Vault/AWS KMS/GCP KMS)
    is a later concern - swap in a different `KeyProvider` implementation
    without changing `encrypt_secret`/`decrypt_secret`.
    """

    def __init__(self, key_bytes: bytes) -> None:
        if len(key_bytes) != KEY_LENGTH_BYTES:
            raise ValueError(f"Master key must be exactly {KEY_LENGTH_BYTES} bytes.")
        self._key_bytes = key_bytes

    @classmethod
    def from_settings(cls, settings: object) -> "EnvKeyProvider":
        """Construct from a `gatekey.config.Settings`-shaped object.

        Accepts `object` (structurally typed via `master_key_bytes()`) to
        avoid a circular import between config.py and this module.
        """
        return cls(settings.master_key_bytes())  # type: ignore[attr-defined]

    def get_key(self) -> bytes:
        return self._key_bytes


@dataclass(frozen=True)
class EncryptedSecret:
    """Result of encrypting a secret: three pieces to persist independently."""

    ciphertext: bytes
    nonce: bytes
    auth_tag: bytes


def build_aad(org_id: str, provider: str) -> bytes:
    """Build the associated-data byte string binding ciphertext to org+provider."""
    return f"{org_id}:{provider}".encode("utf-8")


def encrypt_secret(plaintext: bytes, *, aad: bytes, key_provider: KeyProvider) -> EncryptedSecret:
    """Encrypt `plaintext` with AES-256-GCM using a fresh random nonce.

    A new 12-byte nonce is generated per call via `os.urandom` - nonces must
    never be reused with the same key, and letting the OS CSPRNG generate a
    fresh one per call is the standard-safe approach for GCM (vs. e.g. a
    counter, which would require careful persistence across process
    restarts).
    """
    key = key_provider.get_key()
    nonce = os.urandom(NONCE_LENGTH_BYTES)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext, aad)
    ciphertext = ciphertext_with_tag[:-AUTH_TAG_LENGTH_BYTES]
    auth_tag = ciphertext_with_tag[-AUTH_TAG_LENGTH_BYTES:]
    return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, auth_tag=auth_tag)


def decrypt_secret(
    ciphertext: bytes,
    *,
    nonce: bytes,
    auth_tag: bytes,
    aad: bytes,
    key_provider: KeyProvider,
) -> bytes:
    """Decrypt a secret previously produced by `encrypt_secret`.

    Raises `DecryptionError` (never the underlying `cryptography` exception,
    and never including any plaintext/ciphertext bytes) if the key, nonce,
    AAD, or ciphertext/tag don't match what was used at encryption time.
    """
    key = key_provider.get_key()
    aesgcm = AESGCM(key)
    ciphertext_with_tag = ciphertext + auth_tag
    try:
        return aesgcm.decrypt(nonce, ciphertext_with_tag, aad)
    except InvalidTag:
        raise DecryptionError() from None
    except ValueError:
        # e.g. malformed nonce length - still must not leak input bytes.
        raise DecryptionError() from None
