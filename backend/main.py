"""UVeye Quality Intelligence API.

Endpoints:
  GET  /api/defects            list/filter defects
  GET  /api/defects/{id}       single defect + notes
  PATCH /api/defects/{id}      update status / flag        (CRUD: update)
  POST /api/defects/{id}/notes add a note                  (CRUD: create)
  DELETE /api/notes/{id}       delete a note               (CRUD: delete)
  GET  /api/analytics          aggregates for the dashboard
  GET  /api/vehicle/{vin}      per-vehicle defect rollup
  POST /api/ask                natural-language question -> answer (+ optional chart)
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # lets serverless hosts import sibling modules
from dotenv import load_dotenv
load_dotenv()  # load .env FIRST, before importing modules that read env vars
from collections import defaultdict
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from typing import Optional

from database import get_db, engine, Base
import models, schemas
from seed import seed
from ai import answer_question
from report import build_report, detect_anomalies, build_comparison, priority_list, severity_of
import report_engine
import insights as insight_engine
import security

Base.metadata.create_all(bind=engine)

app = FastAPI(title="UVeye Quality Intelligence API", version="1.0")

from quality_inspection_api import router as qi_router

app.include_router(qi_router)

from quality_app_api import router as qa_router
app.include_router(qa_router)

from admin_api import (router as admin_router, seed_depts,
                       authenticate as admin_authenticate, _log as audit_log)
app.include_router(admin_router)

# simple in-memory cache for expensive analytics (cleared on restart)
_CACHE = {}


def _autoseed_samples():
    """On a fresh deploy (e.g. Render) the real QMS/PDI data files aren't present.
    Load compact committed sample seeds so Quality Site and PDI tabs show data.
    Only runs for a source if it currently has zero rows, so it never touches a
    real local ingest."""
    import json as _json
    from database import SessionLocal
    here = os.path.dirname(__file__)
    db = SessionLocal()
    try:
        for src, fname, defaults in [
            ("quality", "quality_seed.json", {"score_default": None}),
            ("pdi", "pdi_seed.json", {"score_default": 1.0}),
        ]:
            existing = db.query(models.Defect).filter(models.Defect.source == src).count()
            if existing > 0:
                continue
            path = os.path.join(here, "data", fname)
            if not os.path.exists(path):
                continue
            try:
                rows = _json.load(open(path, encoding="utf-8"))
            except Exception as e:
                print(f"autoseed {src}: could not read {fname}: {e}")
                continue
            n = 0
            for r in rows:
                db.add(models.Defect(
                    inspection_id=r.get("inspection_id", ""),
                    vin=r.get("vin", ""),
                    make=r.get("make", ""),
                    model=r.get("model", ""),
                    year=r.get("year", ""),
                    inspected_at=r.get("inspected_at", ""),
                    part_name=r.get("part_name", ""),
                    module=r.get("module", "Other"),
                    description=r.get("description", ""),
                    cost=float(r.get("cost", 0) or 0),
                    score=float(r.get("score", defaults["score_default"]) if r.get("score", defaults["score_default"]) is not None else 1.0),
                    line=r.get("line", ""),
                    plant=r.get("plant", ""),
                    conversion=r.get("conversion", ""),
                    zone=r.get("zone", ""),
                    failure_mode=r.get("failure_mode", ""),
                    source=src,
                    status=r.get("status", "New"),
                    flagged=False,
                ))
                n += 1
                if n % 1000 == 0:
                    db.commit()
            db.commit()
            print(f"autoseed: loaded {n} '{src}' sample rows")
    except Exception as e:
        print("autoseed skipped:", e)
    finally:
        db.close()

# --- lightweight in-memory cache so repeat page loads are instant ---
import time as _time
_CACHE = {}
_CACHE_TTL = 120  # seconds; cleared on any defect write

def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and (_time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None

def _cache_set(key, val):
    _CACHE[key] = (_time.time(), val)
    return val

def _cache_clear():
    _CACHE.clear()

app.add_middleware(
    CORSMiddleware,
    allow_origins=security.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

# public paths that don't require a login (the page shell + login + static)
PUBLIC_PREFIXES = ("/api/login", "/static", "/favicon")
PUBLIC_EXACT = {"/", "/health"}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _is_authed(request: Request) -> bool:
    # token may arrive as a bearer header or a cookie
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("session", "")
    return security.verify_token(token)


class Guardrails:
    """Pure-ASGI middleware: rate-limit, auth-gate, and inject security
    headers without buffering/re-streaming the response body.

    BaseHTTPMiddleware (the @app.middleware('http') decorator) re-streams
    the body via call_next, which corrupts JSON bodies to `null` under
    some Starlette + Python 3.14 combinations. Operating at the ASGI
    layer avoids that path entirely.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        ip = _client_ip(request)

        # 1) rate limit every request
        if security.rate_limited(ip):
            resp = JSONResponse({"detail": "Rate limit exceeded. Slow down."}, status_code=429)
            await resp(scope, receive, send)
            return

        path = request.url.path
        # 2) auth gate (skip for public paths and when auth disabled)
        authed = _is_authed(request)
        if security.REQUIRE_AUTH and not authed:
            is_public = path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)
            if not is_public:
                resp = JSONResponse({"detail": "Authentication required"}, status_code=401)
                await resp(scope, receive, send)
                return

        # stash auth flag for handlers (used for VIN masking)
        scope.setdefault("state", {})["authed"] = authed

        # 3) inject security headers as the response starts, without
        #    touching the body stream
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for k, v in security.SECURITY_HEADERS.items():
                    headers.append((k.encode("latin-1"), v.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(Guardrails)


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    ip = _client_ip(request)

    # A registered employee account wins: the server decides the role, so the
    # role buttons on the login screen stop being the source of truth. Falls
    # back to the shared APP_USERNAME/APP_PASSWORD so demo access still works.
    from database import SessionLocal
    account = None
    db = SessionLocal()
    try:
        account = admin_authenticate(db, username, password)
        profile = {
            "role": account.role,
            "scope": account.scope or "",
            "dept": account.dept or "",
            "name": account.employee_name,
            "signoff_depts": sorted(d.dept for d in account.signoff_depts if d.active),
        } if account else None

        if not account and not security.verify_credentials(username, password):
            security.audit.info(f"LOGIN FAILED user={username!r} ip={ip}")
            # A failed sign-in is the single most important thing an audit trail
            # can hold, and stdout logs don't survive a Render restart. This one
            # goes in the database.
            audit_log(db, username or "(blank)", "auth.login_failed", "", f"ip={ip}")
            raise HTTPException(401, "Invalid username or password")

        mode = "directory" if account else "shared credential"
        token = security.issue_token(username)
        security.audit.info(
            f"LOGIN OK user={username!r} ip={ip} "
            f"mode={'directory' if account else 'shared'} role={profile['role'] if account else 'self-selected'}")
        audit_log(db, username, "auth.login", "",
                  f"{mode}, role={profile['role'] if account else 'self-selected'}, ip={ip}")
    finally:
        db.close()
    resp = JSONResponse({"token": token, "user": username, "profile": profile})
    # session cookie (no max_age) -> cleared when the browser closes,
    # so reopening the site prompts a fresh login
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/health")
def health():
    return {"status": "ok"}


VALID_STATUS = {"New", "Reviewed", "Resolved"}


@app.on_event("startup")
def _startup():
    seed()
    _autoseed_samples()
    try:
        seed_depts()
    except Exception as e:
        print("dept/admin seed skipped:", e)
    # ensure indexes exist on the hot columns (speeds up 88k-row scans massively)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_def_src_date ON defects(source, inspected_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_def_src_line ON defects(source, line)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_def_src_fm ON defects(source, failure_mode)"))
            conn.commit()
    except Exception as e:
        print("index setup skipped:", e)
    # pre-warm the analytics cache in a background thread so the first tab
    # open is instant instead of waiting on a cold 88k-row computation
    import threading, time as _time
    def _warm():
        from database import SessionLocal
        while True:
            try:
                db = SessionLocal()
                try:
                    for src in ("quality", "pdi"):
                        try:
                            get_insights(db=db, source=src)
                            get_changed(db=db, source=src, dimension="line" if src == "quality" else "failure_mode")
                        except Exception:
                            pass
                    try:
                        scorecard(db=db, source="quality")
                        cross_source(db=db, dimension="failure_mode")
                    except Exception:
                        pass
                finally:
                    db.close()
            except Exception as e:
                print("prewarm skipped:", e)
            _time.sleep(480)  # re-warm every 8 min so the 10-min cache never expires cold
    threading.Thread(target=_warm, daemon=True).start()


# ---------------- CRUD: defects ----------------
@app.get("/api/defects", response_model=list[schemas.DefectOut])
def list_defects(
    db: Session = Depends(get_db),
    make: Optional[str] = None,
    module: Optional[str] = None,
    status: Optional[str] = None,
    flagged: Optional[bool] = None,
    vin: Optional[str] = None,
    source: str = "uveye",
    limit: int = Query(200, le=1000),
):
    q = db.query(models.Defect)
    if source and source != "all":
        q = q.filter(models.Defect.source == source)
    if make:
        q = q.filter(models.Defect.make == make)
    if module:
        q = q.filter(models.Defect.module == module)
    if status:
        q = q.filter(models.Defect.status == status)
    if flagged is not None:
        q = q.filter(models.Defect.flagged == flagged)
    if vin:
        q = q.filter(models.Defect.vin.ilike(f"%{vin}%"))
    return q.order_by(models.Defect.inspected_at.desc()).limit(limit).all()


@app.get("/api/defects/{defect_id}", response_model=schemas.DefectOut)
def get_defect(defect_id: int, db: Session = Depends(get_db)):
    d = db.get(models.Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    return d


@app.patch("/api/defects/{defect_id}", response_model=schemas.DefectOut)
def update_defect(defect_id: int, payload: schemas.DefectUpdate, db: Session = Depends(get_db)):
    d = db.get(models.Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    if payload.status is not None:
        if payload.status not in VALID_STATUS:
            raise HTTPException(400, f"status must be one of {sorted(VALID_STATUS)}")
        d.status = payload.status
    if payload.flagged is not None:
        d.flagged = payload.flagged
    db.commit()
    db.refresh(d)
    return d


# ---------------- CRUD: notes ----------------
@app.post("/api/defects/{defect_id}/notes", response_model=schemas.NoteOut)
def add_note(defect_id: int, payload: schemas.NoteCreate, db: Session = Depends(get_db)):
    d = db.get(models.Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    note = models.Note(defect_id=defect_id, body=payload.body, author=payload.author)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db)):
    n = db.get(models.Note, note_id)
    if not n:
        raise HTTPException(404, "Note not found")
    db.delete(n)
    db.commit()
    return {"deleted": note_id}


# ---------------- analytics ----------------
@app.get("/api/analytics")
def analytics(db: Session = Depends(get_db),
              date_from: Optional[str] = None, date_to: Optional[str] = None,
              source: Optional[str] = None):
    """Dashboard aggregates computed in SQL (GROUP BY) so this stays fast at
    scale. Defect-COUNT based. Optional source filter ('uveye'|'quality')."""
    D = models.Defect

    def base():
        q = db.query(D)
        if source:
            q = q.filter(D.source == source)
        if date_from:
            q = q.filter(D.inspected_at >= date_from)
        if date_to:
            q = q.filter(D.inspected_at <= date_to)
        return q

    total = base().count()
    vehicles = base().filter(D.vin != "").with_entities(D.vin).distinct().count()
    inspections = base().with_entities(D.inspection_id).distinct().count()
    flagged = base().filter(D.flagged == True).count()
    resolved = base().filter(D.status == "Resolved").count()
    total_cost = base().with_entities(func.coalesce(func.sum(D.cost), 0)).scalar() or 0

    def grp(col, limit=12, skip_blank=True):
        q = base().with_entities(col, func.count(D.id))
        rows = q.group_by(col).order_by(func.count(D.id).desc()).all()
        out = {}
        for val, cnt in rows:
            label = val or "Unspecified"
            if skip_blank and label in ("Unspecified", "Unknown", "Other", ""):
                continue
            out[label] = cnt
            if len(out) >= limit:
                break
        return out

    # month bucket via substring (inspected_at is 'YYYY-MM-DD')
    month_rows = (base()
                  .with_entities(func.substr(D.inspected_at, 1, 7), func.count(D.id))
                  .filter(D.inspected_at != "")
                  .group_by(func.substr(D.inspected_at, 1, 7))
                  .order_by(func.substr(D.inspected_at, 1, 7))
                  .all())
    by_month = {m: c for m, c in month_rows if m}

    status_rows = base().with_entities(D.status, func.count(D.id)).group_by(D.status).all()
    by_status = {s or "New": c for s, c in status_rows}

    dr = base().with_entities(func.min(D.inspected_at), func.max(D.inspected_at)).first()

    return {
        "kpis": {
            "defects": total,
            "vehicles": vehicles,
            "inspections": inspections,
            "flagged": flagged,
            "resolved": resolved,
            "total_cost": round(total_cost),
            "cost_available": total_cost > 0,
            "avg_defects_per_vehicle": round(total / vehicles, 1) if vehicles else 0,
        },
        "by_make": grp(D.make),
        "by_module": grp(D.module),
        "by_part": grp(D.part_name),
        "by_month": by_month,
        "by_status": by_status,
        "date_range": {"min": dr[0] if dr else "", "max": dr[1] if dr else ""},
    }


@app.get("/api/vehicles")
def vehicles_list(request: Request, db: Session = Depends(get_db),
                  search: Optional[str] = None,
                  make: Optional[str] = None,
                  sort: str = "defects",
                  limit: int = Query(50, le=200),
                  offset: int = 0):
    """Paginated vehicle rollup for the Vehicles tab. Groups defects by VIN
    so the 8k+ vehicles are browsable without loading every defect."""
    D = models.Defect
    q = (db.query(
            D.vin,
            func.max(D.make).label("make"),
            func.max(D.model).label("model"),
            func.max(D.year).label("year"),
            func.count(D.id).label("defects"),
            func.sum(case((D.status == "Resolved", 1), else_=0)).label("resolved"),
            func.sum(case((D.flagged == True, 1), else_=0)).label("flagged"),
            func.max(D.inspected_at).label("last_seen"),
         )
         .filter(D.vin != ""))
    if make:
        q = q.filter(D.make == make)
    if search:
        q = q.filter(D.vin.ilike(f"%{search}%"))
    q = q.group_by(D.vin)

    order = {
        "defects": func.count(D.id).desc(),
        "recent": func.max(D.inspected_at).desc(),
        "flagged": func.sum(case((D.flagged == True, 1), else_=0)).desc(),
    }.get(sort, func.count(D.id).desc())
    q = q.order_by(order)

    total = q.count()
    rows = q.limit(limit).offset(offset).all()
    authed = getattr(request.state, "authed", False)
    items = [{
        "vin": security.mask_vin(r.vin, authed),
        "vin_raw": r.vin if authed else "",
        "make": r.make, "model": r.model, "year": r.year,
        "defects": r.defects, "resolved": r.resolved or 0,
        "flagged": r.flagged or 0, "last_seen": r.last_seen,
    } for r in rows]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/scores")
def scores(db: Session = Depends(get_db),
           date_from: Optional[str] = None, date_to: Optional[str] = None,
           line: Optional[str] = None, make: Optional[str] = None,
           conversion: Optional[str] = None, module: Optional[str] = None,
           source: str = "quality", target: float = 31.0):
    """Score / DPU / FTY metrics matching standard Power BI-style quality reports."""
    D = models.Defect

    def base():
        q = db.query(D).filter(D.source == source)
        if date_from: q = q.filter(D.inspected_at >= date_from)
        if date_to:   q = q.filter(D.inspected_at <= date_to)
        if line:      q = q.filter(D.line == line)
        if make:      q = q.filter(D.make == make)
        if conversion:q = q.filter(D.conversion == conversion)
        if module:    q = q.filter(D.module == module)
        return q

    defects = base().count()
    units = base().with_entities(D.vin).distinct().count()
    avg_score = base().with_entities(func.avg(D.score)).scalar() or 0
    dpu = round(defects / units, 3) if units else 0
    zero_units = (base().with_entities(D.vin)
                  .group_by(D.vin).having(func.sum(D.score) == 0).count())
    fty = round(100.0 * zero_units / units, 1) if units else 0

    trend_rows = (base()
                  .with_entities(D.inspected_at, func.avg(D.score), func.count(D.id))
                  .filter(D.inspected_at != "")
                  .group_by(D.inspected_at).order_by(D.inspected_at).all())
    trend = [{"date": d, "score": round(s or 0, 2), "defects": c} for d, s, c in trend_rows]

    pareto_rows = (base()
                   .with_entities(D.failure_mode, func.count(D.id))
                   .filter(D.failure_mode != "")
                   .group_by(D.failure_mode)
                   .order_by(func.count(D.id).desc()).limit(15).all())
    pareto = [{"label": f, "count": c} for f, c in pareto_rows]

    line_rows = (base()
                 .with_entities(D.line, func.count(D.id), func.avg(D.score),
                                func.count(func.distinct(D.vin)))
                 .filter(D.line != "")
                 .group_by(D.line)
                 .order_by(func.count(D.id).desc()).all())
    by_line = [{"line": ln, "defects": c, "avg_score": round(s or 0, 2),
                "units": u, "dpu": round(c / u, 2) if u else 0}
               for ln, c, s, u in line_rows]

    return {
        "kpis": {
            "avg_score": round(avg_score, 2), "target": target,
            "on_target": avg_score <= target,
            "dpu": dpu, "fty": fty, "defects": defects, "units": units,
        },
        "trend": trend, "pareto": pareto, "by_line": by_line,
    }


@app.get("/api/report/tier")
def report_tier(db: Session = Depends(get_db),
                date_from: Optional[str] = None, date_to: Optional[str] = None,
                line: Optional[str] = None, source: str = "quality"):
    """Tier report: defects by zone/module and by line, plus top issues."""
    D = models.Defect
    def base():
        q = db.query(D).filter(D.source == source)
        if date_from: q = q.filter(D.inspected_at >= date_from)
        if date_to:   q = q.filter(D.inspected_at <= date_to)
        if line:      q = q.filter(D.line == line)
        return q

    by_module = [{"label": m or "Other", "count": c} for m, c in
                 base().with_entities(D.module, func.count(D.id))
                 .group_by(D.module).order_by(func.count(D.id).desc()).limit(15).all()]
    by_line = [{"line": ln or "Other", "count": c} for ln, c in
               base().filter(D.line != "").with_entities(D.line, func.count(D.id))
               .group_by(D.line).order_by(func.count(D.id).desc()).all()]
    top_issues = [{"issue": d or "Unspecified", "count": c} for d, c in
                  base().with_entities(D.description, func.count(D.id))
                  .filter(D.description != "")
                  .group_by(D.description).order_by(func.count(D.id).desc()).limit(20).all()]
    return {"by_module": by_module, "by_line": by_line, "top_issues": top_issues,
            "total": base().count()}


@app.get("/api/report/pareto")
def report_pareto(db: Session = Depends(get_db),
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  by: str = "failure_mode", source: str = "quality"):
    """Pareto of defect drivers with cumulative %. `by` = failure_mode|module|part_name."""
    D = models.Defect
    col = {"failure_mode": D.failure_mode, "module": D.module,
           "part_name": D.part_name, "make": D.make}.get(by, D.failure_mode)
    q = db.query(col, func.count(D.id)).filter(D.source == source)
    if date_from: q = q.filter(D.inspected_at >= date_from)
    if date_to:   q = q.filter(D.inspected_at <= date_to)
    rows = q.filter(col != "").group_by(col).order_by(func.count(D.id).desc()).limit(20).all()
    total = sum(c for _, c in rows)
    out = []; cum = 0
    for label, c in rows:
        cum += c
        out.append({"label": label, "count": c,
                    "pct": round(100*c/total, 1) if total else 0,
                    "cum_pct": round(100*cum/total, 1) if total else 0})
    return {"items": out, "total": total}


@app.get("/api/report/trends")
def report_trends(db: Session = Depends(get_db),
                  bucket: str = "month", source: str = "quality"):
    """Rolling score & volume trend by week or month."""
    D = models.Defect
    keyexpr = func.substr(D.inspected_at, 1, 7) if bucket == "month" else func.substr(D.inspected_at, 1, 10)
    rows = (db.query(keyexpr, func.count(D.id), func.avg(D.score),
                     func.count(func.distinct(D.vin)))
            .filter(D.source == source, D.inspected_at != "")
            .group_by(keyexpr).order_by(keyexpr).all())
    return {"buckets": [{"period": p, "defects": c, "avg_score": round(s or 0, 2),
                         "units": u, "dpu": round(c/u, 2) if u else 0}
                        for p, c, s, u in rows]}


@app.get("/api/report/vcd")
def report_vcd(db: Session = Depends(get_db), source: str = "quality", limit: int = 40):
    """VCD score distribution: total score per vehicle, ranked (like the Power BI
    'Sum of QTYSCORE by SERIALNUM' chart)."""
    D = models.Defect
    rows = (db.query(D.vin, func.sum(D.score), func.count(D.id))
            .filter(D.source == source, D.vin != "")
            .group_by(D.vin).order_by(func.sum(D.score).desc()).limit(limit).all())
    items = [{"vin": v[-6:] if v else "?", "score": round(s or 0, 1), "defects": c}
             for v, s, c in rows]
    avg = db.query(func.avg(D.score)).filter(D.source == source).scalar() or 0
    return {"items": items, "avg_score": round(avg, 2)}


@app.get("/api/report/stations")
def report_stations(db: Session = Depends(get_db), source: str = "quality"):
    """Defects by inspection zone/station and by line (the Tier 'by station' view)."""
    D = models.Defect
    by_zone = [{"label": z or "Unspecified", "count": c} for z, c in
               db.query(D.zone, func.count(D.id)).filter(D.source == source, D.zone != "")
               .group_by(D.zone).order_by(func.count(D.id).desc()).limit(20).all()]
    by_line = [{"label": ln or "Other", "count": c, "avg_score": round(s or 0, 2)} for ln, c, s in
               db.query(D.line, func.count(D.id), func.avg(D.score))
               .filter(D.source == source, D.line != "")
               .group_by(D.line).order_by(func.count(D.id).desc()).all()]
    return {"by_zone": by_zone, "by_line": by_line}


@app.get("/api/report/velocity")
def report_velocity(db: Session = Depends(get_db), source: str = "quality",
                    failure_mode: Optional[str] = None):
    """Velocity deep-dive: pick a failure mode, see its monthly DPU/volume trend.
    Lists available failure modes when none is selected."""
    D = models.Defect
    modes = [{"label": f, "count": c} for f, c in
             db.query(D.failure_mode, func.count(D.id))
             .filter(D.source == source, D.failure_mode != "")
             .group_by(D.failure_mode).order_by(func.count(D.id).desc()).limit(25).all()]
    trend = []
    if failure_mode:
        key = func.substr(D.inspected_at, 1, 7)
        rows = (db.query(key, func.count(D.id), func.count(func.distinct(D.vin)))
                .filter(D.source == source, D.failure_mode == failure_mode, D.inspected_at != "")
                .group_by(key).order_by(key).all())
        trend = [{"period": p, "defects": c, "units": u, "dpu": round(c/u, 2) if u else 0}
                 for p, c, u in rows]
    return {"modes": modes, "selected": failure_mode, "trend": trend}


@app.get("/api/report/makes")
def report_makes(db: Session = Depends(get_db), source: str = "quality"):
    """Make/model analysis: defects, avg score, units per make."""
    D = models.Defect
    rows = (db.query(D.make, func.count(D.id), func.avg(D.score),
                     func.count(func.distinct(D.vin)))
            .filter(D.source == source, D.make != "", D.make.notin_(["Unknown", "Unspecified"]))
            .group_by(D.make).order_by(func.count(D.id).desc()).all())
    return {"items": [{"make": m, "defects": c, "avg_score": round(s or 0, 2),
                       "units": u, "dpu": round(c/u, 2) if u else 0}
                      for m, c, s, u in rows]}


@app.get("/api/report/conversion")
def report_conversion(db: Session = Depends(get_db), source: str = "quality"):
    """Conversion comparison: side-entry vs rear-entry quality."""
    D = models.Defect
    rows = (db.query(D.conversion, func.count(D.id), func.avg(D.score),
                     func.count(func.distinct(D.vin)))
            .filter(D.source == source, D.conversion != "")
            .group_by(D.conversion).order_by(func.count(D.id).desc()).all())
    return {"items": [{"conversion": cv, "defects": c, "avg_score": round(s or 0, 2),
                       "units": u, "dpu": round(c/u, 2) if u else 0}
                      for cv, c, s, u in rows]}


@app.get("/api/me/summary")
def my_summary(db: Session = Depends(get_db),
               role: str = "admin", scope: str = "", source: str = "quality"):
    """Personalized daily digest for the logged-in persona.
    role: wav | dealer | velocity | admin
    scope: e.g. 'LN1' for a wav inspector, dealer account for a dealer."""
    D = models.Defect

    q = db.query(D)
    # velocity + admin see everything; wav/dealer are scoped
    if role == "wav":
        q = q.filter(D.source == "quality")
        if scope:
            q = q.filter(D.line == scope)
    elif role == "dealer":
        q = q.filter(D.source == "pdi")
        if scope:
            q = q.filter(D.plant == scope)
    elif source and role not in ("velocity", "admin"):
        q = q.filter(D.source == source)

    total = q.count()
    vehicles = q.with_entities(D.vin).distinct().count()
    # top issue (failure_mode or module)
    top = (q.with_entities(D.failure_mode, func.count(D.id))
           .filter(D.failure_mode != "")
           .group_by(D.failure_mode).order_by(func.count(D.id).desc()).first())
    top_issue = top[0] if top else None
    # most recent activity date
    last = q.with_entities(D.inspected_at).order_by(D.inspected_at.desc()).first()
    avg_score = q.with_entities(func.avg(D.score)).scalar() or 0

    label = {"wav": f"line {scope}" if scope else "your line",
             "dealer": f"dealer {scope}" if scope else "your dealership",
             "velocity": "all quality data", "admin": "the full quality network"}.get(role, "your data")

    return {"role": role, "scope": scope, "label": label,
            "defects": total, "vehicles": vehicles,
            "top_issue": top_issue, "avg_score": round(avg_score, 2),
            "last_activity": last[0] if last and last[0] else None}


@app.get("/api/pdi/rows")
def pdi_rows(db: Session = Depends(get_db), limit: int = 1000):
    """Raw PDI inspection findings for the searchable log table."""
    D = models.Defect
    rows = (db.query(D).filter(D.source == "pdi")
            .order_by(D.inspected_at.desc()).limit(limit).all())
    return [{"inspection_id": r.inspection_id, "vin": r.vin, "make": r.make,
             "inspected_at": r.inspected_at, "module": r.module,
             "failure_mode": r.failure_mode, "description": r.description,
             "plant": r.plant, "conversion": r.conversion} for r in rows]


@app.get("/api/scorecard")
def scorecard(db: Session = Depends(get_db), source: str = "quality"):
    """Month-over-month quality health. Cached 90s."""
    import time as _t
    ck = ("scorecard", source)
    hit = _CACHE.get(ck)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    from collections import defaultdict
    D = models.Defect
    A = models.ActionItem
    rows = db.query(D).filter(D.source == source, D.inspected_at != "").limit(40000).all()
    if not rows:
        return {"months": [], "current": None, "trend": None}

    by_month_defs = defaultdict(int)
    by_month_units = defaultdict(set)
    by_month_score = defaultdict(list)
    for r in rows:
        m = (r.inspected_at or "")[:7]
        if not m:
            continue
        by_month_defs[m] += 1
        if r.vin:
            by_month_units[m].add(r.vin)
        sc = getattr(r, "score", 0) or 0
        by_month_score[m].append(sc)

    months = sorted(by_month_defs)
    series = []
    for m in months:
        units = len(by_month_units[m]) or 1
        dpu = round(by_month_defs[m] / units, 2)
        avg_score = round(sum(by_month_score[m]) / len(by_month_score[m]), 2) if by_month_score[m] else 0
        series.append({"period": m, "defects": by_month_defs[m], "units": units,
                       "dpu": dpu, "avg_score": avg_score})

    # health score 0-100: lower DPU and lower avg_score = healthier.
    # scale relative to the worst month so the trend is visible.
    max_dpu = max((s["dpu"] for s in series), default=1) or 1
    max_score = max((s["avg_score"] for s in series), default=1) or 1
    for s in series:
        dpu_part = 1 - (s["dpu"] / max_dpu) if max_dpu else 1
        score_part = 1 - (s["avg_score"] / max_score) if max_score else 1
        s["health"] = round((dpu_part * 0.5 + score_part * 0.5) * 100)

    current = series[-1] if series else None
    prev = series[-2] if len(series) >= 2 else None
    trend = None
    if current and prev:
        trend = {
            "health_delta": current["health"] - prev["health"],
            "dpu_delta": round(current["dpu"] - prev["dpu"], 2),
            "score_delta": round(current["avg_score"] - prev["avg_score"], 2),
            "improving": current["health"] >= prev["health"],
        }

    # resolution velocity overlay (opened vs resolved per month)
    items = db.query(A).filter(A.source == source).all()
    opened = defaultdict(int); resolved = defaultdict(int)
    for a in items:
        if a.created_at:
            opened[a.created_at.strftime("%Y-%m")] += 1
        if a.resolved_at:
            resolved[a.resolved_at.strftime("%Y-%m")] += 1
    for s in series:
        s["opened"] = opened.get(s["period"], 0)
        s["resolved"] = resolved.get(s["period"], 0)

    out = {"months": series, "current": current, "trend": trend, "source": source}
    _CACHE[("scorecard", source)] = (__import__("time").time(), out)
    return out


@app.post("/api/actions/autotrack")
def autotrack(db: Session = Depends(get_db)):
    """Proactive: scan current insights across all sources and auto-open action
    items for any HIGH-severity issue not already tracked. The system catches
    critical problems even if no one clicked 'Track'."""
    A = models.ActionItem
    created = []
    # what's already tracked (by entity+dimension) so we don't duplicate
    existing = {(a.dimension, a.entity, a.source) for a in db.query(A).all()}
    for source in ("quality", "pdi"):
        rows = db.query(models.Defect).filter(models.Defect.source == source).limit(20000).all()
        if not rows:
            continue
        for ins in insight_engine.generate_insights(rows, source=source):
            if ins.get("severity") != "high":
                continue
            key = (ins.get("dimension", ""), ins.get("entity", ""), source)
            if key in existing:
                continue
            owner = "admin" if source == "pdi" else "velocity"
            a = models.ActionItem(
                title=ins["title"], detail=ins["detail"], source=source,
                dimension=ins.get("dimension", ""), entity=ins.get("entity", ""),
                severity="high", owner_role=owner, created_by="auto", status="open",
            )
            db.add(a)
            existing.add(key)
            created.append(ins["title"])
    db.commit()
    return {"created": created, "count": len(created)}


@app.get("/api/summary/weekly")
def weekly_summary(db: Session = Depends(get_db)):
    """A shareable executive summary across the whole quality network \u2014 the
    'send this up to leadership' artifact. Plain-language, numbers-backed."""
    from datetime import datetime as _dt, timezone as _tz
    D = models.Defect
    A = models.ActionItem

    sections = []
    # headline numbers per source
    for source, label in (("quality", "Production line (QS2.0)"), ("pdi", "Dealer PDI")):
        rows = db.query(D).filter(D.source == source).limit(20000).all()
        if not rows:
            continue
        ins = insight_engine.generate_insights(rows, source=source)
        crit = [i for i in ins if i["severity"] == "high"]
        n = len(rows)
        veh = len({r.vin for r in rows if r.vin})
        top = crit[0]["title"] if crit else (ins[0]["title"] if ins else "No notable issues")
        sections.append({"label": label, "defects": n, "vehicles": veh,
                         "critical": len(crit), "headline": top,
                         "insights": [i["title"] for i in ins[:3]]})

    # action accountability
    items = db.query(A).all()
    resolved = [a for a in items if a.status == "resolved"]
    open_items = [a for a in items if a.status != "resolved"]
    now = _dt.now(_tz.utc)

    def _age(a):
        s = a.created_at
        if not s:
            return 0
        if s.tzinfo is None:
            s = s.replace(tzinfo=_tz.utc)
        return max(0, (now - s).days)
    stale = [a for a in open_items if _age(a) >= 7]

    actions = {"total": len(items), "resolved": len(resolved), "open": len(open_items),
               "stale": len(stale),
               "resolution_rate": round(len(resolved)/len(items)*100) if items else 0,
               "stale_titles": [a.title for a in stale[:5]]}

    # generated narrative
    lines = []
    total_def = sum(s["defects"] for s in sections)
    total_crit = sum(s["critical"] for s in sections)
    lines.append(f"This period the quality network tracked {total_def:,} defects across "
                 f"{len(sections)} data sources, with {total_crit} critical issue(s) flagged.")
    if actions["total"]:
        lines.append(f"The team is tracking {actions['total']} action item(s) and has resolved "
                     f"{actions['resolved']} ({actions['resolution_rate']}%).")
        if actions["stale"]:
            lines.append(f"\u26A0 {actions['stale']} issue(s) have been open 7+ days and need attention.")
        else:
            lines.append("No issues are stale \u2014 the team is staying on top of the queue.")
    for s in sections:
        if s["critical"]:
            lines.append(f"{s['label']}: top concern is \u201C{s['headline']}\u201D.")

    return {"generated_at": now.isoformat(), "sections": sections,
            "actions": actions, "narrative": " ".join(lines)}


@app.get("/api/actions/stats")
def action_stats(db: Session = Depends(get_db)):
    """Resolution performance: aging, velocity (opened vs resolved over time),
    and stale items. This is the 'are we actually closing issues?' view."""
    from datetime import datetime as _dt, timezone as _tz
    A = models.ActionItem
    items = db.query(A).all()
    now = _dt.now(_tz.utc)

    def _age_days(a, end=None):
        start = a.created_at
        if not start:
            return 0
        if start.tzinfo is None:
            start = start.replace(tzinfo=_tz.utc)
        e = end or now
        if e.tzinfo is None:
            e = e.replace(tzinfo=_tz.utc)
        return max(0, (e - start).days)

    open_items = [a for a in items if a.status != "resolved"]
    resolved = [a for a in items if a.status == "resolved"]

    # average resolution time
    res_times = [_age_days(a, a.resolved_at) for a in resolved if a.resolved_at]
    avg_resolution = round(sum(res_times) / len(res_times), 1) if res_times else None

    # average age of currently-open items
    open_ages = [_age_days(a) for a in open_items]
    avg_open_age = round(sum(open_ages) / len(open_ages), 1) if open_ages else 0

    # stale: open and older than 7 days
    stale = sorted([a for a in open_items if _age_days(a) >= 7], key=lambda a: -_age_days(a))
    stale_out = [{"id": a.id, "title": a.title, "age_days": _age_days(a),
                  "owner_role": a.owner_role, "severity": a.severity} for a in stale[:6]]

    # velocity by week: opened vs resolved
    from collections import defaultdict
    opened_wk = defaultdict(int)
    resolved_wk = defaultdict(int)
    for a in items:
        if a.created_at:
            opened_wk[a.created_at.strftime("%Y-%m-%d")[:7]] += 1
        if a.resolved_at:
            resolved_wk[a.resolved_at.strftime("%Y-%m-%d")[:7]] += 1
    periods = sorted(set(list(opened_wk.keys()) + list(resolved_wk.keys())))
    velocity = [{"period": p, "opened": opened_wk.get(p, 0), "resolved": resolved_wk.get(p, 0)}
                for p in periods]

    # resolution rate
    total = len(items)
    rate = round(len(resolved) / total * 100) if total else 0

    return {"total": total, "open": len(open_items), "resolved": len(resolved),
            "resolution_rate": rate, "avg_resolution_days": avg_resolution,
            "avg_open_age_days": avg_open_age, "stale": stale_out,
            "stale_count": len(stale), "velocity": velocity}


@app.get("/api/actions")
def list_actions(db: Session = Depends(get_db), status: str = "", source: str = "",
                 owner_role: str = ""):
    """List tracked action items, optionally filtered. This is the live
    accountability board \u2014 problems that are owned and being worked."""
    A = models.ActionItem
    q = db.query(A)
    if status:
        q = q.filter(A.status == status)
    if source:
        q = q.filter(A.source == source)
    if owner_role:
        q = q.filter(A.owner_role == owner_role)
    items = q.order_by(A.status, A.created_at.desc()).all()
    import json as _json
    from datetime import datetime as _dt2, timezone as _tz2
    _now = _dt2.now(_tz2.utc)
    out = []
    for a in items:
        try:
            ups = _json.loads(a.updates) if a.updates else []
        except Exception:
            ups = []
        _start = a.created_at
        if _start and _start.tzinfo is None:
            _start = _start.replace(tzinfo=_tz2.utc)
        age = max(0, (_now - _start).days) if _start else 0
        out.append({"id": a.id, "title": a.title, "detail": a.detail, "source": a.source,
                    "dimension": a.dimension, "entity": a.entity, "severity": a.severity,
                    "status": a.status, "owner_role": a.owner_role, "created_by": a.created_by,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                    "age_days": age, "stale": (a.status != "resolved" and age >= 7),
                    "updates": ups})
    # summary counts
    counts = {"open": 0, "in_progress": 0, "resolved": 0}
    for a in items:
        counts[a.status] = counts.get(a.status, 0) + 1
    return {"items": out, "counts": counts}


@app.post("/api/actions")
def create_action(payload: dict, db: Session = Depends(get_db)):
    """Open a tracked issue \u2014 usually from clicking 'Track this' on an insight."""
    if not payload.get("title"):
        raise HTTPException(400, "title required")
    a = models.ActionItem(
        title=payload["title"][:300],
        detail=payload.get("detail", "")[:2000],
        source=payload.get("source", "quality"),
        dimension=payload.get("dimension", ""),
        entity=payload.get("entity", ""),
        severity=payload.get("severity", "medium"),
        owner_role=payload.get("owner_role", "velocity"),
        created_by=payload.get("created_by", ""),
        status="open",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"ok": True, "id": a.id}


@app.patch("/api/actions/{action_id}")
def update_action(action_id: int, payload: dict, db: Session = Depends(get_db)):
    """Update status or append a progress note."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    a = db.query(models.ActionItem).filter(models.ActionItem.id == action_id).first()
    if not a:
        raise HTTPException(404, "not found")
    if "status" in payload:
        a.status = payload["status"]
        if payload["status"] == "resolved":
            a.resolved_at = _dt.now(_tz.utc)
        else:
            a.resolved_at = None
    if payload.get("update"):
        try:
            ups = _json.loads(a.updates) if a.updates else []
        except Exception:
            ups = []
        ups.append({"by": payload.get("by", ""), "text": payload["update"][:500],
                    "at": _dt.now(_tz.utc).isoformat()})
        a.updates = _json.dumps(ups)
    db.commit()
    return {"ok": True}


@app.delete("/api/actions/{action_id}")
def delete_action(action_id: int, db: Session = Depends(get_db)):
    a = db.query(models.ActionItem).filter(models.ActionItem.id == action_id).first()
    if a:
        db.delete(a)
        db.commit()
    return {"ok": True}


@app.get("/api/crosssource")
def cross_source(db: Session = Depends(get_db), dimension: str = "make"):
    """Velocity-team tool: compare the SAME dimension across internal quality
    (QS2.0) and dealer PDI. Cached 90s."""
    import time as _t
    ck = ("crosssource", dimension)
    hit = _CACHE.get(ck)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    D = models.Defect
    allowed = {"make", "module", "failure_mode", "conversion"}
    if dimension not in allowed:
        dimension = "make"
    col = getattr(D, dimension)

    def dist(source):
        rows = (db.query(col, func.count(D.id))
                .filter(D.source == source, col != "")
                .group_by(col).all())
        total = sum(c for _, c in rows) or 1
        return {k: {"count": c, "pct": round(c/total*100, 1)} for k, c in rows}

    q_dist = dist("quality")
    p_dist = dist("pdi")
    keys = set(list(q_dist.keys()) + list(p_dist.keys()))
    rows_out = []
    for k in keys:
        qp = q_dist.get(k, {}).get("pct", 0)
        pp = p_dist.get(k, {}).get("pct", 0)
        rows_out.append({
            "label": insight_engine._title(k),
            "quality_pct": qp, "pdi_pct": pp,
            "quality_count": q_dist.get(k, {}).get("count", 0),
            "pdi_count": p_dist.get(k, {}).get("count", 0),
            "gap": round(pp - qp, 1),  # positive = more common at dealers than on the line
        })
    rows_out.sort(key=lambda x: x["pdi_count"], reverse=True)

    # escape insights: issues notably more common at dealers than internally
    escapes = [r for r in rows_out if r["gap"] >= 5 and r["pdi_count"] >= 3]
    escapes.sort(key=lambda x: -x["gap"])
    return {"dimension": dimension, "rows": rows_out[:14], "escapes": escapes[:4]}


@app.post("/api/defects/new")
def create_defect(payload: dict, db: Session = Depends(get_db)):
    """Structured defect entry — lets dealers/inspectors log a finding the same
    clean way every time, instead of free-text paper/Excel. Lands as a real row
    in the right source so it flows straight into reports and the AI."""
    src = payload.get("source", "pdi")
    make = (payload.get("make") or "").strip()
    if not make:
        raise HTTPException(400, "make is required")
    import datetime as _dt
    d = models.Defect(
        inspection_id=(payload.get("inspection_id") or f"MANUAL-{int(_dt.datetime.now().timestamp())}").strip(),
        vin=(payload.get("vin") or "").strip(),
        make=make,
        model=(payload.get("model") or "").strip(),
        year=(payload.get("year") or "").strip(),
        inspected_at=(payload.get("inspected_at") or _dt.date.today().isoformat()),
        part_name=(payload.get("inspection_point") or payload.get("part_name") or "").strip(),
        module=(payload.get("inspection_category") or payload.get("module") or "Other").strip(),
        description=(payload.get("notes") or payload.get("description") or "").strip(),
        cost=float(payload.get("cost") or 0),
        score=float(payload.get("score") or 1),
        line=(payload.get("line") or "").strip(),
        plant=(payload.get("dealer") or payload.get("plant") or "").strip(),
        conversion=(payload.get("conversion") or "").strip(),
        zone=(payload.get("zone") or "").strip(),
        failure_mode=(payload.get("inspection_point") or payload.get("failure_mode") or "").strip(),
        source=src,
        status="New",
        flagged=bool(payload.get("flagged", False)),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return {"ok": True, "id": d.id, "message": "Defect logged"}


@app.get("/api/form/options")
def form_options(db: Session = Depends(get_db), source: str = "pdi"):
    """Dropdown options for the structured entry form, derived from existing data
    so the vocabulary stays consistent (no more free-text typos)."""
    D = models.Defect
    def distinct(col):
        return sorted({v[0] for v in db.query(col).filter(D.source == source, col != "").distinct().all() if v[0]})
    return {
        "makes": distinct(D.make),
        "categories": distinct(D.module),
        "points": distinct(D.failure_mode),
        "conversions": distinct(D.conversion),
    }


@app.get("/api/analyze")
def analyze(db: Session = Depends(get_db), source: str = "quality",
            scope_field: str = "", scope_value: str = ""):
    """Analytical narrative: the AI reads the insight engine and speaks a
    conclusion, not just a chart. Powers 'what should I look at?'"""
    D = models.Defect
    q = db.query(D).filter(D.source == source)
    if scope_field and scope_value and hasattr(D, scope_field):
        q = q.filter(getattr(D, scope_field) == scope_value)
    rows = q.limit(20000).all()
    ins = insight_engine.generate_insights(rows, source=source)
    if not rows:
        return {"narrative": "No data in this view yet."}

    n = len(rows)
    vehicles = len({r.vin for r in rows if r.vin})
    crit = [i for i in ins if i["severity"] == "high"]
    watch = [i for i in ins if i["severity"] == "medium"]
    good = [i for i in ins if i["severity"] == "good"]

    parts = []
    scope_txt = f" for {scope_value}" if scope_value else ""
    parts.append(f"Looking at {n:,} defects across {vehicles:,} vehicles{scope_txt}.")
    if crit:
        parts.append("The thing to act on: " + crit[0]["title"].lower() + ". " + crit[0]["detail"])
        if len(crit) > 1:
            parts.append("Also critical: " + crit[1]["title"].lower() + ".")
    elif watch:
        parts.append("Nothing critical, but worth watching: " + watch[0]["title"].lower() + ". " + watch[0]["detail"])
    else:
        parts.append("No anomalies stand out right now \u2014 things look stable.")
    if good:
        parts.append("Good sign: " + good[0]["title"].lower() + ".")
    # a recommendation
    if crit:
        rec = crit[0]
        if rec.get("dimension") == "line":
            parts.append(f"Recommendation: pull up {rec.get('entity')}'s defect detail and check its top failure modes against the other lines.")
        elif rec.get("dimension") == "failure_mode":
            parts.append(f"Recommendation: trace {_title_safe(rec.get('entity'))} back to its station or supplier before it spreads further.")
    return {"narrative": " ".join(parts), "insight_count": len(ins),
            "critical": len(crit), "watch": len(watch)}


def _title_safe(s):
    return insight_engine._title(s) if s else ""


@app.get("/api/drill")
def drill(db: Session = Depends(get_db), source: str = "quality",
          field: str = "", value: str = "", limit: int = 200):
    """Drill into the actual records behind a number. Returns the breakdown one
    level down PLUS the raw records, so a click on 'LN2' shows its failure modes
    and the real defects + notes underneath."""
    D = models.Defect
    q = db.query(D).filter(D.source == source)
    if field and value and hasattr(D, field):
        q = q.filter(getattr(D, field) == value)
    rows = q.order_by(D.inspected_at.desc()).limit(2000).all()

    # next-level breakdown: if drilling a line/make, break down by failure mode;
    # if drilling a failure mode, break down by make.
    nxt = "failure_mode" if field in ("line", "make", "module", "plant", "conversion", "zone") else "make"
    bd = defaultdict(int)
    for r in rows:
        k = getattr(r, nxt, "") or "Other"
        bd[k] += 1
    breakdown = sorted([{"label": k, "value": v} for k, v in bd.items()],
                       key=lambda x: -x["value"])[:10]

    records = [{"inspection_id": r.inspection_id, "vin": r.vin, "make": r.make,
                "inspected_at": r.inspected_at, "module": r.module,
                "failure_mode": r.failure_mode, "description": r.description,
                "score": getattr(r, "score", 0), "line": r.line,
                "plant": r.plant, "conversion": r.conversion}
               for r in rows[:limit]]

    avg_score = sum((getattr(r, "score", 0) or 0) for r in rows) / len(rows) if rows else 0
    return {"field": field, "value": value, "source": source,
            "total": len(rows), "vehicles": len({r.vin for r in rows if r.vin}),
            "avg_score": round(avg_score, 2), "next_dim": nxt,
            "breakdown": breakdown, "records": records}


@app.get("/api/insights")
def get_insights(db: Session = Depends(get_db), source: str = "quality",
                 scope_field: str = "", scope_value: str = ""):
    """The insight engine: ranked 'what needs attention'. Cached 90s, and queries
    only the columns the engine needs (fast even on 88k rows)."""
    import time as _t
    ck = ("insights", source, scope_field, scope_value)
    hit = _CACHE.get(ck)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    D = models.Defect
    cols = (D.vin, D.line, D.make, D.score, D.failure_mode, D.inspected_at, D.plant)
    q = db.query(*cols).filter(D.source == source)
    if scope_field and scope_value and hasattr(D, scope_field):
        q = q.filter(getattr(D, scope_field) == scope_value)
    rows = q.limit(20000).all()  # 20k is plenty to surface patterns; keeps it fast
    out = {"insights": insight_engine.generate_insights(rows, source=source)}
    _CACHE[ck] = (_t.time(), out)
    return out


@app.get("/api/changed")
def get_changed(db: Session = Depends(get_db), source: str = "quality",
                dimension: str = "line", scope_field: str = "", scope_value: str = ""):
    """What changed this period vs last. Cached 90s, lightweight column query."""
    import time as _t
    ck = ("changed", source, dimension, scope_field, scope_value)
    hit = _CACHE.get(ck)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    D = models.Defect
    dim_col = getattr(D, dimension, D.line)
    q = db.query(D.inspected_at, dim_col.label("dim")).filter(D.source == source)
    if scope_field and scope_value and hasattr(D, scope_field):
        q = q.filter(getattr(D, scope_field) == scope_value)
    rows = q.limit(40000).all()
    out = insight_engine.period_comparison_fast(rows) or {"rows": []}
    _CACHE[ck] = (_t.time(), out)
    return out


@app.get("/api/catalog")
def get_catalog():
    """Return the full report catalog (umbrellas -> tabs -> reports).
    The frontend builds its entire Quality Site UI from this."""
    return report_engine.load_catalog()


@app.post("/api/report/run")
def run_report_endpoint(cfg: dict, db: Session = Depends(get_db)):
    """Execute ANY report config and return chart-ready data.
    This single endpoint powers every config-driven report."""
    return report_engine.run_report(cfg, db)


@app.post("/api/catalog/add-report")
def add_report(payload: dict, db: Session = Depends(get_db)):
    """Add a new report to a tab and persist it. This is what the (future)
    no-code builder UI calls. umbrella_id + tab_id + report config."""
    umbrella_id = payload.get("umbrella_id")
    tab_id = payload.get("tab_id")
    report = payload.get("report") or {}
    if not report.get("metric") or not report.get("dimension"):
        raise HTTPException(400, "report needs at least a metric and a dimension")
    catalog = report_engine.load_catalog()
    for u in catalog.get("umbrellas", []):
        if u["id"] == umbrella_id:
            for t in u["tabs"]:
                if t["id"] == tab_id:
                    if "id" not in report:
                        report["id"] = f"{report['metric']}_{report['dimension']}_{len(t['reports'])}"
                    t["reports"].append(report)
                    report_engine.save_catalog(catalog)
                    return {"ok": True, "added": report}
    raise HTTPException(404, "umbrella or tab not found")


@app.post("/api/catalog/add-tab")
def add_tab(payload: dict):
    """Add a whole new tab (with optional reports). Also for the builder UI."""
    umbrella_id = payload.get("umbrella_id")
    tab = payload.get("tab") or {}
    if not tab.get("label"):
        raise HTTPException(400, "tab needs a label")
    tab.setdefault("id", tab["label"].lower().replace(" ", "_"))
    tab.setdefault("reports", [])
    catalog = report_engine.load_catalog()
    for u in catalog.get("umbrellas", []):
        if u["id"] == umbrella_id:
            u["tabs"].append(tab)
            report_engine.save_catalog(catalog)
            return {"ok": True, "added": tab}
    raise HTTPException(404, "umbrella not found")


@app.get("/api/filters")
def filter_options(db: Session = Depends(get_db)):
    """Distinct values for the report filter dropdowns."""
    D = models.Defect
    def vals(col):
        return [v for (v,) in db.query(col).distinct().order_by(col).all()
                if v and v.strip()][:100]
    return {
        "lines": vals(D.line), "makes": vals(D.make),
        "conversions": vals(D.conversion), "modules": vals(D.module),
        "failure_modes": vals(D.failure_mode),
    }


@app.get("/api/priority")
def priority(request: Request, db: Session = Depends(get_db)):
    rows = db.query(models.Defect).all()
    result = priority_list(rows)
    authed = getattr(request.state, "authed", False)
    for item in result.get("items", []):
        item["vin"] = security.mask_vin(item.get("vin", ""), authed)
    return result


@app.get("/api/vehicle/{vin}/scans")
def vehicle_scans(vin: str, request: Request, db: Session = Depends(get_db)):
    """Group a vehicle's defects into scans by inspection date, ordered in time.

    For the pre/post-conversion test: each scan of the same VIN lands on a
    different day, so grouping by inspected_at and ordering chronologically
    gives scan 1 (pre) -> scan 2 (post) -> scan 3 (re-verification). Returns
    each scan with its defects/cost plus the delta vs the previous scan.
    """
    rows = db.query(models.Defect).filter(models.Defect.vin.ilike(vin)).all()
    if not rows:
        raise HTTPException(404, "No inspection found for that VIN")

    by_date = {}
    for r in rows:
        by_date.setdefault(r.inspected_at or "unknown", []).append(r)
    dates = sorted(by_date.keys())

    labels = ["Pre-conversion", "Post-conversion", "Re-verification"]
    scans = []
    prev_cost = None
    prev_count = None
    for i, d in enumerate(dates):
        scan_rows = by_date[d]
        cost = round(sum(x.cost for x in scan_rows))
        count = len(scan_rows)
        scans.append({
            "stage": labels[i] if i < len(labels) else f"Scan {i+1}",
            "scan_no": i + 1,
            "date": d,
            "defects": count,
            "cost": cost,
            "items": [{"part": x.part_name, "cost": x.cost,
                       "description": x.description} for x in scan_rows],
            "delta_cost": (cost - prev_cost) if prev_cost is not None else None,
            "delta_defects": (count - prev_count) if prev_count is not None else None,
        })
        prev_cost, prev_count = cost, count

    r0 = rows[0]
    authed = getattr(request.state, "authed", False)
    return {
        "vin": security.mask_vin(r0.vin, authed), "make": r0.make, "model": r0.model, "year": r0.year,
        "scan_count": len(scans), "scans": scans,
    }


@app.get("/api/vehicle/{vin}")
def vehicle(vin: str, request: Request, db: Session = Depends(get_db)):
    rows = db.query(models.Defect).filter(models.Defect.vin.ilike(vin)).all()
    if not rows:
        raise HTTPException(404, "No inspection found for that VIN")
    r0 = rows[0]
    authed = getattr(request.state, "authed", False)
    if authed:
        security.audit.info(f"VIN ACCESS vin={r0.vin}")
    return {
        "vin": security.mask_vin(r0.vin, authed), "make": r0.make, "model": r0.model, "year": r0.year,
        "date": r0.inspected_at, "total_cost": round(sum(r.cost for r in rows)),
        "defects": [
            {"id": r.id, "part": r.part_name, "description": r.description,
             "cost": r.cost, "status": r.status, "flagged": r.flagged}
            for r in rows
        ],
    }


# ---------------- AI ----------------
@app.post("/api/ask")
def ask(payload: schemas.AskRequest, db: Session = Depends(get_db)):
    """Rule-based analytics assistant over live SQL aggregates."""
    D = models.Defect
    src = getattr(payload, "source", None) or "quality"
    q = payload.question.lower()

    # --- ANALYTICAL MODE: 'what should I look at', 'analyze', 'what's wrong' ---
    if any(t in q for t in ["what should i", "what's wrong", "whats wrong", "analyze",
                            "anything wrong", "what needs", "what's important", "whats important",
                            "give me the rundown", "brief me", "what's going on", "whats going on",
                            "summarize what matters", "what should we focus"]):
        rows_a = db.query(models.Defect).filter(models.Defect.source == src).limit(20000).all()
        ins = insight_engine.generate_insights(rows_a, source=src)
        n = len(rows_a); veh = len({r.vin for r in rows_a if r.vin})
        crit = [i for i in ins if i["severity"] == "high"]
        watch = [i for i in ins if i["severity"] == "medium"]
        msg = [f"Looking at {n:,} defects across {veh:,} vehicles."]
        if crit:
            msg.append("**Act on this:** " + crit[0]["title"] + ". " + crit[0]["detail"])
            for extra in crit[1:2]:
                msg.append("**Also critical:** " + extra["title"] + ".")
        elif watch:
            msg.append("**Worth watching:** " + watch[0]["title"] + ". " + watch[0]["detail"])
        else:
            msg.append("Nothing anomalous stands out \u2014 things look stable.")
        for w in (watch[:2] if crit else watch[1:3]):
            msg.append("\u2022 " + w["title"])
        return {"answer": " ".join(msg), "mode": "agent", "analytical": True}

    # --- AGENT MODE: is this a command to build/reshape the dashboard? ---
    import ai as _ai
    intent = _ai.parse_intent(payload.question)
    if intent:
        if intent["action"] == "reset":
            return {"answer": "Back to the executive view.", "action": "reset", "mode": "agent"}
        # render: execute the generated config and hand the frontend a ready report
        import report_engine
        cfg = dict(intent["report"]); cfg["source"] = src
        result = report_engine.run_report(cfg, db)
        if not result.get("error"):
            return {"answer": intent.get("spoken", "Here you go."),
                    "action": "render", "report": result, "mode": "agent"}

    def base():
        return db.query(D).filter(D.source == src)

    makes = [m for (m,) in db.query(D.make).filter(D.source == src).distinct().all()
             if m and m not in ("Unknown", "Unspecified")]
    makes.sort(key=len, reverse=True)

    if any(w in q for w in ["compare", " vs ", "versus", " against "]):
        found = [mk for mk in makes if mk.lower() in q]
        if len(found) >= 2:
            return build_comparison(base().all(), found[0], found[1])

    if "score" in q or "average" in q or "avg" in q:
        avg = base().with_entities(func.avg(D.score)).scalar() or 0
        if "line" in q or "wav" in q:
            rows = (base().filter(D.line != "").with_entities(D.line, func.avg(D.score))
                    .group_by(D.line).order_by(func.avg(D.score).desc()).all())
            data = [{"label": ln, "value": round(s or 0, 2)} for ln, s in rows]
            return {"answer": f"Average score by line (overall {round(avg,2)}).",
                    "chart": {"type": "bar", "data": data}, "mode": "local"}
        return {"answer": f"The average vehicle score is **{round(avg,2)}**. Lower is better; target is around 31.", "mode": "local"}

    if any(w in q for w in ["worst", "highest", "most defect", "top"]):
        dim = D.line if ("line" in q or "wav" in q) else (D.module if "module" in q else (D.make if "make" in q else D.failure_mode))
        rows = (base().filter(dim != "").with_entities(dim, func.count(D.id))
                .group_by(dim).order_by(func.count(D.id).desc()).limit(8).all())
        data = [{"label": x or "?", "value": c} for x, c in rows]
        top = data[0]["label"] if data else "n/a"
        return {"answer": f"**{top}** has the most defects.", "chart": {"type": "bar", "data": data}, "mode": "local"}

    if any(w in q for w in ["best", "fewest", "lowest"]):
        dim = D.line if ("line" in q or "wav" in q) else D.make
        rows = (base().filter(dim != "").with_entities(dim, func.count(D.id))
                .group_by(dim).order_by(func.count(D.id).asc()).limit(8).all())
        data = [{"label": x or "?", "value": c} for x, c in rows]
        return {"answer": "Lowest defect counts (best performers):", "chart": {"type": "bar", "data": data}, "mode": "local"}

    if any(w in q for w in ["trend", "over time", "monthly", "by month", "history"]):
        rows = (base().filter(D.inspected_at != "")
                .with_entities(func.substr(D.inspected_at, 1, 7), func.count(D.id))
                .group_by(func.substr(D.inspected_at, 1, 7))
                .order_by(func.substr(D.inspected_at, 1, 7)).all())
        data = [{"label": p, "value": c} for p, c in rows]
        return {"answer": "Defect volume by month:", "chart": {"type": "line", "data": data}, "mode": "local"}

    if "conversion" in q or "side entry" in q or "rear entry" in q:
        rows = (base().filter(D.conversion != "").with_entities(D.conversion, func.count(D.id))
                .group_by(D.conversion).order_by(func.count(D.id).desc()).all())
        data = [{"label": cv, "value": c} for cv, c in rows]
        return {"answer": "Defects by conversion type:", "chart": {"type": "bar", "data": data}, "mode": "local"}

    for mk in makes:
        if mk.lower() in q:
            c = base().filter(D.make == mk).count()
            u = base().filter(D.make == mk).with_entities(D.vin).distinct().count()
            return {"answer": f"**{mk}**: {c:,} defects across {u:,} vehicles.", "mode": "local"}

    if any(w in q for w in ["report", "dashboard", "full breakdown", "overview of",
                            "everything about", "deep dive", "analyze"]):
        spec = build_report(payload.question, base().all())
        spec["is_report"] = True; spec["mode"] = "local"
        return spec

    return answer_question(payload.question, base().limit(5000).all())


@app.get("/api/anomalies")
def anomalies(db: Session = Depends(get_db)):
    rows = db.query(models.Defect).all()
    return {"anomalies": detect_anomalies(rows)}


@app.post("/api/compare")
def compare(payload: schemas.AskRequest, db: Session = Depends(get_db)):
    rows = db.query(models.Defect).all()
    makes = sorted({r.make for r in rows if r.make and r.make not in ("Unknown","Unspecified")}, key=len, reverse=True)
    found = [mk for mk in makes if mk.lower() in payload.question.lower()]
    if len(found) < 2:
        raise HTTPException(400, "Mention two makes to compare")
    return build_comparison(rows, found[0], found[1])


@app.post("/api/report")
def report(payload: schemas.AskRequest, db: Session = Depends(get_db)):
    rows = db.query(models.Defect).all()
    spec = build_report(payload.question, rows)
    spec["is_report"] = True
    return spec


# ---------------- serve frontend ----------------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}
