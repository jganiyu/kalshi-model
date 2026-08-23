from __future__ import annotations

import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.credentials import CredentialStore, resolve_credentials


def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def test_credentials_are_validated_and_stored_with_private_permissions(tmp_path):
    store = CredentialStore(tmp_path / "Kalshi Model")
    saved = store.save("11111111-2222-4333-8444-555555555555", private_key_pem())

    assert store.load() == saved
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.private_key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.metadata_path.stat().st_mode) == 0o600
    assert "PRIVATE KEY" not in store.metadata_path.read_text(encoding="utf-8")


def test_invalid_private_key_is_rejected(tmp_path):
    store = CredentialStore(tmp_path)

    with pytest.raises(ValueError, match="not an unencrypted PEM private key"):
        store.save("11111111-2222-4333-8444-555555555555", "not-a-key")


def test_local_form_credentials_take_precedence_and_can_be_removed(tmp_path, monkeypatch):
    local_directory = tmp_path / "support"
    environment_key = tmp_path / "environment.pem"
    environment_key.write_text(private_key_pem(), encoding="utf-8")
    monkeypatch.setenv("KALSHI_MODEL_CREDENTIAL_DIR", str(local_directory))
    monkeypatch.setenv("KALSHI_API_KEY_ID", "environment-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(environment_key))
    store = CredentialStore()
    saved = store.save("local-form-key-id", private_key_pem())

    assert resolve_credentials() == (saved.key_id, saved.private_key_path, "local form")

    assert store.remove() is True
    assert resolve_credentials() == (
        "environment-key-id",
        environment_key,
        "environment",
    )
