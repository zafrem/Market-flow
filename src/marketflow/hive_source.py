"""Internal data straight from Hive (read-only): run a configured SELECT and hand the rows to the same validator
as the file import. Connection settings live in settings.toml [hive]; the password only in the HIVE_PASSWORD
environment variable (or settings.toml) and is never logged, printed or shown in errors.

The driver (PyHive) is optional:  pip install -e '.[hive]'   - tests inject a fake `connect`."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|load|export)\b", re.IGNORECASE)


class HiveError(Exception):
    """A Hive problem described without the password or the full query text."""


@dataclass(frozen=True)
class HiveConfig:
    host: str = ""
    port: int = 10000
    username: str = ""
    database: str = "default"
    auth: str = "LDAP"                       # NONE | LDAP | CUSTOM | KERBEROS (PyHive's meaning)
    password: str = field(default="", repr=False)
    queries: Mapping[str, str] = field(default_factory=dict)   # name -> SELECT whose columns map to internal.columns
    max_rows: int = 200_000

    @property
    def ready(self) -> bool:
        return bool(self.host and self.queries)


def load_hive_config(table: Mapping[str, Any], env: Mapping[str, str] | None = None) -> HiveConfig:
    env = os.environ if env is None else env
    t = dict(table or {})
    queries = {str(k): str(v) for k, v in (t.get("queries") or {}).items()}
    for name, sql in queries.items():
        if not _NAME.match(name):
            raise HiveError(f"hive.queries: bad query name {name!r}")
        validate_query(sql, name)
    return HiveConfig(
        host=str(env.get("HIVE_HOST") or t.get("host") or "").strip(),
        port=int(env.get("HIVE_PORT") or t.get("port") or 10000),
        username=str(env.get("HIVE_USERNAME") or t.get("username") or "").strip(),
        database=str(t.get("database") or "default"),
        auth=str(t.get("auth") or "LDAP").upper(),
        password=str(env.get("HIVE_PASSWORD") or t.get("password") or ""),
        queries=queries,
        max_rows=int(t.get("max_rows") or 200_000),
    )


def validate_query(sql: str, name: str = "query") -> None:
    """Only a single read-only SELECT may run: this tool reads Hive, it never changes it."""
    body = sql.strip().rstrip(";").strip()
    if not _READ_ONLY.match(body) or ";" in body or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", body)):
        raise HiveError(f"hive.queries.{name}: only a single read-only SELECT is allowed")


def _default_connect(cfg: HiveConfig):
    try:
        from pyhive import hive
    except ImportError as e:
        raise HiveError("PyHive is not installed; run: pip install -e '.[hive]'") from e
    kwargs: dict[str, Any] = {"host": cfg.host, "port": cfg.port, "database": cfg.database}
    if cfg.username:
        kwargs["username"] = cfg.username
    if cfg.auth != "NONE":
        kwargs["auth"] = cfg.auth
        if cfg.password:
            kwargs["password"] = cfg.password
    return hive.Connection(**kwargs)


def fetch_hive(cfg: HiveConfig, query_name: str, connect: Callable[[HiveConfig], Any] | None = None
               ) -> tuple[list[str], list[tuple[int, list[Any]]]]:
    """(headers, [(row number, cells)]) in the shape read_table() returns for files."""
    if not cfg.ready:
        raise HiveError("Hive is not configured: set [hive] host and at least one query in settings.toml")
    sql = cfg.queries.get(query_name)
    if sql is None:
        raise HiveError(f"unknown Hive query {query_name!r}; configured: {', '.join(sorted(cfg.queries)) or 'none'}")
    validate_query(sql, query_name)
    try:
        conn = (connect or _default_connect)(cfg)
        try:
            cur = conn.cursor()
            cur.execute(sql.strip().rstrip(";"))
            headers = [re.sub(r"^[^.]+\.", "", str(d[0])) for d in cur.description or []]   # Hive prefixes "table.col"
            fetched = cur.fetchmany(cfg.max_rows + 1)
            if len(fetched) > cfg.max_rows:
                raise HiveError(f"Hive query {query_name!r} returned more than {cfg.max_rows} rows; narrow it")
            rows = [(i, list(r)) for i, r in enumerate(fetched, start=2)]
        finally:
            conn.close()
    except HiveError:
        raise
    except Exception as e:      # driver errors can echo the connection string; keep only the type
        raise HiveError(f"Hive query {query_name!r} failed ({type(e).__name__})") from None
    return headers, rows
