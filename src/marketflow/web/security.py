"""Password hashing (stdlib scrypt) and JWT helpers. Token expiry is checked against the injected TimeProvider."""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta
from typing import Any

import jwt

_N, _R, _P = 2 ** 14, 8, 1
MIN_SECRET_LEN = 32
MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(calc, bytes.fromhex(dk))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("not-a-real-password")


def burn_verify(password: str) -> None:
    """Spend the same time as a real check so unknown emails aren't distinguishable by latency."""
    verify_password(password, _DUMMY)


def make_token(secret: str, user_id: int, now: datetime, ttl_minutes: int) -> str:
    exp = now + timedelta(minutes=ttl_minutes)
    return jwt.encode({"sub": str(user_id), "iat": int(now.timestamp()), "exp": int(exp.timestamp())}, secret, algorithm="HS256")


def read_token(secret: str, token: str, now: datetime) -> int | None:
    """User id if the signature is valid and not expired (per `now`), else None."""
    try:
        claims: dict[str, Any] = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": False})
        if int(claims["exp"]) <= int(now.timestamp()):
            return None
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
