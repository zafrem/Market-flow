"""Operator CLI for ERP import and the recommendation workflow.

  python -m marketflow.cli import-erp FILE.csv|xlsx [--date YYYY-MM-DD]
  python -m marketflow.cli decide
  python -m marketflow.cli recs [--status PENDING]
  python -m marketflow.cli approve|reject|apply|revert ID --as NAME:ROLE      (ROLE: ADMIN | MD | BUYER)
  python -m marketflow.cli audit
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .db import init_sqlite, make_engine
from .decision import service
from .erp import import_erp
from .ops_repo import OpsRepository
from .settings import Settings, load_settings
from .time_provider import SystemTimeProvider, TimeProvider


def main(argv: list[str] | None = None, *, settings: Settings | None = None, tp: TimeProvider | None = None,
         out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="marketflow.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-erp"); p.add_argument("file"); p.add_argument("--date")
    sub.add_parser("decide")
    p = sub.add_parser("recs"); p.add_argument("--status")
    for name in ("approve", "reject", "apply", "revert"):
        p = sub.add_parser(name); p.add_argument("rec_id", type=int); p.add_argument("--as", dest="actor", required=True, metavar="NAME:ROLE")
    sub.add_parser("audit")
    a = ap.parse_args(argv)

    settings, tp = settings or load_settings(), tp or SystemTimeProvider()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        init_sqlite(engine)
    repo = OpsRepository(engine)

    if a.cmd == "import-erp":
        res = import_erp(a.file, repo, settings, tp, date.fromisoformat(a.date) if a.date else None)
        print(f"loaded={res.loaded} errors={len(res.errors)}", file=out)
        for e in res.errors:
            print(f"  line {e.line} [{e.field}] {e.message}", file=out)
        return 1 if res.errors and res.loaded == 0 else 0
    if a.cmd == "decide":
        made = service.run_decisions(repo, settings, tp)
        print(f"created={len(made)}", file=out)
        for rec_id, r in made:
            print(f"  #{rec_id} {r.status:<10} {r.internal_sku}/{r.channel_name} {r.current_price} -> {r.suggested_price} "
                  f"({r.reason_code}) {[k for k in r.flags if k.isupper()]}", file=out)
        return 0
    if a.cmd == "recs":
        for r in repo.list_recommendations(a.status):
            print(f"#{r['rec_id']} {r['status']:<10} item={r['std_item_id']} {r['channel_name']} "
                  f"{r['current_price']} -> {r['suggested_price']} ({r['reason_code']})", file=out)
        return 0
    if a.cmd == "audit":
        for e in repo.audit_entries():
            print(f"{e['ts']} {e['actor']} {e['action']} {e['before_state']} -> {e['after_state']}", file=out)
        return 0
    name, _, role = a.actor.partition(":")
    actor = service.Actor(name, role.upper())
    try:
        getattr(service, a.cmd)(repo, tp, a.rec_id, actor)
    except (PermissionError, service.WorkflowError) as e:
        print(f"refused: {e}", file=out)
        return 2
    print(f"{a.cmd} #{a.rec_id} ok", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
