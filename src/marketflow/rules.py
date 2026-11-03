"""Hot-reloadable extraction rules (regexes, field maps, parser entrypoints).

Self-healing (SRS F-02-3) works by *data*, not code changes:
  base rules   config/extraction.json      (versioned)
  overlay      config/overrides.json       (approved fixes; written by RuleStore.promote)
Parsers are referenced by dotted path ("pkg.mod:func") and resolved on every call, so a promoted
rule or a swapped parser takes effect on the next run without a restart. A candidate that fails
validation is rejected; if a hand-edit breaks the file, the last good rules stay active.
"""
from __future__ import annotations

import copy
import importlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
Rules = dict[str, Any]


def load_callable(dotted: str) -> Callable[..., Any]:
    mod, sep, fn = dotted.partition(":")
    if not sep or not mod or not fn:
        raise ValueError(f"parser must look like 'pkg.mod:func', got {dotted!r}")
    obj = getattr(importlib.import_module(mod), fn)
    if not callable(obj):
        raise TypeError(f"{dotted} is not callable")
    return obj


def deep_merge(base: Rules, over: Rules) -> Rules:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def unit_regex(units: Rules) -> re.Pattern[str]:
    tokens = sorted([*units["measure_tokens"], *units["count_tokens"]], key=len, reverse=True)
    return re.compile(units["pattern"].replace("{tokens}", "|".join(re.escape(t) for t in tokens)), re.I)


def validate(rules: Rules) -> None:
    """Raise on anything that would break extraction. Used on load and before promotion."""
    unit_regex(rules["units"])
    re.compile(rules["common"]["price_clean_pattern"])
    for name, src in rules["sources"].items():
        if not callable(load_callable(src["parser"])):
            raise TypeError(name)
        if not src.get("fields") or not src.get("items_path"):
            raise ValueError(f"source {name}: 'fields' and 'items_path' are required")
        if p := src.get("alias_strip_pattern"):
            re.compile(p)


def _sig(p: Path | None) -> tuple[int, int] | None:
    try:
        st = p.stat() if p else None
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size) if st else None


class RuleStore:
    def __init__(self, base: Path, overlay: Path | None = None):
        self._base, self._overlay = Path(base), Path(overlay) if overlay else None
        self._sigs: tuple | None = None
        self._rules: Rules | None = None
        self._lock = threading.Lock()

    def _read(self) -> Rules:
        merged = json.loads(self._base.read_text(encoding="utf-8"))
        if self._overlay and self._overlay.exists():
            merged = deep_merge(merged, json.loads(self._overlay.read_text(encoding="utf-8")))
        validate(merged)
        return merged

    def current(self) -> Rules:
        """Latest valid rules; reloads when either file changed on disk."""
        with self._lock:
            sigs = (_sig(self._base), _sig(self._overlay))
            if self._rules is None or sigs != self._sigs:
                try:
                    self._rules, self._sigs = self._read(), sigs
                    log.info("extraction rules (re)loaded")
                except Exception:
                    if self._rules is None:
                        raise
                    log.exception("invalid extraction rules on disk; keeping last good set")
                    self._sigs = sigs  # don't re-log every call until the file changes again
            return self._rules

    def promote(self, key_path: list[str], value: Any, actor: str) -> None:
        """Persist an approved fix, e.g. promote(['sources','KAMIS','fields','price'], 'dpr2', 'md@x').
        Validated against the full merged ruleset first; written atomically."""
        if not self._overlay:
            raise RuntimeError("no overlay path configured")
        with self._lock:
            over = json.loads(self._overlay.read_text(encoding="utf-8")) if self._overlay.exists() else {}
            node = over
            for k in key_path[:-1]:
                node = node.setdefault(k, {})
            node[key_path[-1]] = value
            validate(deep_merge(json.loads(self._base.read_text(encoding="utf-8")), over))  # raises -> not written
            tmp = self._overlay.with_suffix(".tmp")
            tmp.write_text(json.dumps(over, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._overlay)
            self._sigs = None
        log.info("rule promoted by %s: %s", actor, "/".join(key_path))


_default: RuleStore | None = None


def default_store() -> RuleStore:
    global _default
    if _default is None:
        base = Path(os.getenv("MARKETFLOW_RULES", ROOT / "config" / "extraction.json"))
        overlay = Path(os.getenv("MARKETFLOW_RULES_OVERLAY", base.with_name("overrides.json")))
        _default = RuleStore(base, overlay)
    return _default


def current_rules() -> Rules:
    return default_store().current()
