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
DEFAULT_DB_URL = "sqlite:///data/marketflow.db"


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DB_URL
    mock_llm: bool = True
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    kamis_api_key: str = ""
    kamis_api_id: str = ""
    kamis_product_cls: str = "02"
    garak_url: str = ""
    garak_api_key: str = ""
    decision: Mapping[str, Any] = field(default_factory=dict)   # raw [decision] table

    @property
    def kamis_enabled(self) -> bool:
        return bool(self.kamis_api_key and self.kamis_api_id)

    @property
    def garak_enabled(self) -> bool:
        return bool(self.garak_url)

    def __repr__(self) -> str:  # never leak tokens into logs
        def red(v: str) -> str:
            return "***" if v else "''"
        return (f"Settings(database_url={self.database_url!r}, mock_llm={self.mock_llm}, "
                f"kamis_api_key={red(self.kamis_api_key)}, kamis_api_id={red(self.kamis_api_id)}, "
                f"gemini_api_key={red(self.gemini_api_key)}, gemini_model={self.gemini_model!r}, garak_url={self.garak_url!r}, garak_api_key={red(self.garak_api_key)})")


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
        kamis_api_key=str(pick("KAMIS_API_KEY", "sources", "kamis", "api_key", default="")),
        kamis_api_id=str(pick("KAMIS_API_ID", "sources", "kamis", "api_id", default="")),
        kamis_product_cls=str(pick("KAMIS_PRODUCT_CLS", "sources", "kamis", "product_cls", default="02")),
        garak_url=str(pick("GARAK_API_URL", "sources", "garak", "url", default="")),
        garak_api_key=str(pick("GARAK_API_KEY", "sources", "garak", "api_key", default="")),
        decision=dict(data.get("decision", {})),
    )
