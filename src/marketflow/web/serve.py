"""uvicorn options derived from settings (kept separate from __main__ so it is testable)."""
from __future__ import annotations

from pathlib import Path

from ..settings import Settings


def uvicorn_kwargs(s: Settings, host: str, port: int) -> dict:
    kw: dict = {"host": host, "port": port}
    if s.tls_cert or s.tls_key:
        if not (s.tls_cert and s.tls_key):
            raise SystemExit("server.tls_cert and server.tls_key must be set together")
        for label, p in (("tls_cert", s.tls_cert), ("tls_key", s.tls_key)):
            if not Path(p).is_file():
                raise SystemExit(f"server.{label}: file not found")
        kw.update(ssl_certfile=s.tls_cert, ssl_keyfile=s.tls_key)
    elif host not in ("127.0.0.1", "localhost", "::1") and not s.hsts:
        print("WARNING: serving plain HTTP on a non-local address; set server.tls_cert/tls_key or terminate TLS at a proxy "
              "(then set server.hsts = true).")
    return kw
