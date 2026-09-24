"""
Quality Inspection v2 — FastAPI router
Drop into your existing Quality Network backend.

Wire-up in main.py:
    from quality_inspection_api import router as qi_router
    app.include_router(qi_router)

Requires (add to requirements.txt):
    snowflake-connector-python

Render env vars needed (use a read-only service account
creds for the demo — flag to him that a service account is the proper path):
    SNOWFLAKE_ACCOUNT      e.g. your_account
    SNOWFLAKE_USER
    SNOWFLAKE_PASSWORD
    SNOWFLAKE_ROLE         APP_ROLE
    SNOWFLAKE_WAREHOUSE    APP_WH
    SNOWFLAKE_DATABASE     ANALYTICS_PROD

If Snowflake creds are absent (local dev), reads return cached seed data if
present and writes fall back to a local SQLite table so the demo still works.
"""

import os
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

# ── If your app has an auth dependency, import and reuse it here ──
# from auth import require_session
# AUTH_DEP = [Depends(require_session)]
AUTH_DEP = []

router = APIRouter(prefix="/api/quality-inspection", tags=["quality-inspection"], dependencies=AUTH_DEP)

# ------------------------------------------------------------------
# Snowflake connection (lazy, pooled per-request via context manager)
# ------------------------------------------------------------------
SF_AUTHENTICATOR = os.getenv("SNOWFLAKE_AUTHENTICATOR")  # e.g. "externalbrowser" for SSO
SF_KEY_PATH = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
SF_KEY_INLINE = os.getenv("SNOWFLAKE_PRIVATE_KEY")       # for Render: the PEM itself
SF_KEY_PASS = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
_SF_CONFIGURED = bool(all(os.getenv(k) for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER")) and (
    os.getenv("SNOWFLAKE_PASSWORD") or SF_AUTHENTICATOR or SF_KEY_PATH or SF_KEY_INLINE
))
# Local data mode: serve every "Snowflake" query from a SQLite copy built from
# the seed data in this repo (see local_sf.py). LOCAL_DATA=1 forces it on,
# LOCAL_DATA=0 forces it off, and unset means "on when Snowflake isn't configured".
_LD = os.getenv("LOCAL_DATA")
LOCAL_MODE = (_LD == "1") or (_LD is None and not _SF_CONFIGURED)
SF_ENABLED = _SF_CONFIGURED or LOCAL_MODE


def _private_key_bytes():
    """Load an RSA key for key-pair auth. Preferred over externalbrowser on a
    server: no SAML round-trip, no browser, and it works on Render where there
    is no browser to click. Returns None if key-pair auth isn't configured."""
    if not (SF_KEY_PATH or SF_KEY_INLINE):
        return None
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    if SF_KEY_INLINE:
        pem = SF_KEY_INLINE.replace("\\n", "\n").encode()
    else:
        path = SF_KEY_PATH
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        with open(path, "rb") as f:
            pem = f.read()

    key = serialization.load_pem_private_key(
        pem,
        password=SF_KEY_PASS.encode() if SF_KEY_PASS else None,
        backend=default_backend(),
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

MAPPING_VIEW = "ANALYTICS_PROD.REFINED.MART_OPS_UVEYE_QUALITY_MAPPING"
TAXONOMY_VIEW = "ANALYTICS_DEV.APP.VW_QUALITY_TAXONOMY"
DEFECTS_TABLE = "ANALYTICS_DEV.APP.QUALITY_INSPECTION_DEFECTS"

LOCAL_DB = os.getenv("QI_LOCAL_DB", "quality_inspection_local.db")


_sf_conn = None
_sf_lock = None
_sf_last_used = 0.0

def _get_lock():
    global _sf_lock
    if _sf_lock is None:
        import threading
        _sf_lock = threading.Lock()
    return _sf_lock


class _TimedCursor:
    """Wraps a Snowflake cursor and prints how long each query actually took.
    Set SF_TRACE=0 to silence. Without this we were optimising blind."""
    __slots__ = ("_c",)
    TRACE = os.getenv("SF_TRACE", "1") != "0"
    SLOW = float(os.getenv("SF_SLOW_MS", "400")) / 1000.0

    def __init__(self, c): object.__setattr__(self, "_c", c)
    def __getattr__(self, n): return getattr(self._c, n)
    def __iter__(self): return iter(self._c)

    def execute(self, sql, params=None, *a, **kw):
        import time as _t
        t0 = _t.perf_counter()
        try:
            return self._c.execute(sql, params, *a, **kw) if params is not None \
                   else self._c.execute(sql, *a, **kw)
        finally:
            dt = _t.perf_counter() - t0
            if self.TRACE and dt >= self.SLOW:
                one = " ".join(str(sql).split())
                print(f"[SF] {dt:6.2f}s  {one[:110]}")


@contextmanager
def sf_cursor():
    """Reuses a single cached connection across requests so SSO login only
    happens once per server run, instead of once per concurrent request
    (which was racing multiple SAML popups and timing out as 502s)."""
    global _sf_conn
    if LOCAL_MODE:
        from local_sf import local_cursor
        with local_cursor() as cur:
            yield cur
        return
    import snowflake.connector

    global _sf_last_used
    import time
    now = time.time()

    with _get_lock():
        needs_new = _sf_conn is None
        # Only probe the connection if it has been sitting idle long enough to
        # plausibly have dropped. Probing on every request cost a full Snowflake
        # round-trip per API call — inside this lock — which serialised the whole
        # app behind it. That was the single biggest source of page-load lag.
        if not needs_new and (now - _sf_last_used) > 300:
            try:
                _sf_conn.cursor().execute("SELECT 1")
            except Exception:
                needs_new = True

        if needs_new:
            connect_kwargs = dict(
                account=os.environ["SNOWFLAKE_ACCOUNT"],
                user=os.environ["SNOWFLAKE_USER"],
                role=os.getenv("SNOWFLAKE_ROLE", "APP_ROLE"),
                warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "APP_WH"),
                database=os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS_PROD"),
                client_session_keep_alive=True,  # keep it alive between requests
                login_timeout=60,
                network_timeout=60,
            )
            pk = _private_key_bytes()
            if pk is not None:                       # preferred: no browser, works on Render
                connect_kwargs["private_key"] = pk
            elif SF_AUTHENTICATOR:
                connect_kwargs["authenticator"] = SF_AUTHENTICATOR
                connect_kwargs["client_store_temporary_credential"] = True
            else:
                connect_kwargs["password"] = os.environ["SNOWFLAKE_PASSWORD"]
            _sf_conn = snowflake.connector.connect(**connect_kwargs)
        _sf_last_used = now

    cur = None
    try:
        cur = _TimedCursor(_sf_conn.cursor(snowflake.connector.DictCursor))
        yield cur
    except Exception:
        # a dropped connection surfaces here now instead of being pre-empted by
        # the probe — drop it so the next caller rebuilds, then re-raise
        with _get_lock():
            try:
                if _sf_conn: _sf_conn.close()
            except Exception:
                pass
            _sf_conn = None
        raise
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass


def _local_conn():
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quality_inspection_defects (
            id TEXT PRIMARY KEY, vin TEXT NOT NULL, job_number TEXT,
            part_name TEXT, subsystem TEXT, component TEXT, zone TEXT,
            failure_mode TEXT, ds_code TEXT, notes TEXT, uveye_photo TEXT,
            uveye_sourced INTEGER DEFAULT 0, submitted_by TEXT,
            submitted_at TEXT, source TEXT DEFAULT 'QUALITY_NETWORK_V2'
        )"""
    )
    return conn


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------
class DefectSubmission(BaseModel):
    vin: str = Field(..., min_length=5, max_length=25)
    job_number: Optional[str] = None
    part_name: Optional[str] = None
    subsystem: Optional[str] = None
    component: Optional[str] = None
    zone: Optional[str] = None
    failure_mode: Optional[str] = None
    ds_code: Optional[str] = None
    notes: Optional[str] = None
    uveye_photo: Optional[str] = None
    uveye_sourced: bool = False
    submitted_by: Optional[str] = None


# ------------------------------------------------------------------
# GET /uveye/{vin} — UVeye defects + predicted Quality App fields
# ------------------------------------------------------------------
@router.get("/uveye/{vin}")
def uveye_lookup(vin: str):
    vin = vin.strip().upper()
    if not SF_ENABLED:
        raise HTTPException(503, "Snowflake connection not configured on this deployment")
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT VIN, JOB_NUMBER, MAKE, MODEL, YEAR,
                           INSPECTED_AT, PART_NAME, DEFECT_DESCRIPTION, COST,
                           CROPPED_IMAGE,
                           SUB_SYSTEM AS SUBSYSTEM, COMPONENT,
                           ZONE, FAIL_MODE AS FAILURE_MODE,
                           DS_CODE, PREDICTION_QUALITY
                    FROM {MAPPING_VIEW}
                    WHERE VIN = %s
                    ORDER BY INSPECTED_AT DESC""",
                (vin,),
            )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")

    if not rows:
        return {"vin": vin, "found": False, "vehicle": None, "defects": []}

    first = rows[0]
    vehicle = {
        "vin": vin,
        "job_number": first.get("JOB_NUMBER"),
        "make": first.get("MAKE"),
        "model": first.get("MODEL"),
        "year": first.get("YEAR"),
        "inspected_at": str(first.get("INSPECTED_AT") or ""),
    }
    defects = [
        {
            "part_name": r.get("PART_NAME"),
            "description": r.get("DEFECT_DESCRIPTION"),
            "cost": r.get("COST"),
            "photo": r.get("CROPPED_IMAGE"),
            "subsystem": r.get("SUBSYSTEM"),
            "component": r.get("COMPONENT"),
            "zone": r.get("ZONE"),
            "failure_mode": r.get("FAILURE_MODE"),
            "ds_code": r.get("DS_CODE"),
            "prediction_quality": r.get("PREDICTION_QUALITY"),
        }
        for r in rows
    ]
    return {"vin": vin, "found": True, "vehicle": vehicle, "defects": defects}


# ------------------------------------------------------------------
# GET /taxonomy — dropdown options (cached in-process for 1 hour)
# ------------------------------------------------------------------
_taxonomy_cache: dict = {"at": None, "data": None}


@router.get("/taxonomy")
def taxonomy():
    now = datetime.now(timezone.utc)
    if _taxonomy_cache["data"] and (now - _taxonomy_cache["at"]).seconds < 3600:
        return _taxonomy_cache["data"]

    if not SF_ENABLED:
        # Local fallback: ship a taxonomy_seed.json alongside the app if desired
        if os.path.exists("taxonomy_seed.json"):
            with open("taxonomy_seed.json") as f:
                return json.load(f)
        raise HTTPException(503, "Snowflake connection not configured and no taxonomy seed present")

    try:
        with sf_cursor() as cur:
            cur.execute(f"SELECT SUBSYSTEM, COMPONENT, ZONE, FAILURE_MODE, DS_CODE FROM {TAXONOMY_VIEW}")
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")

    data = {
        "subsystems": sorted({r["SUBSYSTEM"] for r in rows if r["SUBSYSTEM"]}),
        "components": sorted({r["COMPONENT"] for r in rows if r["COMPONENT"]}),
        "zones": sorted({r["ZONE"] for r in rows if r["ZONE"]}),
        "failure_modes": sorted({r["FAILURE_MODE"] for r in rows if r["FAILURE_MODE"]}),
        "ds_codes": sorted({r["DS_CODE"] for r in rows if r["DS_CODE"]}),
        # full rows enable cascading dropdowns client-side
        "rows": rows,
    }
    _taxonomy_cache.update(at=now, data=data)
    return data


# ------------------------------------------------------------------
# POST /defects — submit a defect
# ------------------------------------------------------------------
@router.post("/defects")
def submit_defect(d: DefectSubmission):
    rec_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if SF_ENABLED:
        try:
            with sf_cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {DEFECTS_TABLE}
                        (ID, VIN, JOB_NUMBER, PART_NAME, SUBSYSTEM, COMPONENT, ZONE,
                         FAILURE_MODE, DS_CODE, NOTES, UVEYE_PHOTO, UVEYE_SOURCED,
                         SUBMITTED_BY, SUBMITTED_AT)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP())""",
                    (rec_id, d.vin.strip().upper(), d.job_number, d.part_name,
                     d.subsystem, d.component, d.zone, d.failure_mode, d.ds_code,
                     d.notes, d.uveye_photo, d.uveye_sourced, d.submitted_by),
                )
        except Exception as e:
            raise HTTPException(502, f"Snowflake insert failed: {e}")
    else:
        conn = _local_conn()
        with conn:
            conn.execute(
                """INSERT INTO quality_inspection_defects
                   (id, vin, job_number, part_name, subsystem, component, zone,
                    failure_mode, ds_code, notes, uveye_photo, uveye_sourced,
                    submitted_by, submitted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec_id, d.vin.strip().upper(), d.job_number, d.part_name,
                 d.subsystem, d.component, d.zone, d.failure_mode, d.ds_code,
                 d.notes, d.uveye_photo, int(d.uveye_sourced), d.submitted_by, now),
            )
        conn.close()

    return {"ok": True, "id": rec_id, "backend": "local" if (LOCAL_MODE or not SF_ENABLED) else "snowflake"}


# ------------------------------------------------------------------
# GET /defects/{vin} — previously submitted defects for a VIN
# ------------------------------------------------------------------
@router.get("/defects/{vin}")
def list_defects(vin: str):
    vin = vin.strip().upper()
    if SF_ENABLED:
        try:
            with sf_cursor() as cur:
                cur.execute(
                    f"""SELECT ID, VIN, JOB_NUMBER, PART_NAME, SUBSYSTEM, COMPONENT,
                               ZONE, FAILURE_MODE, DS_CODE, NOTES, UVEYE_PHOTO,
                               UVEYE_SOURCED, SUBMITTED_BY, SUBMITTED_AT
                        FROM {DEFECTS_TABLE}
                        WHERE VIN = %s ORDER BY SUBMITTED_AT DESC""",
                    (vin,),
                )
                rows = cur.fetchall()
            return {"vin": vin, "defects": [
                {k.lower(): (str(v) if k == "SUBMITTED_AT" else v) for k, v in r.items()}
                for r in rows
            ]}
        except Exception as e:
            raise HTTPException(502, f"Snowflake query failed: {e}")

    conn = _local_conn()
    rows = conn.execute(
        "SELECT * FROM quality_inspection_defects WHERE vin=? ORDER BY submitted_at DESC",
        (vin,),
    ).fetchall()
    conn.close()
    return {"vin": vin, "defects": [dict(r) for r in rows]}
