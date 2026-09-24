"""
Admin — the last unreplicated section of the legacy Quality App.

Covers what the legacy quality app exposes under Admin:
    User Management
        - Add New User
        - Manage User Roles
        - Manage User Sign-off Depts
        - Manage Sign-Off Drop Down Depts
    Defect Management
        - edit / remove logged defects

Design note: users live in the app database (SQLite locally, Postgres on
Render), not Snowflake. A user directory is an application concern — routing
it through the warehouse would mean a new grant from Kristine for every
change, and would outlive nobody usefully. Defect Management operates on the
`defects` table, which is the app's own writeable copy.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user random salt. That
is stdlib-only (no new dependency) and materially stronger than the single
shared credential the app shipped with.

Wire-up in main.py:
    from admin_api import router as admin_router, seed_depts
    app.include_router(admin_router)
    seed_depts()          # inside the existing startup handler
"""

import os
import re
import hmac
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

import logging
import base64, json as _json
import security
from database import get_db, SessionLocal
import models

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _actor(request: Request) -> str:
    """Who did this, for the audit log.

    Re-verifies the token signature rather than trusting that the Guardrails
    middleware already did. That check is real today, but it lives in another
    module and depends on the route not being in PUBLIC_PREFIXES — an audit
    trail that silently becomes forgeable if someone edits that tuple is worse
    than no audit trail, because it still looks authoritative. Verifying here
    costs one HMAC and removes the coupling.

    Never raises: an unreadable token logs as 'unverified' rather than failing
    the request this only exists to record."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("session", "")
    try:
        if not security.verify_token(token):
            return "unverified"
        raw = token.split(".", 1)[0]
        payload = _json.loads(base64.urlsafe_b64decode(raw.encode()))
        return payload.get("u", "unverified") or "unverified"
    except Exception:
        return "unverified"


def _log(db: Session, actor: str, action: str, target: str = "", detail: str = ""):
    """Record an action. Deliberately swallows its own failures: the caller has
    already committed the real change, so raising here would report a 500 for
    an operation that actually succeeded. A missing audit line is bad; telling
    someone their user wasn't created when it was is worse."""
    try:
        db.add(models.AuditEvent(actor=actor, action=action,
                                 target=(target or "")[:300], detail=(detail or "")[:1000]))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logging.getLogger("audit").warning(f"audit write failed: {action} {target}: {e}")

ROLES = ["wav", "dealer", "velocity", "admin"]

# The department list from the legacy app's Manage Sign-Off Drop Down Depts screen.
DEFAULT_DEPTS = [
    ("LN1", "Line 1"), ("LN2", "Line 2"), ("LN3", "Line 3"),
    ("LN4", "Line 4"), ("LN5", "Line 5"), ("LN6", "Line 6"),
    ("ENGR", "Engineering"), ("QUAL", "Quality Assurance"), ("WELD", "Weld"),
    ("SUBA", "Weld Sub-assembly"), ("FLOR", "Vehicle Floor"), ("FINL", "Final"),
    ("MATL", "Material Control"), ("PURCH", "Purchasing QMS"),
    ("OEMQ", "OEM QMS"), ("ADA", "ADA Compliance"), ("MFLR", "Main Floor"),
]

PBKDF2_ROUNDS = 120_000


# ------------------------------------------------------------------
# password hashing
# ------------------------------------------------------------------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(rounds))
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def authenticate(db: Session, identifier: str, password: str) -> Optional[models.AppUser]:
    """Look a user up by email (or exact name) and check the password.
    Returns None on any failure — callers must not distinguish which."""
    ident = (identifier or "").strip()
    if not ident or not password:
        return None
    u = (db.query(models.AppUser)
           .filter(models.AppUser.email == ident.lower())
           .first())
    if not u:
        u = (db.query(models.AppUser)
               .filter(models.AppUser.employee_name == ident)
               .first())
    if not u or not u.active or not verify_password(password, u.password_hash):
        return None
    u.last_login = datetime.now(timezone.utc)
    db.commit()
    return u


# ------------------------------------------------------------------
# seeding — department list + a first admin so the app is reachable
# ------------------------------------------------------------------
def seed_depts():
    """Idempotent. Fills the dropdown list and, if the directory is empty,
    creates one admin from ADMIN_EMAIL / ADMIN_PASSWORD so somebody can get in."""
    db = SessionLocal()
    try:
        existing = {d.code for d in db.query(models.SignoffDept).all()}
        for code, label in DEFAULT_DEPTS:
            if code not in existing:
                db.add(models.SignoffDept(code=code, label=label, active=True))
        db.commit()

        if db.query(models.AppUser).count() == 0:
            email = os.getenv("ADMIN_EMAIL", "").strip().lower()
            pw = os.getenv("ADMIN_PASSWORD", "").strip()
            if email and pw:
                db.add(models.AppUser(
                    employee_name=os.getenv("ADMIN_NAME", "Quality Admin"),
                    email=email, dept="QUAL", role="admin",
                    password_hash=hash_password(pw), active=True))
                db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------
# schemas
# ------------------------------------------------------------------
class UserCreate(BaseModel):
    employee_name: str = Field(min_length=1, max_length=120)
    email: str

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v):
        v = (v or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v):
            raise ValueError("Enter a valid email address")
        return v
    dept: str = ""
    role: str = "wav"
    scope: str = ""
    password: str = Field(min_length=8, max_length=200)
    signoff_depts: List[str] = []


class UserPatch(BaseModel):
    employee_name: Optional[str] = None
    dept: Optional[str] = None
    role: Optional[str] = None
    scope: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)


class DeptIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    label: str = ""


class DeptPatch(BaseModel):
    label: Optional[str] = None
    active: Optional[bool] = None


class SignoffDeptIn(BaseModel):
    dept: str


class DefectPatch(BaseModel):
    part_name: Optional[str] = None
    module: Optional[str] = None
    zone: Optional[str] = None
    failure_mode: Optional[str] = None
    line: Optional[str] = None
    plant: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    flagged: Optional[bool] = None
    cost: Optional[float] = None
    score: Optional[float] = None


def _user_out(u: models.AppUser) -> dict:
    return {
        "id": u.id,
        "employee_name": u.employee_name,
        "email": u.email,
        "dept": u.dept or "",
        "role": u.role,
        "scope": u.scope or "",
        "active": bool(u.active),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "signoff_depts": sorted(d.dept for d in u.signoff_depts if d.active),
    }


# ------------------------------------------------------------------
# User Management
# ------------------------------------------------------------------
@router.get("/users")
def list_users(role: Optional[str] = None, q: Optional[str] = None,
               include_inactive: bool = True, db: Session = Depends(get_db)):
    query = db.query(models.AppUser)
    if role:
        query = query.filter(models.AppUser.role == role)
    if not include_inactive:
        query = query.filter(models.AppUser.active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(models.AppUser.employee_name.ilike(like) |
                             models.AppUser.email.ilike(like))
    users = query.order_by(models.AppUser.employee_name).limit(500).all()
    counts = {r: 0 for r in ROLES}
    for u in db.query(models.AppUser).all():
        counts[u.role] = counts.get(u.role, 0) + 1
    return {"users": [_user_out(u) for u in users], "counts": counts,
            "total": db.query(models.AppUser).count()}


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, db: Session = Depends(get_db)):
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(ROLES)}")
    email = str(body.email).lower()
    if db.query(models.AppUser).filter(models.AppUser.email == email).first():
        raise HTTPException(409, "A user with that email already exists")
    u = models.AppUser(
        employee_name=body.employee_name.strip(), email=email,
        dept=body.dept.strip().upper(), role=body.role, scope=body.scope.strip(),
        password_hash=hash_password(body.password), active=True)
    db.add(u)
    db.flush()
    for d in {x.strip().upper() for x in body.signoff_depts if x.strip()}:
        db.add(models.UserSignoffDept(user_id=u.id, dept=d, active=True))
    db.commit()
    db.refresh(u)
    _log(db, _actor(request), "user.created", u.email, f"role={u.role}")
    return _user_out(u)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.AppUser, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    changes = []
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of: {', '.join(ROLES)}")
        if body.role != u.role:
            changes.append(f"role {u.role}->{body.role}")
        u.role = body.role
    if body.employee_name is not None:
        u.employee_name = body.employee_name.strip()
    if body.dept is not None:
        u.dept = body.dept.strip().upper()
    if body.scope is not None:
        u.scope = body.scope.strip()
    if body.active is not None:
        if not body.active and u.role == "admin" and _active_admins(db) <= 1:
            raise HTTPException(400, "Can't deactivate the last active admin")
        if body.active != u.active:
            changes.append("reactivated" if body.active else "deactivated")
        u.active = body.active
    if body.password:
        u.password_hash = hash_password(body.password)
        changes.append("password reset")
    db.commit()
    db.refresh(u)
    if changes:
        _log(db, _actor(request), "user.updated", u.email, ", ".join(changes))
    return _user_out(u)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.AppUser, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if u.role == "admin" and _active_admins(db) <= 1:
        raise HTTPException(400, "Can't remove the last active admin")
    email = u.email
    db.delete(u)
    db.commit()
    _log(db, _actor(request), "user.deleted", email)


def _active_admins(db: Session) -> int:
    return (db.query(models.AppUser)
              .filter(models.AppUser.role == "admin",
                      models.AppUser.active.is_(True)).count())


# ------------------------------------------------------------------
# Manage User Sign-off Depts
# ------------------------------------------------------------------
@router.post("/users/{user_id}/signoff-depts")
def add_signoff_dept(user_id: int, body: SignoffDeptIn, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.AppUser, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    dept = body.dept.strip().upper()
    if not dept:
        raise HTTPException(400, "Pick a department")
    existing = (db.query(models.UserSignoffDept)
                  .filter(models.UserSignoffDept.user_id == user_id,
                          models.UserSignoffDept.dept == dept).first())
    if existing:
        existing.active = True
    else:
        db.add(models.UserSignoffDept(user_id=user_id, dept=dept, active=True))
    db.commit()
    _log(db, _actor(request), "user.signoff_dept_granted", u.email, dept)
    db.refresh(u)
    return _user_out(u)


@router.delete("/users/{user_id}/signoff-depts/{dept}")
def remove_signoff_dept(user_id: int, dept: str, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.AppUser, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    row = (db.query(models.UserSignoffDept)
             .filter(models.UserSignoffDept.user_id == user_id,
                     models.UserSignoffDept.dept == dept.strip().upper()).first())
    if row:
        db.delete(row)
        db.commit()
        _log(db, _actor(request), "user.signoff_dept_revoked", u.email, dept.strip().upper())
    db.refresh(u)
    return _user_out(u)


# ------------------------------------------------------------------
# Manage Sign-Off Drop Down Depts
# ------------------------------------------------------------------
@router.get("/depts")
def list_depts(active_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(models.SignoffDept)
    if active_only:
        q = q.filter(models.SignoffDept.active.is_(True))
    rows = q.order_by(models.SignoffDept.code).all()
    return {"depts": [{"id": d.id, "code": d.code, "label": d.label or "",
                       "active": bool(d.active)} for d in rows]}


@router.post("/depts", status_code=201)
def create_dept(body: DeptIn, request: Request, db: Session = Depends(get_db)):
    code = body.code.strip().upper()
    if db.query(models.SignoffDept).filter(models.SignoffDept.code == code).first():
        raise HTTPException(409, f"{code} is already in the list")
    d = models.SignoffDept(code=code, label=body.label.strip(), active=True)
    db.add(d)
    db.commit()
    db.refresh(d)
    _log(db, _actor(request), "dept.created", code, d.label)
    return {"id": d.id, "code": d.code, "label": d.label, "active": True}


@router.patch("/depts/{dept_id}")
def update_dept(dept_id: int, body: DeptPatch, request: Request, db: Session = Depends(get_db)):
    d = db.get(models.SignoffDept, dept_id)
    if not d:
        raise HTTPException(404, "Department not found")
    changed = []
    if body.label is not None:
        d.label = body.label.strip()
        changed.append("label")
    if body.active is not None:
        if body.active != d.active:
            changed.append("retired" if not body.active else "restored")
        d.active = body.active
    db.commit()
    if changed:
        _log(db, _actor(request), "dept.updated", d.code, ", ".join(changed))
    return {"id": d.id, "code": d.code, "label": d.label or "", "active": bool(d.active)}


# ------------------------------------------------------------------
# Defect Management
# ------------------------------------------------------------------
@router.get("/defects")
def admin_defects(q: Optional[str] = None, source: Optional[str] = None,
                  status: Optional[str] = None, line: Optional[str] = None,
                  limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    query = db.query(models.Defect)
    if source:
        query = query.filter(models.Defect.source == source)
    if status:
        query = query.filter(models.Defect.status == status)
    if line:
        query = query.filter(models.Defect.line == line)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(models.Defect.vin.ilike(like) |
                             models.Defect.part_name.ilike(like) |
                             models.Defect.inspection_id.ilike(like))
    total = query.count()
    rows = query.order_by(models.Defect.updated_at.desc()).limit(limit).all()
    return {"total": total, "showing": len(rows), "defects": [
        {"id": d.id, "vin": d.vin, "inspection_id": d.inspection_id,
         "part_name": d.part_name, "module": d.module, "zone": d.zone,
         "failure_mode": d.failure_mode, "line": d.line, "plant": d.plant,
         "description": d.description, "cost": d.cost, "score": d.score,
         "status": d.status, "flagged": bool(d.flagged), "source": d.source,
         "inspected_at": d.inspected_at,
         "updated_at": d.updated_at.isoformat() if d.updated_at else None}
        for d in rows]}


@router.patch("/defects/{defect_id}")
def edit_defect(defect_id: int, body: DefectPatch, request: Request, db: Session = Depends(get_db)):
    d = db.get(models.Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    changed = body.model_dump(exclude_unset=True)
    for field, value in changed.items():
        if value is not None:
            setattr(d, field, value)
    db.commit()
    if changed:
        _log(db, _actor(request), "defect.edited", d.vin or f"#{d.id}",
             ", ".join(f"{k}={v}" for k, v in changed.items() if v is not None))
    return {"id": d.id, "ok": True}


@router.delete("/defects/{defect_id}", status_code=204)
def remove_defect(defect_id: int, request: Request, db: Session = Depends(get_db)):
    d = db.get(models.Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    vin, part = d.vin or f"#{d.id}", d.part_name or ""
    db.delete(d)
    db.commit()
    _log(db, _actor(request), "defect.deleted", vin, part)


# ------------------------------------------------------------------
# Audit — app-side action log (Sign-offs and Holds have their own
# timestamps already in Snowflake; see /api/quality-app/activity for those)
# ------------------------------------------------------------------
def _iso_utc(dt) -> Optional[str]:
    """Emit an explicit-UTC ISO string.

    The column is naive, so .isoformat() produces '2026-07-29T16:59:44' with no
    zone — which JavaScript's Date() parses as *local* time. That silently
    shifted every audit timestamp by the browser's UTC offset. Stamping the Z
    makes the wire format unambiguous."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _esc_like(v: str) -> str:
    """User input goes into a LIKE pattern, so % and _ have to stop being
    wildcards — otherwise searching '100%' silently matches everything."""
    return (v or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_query(db: Session, actor, action, q, before):
    query = db.query(models.AuditEvent)
    if actor:
        query = query.filter(models.AuditEvent.actor == actor)
    if action:
        query = query.filter(models.AuditEvent.action.like(_esc_like(action) + "%", escape="\\"))
    if q:
        like = f"%{_esc_like(q.strip())}%"
        query = query.filter(models.AuditEvent.target.ilike(like, escape="\\") |
                             models.AuditEvent.detail.ilike(like, escape="\\") |
                             models.AuditEvent.actor.ilike(like, escape="\\") |
                             models.AuditEvent.action.ilike(like, escape="\\"))
    if before:
        try:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
            if cutoff.tzinfo is not None:
                cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
            query = query.filter(models.AuditEvent.created_at < cutoff)
        except Exception:
            raise HTTPException(400, "before must be an ISO timestamp")
    return query.order_by(models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc())


@router.get("/audit")
def list_audit(actor: Optional[str] = None, action: Optional[str] = None,
              q: Optional[str] = None, before: Optional[str] = None,
              limit: int = Query(200, le=1000), db: Session = Depends(get_db)):
    """Newest first. Page by passing the last event's created_at back as `before`."""
    query = _audit_query(db, actor, action, q, before)
    rows = query.limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [
            {"id": e.id, "actor": e.actor, "action": e.action, "target": e.target,
             "detail": e.detail, "created_at": _iso_utc(e.created_at)}
            for e in rows],
        "has_more": has_more,
        "next_before": _iso_utc(rows[-1].created_at) if rows and has_more else None,
        "actors": [a[0] for a in db.query(models.AuditEvent.actor).distinct().limit(100).all() if a[0]],
    }


@router.get("/audit/export")
def export_audit(actor: Optional[str] = None, action: Optional[str] = None,
                 q: Optional[str] = None, db: Session = Depends(get_db)):
    """CSV of the app-side log. An audit trail nobody can hand to a manager or
    keep after the app is gone isn't really an audit trail."""
    import csv, io
    from fastapi.responses import StreamingResponse

    rows = _audit_query(db, actor, action, q, None).limit(20000).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "actor", "action", "target", "detail"])
    for e in rows:
        w.writerow([_iso_utc(e.created_at) or "", e.actor or "", e.action or "",
                    e.target or "", e.detail or ""])
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="quality-audit-{stamp}.csv"'})
