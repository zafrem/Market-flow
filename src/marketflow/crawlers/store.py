"""Crawler settings and run history. Claiming a run is one compare-and-set UPDATE, so two processes never run the same crawler twice."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from ..db import changed, insert_id
from ..ops_repo import OpsRepository, _dt

STALE_AFTER = timedelta(hours=2)          # a RUNNING row older than this belongs to a crashed process


class CrawlerStore(OpsRepository):
    # ---- settings ----------------------------------------------------------------------------
    def get_settings(self, cid: str) -> dict[str, Any]:
        r = self._rows("SELECT * FROM crawler_settings WHERE crawler_id = :c", c=cid)
        if not r:
            return {"enabled": True, "times": None, "config": {}, "last_slot": None}
        row = r[0]
        load = lambda v: json.loads(v) if isinstance(v, str) else v         # noqa: E731
        return {"enabled": bool(row["enabled"]), "times": load(row["times"]), "config": load(row["config"]) or {}, "last_slot": _dt(row["last_slot"])}

    def save_settings(self, cid: str, enabled: bool, times: list[str] | None, config: dict[str, Any], by: str, now: datetime) -> None:
        params = {"c": cid, "e": int(enabled), "t": json.dumps(times) if times is not None else None, "cfg": json.dumps(config, ensure_ascii=False), "u": by, "n": now}
        with self._e.begin() as c:
            if changed(c, "UPDATE crawler_settings SET enabled=:e, times=:t, config=:cfg, updated_by=:u, updated_at=:n WHERE crawler_id=:c", self._b(params)) == 0:
                c.execute(text("INSERT INTO crawler_settings (crawler_id, enabled, times, config, updated_by, updated_at) VALUES (:c,:e,:t,:cfg,:u,:n)"), self._b(params))

    def set_last_slot(self, cid: str, slot: datetime) -> None:
        with self._e.begin() as c:
            if changed(c, "UPDATE crawler_settings SET last_slot=:s WHERE crawler_id=:c", self._b({"s": slot, "c": cid})) == 0:
                c.execute(text("INSERT INTO crawler_settings (crawler_id, enabled, last_slot) VALUES (:c, 1, :s)"), self._b({"s": slot, "c": cid}))

    # ---- runs --------------------------------------------------------------------------------
    def queue_run(self, cid: str, by: str, now: datetime) -> int | None:
        """A manual run request; None when one is already queued or running for this crawler."""
        with self._e.begin() as c:
            if c.execute(text("SELECT 1 FROM crawler_runs WHERE crawler_id=:c AND status IN ('QUEUED','RUNNING')"), {"c": cid}).first():
                return None
            return insert_id(c, "INSERT INTO crawler_runs (crawler_id, trigger_kind, requested_by, status, queued_at) VALUES (:c,'MANUAL',:b,'QUEUED',:n)",
                             self._b({"c": cid, "b": by, "n": now}), "run_id")

    def start_run(self, cid: str, trigger: str, by: str, now: datetime, run_id: int | None = None) -> int | None:
        """Claim a run: promotes a QUEUED row (CAS) or inserts a RUNNING one. None if the crawler is already running."""
        with self._e.begin() as c:
            if c.execute(text("SELECT 1 FROM crawler_runs WHERE crawler_id=:c AND status='RUNNING'"), {"c": cid}).first():
                return None
            if run_id is not None:        # promote our QUEUED row; the status check makes it a compare-and-set
                ok = changed(c, "UPDATE crawler_runs SET status='RUNNING', started_at=:n WHERE run_id=:r AND status='QUEUED'",
                             self._b({"n": now, "r": run_id}))
                return run_id if ok else None
            return insert_id(c, "INSERT INTO crawler_runs (crawler_id, trigger_kind, requested_by, status, queued_at, started_at) VALUES (:c,:t,:b,'RUNNING',:n,:n)",
                             self._b({"c": cid, "t": trigger, "b": by, "n": now}), "run_id")

    def finish_run(self, run_id: int, status: str, stored: int, message: str, now: datetime) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE crawler_runs SET status=:s, finished_at=:n, rows_loaded=:k, message=:m WHERE run_id=:r"),
                      self._b({"s": status, "n": now, "k": stored, "m": message[:500], "r": run_id}))

    def queued_runs(self) -> list[dict[str, Any]]:
        return self._rows("SELECT run_id, crawler_id, requested_by FROM crawler_runs WHERE status='QUEUED' ORDER BY run_id")

    def reap_stale(self, now: datetime) -> int:
        with self._e.begin() as c:
            return changed(c, "UPDATE crawler_runs SET status='FAILED', finished_at=:n, message='interrupted (the worker stopped mid-run)' "
                              "WHERE status='RUNNING' AND started_at < :cut", self._b({"n": now, "cut": now - STALE_AFTER}))

    def runs(self, cid: str, limit: int = 20) -> list[dict[str, Any]]:
        out = self._rows("SELECT run_id, trigger_kind, requested_by, status, queued_at, started_at, finished_at, rows_loaded, message "
                         "FROM crawler_runs WHERE crawler_id=:c ORDER BY run_id DESC LIMIT :l", c=cid, l=limit)
        for r in out:
            r["stored"] = r.pop("rows_loaded")                     # `stored` is a reserved word in MySQL, so only the API uses it
            for k in ("queued_at", "started_at", "finished_at"):
                r[k] = _dt(r[k]).isoformat() + "Z" if r[k] else None          # naive UTC -> explicit UTC for the browser
        return out

    def last_run_per_crawler(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self._rows("SELECT crawler_id, MAX(run_id) AS r FROM crawler_runs GROUP BY crawler_id"):
            out[r["crawler_id"]] = self.runs(r["crawler_id"], 1)[0]
        return out

    def last_success(self, cid: str) -> str | None:
        r = self._rows("SELECT MAX(finished_at) AS t FROM crawler_runs WHERE crawler_id=:c AND status='OK'", c=cid)
        return (_dt(r[0]["t"]).isoformat() + "Z") if r and r[0]["t"] else None
