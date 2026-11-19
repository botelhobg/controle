from __future__ import annotations

import hashlib
import hmac
import secrets


CURRENT_PASSWORD_FORMAT = "scrypt-v2"
MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128
MAX_LOGIN_LENGTH = 512  # aceita também credenciais antigas, anteriores à política atual


def _derive(password: str, salt: bytes, algorithm: str) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8,
        p=5 if algorithm == CURRENT_PASSWORD_FORMAT else 1,
        maxmem=32 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, CURRENT_PASSWORD_FORMAT)
    return f"{CURRENT_PASSWORD_FORMAT}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, salt, expected = encoded.split("$", 2)
        if algorithm not in {CURRENT_PASSWORD_FORMAT, "scrypt"} or len(salt) != 32 or len(expected) != 128:
            return False
        actual = _derive(password, bytes.fromhex(salt), algorithm)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def needs_rehash(encoded: str) -> bool:
    return not encoded.startswith(CURRENT_PASSWORD_FORMAT + "$")


def validate_new_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH or not password.strip():
        raise ValueError(f"Defina uma senha de {MIN_PASSWORD_LENGTH} a {MAX_PASSWORD_LENGTH} caracteres.")
    if password.strip().casefold() == "troque-esta-senha":
        raise ValueError("Escolha outra senha; não use a senha do exemplo.")

