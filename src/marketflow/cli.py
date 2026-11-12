"""Operator CLI for ERP import and the recommendation workflow.

  python -m marketflow.cli import-erp FILE.csv|xlsx [--date YYYY-MM-DD]
  python -m marketflow.cli decide
  python -m marketflow.cli recs [--status PENDING]
  python -m marketflow.cli approve|reject|apply|revert ID --as NAME:ROLE      (ROLE: ADMIN | MD | BUYER)
  python -m marketflow.cli export-prices [--since YYYY-MM-DD] [--file OUT.csv]   (applied price changes, for channel bulk upload)
  python -m marketflow.cli import-mi FILE --type SEARCH|BUYER|SKU|PRODUCT|PROMOTION|MEDIA [--source NAME]   (market-intelligence signals)
  python -m marketflow.cli mi-report [--stable] [--lang en|ko]                     (category health + trend radar)
  python -m marketflow.cli crawlers                                 (every crawler: state, schedule, last run)
  python -m marketflow.cli crawler-run ID                           (run one crawler now, in this process)
  python -m marketflow.cli audit
  python -m marketflow.cli review | resolve ID --as NAME:ROLE        (items the machine could not decide: unmatched/unparseable/quarantined)
  python -m marketflow.cli selectors | approve-selector ID | reject-selector ID --as NAME:ROLE
  python -m marketflow.cli alert-test                                 (sends a test message to alerts.webhook_url)
  python -m marketflow.cli add-user EMAIL NAME ADMIN|MD|BUYER     (prompts for password)
  python -m marketflow.cli assign EMAIL Fresh/Fruit [Fresh/Vegetable ...]
  python -m marketflow.web [--port 8000]                           (API + dashboard; needs auth.jwt_secret)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time

from .db import init_sqlite, make_engine
from .decision import service
from .erp import import_erp
from . import curation
from .ops_repo import Repo
from .settings import Settings, load_settings
from .time_provider import SystemTimeProvider, TimeProvider, today_kst


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
    p = sub.add_parser("export-prices"); p.add_argument("--since"); p.add_argument("--file")
    p = sub.add_parser("import-mi"); p.add_argument("--errors-file", help="write every rejected cell with its reason to this CSV"); p.add_argument("file"); p.add_argument("--type", required=True); p.add_argument("--source", default="")
    p = sub.add_parser("synonym"); p.add_argument("alias"); p.add_argument("canonical", nargs="?"); p.add_argument("--remove", action="store_true")
    p = sub.add_parser("snapshot"); p.add_argument("date"); p.add_argument("--known-at", help="UTC ISO time: what the system knew then")
    sub.add_parser("synonyms"); sub.add_parser("classify-eval")
    p = sub.add_parser("mi-report"); p.add_argument("--stable", action="store_true"); p.add_argument("--lang", choices=["en", "ko"], default="en")
    sub.add_parser("crawlers")
    p = sub.add_parser("crawler-run"); p.add_argument("crawler_id")
    sub.add_parser("audit")
    p = sub.add_parser("matches"); p.add_argument("--unconfirmed", action="store_true")
    p = sub.add_parser("match"); p.add_argument("platform"); p.add_argument("code"); p.add_argument("std_item_id", type=int)
    p.add_argument("--grade", choices=["SAME", "SIMILAR", "SUBSTITUTE"], default="SAME"); p.add_argument("--as", dest="actor", required=True, metavar="NAME:ROLE")
    p = sub.add_parser("unmatch"); p.add_argument("platform"); p.add_argument("code"); p.add_argument("--as", dest="actor", required=True, metavar="NAME:ROLE")
    sub.add_parser("review")
    sub.add_parser("selectors")
    sub.add_parser("alert-test")
    for name in ("resolve", "approve-selector", "reject-selector"):
        p = sub.add_parser(name); p.add_argument("id", type=int); p.add_argument("--as", dest="actor", required=True, metavar="NAME:ROLE")
    p = sub.add_parser("add-user"); p.add_argument("email"); p.add_argument("name"); p.add_argument("role", choices=["ADMIN", "MD", "BUYER"])
    p = sub.add_parser("assign"); p.add_argument("email"); p.add_argument("category", metavar="CATEGORY_L[/CATEGORY_M]", nargs="+")
    a = ap.parse_args(argv)

    settings, tp = settings or load_settings(), tp or SystemTimeProvider()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        init_sqlite(engine)
    repo = Repo(engine)

    if a.cmd == "review":
        for r in repo.list_review("OPEN"):
            print(f"#{r['review_id']} {r['kind']:<24} {r['source']:<8} {r['item_key']}  -- {r['reason']}", file=out)
        return 0
    if a.cmd == "matches":
        for m in repo.list_matches(None, a.unconfirmed):
            state = "confirmed" if m["reviewed_at"] and m["match_grade"] == "SAME" else ("graded" if m["reviewed_at"] else "UNCONFIRMED")
            print(f"{m['platform']}/{m['channel_item_code']:<14} -> #{m['std_item_id']} {m['item_name']:<12} {m['match_grade']:<10} {state:<11} "
                  f"{m['product_title'] or ''}", file=out)
        return 0
    if a.cmd in {"match", "unmatch"}:
        from . import matching
        name, _, role = a.actor.partition(":")
        try:
            if a.cmd == "match":
                matching.set_match(repo, tp, service.Actor(name, role.upper()), a.platform, a.code, a.std_item_id, a.grade)
            else:
                matching.remove_match(repo, tp, service.Actor(name, role.upper()), a.platform, a.code)
        except (PermissionError, service.WorkflowError) as e:
            print(f"refused: {e}", file=out)
            return 2
        print(f"{a.cmd} {a.platform}/{a.code} ok", file=out)
        return 0
    if a.cmd == "selectors":
        for c in repo.list_candidates(None):
            print(f"#{c['cand_id']} {c['status']:<8} {c['platform']} item={c['selectors']['item']!r} "
                  f"title={c['selectors']['fields']['title']['css']!r} sale_price={c['selectors']['fields']['sale_price']['css']!r}", file=out)
        return 0
    if a.cmd == "alert-test":
        import asyncio
        from .notify import Alert, Notifier
        res = asyncio.run(Notifier(settings.alert_webhook_url, repo, tp).dispatch(
            [Alert("TEST:" + tp.now().isoformat(), "INFO", "MarketFlow test alert")]))
        print(f"sent={res.sent} dead={res.dead} no_webhook={res.skipped_no_webhook}", file=out)
        return 0 if res.dead == 0 else 1
    if a.cmd in {"resolve", "approve-selector", "reject-selector"}:
        name, _, role = a.actor.partition(":")
        fn = {"resolve": curation.resolve_review, "approve-selector": curation.approve_selector,
              "reject-selector": curation.reject_selector}[a.cmd]
        try:
            fn(repo, tp, a.id, service.Actor(name, role.upper()))
        except (PermissionError, service.WorkflowError) as e:
            print(f"refused: {e}", file=out)
            return 2
        print(f"{a.cmd} #{a.id} ok", file=out)
        return 0
    if a.cmd == "add-user":
        import getpass
        from .web.repo import AppRepository
        from .web.security import MIN_PASSWORD_LEN, hash_password
        pw = getpass.getpass("Password: ")
        if len(pw) < MIN_PASSWORD_LEN or pw != getpass.getpass("Repeat: "):
            print(f"refused: password must match and be >= {MIN_PASSWORD_LEN} characters", file=out)
            return 2
        users = AppRepository(engine)
        if users.user_by_email(a.email):
            print("refused: email already registered", file=out)
            return 2
        print(f"created user #{users.create_user(a.email, a.name, a.role, hash_password(pw))}", file=out)
        return 0
    if a.cmd == "assign":
        from .web.repo import AppRepository
        users = AppRepository(engine)
        u = users.user_by_email(a.email)
        if u is None:
            print("refused: no such user", file=out)
            return 2
        users.set_assignments(u["user_id"], [tuple((c.split("/", 1) + [""])[:2]) for c in a.category])
        print(f"assigned {a.category} to {a.email}", file=out)
        return 0
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
    if a.cmd == "export-prices":
        import csv
        since = datetime.combine(date.fromisoformat(a.since) if a.since else today_kst(tp), time.min)
        rows = service.price_changes(repo, since)
        cols = ["internal_sku", "channel_name", "new_price", "previous_price", "rec_id", "applied_at", "applied_by"]
        fh = open(a.file, "w", newline="", encoding="utf-8-sig") if a.file else out
        try:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n"); w.writeheader(); w.writerows(rows)
        finally:
            if a.file:
                fh.close()
        print(f"exported {len(rows)} price change(s)", file=sys.stderr if not a.file else out)
        return 0
    if a.cmd == "import-mi":
        from .mi.importer import import_signals
        from .mi.store import MiStore
        try:
            res = import_signals(a.file, MiStore(engine), a.type, a.source or a.file.rsplit("/", 1)[-1])
        except ValueError as e:
            print(f"refused: {e}", file=out)
            return 2
        print(f"loaded={res.loaded} errors={len(res.errors)} pii_skipped={res.pii_skipped} classified={res.classified} unclassified={res.unclassified}", file=out)
        for e in res.errors[:50]:
            print(f"  line {e.line} [{e.field}] {e.message}", file=out)
        if a.errors_file and res.errors:
            import csv as _csv
            with open(a.errors_file, "w", newline="", encoding="utf-8-sig") as fh:
                w = _csv.writer(fh); w.writerow(["line", "field", "reason"])
                w.writerows((e.line, e.field, e.message) for e in res.errors)
            print(f"rejected cells written to {a.errors_file}", file=out)
        return 1 if res.errors and res.loaded == 0 else 0
    if a.cmd == "snapshot":
        from .snapshot import snapshot
        try:
            rows = snapshot(repo, settings, date.fromisoformat(a.date), datetime.fromisoformat(a.known_at).replace(tzinfo=None) if a.known_at else None)
        except ValueError as e:
            print(f"refused: {e}", file=out); return 2
        for r in rows:
            print(f"{r['recorded_date']} {r['internal_sku']}/{r['channel_name']} price={r['current_selling_price']} cost={r['purchase_cost']} "
                  f"margin={r['margin_rate']}% (known since {r['known_since']})", file=out)
        return 0
    if a.cmd in {"synonym", "synonyms", "classify-eval"}:
        from .mi.classify import evaluate
        from .mi.store import MiStore
        ms = MiStore(engine)
        if a.cmd == "synonyms":
            for s in ms.list_synonyms():
                print(f"{s['alias']} -> {s['canonical']}  (affects {ms.synonym_impact(s['alias'])} keyword series)", file=out)
        elif a.cmd == "classify-eval":
            print(evaluate(*ms.classifier_knowledge()), file=out)
        elif a.remove:
            print("removed" if ms.remove_synonym(a.alias) else "refused: no such synonym", file=out)
        else:
            if not a.canonical:
                print("refused: give the canonical keyword", file=out); return 2
            try:
                ms.set_synonym(a.alias, a.canonical, "cli", tp.now().replace(tzinfo=None))
            except ValueError as e:
                print(f"refused: {e}", file=out); return 2
            print(f"{a.alias} -> {a.canonical}  (affects {ms.synonym_impact(a.alias)} keyword series)", file=out)
        return 0
    if a.cmd == "mi-report":
        from .mi.service import mi_report
        from .mi.store import MiStore
        rep = mi_report(MiStore(engine), settings, None, a.stable, a.lang)
        print(f"as of {rep['as_of']}  signals: {', '.join(rep['signals']) or 'none'}", file=out)
        print("\nCATEGORY HEALTH", file=out)
        for h in rep["health"]:
            print(f"  {h['category']:<14} {h['demand_state']:<10} {h['changed']}\n      why: {h['why']}\n      do:  {h['action']}", file=out)
        print("\nTREND RADAR", file=out)
        for r in rep["radar"]:
            print(f"  {r['keyword']:<14} {r['stage']:<10} score={r['score']:<5} opp={r['opportunity']:<5} [{r['category']}] {r['changed']}\n      why: {r['why']}\n      do:  {r['action']}", file=out)
        return 0
    if a.cmd in {"crawlers", "crawler-run"}:
        import asyncio
        from .crawlers.service import CrawlerService
        from .llm import get_llm
        from .transport import HttpTransport
        transport = HttpTransport() if a.cmd == "crawler-run" else None
        svc = CrawlerService(engine, settings, tp, transport, lambda: get_llm(settings))
        if a.cmd == "crawlers":
            for c in svc.list():
                last = c["last_run"]
                print(f"{c['id']:<16} {c['state']:<15} {','.join(c['times']):<22} last: " + (f"{last['status']} {last['finished_at'] or last['queued_at']} {last['message'][:60]}" if last else "never"), file=out)
            return 0
        if a.crawler_id not in svc.registry:
            print(f"refused: unknown crawler (try: {', '.join(svc.registry)})", file=out)
            return 2

        async def go():
            try:
                return await svc.run(a.crawler_id, "MANUAL", "cli")
            finally:
                await transport.aclose()
        res = asyncio.run(go())
        print("already running" if res is None else f"{res['status']} stored={res['stored']} {res['message']}", file=out)
        return 0 if res and res["status"] in {"OK", "SKIPPED"} else 1
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
