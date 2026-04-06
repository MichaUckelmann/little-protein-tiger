"""
Fernet symmetric encryption for user-supplied Anthropic API keys.

The encryption key is stored in FERNET_KEY env var (never in the DB).
Generate a new key with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("FERNET_KEY", "")
    if not key:
        raise RuntimeError(
            "FERNET_KEY env var not set. "
            "Generate one: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_key(plaintext: str) -> bytes:
    """Encrypt a plaintext API key to bytes for storage."""
    return _get_fernet().encrypt(plaintext.encode())


def decrypt_key(ciphertext: bytes) -> str:
    """Decrypt stored bytes back to a plaintext API key."""
    return _get_fernet().decrypt(ciphertext).decode()
