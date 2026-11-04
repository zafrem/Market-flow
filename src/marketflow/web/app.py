"""FastAPI service: auth, role-based masking, three-way matrix, watchlists, recommendation workflow."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from .. import curation
from ..db import init_sqlite, make_engine
from ..decision import service
from ..decision.service import Actor, WorkflowError
from ..settings import Settings
from ..time_provider import SystemTimeProvider, TimeProvider
from .repo import AppRepository, Scope, in_scope
from .security import LoginThrottle, MIN_PASSWORD_LEN, MIN_SECRET_LEN, burn_verify, hash_password, make_token, read_token, verify_password

STATIC = Path(__file__).parent / "static"
COST_KEYS = {"purchase_cost", "fixed_cost", "logistics_cost", "channel_fee_rate", "promo_cost", "min_price_floor",
             "margin_amount", "margin_rate", "margin_trend"}
Role = Literal["ADMIN", "MD", "BUYER"]


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=256)


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
    flags = {k: v for k, v in rec["flags"].items() if k != "LOW_MARGIN"}
    if "metrics" in flags:
        flags["metrics"] = {k: v for k, v in flags["metrics"].items() if k not in {"margin_amount", "margin_rate", "floor"}}
    return {**rec, "flags": flags}


def create_app(settings: Settings, engine: Engine | None = None, tp: TimeProvider | None = None) -> FastAPI:
    if len(settings.jwt_secret) < MIN_SECRET_LEN:
        raise RuntimeError(f"auth.jwt_secret must be at least {MIN_SECRET_LEN} characters (config/settings.toml or JWT_SECRET)")
    tp = tp or SystemTimeProvider()
    engine = engine or make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        init_sqlite(engine)
    repo = AppRepository(engine)
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

    @app.post("/recommendations/{rec_id}/{action}")
    def act(rec_id: int, action: Literal["approve", "reject", "apply", "revert"], user: CurrentUser = Depends(current_user)):
        rec = repo.get_recommendation(rec_id)
        info = repo.item_scope_info(rec["std_item_id"]) if rec else None
        if rec is None or info is None or not in_scope(user.scope, info["category_l"], info["category_m"]):
            raise HTTPException(404, "recommendation not found")       # out-of-scope looks the same as absent
        try:
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
