"""Settings file + env overrides. Holds API tokens, so the real file is git-ignored (config/settings.toml)."""
from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "settings.toml"
DEFAULT_DB_URL = "duckdb:///data/marketflow.duckdb"


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DB_URL
    mock_llm: bool = True
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""    # vision (flyer / image reading) uses the OpenAI API; empty = vision disabled
    openai_model: str = "gpt-4o-mini"
    kamis_api_key: str = ""
    kamis_api_id: str = ""
    kamis_product_cls: str = "02"
    garak_url: str = ""
    garak_api_key: str = ""
    jwt_secret: str = ""
    alert_webhook_url: str = ""
    token_ttl_minutes: int = 60
    tls_cert: str = ""          # PEM paths: serve HTTPS directly
    tls_key: str = ""
    trust_proxy: str = ""       # IPs/CIDRs of a reverse proxy whose X-Forwarded-For uvicorn may trust (e.g. "127.0.0.1")
    solo_mode: bool = True      # one person runs the system: they may approve their own proposals and below-defense-line changes
    alerts_digest: bool = False  # opt-in: alerts below RED are sent as one summary per run
    public_url: str = ""        # dashboard address put in alerts as a link (alerts carry no figures); e.g. https://marketflow.example.com
    inbox_dir: str = "data/inbox"   # file-drop folders for the report/buyer/flyer/new-product crawlers (one subfolder each)
    hsts: bool = False          # send Strict-Transport-Security (also implied by tls_cert); set when a proxy terminates TLS
    decision: Mapping[str, Any] = field(default_factory=dict)   # raw [decision] table
    insights: Mapping[str, Any] = field(default_factory=dict)   # raw [insights] table (P2 analysis thresholds)
    quality: Mapping[str, Any] = field(default_factory=dict)    # raw [quality] table (data-quality thresholds)
    mi: Mapping[str, Any] = field(default_factory=dict)         # raw [mi] table (Market Intelligence tunables)
    hive: Mapping[str, Any] = field(default_factory=dict)       # raw [hive] table (internal-data queries; see hive_source.py)

    @property
    def kamis_enabled(self) -> bool:
        return bool(self.kamis_api_key and self.kamis_api_id)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def garak_enabled(self) -> bool:
        return bool(self.garak_url)

    def __repr__(self) -> str:  # never leak tokens into logs
        def red(v: str) -> str:
            return "***" if v else "''"
        return (f"Settings(database_url={self.database_url!r}, mock_llm={self.mock_llm}, "
                f"kamis_api_key={red(self.kamis_api_key)}, kamis_api_id={red(self.kamis_api_id)}, "
                f"jwt_secret={red(self.jwt_secret)}, alert_webhook_url={red(self.alert_webhook_url)}, gemini_api_key={red(self.gemini_api_key)}, gemini_model={self.gemini_model!r}, openai_api_key={red(self.openai_api_key)}, openai_model={self.openai_model!r}, garak_url={self.garak_url!r}, garak_api_key={red(self.garak_api_key)})")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: Path | str | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    p = Path(path or env.get("MARKETFLOW_SETTINGS") or DEFAULT_PATH)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"invalid settings file {p}: {e}") from e
        if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("%s is readable by other users; run: chmod 600 %s", p, p)
    else:
        log.info("no settings file at %s; using environment/defaults", p)

    def pick(envkey: str, *path: str, default: Any) -> Any:
        """Env var wins; otherwise walk the TOML tables; otherwise default."""
        if env.get(envkey):
            return env[envkey]
        node: Any = data
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        return default if node is None else node

    return Settings(
        database_url=str(pick("DATABASE_URL", "database", "url", default=DEFAULT_DB_URL)),
        mock_llm=_truthy(pick("MOCK_LLM", "llm", "mock", default=True)),
        gemini_api_key=str(pick("GEMINI_API_KEY", "llm", "gemini_api_key", default="")),
        gemini_model=str(pick("GEMINI_MODEL", "llm", "gemini_model", default="gemini-2.5-flash")),
        openai_api_key=str(pick("OPENAI_API_KEY", "vision", "openai_api_key", default="")).strip(),
        openai_model=str(pick("OPENAI_MODEL", "vision", "openai_model", default="gpt-4o-mini")),
        kamis_api_key=str(pick("KAMIS_API_KEY", "sources", "kamis", "api_key", default="")),
        kamis_api_id=str(pick("KAMIS_API_ID", "sources", "kamis", "api_id", default="")),
        kamis_product_cls=str(pick("KAMIS_PRODUCT_CLS", "sources", "kamis", "product_cls", default="02")),
        garak_url=str(pick("GARAK_API_URL", "sources", "garak", "url", default="")),
        garak_api_key=str(pick("GARAK_API_KEY", "sources", "garak", "api_key", default="")),
        alert_webhook_url=str(pick("ALERT_WEBHOOK_URL", "alerts", "webhook_url", default="")),
        jwt_secret=str(pick("JWT_SECRET", "auth", "jwt_secret", default="")),
        token_ttl_minutes=int(pick("TOKEN_TTL_MINUTES", "auth", "token_ttl_minutes", default=60)),
        tls_cert=str(pick("TLS_CERT", "server", "tls_cert", default="")),
        tls_key=str(pick("TLS_KEY", "server", "tls_key", default="")),
        trust_proxy=str(pick("TRUST_PROXY", "server", "trust_proxy", default="")),
        alerts_digest=_truthy(pick("ALERTS_DIGEST", "alerts", "digest", default=False)),
        solo_mode=_truthy(pick("SOLO_MODE", "approval", "solo_mode", default=True)),
        public_url=str(pick("PUBLIC_URL", "server", "public_url", default="")).rstrip("/"),
        inbox_dir=str(pick("CRAWLER_INBOX_DIR", "crawlers", "inbox_dir", default="data/inbox")),
        hsts=_truthy(pick("HSTS", "server", "hsts", default=False)),
        decision=dict(data.get("decision", {})),
        quality=dict(data.get("quality", {})),
        insights=dict(data.get("insights", {})),
        mi=dict(data.get("mi", {})),
        hive=dict(data.get("hive", {})),
    )
