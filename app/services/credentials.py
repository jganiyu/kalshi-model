from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PRIVATE_KEY_FILENAME = "kalshi-private-key.pem"
METADATA_FILENAME = "credentials.json"
MAX_PRIVATE_KEY_BYTES = 64 * 1024


def credential_directory() -> Path:
    configured = os.getenv("KALSHI_MODEL_CREDENTIAL_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "Kalshi Model"


@dataclass(frozen=True)
class StoredCredentials:
    key_id: str
    private_key_path: Path


class CredentialStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or credential_directory()
        self.metadata_path = self.directory / METADATA_FILENAME
        self.private_key_path = self.directory / PRIVATE_KEY_FILENAME

    def load(self) -> StoredCredentials | None:
        if not self.metadata_path.exists():
            return None
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        key_id = payload.get("key_id")
        key_path = Path(payload.get("private_key_path") or self.private_key_path)
        if not isinstance(key_id, str) or not key_id or not key_path.exists():
            return None
        return StoredCredentials(key_id=key_id, private_key_path=key_path)

    def save(self, key_id: str, private_key_pem: str) -> StoredCredentials:
        cleaned_key_id = key_id.strip()
        if not KEY_ID_PATTERN.fullmatch(cleaned_key_id):
            raise ValueError("Enter the Kalshi API Key ID shown with your downloaded key.")

        encoded = private_key_pem.strip().encode("utf-8")
        if not encoded or len(encoded) > MAX_PRIVATE_KEY_BYTES:
            raise ValueError("Choose a valid Kalshi RSA private key file.")
        try:
            private_key = serialization.load_pem_private_key(encoded, password=None)
        except (TypeError, ValueError) as exc:
            raise ValueError("The selected file is not an unencrypted PEM private key.") from exc
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("The selected private key must use RSA.")

        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self._atomic_write(self.private_key_path, encoded + b"\n", 0o600)
        metadata = json.dumps(
            {
                "key_id": cleaned_key_id,
                "private_key_path": str(self.private_key_path),
            },
            indent=2,
        ).encode("utf-8")
        self._atomic_write(self.metadata_path, metadata + b"\n", 0o600)
        return StoredCredentials(cleaned_key_id, self.private_key_path)

    def remove(self) -> bool:
        removed = False
        for path in (self.metadata_path, self.private_key_path):
            if path.exists():
                path.unlink()
                removed = True
        return removed

    @staticmethod
    def _atomic_write(path: Path, contents: bytes, mode: int) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_bytes(contents)
        os.chmod(temporary, mode)
        temporary.replace(path)
        os.chmod(path, mode)


def environment_credentials() -> tuple[str | None, Path | None]:
    key_id = os.getenv("KALSHI_API_KEY_ID") or None
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    return key_id, Path(key_path).expanduser() if key_path else None


def resolve_credentials() -> tuple[str | None, Path | None, str]:
    stored = CredentialStore().load()
    if stored:
        return stored.key_id, stored.private_key_path, "local form"
    key_id, key_path = environment_credentials()
    if key_id or key_path:
        return key_id, key_path, "environment"
    return None, None, "none"


def masked_key_id(key_id: str | None) -> str | None:
    if not key_id:
        return None
    if len(key_id) <= 12:
        return f"{key_id[:3]}...{key_id[-3:]}"
    return f"{key_id[:6]}...{key_id[-4:]}"
