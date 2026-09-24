"""Config-driven report engine.

A report is plain JSON — no SQL, no code. The engine reads a config and
returns chart-ready data. Add a config -> a report exists. This is what lets
non-technical people (later, via a UI) build reports without a developer.

Config shape (everything optional except metric + dimension):
{
  "id": "defects_by_line",
  "title": "Defects by line",
  "source": "quality",          # which dataset: quality | uveye
  "metric": "count",            # count | avg_score | sum_score | avg_cost | units | dpu
  "dimension": "line",          # any indexed field: line, make, module, conversion,
                                #   zone, failure_mode, part_name, plant, status, month
  "chart": "bar",               # bar | doughnut | line | table
  "limit": 12,
  "sort": "desc",               # desc | asc | label
  "filters": {"make": "CHRYSLER"}   # optional fixed filters
}

The catalog of reports lives in report_catalog.json so it can be edited without
touching Python. The engine validates every field against an allow-list, so a
bad/hostile config can never reach raw SQL.
"""
import json
import os
from sqlalchemy import func
from sqlalchemy.orm import Session

import models

D = models.Defect

# ---- allow-lists: the ONLY things a config may reference ----------------
DIMENSIONS = {
    "line": D.line, "make": D.make, "module": D.module, "conversion": D.conversion,
    "zone": D.zone, "failure_mode": D.failure_mode, "part_name": D.part_name,
    "plant": D.plant, "status": D.status, "year": D.year,
    "month": func.substr(D.inspected_at, 1, 7),
    "vin": D.vin,
}
FILTERABLE = {
    "line", "make", "module", "conversion", "zone", "failure_mode",
    "part_name", "plant", "status", "year", "source",
}
METRICS = {"count", "avg_score", "sum_score", "avg_cost", "sum_cost", "units", "dpu"}
CHARTS = {"bar", "doughnut", "line", "table"}
SORTS = {"desc", "asc", "label"}


def _label(s):
    if s is None or s == "":
        return "Unspecified"
    s = str(s)
    # Don't split all-caps tokens (FORD, LN1, VIN) or values with spaces already.
    if s.isupper() or " " in s:
        return s
    # camelCase / PascalCase -> spaced (FailureMode -> Failure Mode)
    out = []
    for i, c in enumerate(s):
        if c.isupper() and i > 0 and not s[i-1].isupper() and s[i-1] != " ":
            out.append(" ")
        out.append(c)
    return "".join(out).strip()


def run_report(cfg: dict, db: Session) -> dict:
    """Execute one report config and return chart-ready data."""
    dim_key = cfg.get("dimension", "module")
    metric = cfg.get("metric", "count")
    chart = cfg.get("chart", "bar")
    source = cfg.get("source", "quality")
    limit = int(cfg.get("limit", 12))
    sort = cfg.get("sort", "desc")

    # validate against allow-lists (security + safety)
    if dim_key not in DIMENSIONS:
        return {"error": f"unknown dimension '{dim_key}'"}
    if metric not in METRICS:
        return {"error": f"unknown metric '{metric}'"}
    if chart not in CHARTS:
        chart = "bar"
    if sort not in SORTS:
        sort = "desc"
    limit = max(1, min(limit, 100))

    dim = DIMENSIONS[dim_key]

    # metric expression
    metric_expr = {
        "count": func.count(D.id),
        "avg_score": func.avg(D.score),
        "sum_score": func.sum(D.score),
        "avg_cost": func.avg(D.cost),
        "sum_cost": func.sum(D.cost),
        "units": func.count(func.distinct(D.vin)),
        "dpu": func.count(D.id),  # combined with unit count below
    }[metric]

    q = db.query(dim, metric_expr)
    if metric == "dpu":
        q = db.query(dim, func.count(D.id), func.count(func.distinct(D.vin)))

    # base source filter
    q = q.filter(D.source == source)

    # fixed filters from config (validated)
    for fk, fv in (cfg.get("filters") or {}).items():
        if fk in FILTERABLE and fv not in (None, "", "all"):
            q = q.filter(getattr(D, fk) == fv)

    # drop empty dimension values
    if dim_key != "month":
        q = q.filter(dim != "")

    q = q.group_by(dim)
    rows = q.all()

    # shape into {label, value}
    items = []
    for row in rows:
        if metric == "dpu":
            key, cnt, units = row
            val = round(cnt / units, 2) if units else 0
        else:
            key, val = row
            val = round(val or 0, 2)
        items.append({"label": _label(key), "value": val})

    # sort
    if sort == "label":
        items.sort(key=lambda x: x["label"])
    elif sort == "asc":
        items.sort(key=lambda x: x["value"])
    else:
        items.sort(key=lambda x: -x["value"])
    items = items[:limit]

    return {
        "id": cfg.get("id"),
        "title": cfg.get("title", f"{_label(metric)} by {_label(dim_key)}"),
        "metric": metric, "dimension": dim_key, "chart": chart,
        "source": source, "data": items,
    }


# ---- catalog -------------------------------------------------------------
_CATALOG_DIR = os.path.dirname(__file__) if os.access(os.path.dirname(os.path.abspath(__file__)), os.W_OK) else __import__("tempfile").gettempdir()
_CATALOG_PATH = os.path.join(_CATALOG_DIR, "report_catalog.json")


def load_catalog() -> dict:
    """Read the report catalog (tabs + their reports). Falls back to a built-in
    default if the file is missing, so the app always works."""
    if os.path.exists(_CATALOG_PATH):
        with open(_CATALOG_PATH) as f:
            return json.load(f)
    return DEFAULT_CATALOG


def save_catalog(catalog: dict):
    """Persist the catalog (used by the builder UI / add-report endpoint)."""
    with open(_CATALOG_PATH, "w") as f:
        json.dump(catalog, f, indent=2)


# A starter catalog: umbrellas -> tabs -> reports. Editing this JSON (or the
# file) is all it takes to add a report or a whole tab.
DEFAULT_CATALOG = {
    "umbrellas": [
        {
            "id": "quality", "label": "Quality Site", "source": "quality",
            "tabs": [
                {"id": "executive", "label": "Executive", "reports": [
                    {"id": "score_by_month", "title": "Average score by month",
                     "metric": "avg_score", "dimension": "month", "chart": "line", "sort": "label"},
                    {"id": "fmode_pareto", "title": "Failure mode Pareto",
                     "metric": "count", "dimension": "failure_mode", "chart": "bar", "limit": 10},
                    {"id": "score_by_line", "title": "Average score by line",
                     "metric": "avg_score", "dimension": "line", "chart": "bar"},
                    {"id": "defects_by_module", "title": "Defects by module",
                     "metric": "count", "dimension": "module", "chart": "doughnut"},
                ]},
                {"id": "lines", "label": "Lines & stations", "reports": [
                    {"id": "defects_by_line", "title": "Defects by line",
                     "metric": "count", "dimension": "line", "chart": "bar"},
                    {"id": "score_by_line2", "title": "Avg score by line",
                     "metric": "avg_score", "dimension": "line", "chart": "bar"},
                    {"id": "dpu_by_line", "title": "DPU by line",
                     "metric": "dpu", "dimension": "line", "chart": "bar"},
                    {"id": "defects_by_zone", "title": "Defects by zone",
                     "metric": "count", "dimension": "zone", "chart": "bar", "limit": 15},
                ]},
                {"id": "makes", "label": "Make analysis", "reports": [
                    {"id": "defects_by_make", "title": "Defects by make",
                     "metric": "count", "dimension": "make", "chart": "bar"},
                    {"id": "score_by_make", "title": "Avg score by make",
                     "metric": "avg_score", "dimension": "make", "chart": "bar"},
                    {"id": "units_by_make", "title": "Units by make",
                     "metric": "units", "dimension": "make", "chart": "bar"},
                    {"id": "dpu_by_make", "title": "DPU by make",
                     "metric": "dpu", "dimension": "make", "chart": "bar"},
                ]},
                {"id": "conversion", "label": "Conversion", "reports": [
                    {"id": "defects_by_conv", "title": "Defects by conversion",
                     "metric": "count", "dimension": "conversion", "chart": "doughnut"},
                    {"id": "score_by_conv", "title": "Avg score by conversion",
                     "metric": "avg_score", "dimension": "conversion", "chart": "bar"},
                    {"id": "dpu_by_conv", "title": "DPU by conversion",
                     "metric": "dpu", "dimension": "conversion", "chart": "bar"},
                    {"id": "units_by_conv", "title": "Units by conversion",
                     "metric": "units", "dimension": "conversion", "chart": "bar"},
                ]},
            ],
        },
    ],
}
