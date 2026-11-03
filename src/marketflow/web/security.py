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


class LoginThrottle:
    """Failed-login limiter, in memory (per process).

    * (email, client) : 5 / 15 min  -> locks that client out of that account only, so a stranger cannot lock the real
                                       user out by guessing from elsewhere
    * client          : 20 / 15 min -> stops one machine spraying many accounts
    * email           : 30 / 15 min -> stops distributed guessing against one account
    Old entries are dropped as they expire and the table size is capped, so random emails cannot grow memory.
    Behind a reverse proxy, `client` is the proxy's address unless uvicorn is started with --proxy-headers."""
    WINDOW = timedelta(minutes=15)
    LIMITS = {"pair": 5, "client": 20, "email": 30}
    MAX_KEYS = 10_000

    def __init__(self) -> None:
        self._hits: dict[tuple, list[datetime]] = {}

    def _recent(self, key: tuple, now: datetime) -> list[datetime]:
        live = [t for t in self._hits.get(key, ()) if now - t < self.WINDOW]
        if live:
            self._hits[key] = live
        else:
            self._hits.pop(key, None)
        return live

    @staticmethod
    def _keys(email: str, client: str) -> dict[str, tuple]:
        return {"pair": ("pair", email, client), "client": ("client", client), "email": ("email", email)}

    def blocked(self, email: str, client: str, now: datetime) -> bool:
        return any(len(self._recent(k, now)) >= self.LIMITS[n] for n, k in self._keys(email, client).items())

    def fail(self, email: str, client: str, now: datetime) -> None:
        room = self.MAX_KEYS - 3                                    # a failure adds up to 3 keys
        if len(self._hits) > room:                                  # sweep expired entries; if still full, drop the oldest
            for k in list(self._hits):
                self._recent(k, now)
            while len(self._hits) > room:
                self._hits.pop(next(iter(self._hits)))
        for k in self._keys(email, client).values():
            self._hits.setdefault(k, []).append(now)

    def succeed(self, email: str, client: str) -> None:
        self._hits.pop(("pair", email, client), None)
