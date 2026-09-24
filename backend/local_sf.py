"""
Local data mode: a SQLite stand-in for the Snowflake connection.

The Quality App tabs (Today, Inspection, Vehicle 360, Repair queue, My queue,
Holds, Sign-Off, Trends, 510 Report, Audit feed) were written against
Snowflake. This module lets them run with no Snowflake account at all:

  * build_db() creates a SQLite file with the same tables and views the app
    queries, filled from the seed data already in this repo
    (seed_data.json = UVeye scans, data/quality_seed.json = QMS defects).
  * The Snowflake-only pieces of SQL (DATEADD, DATEDIFF, QUALIFY, ILIKE,
    ANY_VALUE, DATE_TRUNC, CURRENT_DATE(), three-part table names, %s) are
    rewritten to SQLite on the way through, so the endpoints are unchanged.
  * Dates are shifted so the newest record is "today", which keeps trends,
    aging buckets and the 180-day windows populated whenever you run it.

Columns the seed files don't carry (department accountability, zone,
DS codes, holds, sign-offs, locations, check-ins, job numbers, 510 dates)
are generated deterministically. It is demo data, not Northwind data.

Turn it on/off with LOCAL_DATA=1 / LOCAL_DATA=0. If Snowflake credentials
are not configured, local mode is used automatically.
"""
import json
import os
import random
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
VERSION = "local-v1"

# Deliberately outside the source folder: uvicorn --reload watches it, and a
# rebuilt DB there would restart the server in a loop.
DB_PATH = os.getenv("LOCAL_DB_PATH") or os.path.join(tempfile.gettempdir(), "quality_app_local.db")

_build_lock = threading.Lock()
_ready = False


# ══════════════════════════════════════════════════════════════════
# SQL translation: Snowflake -> SQLite
# ══════════════════════════════════════════════════════════════════
_LOCAL = "'localtime'"


def _dateadd(m):
    unit, n, base = m.group(1).lower(), int(m.group(2)), m.group(3).upper()
    mult = {"day": 1, "week": 7, "hour": 1, "minute": 1}.get(unit.rstrip("s"))
    if mult is None:
        raise ValueError(f"DATEADD unit not supported locally: {unit}")
    if unit.startswith("hour"):
        return f"datetime('now',{_LOCAL},'{n} hours')"
    if unit.startswith("minute"):
        return f"datetime('now',{_LOCAL},'{n} minutes')"
    fn = "date" if "DATE" in base else "datetime"
    return f"{fn}('now',{_LOCAL},'{n * mult} days')"


_QUALIFY = re.compile(
    r"SELECT\s+(?P<cols>.*?)\s+FROM\s+(?P<tbl>\S+)\s+WHERE\s+(?P<where>.*?)\s+"
    r"QUALIFY\s+(?P<win>ROW_NUMBER\(\)\s+OVER\s*\(.*?\))\s*=\s*1(?P<tail>.*)$",
    re.S | re.I)


def translate(sql: str) -> str:
    s = sql
    # QUALIFY ROW_NUMBER() ... = 1  ->  filter on a windowed subquery
    m = _QUALIFY.search(s)
    if m:
        s = (f"SELECT {m['cols']} FROM (SELECT *, {m['win']} AS _RN FROM {m['tbl']} "
             f"WHERE {m['where']}) WHERE _RN = 1 {m['tail']}")
    # DB.SCHEMA.TABLE -> TABLE
    s = re.sub(r"\b[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.([A-Za-z0-9_]+)\b", r"\1", s)
    s = re.sub(r"DATEADD\(\s*(\w+)\s*,\s*(-?\d+)\s*,\s*(CURRENT_DATE|CURRENT_TIMESTAMP)\(\)\s*\)",
               _dateadd, s, flags=re.I)
    s = re.sub(r"DATEDIFF\(\s*day\s*,\s*(.+?)\s*,\s*CURRENT_DATE\(\)\s*\)",
               rf"CAST(julianday(date('now',{_LOCAL})) - julianday(\1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r"DATE_TRUNC\(\s*'week'\s*,\s*(.+?)\s*\)",
               r"date(\1,'weekday 0','-6 days')", s, flags=re.I)
    s = re.sub(r"CURRENT_TIMESTAMP\(\)", f"datetime('now',{_LOCAL})", s, flags=re.I)
    s = re.sub(r"CURRENT_DATE\(\)", f"date('now',{_LOCAL})", s, flags=re.I)
    s = re.sub(r"\bANY_VALUE\(", "MAX(", s, flags=re.I)
    s = re.sub(r"\bILIKE\b", "LIKE", s, flags=re.I)      # SQLite LIKE is case-insensitive for ASCII
    s = s.replace("%s", "?")
    return s


_BOOL_COL = re.compile(r"^(ON_HOLD|REPAIRREQUIRED|REMOVED|UVEYE_SOURCED|IS_.+|SEAT_HEATED|SEAT_VENTED|HAS_.+)$")


class LocalCursor:
    """Quacks like snowflake's DictCursor: rows are dicts with UPPERCASE keys."""

    def __init__(self, conn):
        self._conn = conn
        self._c = None

    def execute(self, sql, params=None, *a, **kw):
        self._c = self._conn.execute(translate(sql), tuple(params) if params is not None else ())
        return self

    def _row(self, r):
        return {k.upper(): (bool(v) if (v is not None and _BOOL_COL.match(k.upper())) else v)
                for k, v in dict(r).items()}

    def fetchall(self):
        return [self._row(r) for r in self._c.fetchall()]

    def fetchone(self):
        r = self._c.fetchone()
        return self._row(r) if r is not None else None

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


@contextmanager
def local_cursor():
    ensure_db()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield LocalCursor(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════
_UUID = "(lower(hex(randomblob(16))))"
_NOW = "(datetime('now','localtime'))"

SCHEMA = f"""
CREATE TABLE STG_QMS_QUALITY_UNIT (
  ID INTEGER PRIMARY KEY, SERIALNUM TEXT, PARTNUM TEXT, PRODCODE TEXT,
  PLANT TEXT, LINE TEXT, JOBNUM TEXT, JOBDUEDATE TEXT);
CREATE INDEX ix_unit_serial ON STG_QMS_QUALITY_UNIT(SERIALNUM);
CREATE TABLE STG_QMS_UNIT_DEFECT (
  ID INTEGER PRIMARY KEY, QDID INTEGER, UNITID INTEGER, INSPECTIONID TEXT,
  COMPONENT TEXT, SUBSYSTEM TEXT, ZONE TEXT, FAILUREMODE TEXT, QTYCODE TEXT,
  QTYSCORE INTEGER, ACCDEPT TEXT, ACCSTATION TEXT, CREATEDATE TEXT,
  CREATEDBY TEXT, REPAIRREQUIRED INTEGER, PHOTOURL TEXT, REMOVED INTEGER DEFAULT 0);
CREATE INDEX ix_def_unit ON STG_QMS_UNIT_DEFECT(UNITID);
CREATE INDEX ix_def_date ON STG_QMS_UNIT_DEFECT(CREATEDATE);
CREATE TABLE MART_OPS_Q2O_JOBDATA_OPER_DATES (
  JOBNUM TEXT, VINSERIAL_C TEXT, PARTNUM TEXT, PRODCODE TEXT, REQDUEDATE TEXT,
  COMPLETEDATE_310 TEXT, COMPLETEDATE_410 TEXT, COMPLETEDATE_510 TEXT, COMPLETEDATE_710 TEXT,
  IS_310D INTEGER, IS_410D INTEGER, IS_510D INTEGER, IS_710D INTEGER);
CREATE TABLE STG_UVEYE_INSPECTION (
  VIN TEXT, MAKE TEXT, MODEL TEXT, INSPECTED_AT TEXT, IS_SCAN_INVALID INTEGER DEFAULT 0);
CREATE TABLE MART_OPS_UVEYE_VIN_DEFECTS (
  INSPECTION_ID TEXT, VIN TEXT, MAKE TEXT, MODEL TEXT, YEAR TEXT, INSPECTED_AT TEXT,
  TOTAL_RECONDITIONING_COST REAL, PART_NAME TEXT, MODULE TEXT, DEFECT_DESCRIPTION TEXT,
  COST REAL, CROPPED_IMAGE TEXT);
CREATE INDEX ix_uvd_vin ON MART_OPS_UVEYE_VIN_DEFECTS(VIN);
CREATE TABLE MART_OPS_UVEYE_QUALITY_MAPPING (
  VIN TEXT, JOB_NUMBER TEXT, MAKE TEXT, MODEL TEXT, YEAR TEXT, INSPECTED_AT TEXT,
  PART_NAME TEXT, DEFECT_DESCRIPTION TEXT, COST REAL, CROPPED_IMAGE TEXT,
  SUB_SYSTEM TEXT, COMPONENT TEXT, ZONE TEXT, FAIL_MODE TEXT, DS_CODE TEXT,
  PREDICTION_QUALITY TEXT);
CREATE INDEX ix_map_vin ON MART_OPS_UVEYE_QUALITY_MAPPING(VIN);
CREATE TABLE VW_QUALITY_TAXONOMY (
  SUBSYSTEM TEXT, COMPONENT TEXT, ZONE TEXT, FAILURE_MODE TEXT, DS_CODE TEXT);

CREATE TABLE QUALITY_INSPECTION_DEFECTS (
  ID TEXT PRIMARY KEY DEFAULT {_UUID}, VIN TEXT, JOB_NUMBER TEXT, PART_NAME TEXT,
  SUBSYSTEM TEXT, COMPONENT TEXT, ZONE TEXT, FAILURE_MODE TEXT, DS_CODE TEXT,
  NOTES TEXT, UVEYE_PHOTO TEXT, UVEYE_SOURCED INTEGER DEFAULT 0, SUBMITTED_BY TEXT,
  SUBMITTED_AT TEXT DEFAULT {_NOW}, SOURCE TEXT DEFAULT 'QUALITY_NETWORK_V2');
CREATE TABLE QUALITY_HOLDS (
  ID TEXT PRIMARY KEY DEFAULT {_UUID}, VIN TEXT, JOB_NUMBER TEXT, HOLD_TYPE TEXT,
  NOTES TEXT, ON_HOLD INTEGER DEFAULT 1, CREATED_BY TEXT, CREATED_AT TEXT DEFAULT {_NOW},
  RELEASED_BY TEXT, RELEASED_AT TEXT);
CREATE TABLE QUALITY_SIGNOFFS (
  ID TEXT PRIMARY KEY DEFAULT {_UUID}, VIN TEXT, DEFECT_REF TEXT, SUBSYSTEM TEXT,
  COMPONENT TEXT, FAIL_MODE TEXT, DEPT TEXT, SIGNED_BY TEXT, SIGNED_AT TEXT DEFAULT {_NOW},
  NOTES TEXT);
CREATE TABLE QUALITY_UNIT_LOCATION (
  ID TEXT PRIMARY KEY DEFAULT {_UUID}, VIN TEXT, LOCATION TEXT, NOTES TEXT,
  MOVED_BY TEXT, MOVED_AT TEXT DEFAULT {_NOW});
CREATE VIEW VW_UNIT_CURRENT_LOCATION AS
  SELECT l.VIN AS VIN, l.LOCATION AS LOCATION FROM QUALITY_UNIT_LOCATION l
  WHERE l.MOVED_AT = (SELECT MAX(x.MOVED_AT) FROM QUALITY_UNIT_LOCATION x WHERE x.VIN = l.VIN)
  GROUP BY l.VIN;
CREATE TABLE QUALITY_CHECKIN (
  ID TEXT PRIMARY KEY DEFAULT {_UUID}, VIN TEXT, JOB_NUMBER TEXT, STAGE TEXT, MILEAGE INTEGER,
  GAS_GAUGE TEXT, OEM_KEYS INTEGER, BATTERY_VOLTS REAL, TIRE_SIZE TEXT, SEAT_COLOR TEXT,
  SEAT_MATERIAL TEXT, SEAT_HEATED INTEGER, SEAT_VENTED INTEGER, HAS_SPARE INTEGER,
  HAS_HEADPHONES INTEGER, HAS_REMOTES INTEGER, HAS_ANTENNA INTEGER, NOTES TEXT,
  CHECKED_BY TEXT, CHECKED_AT TEXT DEFAULT {_NOW});
CREATE TABLE _META (K TEXT PRIMARY KEY, V TEXT);
"""


# ══════════════════════════════════════════════════════════════════
# Data generation
# ══════════════════════════════════════════════════════════════════
COMPONENTS = {
    "Electrical": ["Wiring Harness", "Ramp Motor", "Interior Lighting", "Control Module", "Battery Tray"],
    "Ramp":       ["Ramp Deck", "Ramp Hinge", "Ramp Cable", "Threshold Plate", "Ramp Latch"],
    "Door":       ["Sliding Door", "Door Seal", "Door Handle", "Door Latch", "Door Track"],
    "Paint":      ["Body Panel", "Roof Panel", "Bumper Cover", "Fender", "Door Skin"],
    "Structure":  ["Floor Cutout", "Cross Member", "Weld Seam", "Reinforcement Plate", "Rear Frame"],
    "Floor":      ["Floor Pan", "Track System", "Securement Anchor", "Flooring", "Wheelchair Track"],
}
ZONES = {
    "Electrical": ["Under Dash", "Rear Cargo", "Ramp Bay"],
    "Ramp": ["Rear Threshold", "Ramp Deck", "Ramp Bay"],
    "Door": ["Passenger Side", "Driver Side", "Rear Opening"],
    "Paint": ["Exterior Left", "Exterior Right", "Roof", "Rear"],
    "Structure": ["Underbody", "Rear Frame", "Side Sill"],
    "Floor": ["Cabin Floor", "Rear Cargo", "Ramp Bay"],
}
PEOPLE = ["t.nguyen", "m.okafor", "r.patel", "s.kim", "d.alvarez", "l.johnson", "a.mehta", "c.brown"]
HOLD_TYPES = ["Hold In VCD Lot", "Hold For Audit", "Hold For Deviation Approval",
              "Engineering Hold", "LN1 Hold", "LN2 Hold", "LN4 Hold", "LN5 Hold"]
LOCATIONS = ["Quality", "VCD Lot", "Bone Yard", "Shipping", "Line", "Rework", "Tunnel"]

# UVeye part -> (sub-system, component, zone). Six part families are left
# unmapped on purpose, like the real mapping view (tires, antenna, mirror
# covers, sunroof, roof rack, alignment come back NULL for manual entry).
_UV_MAP = {
    "FenderRearRight": ("Body", "Fender", "Rear Right"), "FenderRearLeft": ("Body", "Fender", "Rear Left"),
    "FenderFrontRight": ("Body", "Fender", "Front Right"), "FenderFrontLeft": ("Body", "Fender", "Front Left"),
    "Roof": ("Body", "Roof Panel", "Roof"), "Trunk": ("Body", "Liftgate", "Rear"),
    "DoorFrontLeft": ("Door", "Front Door", "Front Left"), "DoorFrontRight": ("Door", "Front Door", "Front Right"),
    "DoorRearLeft": ("Door", "Rear Door", "Rear Left"), "DoorRearRight": ("Door", "Rear Door", "Rear Right"),
    "BumperFront": ("Body", "Bumper Cover", "Front"), "BumperRear": ("Body", "Bumper Cover", "Rear"),
    "Hood": ("Body", "Hood", "Front"), "LicensePlateRear": ("Body", "Plate Bracket", "Rear"),
    "Undercarriage": ("Structure", "Underbody", "Underbody"),
}


def _uv_fail_and_ds(desc: str):
    d = (desc or "").lower()
    if "dent" in d:      fm = "Dent"
    elif "crack" in d:   fm = "Crack"
    elif "chip" in d:    fm = "Paint Chip"
    elif "scuff" in d:   fm = "Scuff"
    elif "rust" in d:    fm = "Corrosion"
    elif "scratch" in d: fm = "Scratch"
    else:                fm = "Cosmetic Damage"
    if re.search(r"severe|major|large|deep|significant|extensive", d): ds = "DS09"
    elif re.search(r"medium|moderate", d):                              ds = "DS05"
    else:                                                               ds = "DS02"
    return fm, ds


def _load(path):
    with open(os.path.join(BASE, path), encoding="utf-8") as f:
        return json.load(f)


def _ts(day: date, rnd: random.Random, lo=6, hi=17):
    if day >= date.today():                 # never stamp a record in the future
        hi = min(hi, datetime.now().hour)
        lo = min(lo, hi)
    return f"{day.isoformat()} {rnd.randint(lo, hi):02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 59):02d}"


def build_db():
    rnd = random.Random(7)
    today = date.today()

    qms = _load("data/quality_seed.json")
    uv = _load("seed_data.json")

    q_max = max(date.fromisoformat(r["inspected_at"]) for r in qms)
    shift = timedelta(days=(today - q_max).days)

    tmp = DB_PATH + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)

    # ── UVeye ───────────────────────────────────────────────────────
    uv = [r for r in uv if r.get("vin") and len(r["vin"]) == 17]
    by_insp = {}
    for r in uv:
        by_insp.setdefault(r["inspection"], []).append(r)
    insp_total = {k: round(sum((x["cost"] or 0) for x in v), 2) for k, v in by_insp.items()}
    uv_vins = sorted({r["vin"] for r in uv})

    # ── QMS: give ~45 UVeye VINs a realistic pile of open QMS defects so
    #    Vehicle 360 shows both sources for the same van ────────────────
    rows = qms[:]
    rnd.shuffle(rows)
    heavy = rnd.sample(uv_vins, min(45, len(uv_vins)))
    i = 0
    for v in heavy:
        for _ in range(rnd.randint(6, 22)):
            if i < len(rows):
                rows[i] = dict(rows[i], vin=v)
                i += 1

    units, unit_id = {}, {}
    prod_by_conv = {"Side Entry": "VAN-SIDE", "Rear Entry": "VAN-REAR"}
    for r in sorted(rows, key=lambda x: x["inspected_at"]):
        v = r["vin"]
        if v not in units:
            jd = date.fromisoformat(r["inspected_at"]) + shift
            units[v] = dict(
                id=len(units) + 1, serial=v, prod=prod_by_conv.get(r["conversion"], "VAN"),
                part=f"{r['make'][:3].upper()}-{'RE' if r['conversion'] == 'Rear Entry' else 'SE'}-{rnd.randint(100, 999)}",
                plant=r["plant"], line=r["line"], job=str(rnd.randint(410000, 489999)),
                due=(jd + timedelta(days=14)).isoformat())
    for v, u in units.items():
        unit_id[v] = u["id"]
    con.executemany(
        "INSERT INTO STG_QMS_QUALITY_UNIT VALUES (?,?,?,?,?,?,?,?)",
        [(u["id"], u["serial"], u["part"], u["prod"], u["plant"], u["line"], u["job"], u["due"])
         for u in units.values()])

    defect_rows, taxonomy = [], set()
    qdid = 100000
    for r in rows:
        qdid += 1
        day = date.fromisoformat(r["inspected_at"]) + shift
        age = (today - day).days
        sub = r["module"]
        comp = COMPONENTS[sub][sum(map(ord, r["failure_mode"])) % len(COMPONENTS[sub])]
        zone = ZONES[sub][(sum(map(ord, r["failure_mode"])) + r["score"]) % len(ZONES[sub])]
        ds = f"DS{r['score']:02d}"
        open_p = 0.8 if age <= 45 else 0.3
        removed = 1 if rnd.random() < 0.02 else 0
        defect_rows.append((
            qdid, qdid, unit_id[r["vin"]], f"INS26-{rnd.randint(10000, 99999)}", comp, sub, zone,
            r["failure_mode"], ds, r["score"], r["line"], f"{r['line']}-ST{rnd.randint(1, 4)}",
            _ts(day, rnd), rnd.choice(PEOPLE), 1 if rnd.random() < open_p else 0, None, removed))
        taxonomy.add((sub, comp, zone, r["failure_mode"], ds))
    con.executemany("INSERT INTO STG_QMS_UNIT_DEFECT VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", defect_rows)

    # 510 report dates per unit
    q2o = []
    for u in units.values():
        due = date.fromisoformat(u["due"])
        steps = []
        for k, off in (("310", -12), ("410", -9), ("510", -6), ("710", -2)):
            d = due + timedelta(days=off + rnd.randint(-1, 2))
            steps.append(d.isoformat() if d <= today and rnd.random() > 0.05 else None)
        q2o.append((u["job"], u["serial"], u["part"], u["prod"], u["due"], *steps,
                    *[1 if s else 0 for s in steps]))
    con.executemany("INSERT INTO MART_OPS_Q2O_JOBDATA_OPER_DATES VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", q2o)

    # ── UVeye tables ────────────────────────────────────────────────
    job_for = {u["serial"]: u["job"] for u in units.values()}
    insp_rows, vin_def, mapping = {}, [], []
    for r in uv:
        d = date.fromisoformat(r["date"]) + shift
        at = _ts(d, rnd, 5, 20)
        insp_rows.setdefault(r["inspection"], (r["vin"], r["make"], r["model"], at, 0))
        at = insp_rows[r["inspection"]][3]
        vin_def.append((r["inspection"], r["vin"], r["make"], r["model"], r["year"], at,
                        insp_total[r["inspection"]], r["part"], r["module"], r["desc"], r["cost"], None))
        m = _UV_MAP.get(r["part"])
        fm, ds = _uv_fail_and_ds(r["desc"])
        if m:
            mapping.append((r["vin"], job_for.get(r["vin"]) or str(rnd.randint(410000, 489999)), r["make"],
                            r["model"], r["year"], at, r["part"], r["desc"], r["cost"], None,
                            m[0], m[1], m[2], fm, ds, "High (sub-system/component/zone); review fail mode & DS code"))
            taxonomy.add((m[0], m[1], m[2], fm, ds))
        else:
            mapping.append((r["vin"], job_for.get(r["vin"]) or str(rnd.randint(410000, 489999)), r["make"],
                            r["model"], r["year"], at, r["part"], r["desc"], r["cost"], None,
                            None, None, None, None, None, "No mapping - manual entry"))
    con.executemany("INSERT INTO STG_UVEYE_INSPECTION VALUES (?,?,?,?,?)", list(insp_rows.values()))
    con.executemany("INSERT INTO MART_OPS_UVEYE_VIN_DEFECTS VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", vin_def)
    con.executemany("INSERT INTO MART_OPS_UVEYE_QUALITY_MAPPING VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mapping)
    con.executemany("INSERT INTO VW_QUALITY_TAXONOMY VALUES (?,?,?,?,?)", sorted(taxonomy))

    # ── Write-side tables get some history so the tabs aren't empty ──
    vins = list(units)
    open_by_vin = {}
    for row in defect_rows:
        if row[14] and not row[16]:
            open_by_vin.setdefault(row[2], []).append(row)
    id_to_vin = {u["id"]: u["serial"] for u in units.values()}

    def when(days_back, hours=0):
        t = datetime.now() - timedelta(days=days_back, hours=hours, minutes=rnd.randint(0, 50))
        return t.strftime("%Y-%m-%d %H:%M:%S")

    # locations: every heavy van + a spread of others
    loc_rows = []
    for v in heavy + rnd.sample(vins, 120):
        hist = rnd.sample(LOCATIONS, rnd.randint(1, 3))
        for n, loc in enumerate(hist):
            loc_rows.append((v, loc, None, rnd.choice(PEOPLE), when(len(hist) - n + rnd.randint(0, 3))))
    con.executemany("INSERT INTO QUALITY_UNIT_LOCATION (VIN,LOCATION,NOTES,MOVED_BY,MOVED_AT) VALUES (?,?,?,?,?)", loc_rows)

    # holds: some open, some released
    for v in rnd.sample(heavy, min(9, len(heavy))):
        con.execute("INSERT INTO QUALITY_HOLDS (VIN,JOB_NUMBER,HOLD_TYPE,NOTES,ON_HOLD,CREATED_BY,CREATED_AT) VALUES (?,?,?,?,1,?,?)",
                    (v, units[v]["job"], rnd.choice(HOLD_TYPES), rnd.choice(["", "Waiting on parts", "Audit pending", "Deviation request open"]),
                     rnd.choice(PEOPLE), when(rnd.randint(0, 9))))
    for v in rnd.sample(vins, 12):
        c = when(rnd.randint(5, 12))
        con.execute("INSERT INTO QUALITY_HOLDS (VIN,JOB_NUMBER,HOLD_TYPE,NOTES,ON_HOLD,CREATED_BY,CREATED_AT,RELEASED_BY,RELEASED_AT) VALUES (?,?,?,?,0,?,?,?,?)",
                    (v, units[v]["job"], rnd.choice(HOLD_TYPES), "", rnd.choice(PEOPLE), c, rnd.choice(PEOPLE), when(rnd.randint(1, 4))))

    # sign-offs on a share of open defects
    so = 0
    for uid, dl in open_by_vin.items():
        for d in dl:
            if rnd.random() < 0.18 and so < 260:
                so += 1
                con.execute("INSERT INTO QUALITY_SIGNOFFS (VIN,DEFECT_REF,SUBSYSTEM,COMPONENT,FAIL_MODE,DEPT,SIGNED_BY,SIGNED_AT,NOTES) VALUES (?,?,?,?,?,?,?,?,?)",
                            (id_to_vin[uid], str(d[1]), d[5], d[4], d[7], d[10], rnd.choice(PEOPLE), when(rnd.randint(0, 12)), ""))

    # defects logged in the new app, incl. UVeye-sourced
    for v in rnd.sample(uv_vins, 10):
        m = next((x for x in mapping if x[0] == v and x[10]), None)
        con.execute("INSERT INTO QUALITY_INSPECTION_DEFECTS (VIN,JOB_NUMBER,PART_NAME,SUBSYSTEM,COMPONENT,ZONE,FAILURE_MODE,DS_CODE,NOTES,UVEYE_SOURCED,SUBMITTED_BY,SUBMITTED_AT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v, job_for.get(v), m[6] if m else None, m[10] if m else "Body", m[11] if m else "Panel",
                     m[12] if m else "Exterior", m[13] if m else "Scratch", m[14] if m else "DS02", "",
                     1 if m else 0, rnd.choice(PEOPLE), when(rnd.randint(0, 6))))

    # check-ins
    for v in rnd.sample(vins, 10):
        con.execute("INSERT INTO QUALITY_CHECKIN (VIN,JOB_NUMBER,STAGE,MILEAGE,GAS_GAUGE,OEM_KEYS,BATTERY_VOLTS,TIRE_SIZE,SEAT_COLOR,SEAT_MATERIAL,SEAT_HEATED,SEAT_VENTED,HAS_SPARE,HAS_HEADPHONES,HAS_REMOTES,HAS_ANTENNA,NOTES,CHECKED_BY,CHECKED_AT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v, units[v]["job"], rnd.choice(["Plant 7", "Tunnel"]), rnd.randint(8, 240),
                     rnd.choice(["1/4", "1/2", "3/4", "Full"]), rnd.randint(1, 2), round(rnd.uniform(12.1, 12.9), 1),
                     rnd.choice(["225/65R17", "235/60R18"]), rnd.choice(["Black", "Gray", "Tan"]),
                     rnd.choice(["Cloth", "Leather"]), rnd.randint(0, 1), rnd.randint(0, 1), rnd.randint(0, 1),
                     rnd.randint(0, 1), rnd.randint(0, 1), rnd.randint(0, 1), "", rnd.choice(PEOPLE), when(rnd.randint(0, 8))))

    con.execute("INSERT INTO _META VALUES ('version', ?)", (VERSION,))
    con.execute("INSERT INTO _META VALUES ('built', ?)", (today.isoformat(),))
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)


def ensure_db():
    """Build once per day (dates roll forward), or when the schema version changes."""
    global _ready
    if _ready:
        return
    with _build_lock:
        if _ready:
            return
        need = os.getenv("LOCAL_REBUILD") == "1" or not os.path.exists(DB_PATH)
        if not need:
            try:
                c = sqlite3.connect(DB_PATH)
                meta = dict(c.execute("SELECT K, V FROM _META").fetchall())
                c.close()
                need = meta.get("version") != VERSION or meta.get("built") != date.today().isoformat()
            except Exception:
                need = True
        if need:
            build_db()
            print(f"[local-data] built demo database at {DB_PATH}")
        _ready = True
