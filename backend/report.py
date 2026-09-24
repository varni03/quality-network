"""Dynamic report generation.

Takes an open-ended request ("show me a report on Chrysler") and returns a
structured spec describing a custom dashboard: a title, a headline stat, a
summary line, and an ordered list of visuals (each with its own data). The
frontend renders whatever visuals the spec contains, so the layout is
generated dynamically rather than hardcoded.

This local builder produces the spec deterministically. The same spec shape
can later be produced by Claude (return JSON in this format), so the frontend
needs no changes when the real LLM is wired in.

Spec shape:
{
  "title": str,
  "subtitle": str,
  "headline": {"label": str, "value": str},
  "summary": str,
  "visuals": [
     {"kind": "bar"|"doughnut"|"line"|"stat-row"|"table",
      "title": str,
      "data": [{"label": str, "value": number}]  # or rows for table
     }, ...
  ]
}
"""
from collections import defaultdict


def _title(s):
    out = []
    for c in str(s):
        if c.isupper() and out and out[-1] != " ":
            out.append(" ")
        out.append(c)
    return "".join(out).strip()


def _fmt(n):
    return "$" + format(round(n), ",")


def _scope(rows, q):
    """Pick a subset of rows + a label based on what the request mentions."""
    ql = q.lower()
    makes = {r.make for r in rows if r.make}
    for mk in makes:
        if mk not in ("Unknown","Unspecified") and mk.lower() in ql:
            return [r for r in rows if r.make == mk], mk, "make"
    if any(w in ql for w in ["exterior", "atlas", "body"]):
        return [r for r in rows if r.module == "Atlas"], "Exterior (Atlas)", "module"
    if any(w in ql for w in ["tire", "tyre", "artemis"]):
        return [r for r in rows if r.module == "Artemis"], "Tires (Artemis)", "module"
    if any(w in ql for w in ["flagged", "follow up", "follow-up", "priority", "prioritize"]):
        flagged = [r for r in rows if r.flagged]
        if flagged:
            return flagged, "Flagged for follow-up", "flag"
        # priority report with nothing flagged -> high-cost defects
        ranked = sorted(rows, key=lambda r: -r.cost)
        cutoff = ranked[: max(20, len(ranked) // 5)]
        return cutoff, "Priority (highest-cost)", "priority"
    return rows, "All inspections", "all"


def build_report(question, rows):
    scoped, label, kind = _scope(rows, question)
    n = len(scoped)
    total = sum(r.cost for r in scoped)
    vehicles = len({r.vin for r in scoped if r.vin})
    inspections = len({r.inspection_id for r in scoped})
    priced = [r for r in scoped if r.cost > 0]
    avg = round(total / len(priced)) if priced else 0

    def grp(attr, top=7):
        m = defaultdict(float)
        for r in scoped:
            m[getattr(r, attr) or "Other"] += r.cost
        return [{"label": _title(k), "value": round(v)}
                for k, v in sorted(m.items(), key=lambda x: -x[1])[:top]]

    months = defaultdict(int)
    for r in scoped:
        if r.inspected_at:
            months[r.inspected_at[:7]] += 1
    month_data = [{"label": k, "value": v} for k, v in sorted(months.items())]

    status = defaultdict(int)
    for r in scoped:
        status[r.status] += 1

    # top individual defects as a table
    ranked = sorted(scoped, key=lambda r: -r.cost)[:6]
    table_rows = [{"part": _title(r.part_name), "vehicle": f"{r.year} {r.make}",
                   "cost": _fmt(r.cost)} for r in ranked]

    visuals = []
    # headline stat-row
    visuals.append({"kind": "stat-row", "title": "At a glance", "data": [
        {"label": "Reconditioning", "value": _fmt(total)},
        {"label": "Defects", "value": str(n)},
        {"label": "Vehicles", "value": str(vehicles)},
        {"label": "Avg / defect", "value": _fmt(avg)},
    ]})
    # by part (always useful)
    if grp("part_name"):
        visuals.append({"kind": "bar", "title": "Cost by part", "data": grp("part_name")})
    # by make only if scope spans multiple makes
    makes_in = {r.make for r in scoped}
    if len(makes_in) > 1 and kind != "make":
        visuals.append({"kind": "doughnut", "title": "Cost by make", "data": grp("make", 6)})
    else:
        # single make/scope -> show module split instead
        visuals.append({"kind": "doughnut", "title": "Cost by module", "data": grp("module", 4)})
    # trend if more than one month
    if len(month_data) > 1:
        visuals.append({"kind": "line", "title": "Defect volume by month", "data": month_data})
    # top defects table
    visuals.append({"kind": "table", "title": "Highest-cost defects",
                    "columns": ["Part", "Vehicle", "Cost"], "rows": table_rows})

    # summary sentence
    top_part = grp("part_name", 1)
    share = ""
    if kind == "make" and total:
        allrows_total = sum(r.cost for r in rows)
        share = f" — about {round(total/allrows_total*100)}% of all reconditioning cost" if allrows_total else ""
    summary = (f"{label}: {n} defects across {inspections} inspections totaling {_fmt(total)}{share}. "
               f"The biggest cost driver is {top_part[0]['label']} ({_fmt(top_part[0]['value'])})." if top_part
               else f"{label}: {n} defects totaling {_fmt(total)}.")

    return {
        "title": f"{label} — quality report",
        "subtitle": f"{n} defects · {inspections} inspections · generated from inspection data",
        "headline": {"label": "Total reconditioning", "value": _fmt(total)},
        "summary": summary,
        "visuals": visuals,
    }


def detect_anomalies(rows):
    """Surface statistically notable patterns the dashboard wouldn't call out.

    Compares each make's per-defect average and each part's share against the
    fleet baseline, and flags outliers. Returns a list of {severity, text}.
    """
    if not rows:
        return []
    out = []
    total = sum(r.cost for r in rows)
    fleet_avg = total / len([r for r in rows if r.cost > 0]) if any(r.cost for r in rows) else 0

    # make-level cost-per-defect outliers
    by_make_cost = defaultdict(float)
    by_make_n = defaultdict(int)
    for r in rows:
        if r.make and r.make not in ("Unknown","Unspecified"):
            by_make_cost[r.make] += r.cost
            by_make_n[r.make] += 1
    for mk, c in by_make_cost.items():
        n = by_make_n[mk]
        if n >= 5:
            avg = c / n
            ratio = avg / fleet_avg if fleet_avg else 1
            if ratio >= 1.4:
                out.append({"severity": "high",
                            "text": f"{mk} averages {_fmt(avg)} per defect \u2014 {round((ratio-1)*100)}% above the "
                                    f"fleet average of {_fmt(fleet_avg)}. Worth a supplier or handling review."})

    # part concentration
    by_part = defaultdict(float)
    for r in rows:
        by_part[r.part_name] += r.cost
    top_part, top_cost = max(by_part.items(), key=lambda x: x[1])
    if total and top_cost / total >= 0.12:
        out.append({"severity": "medium",
                    "text": f"{_title(top_part)} alone drives {round(top_cost/total*100)}% of all reconditioning cost "
                            f"({_fmt(top_cost)}) \u2014 a concentration worth targeting."})

    # make+part hotspot: a make whose top part is far above that part's fleet norm
    part_fleet_avg = {p: c / sum(1 for r in rows if r.part_name == p) for p, c in by_part.items()}
    hotspots = []
    mp = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.make and r.make not in ("Unknown","Unspecified"):
            mp[r.make][r.part_name].append(r.cost)
    for mk, parts in mp.items():
        for pt, costs in parts.items():
            if len(costs) >= 4:
                avg = sum(costs) / len(costs)
                base = part_fleet_avg.get(pt, avg)
                if base and avg / base >= 1.5:
                    hotspots.append((avg / base, mk, pt, avg))
    hotspots.sort(reverse=True)
    for ratio, mk, pt, avg in hotspots[:2]:
        out.append({"severity": "high",
                    "text": f"{mk} {_title(pt)} defects average {_fmt(avg)} \u2014 {round((ratio-1)*100)}% above the "
                            f"norm for that part across all makes."})

    # volume trend
    months = defaultdict(int)
    for r in rows:
        if r.inspected_at:
            months[r.inspected_at[:7]] += 1
    mk_sorted = sorted(months.items())
    if len(mk_sorted) >= 2:
        prev, last = mk_sorted[-2][1], mk_sorted[-1][1]
        if prev and (last - prev) / prev >= 0.5:
            out.append({"severity": "medium",
                        "text": f"Defect volume jumped {round((last/prev-1)*100)}% from {mk_sorted[-2][0]} to "
                                f"{mk_sorted[-1][0]} ({prev} \u2192 {last}). Likely expanded scan coverage \u2014 confirm."})

    return out[:5]


def build_comparison(rows, a_label, b_label):
    """Side-by-side comparison of two makes (or scopes)."""
    def scope_rows(label):
        return [r for r in rows if r.make and r.make.lower() == label.lower()]

    def metrics(subset):
        total = sum(r.cost for r in subset)
        priced = [r for r in subset if r.cost > 0]
        by_part = defaultdict(float)
        for r in subset:
            by_part[r.part_name] += r.cost
        top = sorted(by_part.items(), key=lambda x: -x[1])[:5]
        return {
            "total": round(total), "defects": len(subset),
            "vehicles": len({r.vin for r in subset if r.vin}),
            "avg": round(total / len(priced)) if priced else 0,
            "top_parts": [{"label": _title(k), "value": round(v)} for k, v in top],
        }

    A = metrics(scope_rows(a_label))
    B = metrics(scope_rows(b_label))
    return {
        "is_comparison": True,
        "title": f"{a_label.upper()} vs {b_label.upper()}",
        "subtitle": "Side-by-side quality comparison",
        "a_label": a_label.upper(), "b_label": b_label.upper(),
        "a": A, "b": B,
    }


# severity / priority scoring -------------------------------------------------
SEVERITY_KEYWORDS = {
    "high": ["replacement", "replace", "structural", "severe", "large dent", "extensive",
             "alignment", "wheel damage", "crack"],
    "medium": ["repaint", "repainting", "medium", "dent", "touch-up", "polish"],
}


def severity_of(row):
    """Derive a severity tier from cost and description language."""
    desc = (row.description or "").lower()
    score = 0
    # cost contributes the most
    if row.cost >= 500:
        score += 3
    elif row.cost >= 200:
        score += 2
    elif row.cost > 0:
        score += 1
    # language signals
    if any(k in desc for k in SEVERITY_KEYWORDS["high"]):
        score += 2
    elif any(k in desc for k in SEVERITY_KEYWORDS["medium"]):
        score += 1
    if score >= 4:
        return "High", score
    if score >= 2:
        return "Medium", score
    return "Low", score


def priority_list(rows, limit=25):
    """Rank defects by a priority score (cost-weighted severity)."""
    scored = []
    for r in rows:
        tier, s = severity_of(r)
        # priority score blends raw cost and severity tier
        pscore = round(r.cost * (1 + s / 5))
        scored.append({
            "id": r.id, "part": _title(r.part_name),
            "vehicle": f"{r.year} {r.make}", "vin": r.vin,
            "cost": round(r.cost), "severity": tier, "score": pscore,
            "description": r.description or "",
        })
    scored.sort(key=lambda x: -x["score"])
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for r in rows:
        counts[severity_of(r)[0]] += 1
    return {"items": scored[:limit], "counts": counts}
