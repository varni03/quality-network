"""
Quality App v2 — router for Vehicle 360, 510 Report, Holds, Sign-Off.
Drop next to quality_inspection_api.py (it reuses that module's cached
Snowflake connection so SSO auth happens once for both routers).

Wire-up in main.py (below the existing include):
    from quality_app_api import router as qa_router
    app.include_router(qa_router)
"""

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# reuse the shared, cached Snowflake connection + flags
from quality_inspection_api import sf_cursor, SF_ENABLED, LOCAL_MODE

router = APIRouter(prefix="/api/quality-app", tags=["quality-app"])

QMS_UNIT = "ANALYTICS_PROD.STAGING.STG_QMS_QUALITY_UNIT"
QMS_DEFECT = "ANALYTICS_PROD.STAGING.STG_QMS_UNIT_DEFECT"
Q2O_DATES = "ANALYTICS_PROD.REFINED.MART_OPS_Q2O_JOBDATA_OPER_DATES"
MY_DEFECTS = "ANALYTICS_DEV.APP.QUALITY_INSPECTION_DEFECTS"
HOLDS = "ANALYTICS_DEV.APP.QUALITY_HOLDS"
SIGNOFFS = "ANALYTICS_DEV.APP.QUALITY_SIGNOFFS"


def _require_sf():
    if not SF_ENABLED:
        raise HTTPException(503, "Snowflake connection not configured on this deployment")


def _rows(cur):
    return [{k.lower(): (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
            for r in cur.fetchall()]


# ------------------------------------------------------------------
# Vehicle 360 — everything known about one VIN in one call
# ------------------------------------------------------------------
_veh_cache = {}          # vin -> (ts, payload)
VEH_TTL = 45


@router.get("/vehicle/{vin}")
def vehicle_360(vin: str, sections: Optional[str] = None):
    """The full record is six Snowflake round-trips, which is right for Vehicle
    360 — but Holds and Sign-off were paying all six to read one slice. Pass
    `sections=holds` (comma separated) to fetch only what you render. Results
    are cached per VIN for VEH_TTL so tabbing between views is instant."""
    import time
    _require_sf()
    vin = vin.strip().upper()
    want = {s.strip() for s in sections.split(",") if s.strip()} if sections else None

    now = time.time()
    hit = _veh_cache.get(vin)
    if hit and (want is None or hit[1].get("_full")):
        age = now - hit[0]
        if age < VEH_TTL:
            return hit[1]
        if age < VEH_TTL * 8:            # stale but recent — serve now, refresh behind
            _revalidate(lambda: vehicle_360(vin))
            return hit[1]

    out = {"vin": vin, "legacy_defects": [], "app_defects": [], "holds": [],
           "signoffs": [], "location": None, "location_history": [], "checkin": None}

    def wants(k):
        return want is None or k in want

    try:
        with sf_cursor() as cur:
            if wants("legacy_defects"):
                # ACCSTATION ("Accountable Station" in the legacy Sign-Off grid)
                # isn't present on every QMS deployment, so try it and fall back
                # rather than failing the whole vehicle record over one column.
                base = """SELECT d.QDID AS DEFECT_REF, d.SUBSYSTEM, d.COMPONENT, d.ZONE,
                                 d.FAILUREMODE AS FAIL_MODE, d.QTYCODE AS DS_CODE,
                                 d.ACCDEPT AS DEPT, {station} d.PHOTOURL, d.CREATEDBY,
                                 d.CREATEDATE, d.REPAIRREQUIRED
                          FROM {unit} u
                          JOIN {defect} d ON u.ID = d.UNITID
                          WHERE UPPER(u.SERIALNUM) = %s AND COALESCE(d.REMOVED, FALSE) = FALSE
                          ORDER BY d.CREATEDATE DESC
                          LIMIT 200"""
                try:
                    cur.execute(base.format(station="d.ACCSTATION AS ACC_STATION,",
                                            unit=QMS_UNIT, defect=QMS_DEFECT), (vin,))
                except Exception:
                    cur.execute(base.format(station="", unit=QMS_UNIT, defect=QMS_DEFECT), (vin,))
                out["legacy_defects"] = _rows(cur)

            if wants("app_defects"):
                cur.execute(
                    f"""SELECT ID, SUBSYSTEM, COMPONENT, ZONE, FAILURE_MODE AS FAIL_MODE,
                               DS_CODE, NOTES, UVEYE_SOURCED, SUBMITTED_BY, SUBMITTED_AT
                        FROM {MY_DEFECTS} WHERE VIN = %s ORDER BY SUBMITTED_AT DESC""",
                    (vin,),
                )
                out["app_defects"] = _rows(cur)

            if wants("holds"):
                cur.execute(
                    f"""SELECT ID, HOLD_TYPE, NOTES, ON_HOLD, CREATED_BY, CREATED_AT,
                               RELEASED_BY, RELEASED_AT
                        FROM {HOLDS} WHERE VIN = %s ORDER BY CREATED_AT DESC""",
                    (vin,),
                )
                out["holds"] = _rows(cur)

            if wants("signoffs"):
                cur.execute(
                    f"""SELECT ID, DEFECT_REF, SUBSYSTEM, COMPONENT, FAIL_MODE, DEPT,
                               SIGNED_BY, SIGNED_AT, NOTES
                        FROM {SIGNOFFS} WHERE VIN = %s ORDER BY SIGNED_AT DESC""",
                    (vin,),
                )
                out["signoffs"] = _rows(cur)

            if wants("location"):
                cur.execute(
                    f"""SELECT LOCATION, NOTES, MOVED_BY, MOVED_AT
                        FROM {LOCATION_TBL}
                        WHERE VIN = %s ORDER BY MOVED_AT DESC LIMIT 12""", (vin,))
                hist = _rows(cur)
                out["location_history"] = hist
                out["location"] = hist[0]["location"] if hist else None

            if wants("checkin"):
                cur.execute(
                    f"""SELECT * FROM {CHECKIN_TBL}
                        WHERE VIN = %s ORDER BY CHECKED_AT DESC LIMIT 1""", (vin,))
                ci = _rows(cur)
                out["checkin"] = ci[0] if ci else None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")

    out["_full"] = want is None
    if want is None:                     # only cache complete records
        _veh_cache[vin] = (now, out)
    return out


def _invalidate_vehicle(vin: str):
    _veh_cache.pop((vin or "").strip().upper(), None)


# ------------------------------------------------------------------
# 510 Report — production milestone completions per job/VIN
# ------------------------------------------------------------------
@router.get("/report-510")
def report_510(start: Optional[str] = None, end: Optional[str] = None, q: Optional[str] = None):
    _require_sf()
    where, params = ["1=1"], []
    if start:
        where.append("REQDUEDATE >= %s"); params.append(start)
    if end:
        where.append("REQDUEDATE <= %s"); params.append(end)
    if q:
        where.append("(JOBNUM ILIKE %s OR VINSERIAL_C ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT JOBNUM, VINSERIAL_C AS VIN, PARTNUM, PRODCODE, REQDUEDATE,
                           COMPLETEDATE_310, COMPLETEDATE_410, COMPLETEDATE_510,
                           COMPLETEDATE_710, IS_310D, IS_410D, IS_510D, IS_710D
                    FROM {Q2O_DATES}
                    WHERE {' AND '.join(where)}
                    ORDER BY REQDUEDATE DESC
                    LIMIT 500""",
                tuple(params),
            )
            return {"rows": _rows(cur)}
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")


# ------------------------------------------------------------------
# Holds
# ------------------------------------------------------------------
class HoldCreate(BaseModel):
    vin: str = Field(..., min_length=5)
    job_number: Optional[str] = None
    hold_type: str
    notes: Optional[str] = None
    created_by: Optional[str] = None


class HoldRelease(BaseModel):
    released_by: Optional[str] = None


@router.post("/holds")
def create_hold(h: HoldCreate):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""INSERT INTO {HOLDS} (VIN, JOB_NUMBER, HOLD_TYPE, NOTES, CREATED_BY)
                    VALUES (%s,%s,%s,%s,%s)""",
                (h.vin.strip().upper(), h.job_number, h.hold_type, h.notes, h.created_by),
            )
        _invalidate_vehicle(h.vin)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Snowflake insert failed: {e}")


@router.post("/holds/{hold_id}/release")
def release_hold(hold_id: str, body: HoldRelease):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""UPDATE {HOLDS}
                    SET ON_HOLD = FALSE, RELEASED_BY = %s, RELEASED_AT = CURRENT_TIMESTAMP()
                    WHERE ID = %s""",
                (body.released_by, hold_id),
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Snowflake update failed: {e}")


# ------------------------------------------------------------------
# Sign-Off
# ------------------------------------------------------------------
class SignoffCreate(BaseModel):
    vin: str = Field(..., min_length=5)
    defect_ref: Optional[str] = None
    subsystem: Optional[str] = None
    component: Optional[str] = None
    fail_mode: Optional[str] = None
    dept: Optional[str] = None
    signed_by: Optional[str] = None
    notes: Optional[str] = None


@router.post("/signoffs")
def create_signoff(s: SignoffCreate):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SIGNOFFS}
                    (VIN, DEFECT_REF, SUBSYSTEM, COMPONENT, FAIL_MODE, DEPT, SIGNED_BY, NOTES)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (s.vin.strip().upper(), s.defect_ref, s.subsystem, s.component,
                 s.fail_mode, s.dept, s.signed_by, s.notes),
            )
        _invalidate_vehicle(s.vin)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Snowflake insert failed: {e}")


# ------------------------------------------------------------------
# Pulse — powers the "Today" mission-control home
# ------------------------------------------------------------------
MAPPING_VIEW = "ANALYTICS_PROD.REFINED.MART_OPS_UVEYE_QUALITY_MAPPING"
VIN_DEFECTS = "ANALYTICS_PROD.REFINED.MART_OPS_UVEYE_VIN_DEFECTS"
UVEYE_INSPECTION = "ANALYTICS_PROD.STAGING.STG_UVEYE_INSPECTION"


_pulse_cache = {"at": 0.0, "data": None}
PULSE_TTL = 90        # seconds — a floor overview is fine slightly stale
def _pulse_disk_path():
    """Deliberately NOT next to this file. uvicorn --reload watches the source
    folder, so writing the cache there made the server restart itself every
    time pulse refreshed — which wiped the in-memory cache and forced the next
    load to go cold again. It was fighting itself in a loop. Temp dir is
    outside the watcher, so writes are invisible to --reload."""
    import tempfile, getpass
    try:
        who = getpass.getuser()
    except Exception:
        who = "user"
    return os.path.join(tempfile.gettempdir(), f"qapp_pulse_cache_{who}{'_local' if LOCAL_MODE else ''}.json")


PULSE_DISK = _pulse_disk_path()
PULSE_DISK_MAX = 3600  # serve from disk up to an hour old rather than make anyone wait


def _pulse_from_disk():
    """uvicorn --reload restarts the process on every file save, which wiped the
    in-memory cache and made you pay a cold Snowflake load again and again.
    Persisting the payload means a restart costs nothing — the page paints from
    disk immediately and refreshes behind the scenes."""
    import json, time
    try:
        with open(PULSE_DISK) as f:
            blob = json.load(f)
        if time.time() - blob.get("at", 0) < PULSE_DISK_MAX:
            return blob
    except Exception:
        pass
    return None


def _pulse_to_disk(at, data):
    import json
    try:
        with open(PULSE_DISK, "w") as f:
            json.dump({"at": at, "data": data}, f)
    except Exception:
        pass


@router.get("/pulse")
def pulse(refresh: bool = False):
    """Floor overview. Four Snowflake round-trips cost ~12s cold, and every
    user opening Today paid it. Cached for PULSE_TTL so only the first caller
    after expiry waits; pass ?refresh=true to force a rebuild."""
    import time
    now = time.time()
    _t0 = time.perf_counter()

    # cold process (e.g. just after a --reload): rehydrate from disk
    if not refresh and _pulse_cache["data"] is None:
        blob = _pulse_from_disk()
        if blob:
            _pulse_cache["at"], _pulse_cache["data"] = blob["at"], blob["data"]
            print(f"[pulse] rehydrated from disk ({PULSE_DISK})")
        else:
            print(f"[pulse] no disk cache at {PULSE_DISK} — this load will be cold")

    if not refresh and _pulse_cache["data"]:
        age = now - _pulse_cache["at"]
        if age < PULSE_TTL:
            print(f"[pulse] CACHE HIT  {(time.perf_counter()-_t0)*1000:.0f}ms  (age {age:.0f}s)")
            return _pulse_cache["data"]
        # expired but usable: hand it over now, rebuild in the background
        if not _pulse_cache.get("building"):
            _pulse_cache["building"] = True
            def _rebuild():
                try: pulse(refresh=True)
                finally: _pulse_cache["building"] = False
            _revalidate(_rebuild)
        print(f"[pulse] STALE HIT  {(time.perf_counter()-_t0)*1000:.0f}ms  (age {age:.0f}s, refreshing behind)")
        return _pulse_cache["data"]
    _require_sf()
    out = {"recent_scans": [], "open_holds": [], "recent_submissions": [], "recent_signoffs": []}
    try:
        with sf_cursor() as cur:
            # Latest scanned vehicles.
            # This used to GROUP BY over MART_OPS_UVEYE_QUALITY_MAPPING, which is
            # a view on a view — returning 12 rows forced Snowflake to build the
            # whole thing (6 joins + a dedup aggregate + a CASE/GROUP BY over 88k
            # QMS rows) first. Measured at 10.1s, and it was the single slowest
            # thing in the app. Split into a cheap ORDER BY/LIMIT on the base
            # inspection table, then count defects for just those VINs.
            cur.execute(
                f"""SELECT VIN, MAKE, MODEL, INSPECTED_AT
                    FROM {UVEYE_INSPECTION}
                    WHERE VIN IS NOT NULL AND VIN != ''
                      AND COALESCE(IS_SCAN_INVALID, FALSE) = FALSE
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY VIN ORDER BY INSPECTED_AT DESC) = 1
                    ORDER BY INSPECTED_AT DESC
                    LIMIT 12"""
            )
            scans = _rows(cur)
            if scans:
                ph = ",".join(["%s"] * len(scans))
                cur.execute(
                    f"""SELECT VIN, COUNT(*) AS DEFECTS
                        FROM {VIN_DEFECTS}
                        WHERE VIN IN ({ph})
                        GROUP BY VIN""",
                    tuple(s["vin"] for s in scans),
                )
                cnt = {r["vin"]: r["defects"] for r in _rows(cur)}
                for s in scans:
                    s["defects"] = cnt.get(s["vin"], 0)
            out["recent_scans"] = scans

            cur.execute(
                f"""SELECT ID, VIN, HOLD_TYPE, NOTES, CREATED_BY, CREATED_AT
                    FROM {HOLDS} WHERE ON_HOLD = TRUE
                    ORDER BY CREATED_AT DESC LIMIT 8"""
            )
            out["open_holds"] = _rows(cur)

            cur.execute(
                f"""SELECT VIN, SUBSYSTEM, COMPONENT, FAILURE_MODE AS FAIL_MODE,
                           UVEYE_SOURCED, SUBMITTED_BY, SUBMITTED_AT
                    FROM {MY_DEFECTS} ORDER BY SUBMITTED_AT DESC LIMIT 8"""
            )
            out["recent_submissions"] = _rows(cur)

            cur.execute(
                f"""SELECT VIN, DEPT, SIGNED_BY, SIGNED_AT
                    FROM {SIGNOFFS} ORDER BY SIGNED_AT DESC LIMIT 6"""
            )
            out["recent_signoffs"] = _rows(cur)

            # ── plant state.
            # These were four separate queries — four network round-trips for
            # four numbers. Snowflake will happily compute all of them in one
            # statement via scalar subqueries, so that's three trips saved on
            # every cold load of Today.
            cur.execute(
                f"""SELECT
                      (SELECT COUNT(DISTINCT u.SERIALNUM)
                         FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                        WHERE d.REPAIRREQUIRED = TRUE
                          AND COALESCE(d.REMOVED, FALSE) = FALSE)          AS UNITS_OPEN,
                      (SELECT COUNT(*)
                         FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                        WHERE d.REPAIRREQUIRED = TRUE
                          AND COALESCE(d.REMOVED, FALSE) = FALSE)          AS DEFECTS_OPEN,
                      (SELECT COUNT(*)   FROM {HOLDS} WHERE ON_HOLD = TRUE) AS HOLDS_OPEN,
                      (SELECT COUNT(DISTINCT VIN) FROM {HOLDS} WHERE ON_HOLD = TRUE) AS UNITS_HELD,
                      (SELECT COUNT(*)   FROM {SIGNOFFS}
                        WHERE SIGNED_AT >= DATEADD(hour,-24,CURRENT_TIMESTAMP())) AS SIGNOFFS_24H,
                      (SELECT COUNT(*)   FROM {MY_DEFECTS}
                        WHERE SUBMITTED_AT >= DATEADD(hour,-24,CURRENT_TIMESTAMP())) AS LOGGED_24H"""
            )
            st = _rows(cur)
            r0 = st[0] if st else {}
            out["stats"] = {
                "units_open":   r0.get("units_open", 0),
                "defects_open": r0.get("defects_open", 0),
                "holds_open":   r0.get("holds_open", 0),
                "units_held":   r0.get("units_held", 0),
                "signoffs_24h": r0.get("signoffs_24h", 0),
                "logged_24h":   r0.get("logged_24h", 0),
            }

            # ── triage: what actually needs a human first
            cur.execute(
                f"""SELECT u.SERIALNUM AS VIN,
                           COUNT(*) AS OPEN_DEFECTS,
                           MAX(d.CREATEDATE) AS LATEST,
                           DATEDIFF(day, MIN(d.CREATEDATE), CURRENT_DATE()) AS AGE_DAYS,
                           ANY_VALUE(d.ACCDEPT) AS DEPT
                    FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                    WHERE d.REPAIRREQUIRED = TRUE
                      AND COALESCE(d.REMOVED, FALSE) = FALSE
                    GROUP BY u.SERIALNUM
                    ORDER BY OPEN_DEFECTS DESC
                    LIMIT 40"""
            )
            cand = _rows(cur)
            held = {r["vin"] for r in out["open_holds"]}
            locs = {}
            if cand:
                ph = ",".join(["%s"] * len(cand))
                cur.execute(f"SELECT VIN, LOCATION FROM {CURRENT_LOC} WHERE VIN IN ({ph})",
                            tuple(r["vin"] for r in cand))
                locs = {r["vin"]: r["location"] for r in _rows(cur)}
            for r in cand:
                r["on_hold"] = r["vin"] in held
                r["location"] = locs.get(r["vin"])
                n, age = r["open_defects"] or 0, r["age_days"] or 0
                if r["on_hold"]:   r["why"], r["rank"] = "On hold — cannot ship", 0
                elif n >= 15:      r["why"], r["rank"] = f"{n} open defects", 1
                elif age >= 21:    r["why"], r["rank"] = f"Open {age} days", 2
                elif n >= 8:       r["why"], r["rank"] = f"{n} open defects", 3
                else:              r["why"], r["rank"] = f"{n} open", 4
            cand.sort(key=lambda r: (r["rank"], -(r["open_defects"] or 0)))
            out["attention"] = cand[:8]
    except Exception as e:
        # serve stale rather than break the dashboard, if we have anything
        if _pulse_cache["data"]:
            return _pulse_cache["data"]
        raise HTTPException(502, f"Snowflake query failed: {e}")
    _pulse_cache.update(at=now, data=out)
    _pulse_to_disk(now, out)
    print(f"[pulse] REBUILT    {(time.perf_counter()-_t0)*1000:.0f}ms  (queried Snowflake, cached to disk)")
    return out


# ══════════════════════════════════════════════════════════════════
# v2.1 — Repair queue, location tracking, vehicle check-in
# ══════════════════════════════════════════════════════════════════
LOCATION_TBL = "ANALYTICS_DEV.APP.QUALITY_UNIT_LOCATION"
CURRENT_LOC = "ANALYTICS_DEV.APP.VW_UNIT_CURRENT_LOCATION"
CHECKIN_TBL = "ANALYTICS_DEV.APP.QUALITY_CHECKIN"

LOCATIONS = ["Quality", "VCD Lot", "Bone Yard", "Shipping", "Line", "Rework", "Tunnel"]


def _revalidate(fn):
    """Run a cache rebuild off the request thread. The caller gets whatever we
    already have, immediately; the fresh copy lands for whoever asks next."""
    import threading
    t = threading.Thread(target=fn, daemon=True)
    t.start()


def _warm():
    """Pre-build the caches a few seconds after boot so the first real user
    doesn't pay for a cold Snowflake warehouse. Off by default because with
    SSO it would trigger a browser prompt on every --reload; set SF_WARM=1
    once you've moved to key-pair auth."""
    import os as _os, time as _t
    if _os.getenv("SF_WARM", "0") != "1":
        return
    def run():
        _t.sleep(3)
        for fn, name in ((lambda: pulse(refresh=True), "pulse"), (depts, "depts")):
            try:
                t0 = _t.perf_counter(); fn()
                print(f"[warm] {name} ready in {_t.perf_counter()-t0:.1f}s")
            except Exception as e:
                print(f"[warm] {name} failed: {e}")
    _revalidate(run)


@router.get("/locations")
def locations():
    return {"locations": LOCATIONS}


# ── Repair queue: the legacy app spread this across six near-identical
#    "Approve" screens (by responsible party, by plant/line, by location...).
#    It's one query with filters, returning units and their open issue counts.
@router.get("/repair-queue")
def repair_queue(dept: Optional[str] = None, location: Optional[str] = None,
                 q: Optional[str] = None, limit: int = 120):
    _require_sf()
    # without a date bound this scanned the entire defect history on every open
    where, params = ["COALESCE(d.REMOVED, FALSE) = FALSE", "d.REPAIRREQUIRED = TRUE",
                     "d.CREATEDATE >= DATEADD(day,-180,CURRENT_DATE())"], []
    if dept:
        where.append("d.ACCDEPT = %s"); params.append(dept)
    if q:
        where.append("u.SERIALNUM ILIKE %s"); params.append(f"%{q}%")
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT u.SERIALNUM AS VIN,
                           COUNT(*) AS OPEN_DEFECTS,
                           COUNT(DISTINCT d.ACCDEPT) AS DEPT_COUNT,
                           MAX(d.CREATEDATE) AS LATEST,
                           MIN(d.QTYCODE) AS TOP_DS,
                           ANY_VALUE(d.SUBSYSTEM) AS SAMPLE_SUBSYSTEM,
                           ANY_VALUE(d.ACCDEPT) AS DEPT
                    FROM {QMS_UNIT} u
                    JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                    WHERE {' AND '.join(where)}
                    GROUP BY u.SERIALNUM
                    ORDER BY OPEN_DEFECTS DESC, LATEST DESC
                    LIMIT {int(limit)}""",
                tuple(params),
            )
            rows = _rows(cur)

            # attach current location + open hold count in two small lookups
            vins = [r["vin"] for r in rows if r.get("vin")]
            locmap, holdmap = {}, {}
            if vins:
                ph = ",".join(["%s"] * len(vins))
                cur.execute(f"SELECT VIN, LOCATION FROM {CURRENT_LOC} WHERE VIN IN ({ph})", tuple(vins))
                locmap = {r["vin"]: r["location"] for r in _rows(cur)}
                cur.execute(
                    f"""SELECT VIN, COUNT(*) C FROM {HOLDS}
                        WHERE ON_HOLD = TRUE AND VIN IN ({ph}) GROUP BY VIN""", tuple(vins))
                holdmap = {r["vin"]: r["c"] for r in _rows(cur)}
            for r in rows:
                r["location"] = locmap.get(r["vin"])
                r["open_holds"] = holdmap.get(r["vin"], 0)
            if location:
                rows = [r for r in rows if (r["location"] or "") == location]
        return {"rows": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")


_depts_cache = {"at": 0.0, "data": None}


@router.get("/depts")
def depts():
    import time
    now = time.time()
    if _depts_cache["data"] and (now - _depts_cache["at"]) < 1800:
        return _depts_cache["data"]
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT ACCDEPT AS DEPT FROM {QMS_DEFECT}
                    WHERE ACCDEPT IS NOT NULL AND ACCDEPT != '' ORDER BY 1""")
            res = {"depts": [r["dept"] for r in _rows(cur)]}
            _depts_cache.update(at=now, data=res)
            return res
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")


class LocationMove(BaseModel):
    vin: str = Field(..., min_length=5)
    location: str
    notes: Optional[str] = None
    moved_by: Optional[str] = None


@router.post("/location")
def move_location(m: LocationMove):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""INSERT INTO {LOCATION_TBL} (VIN, LOCATION, NOTES, MOVED_BY)
                    VALUES (%s,%s,%s,%s)""",
                (m.vin.strip().upper(), m.location, m.notes, m.moved_by),
            )
        _invalidate_vehicle(m.vin)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Snowflake insert failed: {e}")


# ── Vehicle check-in (legacy Plant 7 Inspection / Tunnel Check-In form)
class CheckIn(BaseModel):
    vin: str = Field(..., min_length=5)
    job_number: Optional[str] = None
    stage: str = "Plant 7"
    mileage: Optional[int] = None
    gas_gauge: Optional[str] = None
    oem_keys: Optional[int] = None
    battery_volts: Optional[float] = None
    tire_size: Optional[str] = None
    seat_color: Optional[str] = None
    seat_material: Optional[str] = None
    seat_heated: bool = False
    seat_vented: bool = False
    has_spare: bool = False
    has_headphones: bool = False
    has_remotes: bool = False
    has_antenna: bool = False
    notes: Optional[str] = None
    checked_by: Optional[str] = None


@router.post("/checkin")
def create_checkin(c: CheckIn):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""INSERT INTO {CHECKIN_TBL}
                    (VIN, JOB_NUMBER, STAGE, MILEAGE, GAS_GAUGE, OEM_KEYS, BATTERY_VOLTS,
                     TIRE_SIZE, SEAT_COLOR, SEAT_MATERIAL, SEAT_HEATED, SEAT_VENTED,
                     HAS_SPARE, HAS_HEADPHONES, HAS_REMOTES, HAS_ANTENNA, NOTES, CHECKED_BY)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (c.vin.strip().upper(), c.job_number, c.stage, c.mileage, c.gas_gauge,
                 c.oem_keys, c.battery_volts, c.tire_size, c.seat_color, c.seat_material,
                 c.seat_heated, c.seat_vented, c.has_spare, c.has_headphones,
                 c.has_remotes, c.has_antenna, c.notes, c.checked_by),
            )
        _invalidate_vehicle(c.vin)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Snowflake insert failed: {e}")


@router.get("/checkin/{vin}")
def get_checkins(vin: str):
    _require_sf()
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT * FROM {CHECKIN_TBL} WHERE VIN = %s
                    ORDER BY CHECKED_AT DESC LIMIT 10""", (vin.strip().upper(),))
            return {"vin": vin.upper(), "checkins": _rows(cur)}
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")


_warm()


# ══════════════════════════════════════════════════════════════════
# Trends — the question the app couldn't answer: are we improving?
# ══════════════════════════════════════════════════════════════════
_trend_cache = {"at": 0.0, "data": None}
TREND_TTL = 600


@router.get("/trends")
def trends(weeks: int = 12, refresh: bool = False):
    """Defects over time, by department, by failure mode. Everything on
    Today is a snapshot — a supervisor's actual first question is whether
    the number is better or worse than last week, and nothing in the app
    could answer that. Cached hard (10 min) since it aggregates history."""
    import time
    now = time.time()
    if not refresh and _trend_cache["data"] and (now - _trend_cache["at"]) < TREND_TTL:
        return _trend_cache["data"]
    _require_sf()

    weeks = max(4, min(int(weeks), 26))
    out = {"weekly": [], "by_dept": [], "by_mode": [], "delta": {}}
    try:
        with sf_cursor() as cur:
            # one row per week: how many defects, across how many vehicles
            cur.execute(
                f"""SELECT DATE_TRUNC('week', d.CREATEDATE) AS WK,
                           COUNT(*) AS DEFECTS,
                           COUNT(DISTINCT u.SERIALNUM) AS UNITS
                    FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                    WHERE COALESCE(d.REMOVED, FALSE) = FALSE
                      AND d.CREATEDATE >= DATEADD(week, -{weeks}, CURRENT_DATE())
                    GROUP BY 1 ORDER BY 1"""
            )
            wk = _rows(cur)
            for r in wk:
                r["per_unit"] = round((r["defects"] or 0) / (r["units"] or 1), 1)
            out["weekly"] = wk

            # this week vs last — the actual comparison a supervisor makes
            if len(wk) >= 2:
                cur_w, prev_w = wk[-1], wk[-2]
                def pct(a, b):
                    return None if not b else round(((a - b) / b) * 100, 1)
                out["delta"] = {
                    "defects": cur_w["defects"], "defects_prev": prev_w["defects"],
                    "defects_pct": pct(cur_w["defects"] or 0, prev_w["defects"] or 0),
                    "per_unit": cur_w["per_unit"], "per_unit_prev": prev_w["per_unit"],
                    "per_unit_pct": pct(cur_w["per_unit"] or 0, prev_w["per_unit"] or 0),
                }

            cur.execute(
                f"""SELECT COALESCE(NULLIF(TRIM(d.ACCDEPT),''),'Unassigned') AS DEPT,
                           COUNT(*) AS DEFECTS
                    FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                    WHERE COALESCE(d.REMOVED, FALSE) = FALSE
                      AND d.CREATEDATE >= DATEADD(week, -{weeks}, CURRENT_DATE())
                    GROUP BY 1 ORDER BY DEFECTS DESC LIMIT 8"""
            )
            out["by_dept"] = _rows(cur)

            cur.execute(
                f"""SELECT COALESCE(NULLIF(TRIM(d.FAILUREMODE),''),'Unspecified') AS MODE,
                           COUNT(*) AS DEFECTS
                    FROM {QMS_UNIT} u JOIN {QMS_DEFECT} d ON u.ID = d.UNITID
                    WHERE COALESCE(d.REMOVED, FALSE) = FALSE
                      AND d.FAILUREMODE != 'Content Photo'
                      AND d.CREATEDATE >= DATEADD(week, -{weeks}, CURRENT_DATE())
                    GROUP BY 1 ORDER BY DEFECTS DESC LIMIT 8"""
            )
            out["by_mode"] = _rows(cur)
    except Exception as e:
        if _trend_cache["data"]:
            return _trend_cache["data"]
        raise HTTPException(502, f"Snowflake query failed: {e}")

    _trend_cache.update(at=now, data=out)
    return out


# ------------------------------------------------------------------
# Activity — plant-wide sign-off and hold feed for the Audit screen.
# Sign-offs and holds already carry who/when in Snowflake, so this reads
# them live instead of duplicating them into the app database.
# ------------------------------------------------------------------
_activity_cache = {"at": 0, "data": None}
ACTIVITY_TTL = 30


@router.get("/activity")
def plant_activity(days: int = 14, limit: int = 150):
    _require_sf()
    now = __import__("time").time()
    if _activity_cache["data"] and (now - _activity_cache["at"]) < ACTIVITY_TTL:
        return _activity_cache["data"]

    events = []
    try:
        with sf_cursor() as cur:
            cur.execute(
                f"""SELECT VIN, DEFECT_REF, SUBSYSTEM, COMPONENT, FAIL_MODE, DEPT,
                           SIGNED_BY, SIGNED_AT, NOTES
                    FROM {SIGNOFFS}
                    WHERE SIGNED_AT >= DATEADD(day, -{days}, CURRENT_DATE())
                    ORDER BY SIGNED_AT DESC LIMIT {limit}""")
            for r in _rows(cur):
                events.append({
                    "kind": "signoff", "vin": r.get("vin"),
                    "actor": r.get("signed_by"), "at": r.get("signed_at"),
                    "summary": f"Signed off {r.get('subsystem') or '—'} · {r.get('component') or '—'}"
                              + (f" ({r.get('dept')})" if r.get("dept") else ""),
                    "notes": r.get("notes") or "",
                })

            cur.execute(
                f"""SELECT VIN, HOLD_TYPE, NOTES, ON_HOLD, CREATED_BY, CREATED_AT,
                           RELEASED_BY, RELEASED_AT
                    FROM {HOLDS}
                    WHERE CREATED_AT >= DATEADD(day, -{days}, CURRENT_DATE())
                       OR RELEASED_AT >= DATEADD(day, -{days}, CURRENT_DATE())
                    ORDER BY CREATED_AT DESC LIMIT {limit}""")
            for r in _rows(cur):
                events.append({
                    "kind": "hold", "vin": r.get("vin"),
                    "actor": r.get("created_by"), "at": r.get("created_at"),
                    "summary": f"Placed hold — {r.get('hold_type') or 'Unspecified'}",
                    "notes": r.get("notes") or "",
                })
                if r.get("released_at"):
                    events.append({
                        "kind": "hold_released", "vin": r.get("vin"),
                        "actor": r.get("released_by"), "at": r.get("released_at"),
                        "summary": f"Released hold — {r.get('hold_type') or 'Unspecified'}",
                        "notes": "",
                    })
    except Exception as e:
        if _activity_cache["data"]:
            return _activity_cache["data"]
        raise HTTPException(502, f"Snowflake query failed: {e}")

    events = [e for e in events if e.get("at")]
    events.sort(key=lambda e: e["at"], reverse=True)
    out = {"events": events[:limit]}
    _activity_cache.update(at=now, data=out)
    return out


# ------------------------------------------------------------------
# Worklist — "what does my department owe, across the whole plant"
#
# Every other view here starts from a VIN. That's the wrong entry point for
# the person who actually clears defects: a WELD lead doesn't know which vans
# to look up, they want their queue. This is defect-level rather than
# vehicle-level, spans the plant, already excludes anything signed off, and
# carries the age of each item so the oldest work surfaces itself.
# ------------------------------------------------------------------
@router.get("/worklist")
def worklist(dept: Optional[str] = None, days: int = 180,
             include_signed: bool = False, q: Optional[str] = None,
             limit: int = 400):
    _require_sf()
    where = ["COALESCE(d.REMOVED, FALSE) = FALSE",
             f"d.CREATEDATE >= DATEADD(day, -{int(days)}, CURRENT_DATE())"]
    params = []
    if dept:
        where.append("UPPER(TRIM(d.ACCDEPT)) = %s")
        params.append(dept.strip().upper())
    if q:
        where.append("u.SERIALNUM ILIKE %s")
        params.append(f"%{q.strip()}%")

    try:
        with sf_cursor() as cur:
            # ACCSTATION isn't on every QMS deployment — same fallback as vehicle_360
            base = """SELECT u.SERIALNUM AS VIN, d.QDID AS DEFECT_REF,
                             d.SUBSYSTEM, d.COMPONENT, d.ZONE,
                             d.FAILUREMODE AS FAIL_MODE, d.QTYCODE AS DS_CODE,
                             d.ACCDEPT AS DEPT, {station} d.PHOTOURL,
                             d.CREATEDBY, d.CREATEDATE,
                             DATEDIFF(day, d.CREATEDATE, CURRENT_DATE()) AS AGE_DAYS
                      FROM {unit} u
                      JOIN {defect} d ON u.ID = d.UNITID
                      WHERE {where}
                      ORDER BY d.CREATEDATE ASC
                      LIMIT {limit}"""
            sql = dict(unit=QMS_UNIT, defect=QMS_DEFECT,
                       where=" AND ".join(where), limit=int(limit))
            try:
                cur.execute(base.format(station="d.ACCSTATION AS ACC_STATION,", **sql), tuple(params))
            except Exception:
                cur.execute(base.format(station="", **sql), tuple(params))
            rows = _rows(cur)

            vins = sorted({r["vin"] for r in rows if r.get("vin")})
            signed, locmap, holdmap = set(), {}, {}
            if vins:
                ph = ",".join(["%s"] * len(vins))
                if not include_signed:
                    cur.execute(
                        f"SELECT VIN, DEFECT_REF FROM {SIGNOFFS} WHERE VIN IN ({ph})",
                        tuple(vins))
                    signed = {(r["vin"], str(r["defect_ref"])) for r in _rows(cur)}
                cur.execute(
                    f"SELECT VIN, LOCATION FROM {CURRENT_LOC} WHERE VIN IN ({ph})", tuple(vins))
                locmap = {r["vin"]: r["location"] for r in _rows(cur)}
                cur.execute(
                    f"""SELECT VIN, COUNT(*) C FROM {HOLDS}
                        WHERE ON_HOLD = TRUE AND VIN IN ({ph}) GROUP BY VIN""", tuple(vins))
                holdmap = {r["vin"]: r["c"] for r in _rows(cur)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Snowflake query failed: {e}")

    out, buckets = [], {"fresh": 0, "week": 0, "twoweek": 0, "stale": 0}
    for r in rows:
        if not include_signed and (r["vin"], str(r.get("defect_ref"))) in signed:
            continue
        age = r.get("age_days") or 0
        try:
            age = int(age)
        except Exception:
            age = 0
        r["age_days"] = age
        r["location"] = locmap.get(r["vin"])
        r["on_hold"] = bool(holdmap.get(r["vin"]))
        key = "fresh" if age <= 3 else "week" if age <= 7 else "twoweek" if age <= 14 else "stale"
        r["age_bucket"] = key
        buckets[key] += 1
        out.append(r)

    by_vin = {}
    for r in out:
        by_vin.setdefault(r["vin"], []).append(r)

    return {
        "dept": (dept or "").upper(),
        "total": len(out),
        "vehicles": len(by_vin),
        "oldest_days": max((r["age_days"] for r in out), default=0),
        "on_hold_vehicles": sum(1 for v in by_vin if holdmap.get(v)),
        "buckets": buckets,
        "items": out,
    }
