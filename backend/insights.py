"""Insight engine — the analytical brain of the dashboard.

For any slice of defect rows it computes what *matters*, not just what happened:
  - alerts:    lines/makes over target, defects spiking vs their own baseline
  - recurring: the same issue hitting many units in a short window (supply/process)
  - deltas:    this period vs the previous one (is it getting better or worse?)
  - benchmarks: an entity vs the group average (this line vs plant average)

Everything is count/score based so it works for quality (QS2.0), uveye, and pdi.
Each insight is {severity, kind, title, detail, dimension?, value?} so the
frontend can render and (later) make them clickable into drill-downs.
"""
from collections import defaultdict
from datetime import datetime, timedelta

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "good": 3}


def _title(s):
    if not s:
        return ""
    s = str(s)
    if s.isupper() or " " in s:
        return s
    out = []
    for i, c in enumerate(s):
        if c.isupper() and i and not s[i-1].isupper() and s[i-1] != " ":
            out.append(" ")
        out.append(c)
    return "".join(out).strip()


def _month(s):
    return (s or "")[:7]


def _safe_div(a, b):
    return a / b if b else 0


def generate_insights(rows, source="quality", target=31.0):
    """Return a ranked list of insight cards for this slice of rows."""
    if not rows:
        return []
    insights = []
    n = len(rows)
    vehicles = len({r.vin for r in rows if r.vin})

    # ---- group helpers ----
    by_line = defaultdict(list)
    by_make = defaultdict(list)
    by_fmode = defaultdict(int)
    by_month = defaultdict(int)
    by_month_fmode = defaultdict(lambda: defaultdict(int))
    by_dealer = defaultdict(int)
    fmode_vins = defaultdict(set)
    for r in rows:
        sc = getattr(r, "score", 0) or 0
        if r.line:
            by_line[r.line].append(sc)
        if r.make and r.make not in ("Unknown", "Unspecified"):
            by_make[r.make].append(sc)
        fm = getattr(r, "failure_mode", "") or ""
        if fm:
            by_fmode[fm] += 1
            fmode_vins[fm].add(r.vin)
        m = _month(r.inspected_at)
        if m:
            by_month[m] += 1
            if fm:
                by_month_fmode[m][fm] += 1
        if source == "pdi" and r.plant:
            by_dealer[r.plant] += 1

    months = sorted(by_month)

    # ============ 1. SCORE vs TARGET (quality only) ============
    if source == "quality":
        all_scores = [getattr(r, "score", 0) or 0 for r in rows]
        avg = _safe_div(sum(all_scores), len(all_scores))
        # per-line over-target
        line_avgs = {ln: _safe_div(sum(v), len(v)) for ln, v in by_line.items() if len(v) >= 20}
        if line_avgs:
            worst_line, worst_val = max(line_avgs.items(), key=lambda x: x[1])
            grp_avg = _safe_div(sum(line_avgs.values()), len(line_avgs))
            if worst_val > grp_avg * 1.25 and grp_avg > 0:
                insights.append({
                    "severity": "high", "kind": "benchmark", "dimension": "line",
                    "entity": worst_line,
                    "title": f"{worst_line} is running {round((worst_val/grp_avg-1)*100)}% above the line average",
                    "detail": f"Average score {round(worst_val,1)} vs {round(grp_avg,1)} across all lines. Worth a closer look at this line's process.",
                })

    # ============ 2. WEEK/MONTH-OVER-PERIOD DELTA ============
    if len(months) >= 2:
        last, prev = months[-1], months[-2]
        lc, pc = by_month[last], by_month[prev]
        if pc >= 5:
            change = _safe_div(lc - pc, pc) * 100
            if change >= 25:
                insights.append({
                    "severity": "high" if change >= 50 else "medium", "kind": "trend",
                    "title": f"Defects up {round(change)}% vs last period",
                    "detail": f"{lc} defects in {last}, up from {pc} in {prev}. Volume is climbing \u2014 check what's driving it.",
                })
            elif change <= -25:
                insights.append({
                    "severity": "good", "kind": "trend",
                    "title": f"Defects down {abs(round(change))}% vs last period",
                    "detail": f"{lc} in {last}, down from {pc} in {prev}. Whatever changed is working.",
                })

    # ============ 3. EMERGING / SPIKING FAILURE MODE ============
    if len(months) >= 2:
        last, prev = months[-1], months[-2]
        for fm, cnt_last in by_month_fmode[last].items():
            cnt_prev = by_month_fmode[prev].get(fm, 0)
            if cnt_last >= 4 and cnt_last >= cnt_prev * 2.5 and cnt_last > cnt_prev:
                kind_word = "new this period" if cnt_prev == 0 else f"up from {cnt_prev}"
                insights.append({
                    "severity": "high" if cnt_prev == 0 else "medium", "kind": "spike",
                    "dimension": "failure_mode", "entity": fm,
                    "title": f"{_title(fm)} is spiking ({cnt_last} this period, {kind_word})",
                    "detail": f"This failure mode jumped sharply. Early sign of a process or supply issue worth catching now.",
                })

    # ============ 4. RECURRING PROBLEM (same issue, many units) ============
    # the license-plate-bracket-on-5-vehicles pattern. Only fire when an issue is
    # both repeated AND concentrated (high share of a small window), not just common.
    span_days = _date_span(rows)
    recur_count = 0
    # a recurring concern = many distinct units hit by ONE specific issue in a short span,
    # relative to how many units there are. Require it to stand out from the typical issue.
    fmode_unit_counts = sorted((len(v) for v in fmode_vins.values()), reverse=True)
    typical = fmode_unit_counts[len(fmode_unit_counts)//2] if fmode_unit_counts else 0  # median
    for fm, vins in sorted(fmode_vins.items(), key=lambda x: -len(x[1])):
        u = len(vins)
        concentrated = u >= max(4, typical * 2)  # at least double the typical issue's spread
        short_window = (span_days is not None and span_days <= 21)
        if u >= 4 and concentrated and (short_window or source == "pdi"):
            window = f"in {span_days} days" if span_days else "recently"
            insights.append({
                "severity": "medium", "kind": "recurring",
                "dimension": "failure_mode", "entity": fm,
                "title": f"{_title(fm)} recurring across {u} vehicles {window}",
                "detail": f"Same issue on {u} different units \u2014 points to a systemic cause (supplier, part, or process), not one-off damage.",
            })
            recur_count += 1
            if recur_count >= 2:
                break

    # ============ 5. DEALER OUTLIER (pdi only) ============
    if source == "pdi" and len(by_dealer) >= 4:
        vals = sorted(by_dealer.values(), reverse=True)
        avg_d = _safe_div(sum(vals), len(vals))
        top_dealer, top_n = max(by_dealer.items(), key=lambda x: x[1])
        if top_n > avg_d * 2 and top_n >= 4:
            insights.append({
                "severity": "medium", "kind": "outlier", "dimension": "plant",
                "entity": top_dealer,
                "title": f"Dealer {top_dealer} reported {top_n} defects \u2014 well above average",
                "detail": f"Average dealer reports {round(avg_d,1)}. This one is {round(top_n/avg_d,1)}x higher \u2014 either a quality cluster or a very thorough inspector.",
            })

    # ============ 6. TOP CONCENTRATION (always useful) ============
    if by_fmode:
        top_fm, top_c = max(by_fmode.items(), key=lambda x: x[1])
        share = _safe_div(top_c, n) * 100
        if share >= 12:
            insights.append({
                "severity": "low", "kind": "concentration",
                "dimension": "failure_mode", "entity": top_fm,
                "title": f"{_title(top_fm)} is your single biggest issue ({round(share)}% of all defects)",
                "detail": f"{top_c} of {n} defects. The textbook place to focus \u2014 fixing the vital few moves the number most.",
            })

    insights.sort(key=lambda x: SEV_ORDER.get(x["severity"], 9))
    return insights[:8]


def _date_span(rows):
    ds = [r.inspected_at for r in rows if r.inspected_at]
    if not ds:
        return None
    try:
        dts = [datetime.strptime(d[:10], "%Y-%m-%d") for d in ds]
        return (max(dts) - min(dts)).days or 1
    except Exception:
        return None


def period_comparison(rows, dimension="line", source="quality"):
    """This-period-vs-last summary for a dimension, for the 'what changed' strip."""
    by_month = defaultdict(list)
    for r in rows:
        m = _month(r.inspected_at)
        if m:
            by_month[m].append(r)
    months = sorted(by_month)
    if len(months) < 2:
        return None
    last, prev = months[-1], months[-2]

    def agg(rs):
        d = defaultdict(int)
        for r in rs:
            key = getattr(r, dimension, "") or "Other"
            d[key] += 1
        return d
    a, b = agg(by_month[last]), agg(by_month[prev])
    rows_out = []
    for key in set(list(a.keys()) + list(b.keys())):
        cur, prv = a.get(key, 0), b.get(key, 0)
        if cur + prv < 3:
            continue
        delta = cur - prv
        rows_out.append({"label": _title(key), "current": cur, "previous": prv,
                         "delta": delta, "pct": round(_safe_div(delta, prv) * 100) if prv else None})
    rows_out.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return {"current_period": last, "previous_period": prev, "rows": rows_out[:8]}


def period_comparison_fast(rows):
    """Fast period comparison from lightweight (inspected_at, dim) tuples."""
    by_month = defaultdict(lambda: defaultdict(int))
    for r in rows:
        m = _month(r.inspected_at)
        if not m:
            continue
        by_month[m][(r.dim or "Other")] += 1
    months = sorted(by_month)
    if len(months) < 2:
        return None
    last, prev = months[-1], months[-2]
    a, b = by_month[last], by_month[prev]
    rows_out = []
    for key in set(list(a.keys()) + list(b.keys())):
        cur, prv = a.get(key, 0), b.get(key, 0)
        if cur + prv < 3:
            continue
        delta = cur - prv
        rows_out.append({"label": _title(key), "current": cur, "previous": prv,
                         "delta": delta, "pct": round(_safe_div(delta, prv) * 100) if prv else None})
    rows_out.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return {"current_period": last, "previous_period": prev, "rows": rows_out[:8]}
