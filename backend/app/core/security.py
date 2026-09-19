"""Authentication primitives.

Passwords use PBKDF2-HMAC-SHA256. Release approval requires a second factor;
the TOTP implementation follows RFC 6238 so an ordinary authenticator app works
without adding a dependency (PRD 11, Security; FR-REL-001).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import get_settings

_PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(derived.hex(), expected)


# --------------------------------------------------------------------------- tokens
def create_access_token(subject: str, claims: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
        **(claims or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# --------------------------------------------------------------------------- TOTP
def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_now(secret: str, at: int | None = None, step: int = 30, digits: int = 6) -> str:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Accept the current step plus ``window`` steps either side for clock skew."""
    now = int(time.time())
    candidate = (code or "").strip().replace(" ", "")
    return any(
        hmac.compare_digest(totp_now(secret, now + offset * 30), candidate)
        for offset in range(-window, window + 1)
    )


# --------------------------------------------------------------------------- signatures
def sign_payload(payload: bytes) -> str:
    """Detached HMAC signature for released artifacts (PRD 7.3)."""
    settings = get_settings()
    return hmac.new(settings.release_signing_secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(payload), signature)
