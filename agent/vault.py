"""Encryption at rest for user profiles (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from DATA_KEY and never touches the database. Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, key: str):
        if not key:
            raise VaultError("DATA_KEY is not set")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            raise VaultError("DATA_KEY is not a valid Fernet key") from None

    def encrypt(self, text: str) -> bytes:
        return self._fernet.encrypt(text.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            raise VaultError("could not decrypt: wrong DATA_KEY or corrupted data") from None
