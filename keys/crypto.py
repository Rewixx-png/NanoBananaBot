"""
Fernet-based encryption for the API keys config file (r.txt → r.txt.enc).

Uses KEYS_ENCRYPTION_PASSWORD from environment (separate from DB_ENCRYPTION_KEY).
"""
import base64
import logging
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_ENCRYPTED_EXT = ".enc"
_SALT_SIZE = 16
_ITERATIONS = 600_000


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit Fernet key from password + salt via PBKDF2-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _get_password() -> str:
    """Read KEYS_ENCRYPTION_PASSWORD from environment."""
    pw = os.getenv("KEYS_ENCRYPTION_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(
            "KEYS_ENCRYPTION_PASSWORD is not set. "
            "Set it in .env to encrypt/decrypt API keys."
        )
    return pw


def encrypt_bytes(plaintext: bytes) -> bytes:
    """Encrypt raw bytes → salt + Fernet token."""
    password = _get_password()
    salt = os.urandom(_SALT_SIZE)
    key = _derive_key(password, salt)
    return salt + Fernet(key).encrypt(plaintext)


def decrypt_bytes(token: bytes) -> bytes:
    """Decrypt salt + Fernet token → raw plaintext bytes."""
    password = _get_password()
    salt = token[:_SALT_SIZE]
    data = token[_SALT_SIZE:]
    key = _derive_key(password, salt)
    return Fernet(key).decrypt(data)


def encrypt_keys_file(source_path: str) -> str:
    """Encrypt `source_path` (plaintext) → `source_path.enc`.
    Returns path to encrypted file."""
    with open(source_path, "rb") as f:
        plaintext = f.read()
    token = encrypt_bytes(plaintext)
    enc_path = source_path + _ENCRYPTED_EXT
    with open(enc_path, "wb") as f:
        f.write(token)
    logger.info(f"Encrypted {source_path} → {enc_path}")
    return enc_path


def decrypt_keys_file(enc_path: str) -> bytes:
    """Decrypt `enc_path` → raw plaintext bytes.
    Falls back to plaintext path if `.enc` doesn't exist (backward compat)."""
    if not os.path.exists(enc_path):
        plain_path = enc_path
        if plain_path.endswith(_ENCRYPTED_EXT):
            plain_path = plain_path[: -len(_ENCRYPTED_EXT)]
        if os.path.exists(plain_path):
            logger.warning(
                f"Encrypted file {enc_path} not found, "
                f"reading plaintext {plain_path} (migrate with encrypt_keys_file)"
            )
            with open(plain_path, "rb") as f:
                return f.read()
        raise FileNotFoundError(f"Neither {enc_path} nor {plain_path} found")
    with open(enc_path, "rb") as f:
        token = f.read()
    return decrypt_bytes(token)
