"""Unit tests for services/encryption.py.

No DB, no network - pure AES-256-GCM round-trip / tamper-detection tests.
"""

from __future__ import annotations

import os

import pytest

from gatekey.services.encryption import (
    AUTH_TAG_LENGTH_BYTES,
    NONCE_LENGTH_BYTES,
    DecryptionError,
    EnvKeyProvider,
    build_aad,
    decrypt_secret,
    encrypt_secret,
)


def _key_provider(seed: bytes | None = None) -> EnvKeyProvider:
    return EnvKeyProvider(seed or os.urandom(32))


def test_round_trip_correctness():
    key_provider = _key_provider()
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")

    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)
    recovered = decrypt_secret(
        encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        aad=aad,
        key_provider=key_provider,
    )

    assert recovered == plaintext
    assert len(encrypted.nonce) == NONCE_LENGTH_BYTES
    assert len(encrypted.auth_tag) == AUTH_TAG_LENGTH_BYTES
    # Ciphertext must not simply be the plaintext (sanity check it's encrypted).
    assert encrypted.ciphertext != plaintext


def test_wrong_aad_fails():
    key_provider = _key_provider()
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")
    wrong_aad = build_aad("00000000-0000-0000-0000-000000000001", "anthropic")

    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)

    with pytest.raises(DecryptionError):
        decrypt_secret(
            encrypted.ciphertext,
            nonce=encrypted.nonce,
            auth_tag=encrypted.auth_tag,
            aad=wrong_aad,
            key_provider=key_provider,
        )


def test_wrong_key_fails():
    encrypting_key_provider = _key_provider()
    decrypting_key_provider = _key_provider()  # different random key
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")

    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=encrypting_key_provider)

    with pytest.raises(DecryptionError):
        decrypt_secret(
            encrypted.ciphertext,
            nonce=encrypted.nonce,
            auth_tag=encrypted.auth_tag,
            aad=aad,
            key_provider=decrypting_key_provider,
        )


def test_tampered_ciphertext_fails():
    key_provider = _key_provider()
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")

    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)

    tampered = bytearray(encrypted.ciphertext)
    tampered[0] ^= 0xFF  # flip a bit

    with pytest.raises(DecryptionError):
        decrypt_secret(
            bytes(tampered),
            nonce=encrypted.nonce,
            auth_tag=encrypted.auth_tag,
            aad=aad,
            key_provider=key_provider,
        )


def test_tampered_auth_tag_fails():
    key_provider = _key_provider()
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")

    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)

    tampered_tag = bytearray(encrypted.auth_tag)
    tampered_tag[0] ^= 0xFF

    with pytest.raises(DecryptionError):
        decrypt_secret(
            encrypted.ciphertext,
            nonce=encrypted.nonce,
            auth_tag=bytes(tampered_tag),
            aad=aad,
            key_provider=key_provider,
        )


def test_nonce_uniqueness_across_calls():
    key_provider = _key_provider()
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")

    nonces = {
        encrypt_secret(b"same-plaintext-every-time", aad=aad, key_provider=key_provider).nonce
        for _ in range(200)
    }

    # Extremely unlikely to collide with a correct os.urandom(12) source;
    # a collision here would indicate a broken/non-random nonce generator.
    assert len(nonces) == 200


def test_decryption_error_message_has_no_plaintext_or_ciphertext():
    key_provider = _key_provider()
    plaintext = b"sk-super-secret-provider-key-value"
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")
    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)

    wrong_key_provider = _key_provider()
    try:
        decrypt_secret(
            encrypted.ciphertext,
            nonce=encrypted.nonce,
            auth_tag=encrypted.auth_tag,
            aad=aad,
            key_provider=wrong_key_provider,
        )
        pytest.fail("expected DecryptionError")
    except DecryptionError as exc:
        message = str(exc)
        assert plaintext.decode() not in message
        assert encrypted.ciphertext.hex() not in message
        assert message == "Failed to decrypt secret: authentication check failed."


def test_env_key_provider_rejects_wrong_length_key():
    with pytest.raises(ValueError):
        EnvKeyProvider(os.urandom(16))


def test_different_plaintexts_produce_different_ciphertexts_same_length_input():
    key_provider = _key_provider()
    aad = build_aad("00000000-0000-0000-0000-000000000001", "openai")
    a = encrypt_secret(b"aaaaaaaaaaaaaaaa", aad=aad, key_provider=key_provider)
    b = encrypt_secret(b"aaaaaaaaaaaaaaaa", aad=aad, key_provider=key_provider)

    # Same plaintext, different nonces -> different ciphertext each time.
    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext
