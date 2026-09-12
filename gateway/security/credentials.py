"""Credential encryption and salted verification, separate from routing policy."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def digest(value: str) -> str:
    return HASHER.hash(value)


def verify(encoded: str, value: str) -> bool:
    try:
        return HASHER.verify(encoded, value)
    except (VerificationError, InvalidHashError):
        return False


class CredentialCipher:
    """A database backup never includes the external encryption key."""

    def __init__(self, state: Path):
        supplied = os.environ.get("MODEL_ROUTER_SECRET_KEY")
        self.key_file = Path(
            os.environ.get("MODEL_ROUTER_KEY_FILE", str(state) + ".key")
        )
        if supplied:
            key = supplied.encode()
        else:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.key_file.exists():
                try:
                    fd = os.open(
                        self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(fd, "wb") as output:
                        output.write(Fernet.generate_key())
                except FileExistsError:
                    pass
            if os.name != "nt" and self.key_file.stat().st_mode & 0o077:
                raise PermissionError(
                    "Credential key file must be readable only by its owner"
                )
            key = self.key_file.read_bytes().strip()
        self.fernet = Fernet(key)
        self.lookup_key = hmac.digest(
            key, b"model-router/client-key-index/v1", "sha256"
        )

    def encrypt(self, value: str) -> str:
        return "fernet:" + self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not value.startswith("fernet:"):
            raise ValueError("Plaintext credential requires migration")
        return self.fernet.decrypt(value[7:].encode()).decode()

    def lookup(self, key: str) -> str:
        return hmac.new(self.lookup_key, key.encode(), hashlib.sha256).hexdigest()


def bootstrap_key(state: Path) -> str:
    """Create a one-time operator key for the local owner, never in API output."""
    path = state / "operator-bootstrap.key"
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(key + "\n")
    return key
