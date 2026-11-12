"""FastAPI service: auth, role-based masking, three-way matrix, watchlists, recommendation workflow."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from .. import curation, matching
from ..promotions import add_promotion, list_promotions, remove_promotion
from ..quality import load_thresholds, quality_report
from ..snapshot import snapshot as take_snapshot
from ..db import init_sqlite, make_engine
from ..decision import service
from ..decision.service import Actor, WorkflowError
from ..crawlers.adapters import InboxCrawler
from ..crawlers.service import CrawlerService
from ..mi.classify import evaluate as classify_eval
from ..mi.importer import import_signals
from ..mi.service import mi_report
from ..mi.store import MiStore
from ..settings import Settings
from ..time_provider import SystemTimeProvider, TimeProvider
from .repo import AppRepository, Scope, in_scope
from .security import LoginThrottle, MIN_PASSWORD_LEN, MIN_SECRET_LEN, burn_verify, hash_password, make_token, read_token, verify_password

STATIC = Path(__file__).parent / "static"
COST_KEYS = {"purchase_cost", "fixed_cost", "logistics_cost", "channel_fee_rate", "promo_cost", "min_price_floor",
             "margin_amount", "margin_rate", "margin_trend", "disposal_cost", "match_simulation"}
MI_MAX_UPLOAD = 5 * 1024 * 1024
Role = Literal["ADMIN", "MD", "BUYER"]


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=256)


class DraftIn(BaseModel):
    internal_sku: str = Field(min_length=1, max_length=50)
    channel_name: str = Field(min_length=1, max_length=50)
    price: int = Field(gt=0, le=100_000_000)
    note: str = Field(default="", max_length=500)


class SynonymIn(BaseModel):
    canonical: str = Field(min_length=1, max_length=100)


class MatchIn(BaseModel):
    std_item_id: int
    grade: Literal["SAME", "SIMILAR", "SUBSTITUTE"] = "SAME"


class PromotionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_date: date
    end_date: date
    category_l: str = Field(default="", max_length=50)
    channel_name: str = Field(default="", max_length=50)
    discount_pct: float | None = None
    note: str = Field(default="", max_length=255)


def cat_ok(scope, category_l: str) -> bool:
    return scope is None or any(l == category_l for l, _ in scope)


class WatchlistIn(BaseModel):
    std_item_ids: list[int] = Field(max_length=500)


class PrefsIn(BaseModel):
    alert_margin_bep: Decimal = Field(ge=0, le=100)
    ui_layout: dict[str, Any] = Field(default_factory=dict)


class UserIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    name: str = Field(min_length=1, max_length=100)
    role: Role
    password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=256)


class AssignmentIn(BaseModel):
    category_l: str = Field(min_length=1, max_length=50)
    category_m: str = Field(default="", max_length=50)


class CrawlerPatch(BaseModel):
    enabled: bool | None = None
    times: list[str] | None = Field(default=None, max_length=12)
    config: dict[str, Any] | None = None


class UserPatch(BaseModel):
    active: bool | None = None
    role: Role | None = None


class CurrentUser(BaseModel):
    user_id: int
    email: str
    name: str
    role: str
    scope: Scope = None


def mask_internal(role: str, internal: dict[str, Any]) -> dict[str, Any]:
    """BUYER never receives cost sheet or margin fields (keys are omitted, not blanked)."""
    return internal if role != "BUYER" else {k: v for k, v in internal.items() if k not in COST_KEYS}


def mask_recommendation(role: str, rec: dict[str, Any]) -> dict[str, Any]:
    if role != "BUYER":
        return rec
    flags = {k: v for k, v in rec["flags"].items() if k not in {"LOW_MARGIN", "evidence"}}
    if "metrics" in flags:
        flags["metrics"] = {k: v for k, v in flags["metrics"].items() if k not in {"margin_amount", "margin_rate", "floor", "match_simulation"}}
    return {**rec, "flags": flags}


def create_app(settings: Settings, engine: Engine | None = None, tp: TimeProvider | None = None) -> FastAPI:
    if len(settings.jwt_secret) < MIN_SECRET_LEN:
        raise RuntimeError(f"auth.jwt_secret must be at least {MIN_SECRET_LEN} characters (config/settings.toml or JWT_SECRET)")
    tp = tp or SystemTimeProvider()
    engine = engine or make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        init_sqlite(engine)
    repo = AppRepository(engine)
    mi_store = MiStore(engine)
    crawlers = CrawlerService(engine, settings, tp)          # no transport: the API never crawls, it edits settings, queues runs and takes uploads
    throttle = LoginThrottle()
    app = FastAPI(title="MarketFlow", docs_url=None, redoc_url=None, openapi_url=None)

    def now() -> datetime:
        return tp.now().replace(tzinfo=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        if settings.hsts or settings.tls_cert:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    def current_user(request: Request) -> CurrentUser:
        header = request.headers.get("authorization", "")
        uid = read_token(settings.jwt_secret, header[7:], tp.now()) if header.lower().startswith("bearer ") else None
        user = repo.user_by_id(uid) if uid is not None else None
        if user is None or not user["active"]:           # role/active changes take effect immediately
            raise HTTPException(401, "invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"})
        return CurrentUser(**{k: user[k] for k in ("user_id", "email", "name", "role")}, scope=repo.scope_for(user))

    def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role != "ADMIN":
            raise HTTPException(403, "admin only")
        return user

    # ---- auth ---------------------------------------------------------------------------------
    @app.post("/auth/login")
    def login(body: LoginIn, request: Request):
        email, client = body.email.strip().lower(), request.client.host if request.client else "?"
        if throttle.blocked(email, client, now()):
            raise HTTPException(429, "too many failed attempts; try again later",
                                headers={"Retry-After": str(int(LoginThrottle.WINDOW.total_seconds()))})
        user = repo.user_by_email(email)
        if user is None or not user["active"]:
            burn_verify(body.password)
            ok = False
        else:
            ok = verify_password(body.password, user["password_hash"])
        if not ok:
            throttle.fail(email, client, now())
            raise HTTPException(401, "invalid email or password")
        throttle.succeed(email, client)
        return {"access_token": make_token(settings.jwt_secret, user["user_id"], tp.now(), settings.token_ttl_minutes),
                "token_type": "bearer", "expires_in": settings.token_ttl_minutes * 60,
                "user": {"email": user["email"], "name": user["name"], "role": user["role"]}}

    @app.get("/auth/me")
    def me(user: CurrentUser = Depends(current_user)):
        return {"email": user.email, "name": user.name, "role": user.role,
                "scope": None if user.scope is None else [{"category_l": l, "category_m": m} for l, m in user.scope]}

    # ---- matrix / watchlist / preferences ---------------------------------------------------------------
    @app.get("/matrix")
    def matrix(watchlist: bool = False, user: CurrentUser = Depends(current_user)):
        ids = repo.watchlist(user.user_id) if watchlist else None
        rows = repo.matrix(user.scope, settings, now(), ids)
        for r in rows:
            r["internal"] = mask_internal(user.role, r["internal"])
        return {"rows": rows}

    EXPORT_COLUMNS = [
        ("item", "item_name"), ("category", "category_l"), ("sku", "internal_sku"), ("channel", "channel_name"),
        ("data_date", "recorded_date"), ("our_price", None), ("competitor_lowest_effective_pack_price", None),
        ("confirmed_listings", None), ("unconfirmed_listings", None), ("wholesale_unit_price", None),
        ("net_margin_rate_pct", None), ("expected_margin_rate_pct_if_matched", None), ("bep_units_per_week", None),
        ("bep_status", None), ("matched_price_below_floor", None)]

    def csv_cell(v: Any) -> str:
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s       # no spreadsheet formula injection

    @app.get("/matrix/export")
    def matrix_export(watchlist: bool = False, user: CurrentUser = Depends(current_user)):
        """Price/margin comparison sheet as CSV (SRS F-08-4.1). Carries margins, so MD/ADMIN only; every export is audited."""
        if user.role not in service.DECIDERS:
            raise HTTPException(403, "MD or admin only")
        rows = repo.matrix(user.scope, settings, now(), repo.watchlist(user.user_id) if watchlist else None)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow([name for name, _ in EXPORT_COLUMNS])
        for r in rows:
            i, c, sim = r["internal"], r["competitors"], r["internal"].get("match_simulation") or {}
            w.writerow([csv_cell(v) for v in (
                r["item_name"], r["category_l"], r["internal_sku"], r["channel_name"], r["recorded_date"], i["current_selling_price"],
                c["lowest_pack_price"], c["listings"], c["unconfirmed"],
                (r["public_benchmark"] or {}).get("normalized_price"), i["margin_rate"], sim.get("expected_margin_rate"),
                sim.get("bep_units"), sim.get("bep_status"), sim.get("below_floor"))])
        repo.audit(user.email, "MATRIX_EXPORTED", None, {"rows": len(rows), "watchlist": watchlist}, now())
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="marketflow-comparison.csv"', "Cache-Control": "no-store"})

    @app.get("/watchlist")
    def get_watchlist(user: CurrentUser = Depends(current_user)):
        return {"std_item_ids": repo.watchlist(user.user_id)}

    @app.put("/watchlist")
    def put_watchlist(body: WatchlistIn, user: CurrentUser = Depends(current_user)):
        for item_id in body.std_item_ids:                # only items inside the caller's scope may be watched
            info = repo.item_scope_info(item_id)
            if info is None or not in_scope(user.scope, info["category_l"], info["category_m"]):
                raise HTTPException(404, f"item {item_id} not found")
        repo.set_watchlist(user.user_id, body.std_item_ids)
        return {"std_item_ids": repo.watchlist(user.user_id)}

    @app.get("/preferences")
    def get_prefs(user: CurrentUser = Depends(current_user)):
        return repo.preferences(user.user_id)

    @app.put("/preferences")
    def put_prefs(body: PrefsIn, user: CurrentUser = Depends(current_user)):
        repo.set_preferences(user.user_id, body.alert_margin_bep, body.ui_layout)
        return repo.preferences(user.user_id)

    # ---- recommendations ---------------------------------------------------------------------------------------
    @app.get("/recommendations")
    def recommendations(status: str | None = None, user: CurrentUser = Depends(current_user)):
        if status is not None and status not in {"PENDING", "APPROVED", "REJECTED", "APPLIED", "SUPPRESSED", "REVERTED"}:
            raise HTTPException(422, "unknown status")
        return {"recommendations": [mask_recommendation(user.role, r) for r in repo.recommendations_view(user.scope, status)]}

    @app.post("/recommendations/draft", status_code=201)
    def draft(body: DraftIn, user: CurrentUser = Depends(current_user)):
        """MD/ADMIN propose a price change from the comparison sheet. Below the defense line it needs an admin's approval."""
        if user.role not in service.DECIDERS:
            raise HTTPException(403, "MD or admin only")
        row = repo.inventory_latest(body.internal_sku, body.channel_name)
        if row is None or not in_scope(user.scope, row["category_l"], row["category_m"]):
            raise HTTPException(404, "SKU not found")
        try:
            rec_id = service.create_draft(repo, settings, tp, Actor(user.email, user.role), body.internal_sku, body.channel_name, body.price, body.note)
        except WorkflowError as e:
            raise HTTPException(409, str(e)) from e
        rec = repo.get_recommendation(rec_id)
        return {"rec_id": rec_id, "status": rec["status"], "requires_admin": bool(rec["flags"].get("REQUIRES_ADMIN"))}

    @app.post("/recommendations/{rec_id}/{action}")
    def act(rec_id: int, action: Literal["approve", "reject", "apply", "revert"], user: CurrentUser = Depends(current_user)):
        rec = repo.get_recommendation(rec_id)
        info = repo.item_scope_info(rec["std_item_id"]) if rec else None
        if rec is None or info is None or not in_scope(user.scope, info["category_l"], info["category_m"]):
            raise HTTPException(404, "recommendation not found")       # out-of-scope looks the same as absent
        try:
            if action == "approve":
                service.approve(repo, tp, rec_id, Actor(user.email, user.role), separate_duties=not settings.solo_mode)
            else:
                getattr(service, action)(repo, tp, rec_id, Actor(user.email, user.role))
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except WorkflowError as e:
            raise HTTPException(409, str(e)) from e
        return {"rec_id": rec_id, "status": repo.get_recommendation(rec_id)["status"]}

    # ---- curation (MD / ADMIN) ---------------------------------------------------------------------------------
    def require_decider(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role not in service.DECIDERS:
            raise HTTPException(403, "MD or admin only")
        return user

    def run_curation(fn, item_id: int, user: CurrentUser) -> dict[str, Any]:
        try:
            fn(repo, tp, item_id, Actor(user.email, user.role))
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except WorkflowError as e:
            raise HTTPException(409, str(e)) from e
        return {"id": item_id, "ok": True}

    @app.get("/snapshot")
    def point_in_time(date: str, known_at: str | None = None, user: CurrentUser = Depends(require_decider)):
        """Cost, price and margin of one day as the system knew them at `known_at` (UTC, ISO). Without it: as known now."""
        try:
            day = datetime.fromisoformat(date).date()
            moment = datetime.fromisoformat(known_at).replace(tzinfo=None) if known_at else None
        except ValueError as e:
            raise HTTPException(422, "date and known_at must be ISO formatted (YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS UTC)") from e
        rows = [r for r in take_snapshot(repo, settings, day, moment) if in_scope(user.scope, r["category_l"], r["category_m"])]
        repo.audit(user.email, "SNAPSHOT_READ", None, {"date": str(day), "known_at": known_at, "rows": len(rows)}, now())
        return {"date": str(day), "known_at": known_at, "rows": rows}

    # ---- product matches: which competitor listings count as "the same product" (MD / ADMIN) --------------------
    @app.get("/matches")
    def matches(unconfirmed: bool = False, user: CurrentUser = Depends(require_decider)):
        return {"matches": [{**m, "match_confidence": str(m["match_confidence"]), "reviewed_at": str(m["reviewed_at"]) if m["reviewed_at"] else None,
                             "confirmed": m["reviewed_at"] is not None and m["match_grade"] == "SAME"}
                            for m in repo.list_matches(None, unconfirmed) if in_scope(user.scope, m["category_l"], m["category_m"])]}

    @app.put("/matches/{platform}/{code}")
    def put_match(platform: str, code: str, body: MatchIn, user: CurrentUser = Depends(require_decider)):
        existing = repo.get_match(platform, code)
        for item_id in {body.std_item_id, *([existing["std_item_id"]] if existing else [])}:
            info = repo.item_scope_info(item_id)
            if info is None or not in_scope(user.scope, info["category_l"], info["category_m"]):
                raise HTTPException(404, "item not found")
        try:
            matching.set_match(repo, tp, Actor(user.email, user.role), platform, code, body.std_item_id, body.grade)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except WorkflowError as e:
            raise HTTPException(409, str(e)) from e
        return {"platform": platform, "code": code, "std_item_id": body.std_item_id, "grade": body.grade, "confirmed": body.grade == "SAME"}

    @app.delete("/matches/{platform}/{code}")
    def delete_match(platform: str, code: str, user: CurrentUser = Depends(require_decider)):
        m = repo.get_match(platform, code)
        if m is None or not in_scope(user.scope, m["category_l"], m["category_m"]):
            raise HTTPException(404, "match not found")
        try:
            matching.remove_match(repo, tp, Actor(user.email, user.role), platform, code)
        except WorkflowError as e:
            raise HTTPException(409, str(e)) from e
        return {"platform": platform, "code": code, "removed": True}

    @app.get("/review")
    def review_items(_: CurrentUser = Depends(require_decider)):
        return {"items": repo.list_review("OPEN")}

    @app.post("/review/{review_id}/resolve")
    def resolve(review_id: int, user: CurrentUser = Depends(require_decider)):
        return run_curation(curation.resolve_review, review_id, user)

    @app.get("/selectors")
    def selector_candidates(_: CurrentUser = Depends(require_decider)):
        return {"candidates": repo.list_candidates(None)}

    @app.post("/selectors/{cand_id}/{action}")
    def decide_selector(cand_id: int, action: Literal["approve", "reject"], user: CurrentUser = Depends(require_decider)):
        return run_curation(curation.approve_selector if action == "approve" else curation.reject_selector, cand_id, user)

    # ---- market intelligence (no cost data; filtered to the caller's categories) ---------------------------------------
    @app.get("/mi/report")
    def mi(include_stable: bool = False, lang: Literal["en", "ko"] = "en", user: CurrentUser = Depends(current_user)):
        allowed = None if user.scope is None else {l for l, _ in user.scope}
        return mi_report(mi_store, settings, allowed, include_stable, lang, now())

    @app.post("/mi/import")
    async def mi_import(request: Request, signal_type: str, source: str = "", admin: CurrentUser = Depends(require_admin)):
        """Body: the CSV text (UTF-8 or CP949). Admin only; capped at MI_MAX_UPLOAD bytes."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MI_MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MI_MAX_UPLOAD:
                raise HTTPException(413, "file too large")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "upload.csv"
            f.write_bytes(bytes(body))
            try:
                res = import_signals(f, mi_store, signal_type, source)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        repo.audit(admin.email, "MI_IMPORT", None, {"signal_type": signal_type.upper(), "loaded": res.loaded, "errors": len(res.errors), "pii_skipped": res.pii_skipped}, now())
        return {"loaded": res.loaded, "pii_skipped": res.pii_skipped, "classified": res.classified, "unclassified": res.unclassified, "errors": [{"line": e.line, "field": e.field, "message": e.message} for e in res.errors[:50]],
                "error_count": len(res.errors)}

    @app.get("/promotions")
    def promotions_list(user: CurrentUser = Depends(require_decider)):
        today = now().date()
        return {"today": today.isoformat(), "promotions": [p for p in list_promotions(repo, today) if p["category_l"] == "" or cat_ok(user.scope, p["category_l"])]}

    @app.post("/promotions", status_code=201)
    def promotions_add(body: PromotionIn, user: CurrentUser = Depends(require_decider)):
        if body.category_l and not cat_ok(user.scope, body.category_l):
            raise HTTPException(404, "category not found")
        if not body.category_l and user.role != "ADMIN":
            raise HTTPException(403, "only an administrator can record a promotion for every category")
        try:
            pid = add_promotion(repo, Actor(user.email, user.role), now(), body.name, body.start_date, body.end_date, body.category_l, body.channel_name,
                                body.discount_pct, body.note)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"promo_id": pid}

    @app.delete("/promotions/{promo_id}")
    def promotions_remove(promo_id: int, user: CurrentUser = Depends(require_decider)):
        row = repo._rows("SELECT category_l FROM promotions WHERE promo_id=:i", i=promo_id)
        if not row or (row[0]["category_l"] and not cat_ok(user.scope, row[0]["category_l"])) or (not row[0]["category_l"] and user.role != "ADMIN"):
            raise HTTPException(404, "promotion not found")
        remove_promotion(repo, Actor(user.email, user.role), now(), promo_id)
        return {"ok": True}

    @app.get("/quality")
    def data_quality(_: CurrentUser = Depends(require_decider)):
        return quality_report(repo, crawlers, load_thresholds(settings.quality), now())

    # ---- keyword dictionary: synonyms (admin edits, MD/ADMIN read) and the category classifier -------------------------------
    @app.get("/mi/synonyms")
    def synonyms(_: CurrentUser = Depends(require_decider)):
        return {"synonyms": [{**s, "updated_at": str(s["updated_at"]) if s["updated_at"] else None, "affected_keywords": mi_store.synonym_impact(s["alias"])}
                             for s in mi_store.list_synonyms()]}

    @app.put("/mi/synonyms/{alias}")
    def put_synonym(alias: str, body: SynonymIn, admin: CurrentUser = Depends(require_admin)):
        """Takes effect in the next report/batch; the response says how many stored keyword series it folds."""
        before = mi_store.synonyms().get(alias.strip().lower())
        try:
            mi_store.set_synonym(alias, body.canonical, admin.email, now())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        impact = mi_store.synonym_impact(alias)
        repo.audit(admin.email, "SYNONYM_SET", {"alias": alias, "canonical": before}, {"alias": alias, "canonical": body.canonical, "affected_keywords": impact}, now())
        return {"alias": alias, "canonical": body.canonical, "affected_keywords": impact}

    @app.delete("/mi/synonyms/{alias}")
    def delete_synonym(alias: str, admin: CurrentUser = Depends(require_admin)):
        before = mi_store.synonyms().get(alias.strip().lower())
        if before is None or not mi_store.remove_synonym(alias):
            raise HTTPException(404, "synonym not found")
        repo.audit(admin.email, "SYNONYM_REMOVED", {"alias": alias, "canonical": before}, None, now())
        return {"alias": alias, "removed": True}

    @app.get("/mi/classifier")
    def classifier_quality(_: CurrentUser = Depends(require_admin)):
        """How well keyword -> category guessing does on keywords that already have a category (leave-one-out)."""
        return classify_eval(*mi_store.classifier_knowledge())

    # ---- crawlers: one per data item, each with its own settings, schedule and run history --------------------------------
    def crawler_or_404(cid: str):
        if cid not in crawlers.registry:
            raise HTTPException(404, "crawler not found")
        return crawlers.registry[cid]

    @app.get("/crawlers")
    def list_crawlers(_: CurrentUser = Depends(require_decider)):
        return {"crawlers": crawlers.list()}

    @app.get("/crawlers/{cid}")
    def crawler_detail(cid: str, _: CurrentUser = Depends(require_decider)):
        crawler_or_404(cid)
        return crawlers.detail(cid)

    @app.put("/crawlers/{cid}")
    def update_crawler(cid: str, body: CrawlerPatch, admin: CurrentUser = Depends(require_admin)):
        crawler_or_404(cid)
        before = crawlers.effective(crawlers.registry[cid])
        try:
            crawlers.update(cid, admin.email, body.enabled, body.times, body.config)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        repo.audit(admin.email, "CRAWLER_UPDATED", {"crawler": cid, "enabled": before["enabled"], "times": before["times"]},
                   {"crawler": cid, **body.model_dump(exclude_none=True)}, now())
        return crawlers.detail(cid)

    @app.post("/crawlers/{cid}/run", status_code=202)
    def run_crawler(cid: str, admin: CurrentUser = Depends(require_admin)):
        crawler_or_404(cid)
        run_id = crawlers.store.queue_run(cid, admin.email, now())
        if run_id is None:
            raise HTTPException(409, "this crawler already has a run queued or in progress")
        repo.audit(admin.email, "CRAWLER_RUN_QUEUED", None, {"crawler": cid, "run_id": run_id}, now())
        return {"run_id": run_id, "status": "QUEUED"}

    @app.post("/crawlers/{cid}/upload")
    async def upload_to_crawler(cid: str, request: Request, filename: str = "", admin: CurrentUser = Depends(require_admin)):
        """File-inbox crawlers only: stores the file in the crawler's inbox and imports it now."""
        c = crawler_or_404(cid)
        ext = Path(filename).suffix.lower()
        if not isinstance(c, InboxCrawler):
            raise HTTPException(422, "this crawler does not take file uploads")
        if ext not in {".csv", ".xlsx"}:
            raise HTTPException(422, "upload a .csv or .xlsx file")
        if (declared := request.headers.get("content-length")) and declared.isdigit() and int(declared) > MI_MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MI_MAX_UPLOAD:
                raise HTTPException(413, "file too large")
        c.ensure_dirs()
        import uuid                                                   # the client's file name is never used on disk
        (c.dir / f"upload-{now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}{ext}").write_bytes(bytes(body))
        repo.audit(admin.email, "CRAWLER_UPLOAD", None, {"crawler": cid, "bytes": len(body)}, now())
        result = await crawlers.run(cid, "MANUAL", admin.email)
        if result is None:
            raise HTTPException(409, "the crawler is running; the file will be imported on its next run")
        return result

    # ---- admin --------------------------------------------------------------------------------------------------------
    @app.get("/admin/users")
    def list_users(_: CurrentUser = Depends(require_admin)):
        return {"users": repo.list_users()}

    @app.post("/admin/users", status_code=201)
    def create_user(body: UserIn, admin: CurrentUser = Depends(require_admin)):
        if repo.user_by_email(body.email):
            raise HTTPException(409, "email already registered")
        uid = repo.create_user(body.email, body.name, body.role, hash_password(body.password))
        repo.audit(admin.email, "USER_CREATED", None, {"user_id": uid, "role": body.role}, now())
        return {"user_id": uid}

    @app.patch("/admin/users/{uid}")
    def patch_user(uid: int, body: UserPatch, admin: CurrentUser = Depends(require_admin)):
        target = repo.user_by_id(uid)
        if target is None:
            raise HTTPException(404, "user not found")
        if uid == admin.user_id and (body.active is False or (body.role and body.role != "ADMIN")):
            raise HTTPException(409, "admins cannot demote or deactivate themselves")
        if not repo.update_user(uid, body.role, body.active):
            raise HTTPException(409, "refused: this would leave no active administrator")
        repo.audit(admin.email, "USER_UPDATED", {"role": target["role"], "active": bool(target["active"])},
                   body.model_dump(exclude_none=True), now())
        return {"ok": True}

    @app.put("/admin/users/{uid}/assignments")
    def assign(uid: int, body: list[AssignmentIn], admin: CurrentUser = Depends(require_admin)):
        if repo.user_by_id(uid) is None:
            raise HTTPException(404, "user not found")
        repo.set_assignments(uid, [(a.category_l, a.category_m) for a in body])
        repo.audit(admin.email, "ASSIGNMENTS_SET", None, {"user_id": uid, "count": len(body)}, now())
        return {"ok": True}

    @app.get("/admin/audit")
    def audit(_: CurrentUser = Depends(require_admin)):
        return {"entries": repo.audit_entries()}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
