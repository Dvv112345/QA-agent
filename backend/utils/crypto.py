"""Symmetric encryption helpers for GitHub access tokens.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` library.
The encryption key is read from the ``ENCRYPTION_KEY`` environment variable.
"""

import logging
import os

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Cached cipher instance — created once per process lifetime.
_cipher: Fernet | None = None


def _get_cipher() -> Fernet:
    """Return the process-wide Fernet cipher, creating it on first access.

    Raises ``RuntimeError`` if ``ENCRYPTION_KEY`` is missing or invalid.
    """
    global _cipher
    if _cipher is not None:
        return _cipher

    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate one with:\n"
            'python -c "from cryptography.fernet import Fernet;'
            ' print(Fernet.generate_key().decode())"'
        )

    try:
        _cipher = Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc

    return _cipher


def encrypt_token(token: str) -> str:
    """Encrypt a GitHub access token for storage in the database.

    Returns the base64-encoded Fernet token as a string.
    """
    return _get_cipher().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a previously encrypted GitHub access token.

    Returns the original plain-text token.
    """
    return _get_cipher().decrypt(encrypted.encode()).decode()
