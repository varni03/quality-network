"""Natural-language analytics.

If ANTHROPIC_API_KEY is set, routes the question to Claude with a compact
data summary as context. Otherwise (or on any error) falls back to a rich
local rule-based engine so the endpoint always returns a useful answer + chart.
"""
import os
from collections import defaultdict

import httpx

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _title(s):
    out = []
    for c in str(s):
        if c.isupper() and out and out[-1] != " ":
            out.append(" ")
        out.append(c)
    return "".join(out).strip()


def _aggregate(rows):
    total = sum(r.cost for r in rows)
    by_make, by_module, by_part = defaultdict(float), defaultdict(float), defaultdict(float)
    cnt_make, cnt_part = defaultdict(int), defaultdict(int)
    months = defaultdict(int)
    by_status = defaultdict(int)
    flagged = 0
    # QS2.0 dimensions
    by_line_cnt, by_line_score = defaultdict(int), defaultdict(list)
    by_conv_cnt, by_conv_score = defaultdict(int), defaultdict(list)
    by_fmode = defaultdict(int)
    by_module_cnt = defaultdict(int)
    scores_all = []
    for r in rows:
        by_make[r.make or "Unknown"] += r.cost
        by_module[r.module or "Other"] += r.cost
        by_part[r.part_name] += r.cost
        cnt_make[r.make or "Unknown"] += 1
        cnt_part[r.part_name] += 1
        by_status[r.status] += 1
        if r.flagged:
            flagged += 1
        if r.inspected_at:
            months[r.inspected_at[:7]] += 1
        ln = getattr(r, "line", "") or ""
        cv = getattr(r, "conversion", "") or ""
        fm = getattr(r, "failure_mode", "") or ""
        sc = getattr(r, "score", 0) or 0
        mod = r.module or "Other"
        by_module_cnt[mod] += 1
        if ln:
            by_line_cnt[ln] += 1; by_line_score[ln].append(sc)
        if cv:
            by_conv_cnt[cv] += 1; by_conv_score[cv].append(sc)
        if fm:
            by_fmode[fm] += 1
        scores_all.append(sc)
    top = lambda d, n=8: sorted(d.items(), key=lambda x: -x[1])[:n]
    avgmap = lambda d: {k: round(sum(v)/len(v), 2) if v else 0 for k, v in d.items()}
    priced = [r for r in rows if r.cost > 0]
    return {
        "rows": rows,
        "total": round(total),
        "defects": len(rows),
        "inspections": len({r.inspection_id for r in rows}),
        "vehicles": len({r.vin for r in rows if r.vin}),
        "avg": round(total / len(priced)) if priced else 0,
        "avg_score": round(sum(scores_all)/len(scores_all), 2) if scores_all else 0,
        "by_make": top(by_make), "by_module": top(by_module, 4), "by_part": top(by_part),
        "cnt_make": cnt_make, "cnt_part": cnt_part,
        "by_module_cnt": top(by_module_cnt),
        "by_line_cnt": top(by_line_cnt), "by_line_score": avgmap(by_line_score),
        "by_conv_cnt": top(by_conv_cnt), "by_conv_score": avgmap(by_conv_score),
        "by_fmode": top(by_fmode),
        "by_month": dict(sorted(months.items())),
        "by_status": dict(by_status), "flagged": flagged,
        "makes": list(by_make.keys()), "parts": list(by_part.keys()),
    }


def _fmt(n):
    return "$" + format(round(n), ",")


def _chart(pairs, kind="bar"):
    return {"type": kind, "data": [{"label": _title(k), "value": round(v)} for k, v in pairs]}


def _chart_cnt(pairs, kind="bar"):
    return {"type": kind, "data": [{"label": _title(k), "value": round(v)} for k, v in pairs]}


# ---- intent parsing: turn a sentence into a dashboard ACTION -------------
# Maps spoken words to the engine's allow-listed dimensions and metrics.
_DIM_WORDS = {
    "line": "line", "lines": "line", "wav": "line",
    "make": "make", "makes": "make", "brand": "make", "manufacturer": "make",
    "model": "make",
    "module": "module", "modules": "module", "subsystem": "module", "category": "module",
    "conversion": "conversion", "side entry": "conversion", "rear entry": "conversion",
    "entry type": "conversion",
    "zone": "zone", "zones": "zone", "station": "zone", "stations": "zone",
    "failure mode": "failure_mode", "failure": "failure_mode", "defect type": "failure_mode",
    "issues found": "failure_mode", "top issues": "failure_mode", "issue": "failure_mode",
    "issues": "failure_mode", "inspection point": "failure_mode", "problems": "failure_mode",
    "inspection category": "module", "category": "module", "categories": "module",
    "dealer": "plant", "dealers": "plant", "account": "plant", "by dealer": "plant",
    "part": "part_name", "parts": "part_name", "component": "part_name",
    "plant": "plant", "plants": "plant",
    "month": "month", "monthly": "month", "time": "month", "trend": "month",
    "over time": "month", "date": "month",
    "status": "status", "workflow": "status",
}
_METRIC_WORDS = {
    "score": "avg_score", "average score": "avg_score", "avg score": "avg_score",
    "quality score": "avg_score", "qtyscore": "avg_score",
    "dpu": "dpu", "defects per unit": "dpu", "per unit": "dpu",
    "count": "count", "number of defects": "count", "how many": "count",
    "defects": "count", "volume": "count",
    "units": "units", "vehicles": "units", "number of vehicles": "units",
}
_CHART_WORDS = {
    "bar": "bar", "bar chart": "bar", "doughnut": "doughnut", "donut": "doughnut",
    "pie": "doughnut", "pie chart": "doughnut", "line": "line", "line chart": "line",
    "trend": "line", "over time": "line", "table": "table", "list": "table",
}
_BUILD_TRIGGERS = ("build", "show me", "show ", "add a", "add report", "create", "make a",
                   "give me", "chart of", "graph of", "report of", "report on",
                   "visualize", "plot", "display", "pull up", "i want to see",
                   "can you show", "break down", "breakdown", "group by", " by ",
                   "top issues", "top defect", "most common", "defects by")
_RESET_TRIGGERS = ("reset", "default view", "executive view", "clear dashboard",
                   "go back", "start over", "executive dashboard")


def parse_intent(question):
    """Return a dashboard action dict, or None if this isn't a command.
    Actions:
      {"action":"reset"}
      {"action":"render","report":{...config...}}
    """
    q = " " + question.lower().strip() + " "

    if any(t in q for t in _RESET_TRIGGERS):
        return {"action": "reset"}

    is_build = any(t in q for t in _BUILD_TRIGGERS)

    # find a dimension (longest match first so "failure mode" beats "mode")
    dim = None
    for word in sorted(_DIM_WORDS, key=len, reverse=True):
        if word in q:
            dim = _DIM_WORDS[word]; break
    # find a metric
    metric = None
    for word in sorted(_METRIC_WORDS, key=len, reverse=True):
        if word in q:
            metric = _METRIC_WORDS[word]; break
    # find a chart type
    chart = None
    for word in sorted(_CHART_WORDS, key=len, reverse=True):
        if word in q:
            chart = _CHART_WORDS[word]; break

    # Only treat as a build command if they clearly asked to build/show AND
    # we have at least a dimension to group by.
    if not is_build or not dim:
        return None

    metric = metric or "count"
    # sensible default chart per shape
    if not chart:
        chart = "line" if dim == "month" else ("doughnut" if dim in ("conversion", "module") else "bar")
    title_metric = {"avg_score": "Average score", "dpu": "DPU", "count": "Defects",
                    "units": "Units", "sum_score": "Total score"}.get(metric, metric.title())
    title_dim = {"failure_mode": "failure mode", "part_name": "part"}.get(dim, dim)
    return {"action": "render",
            "report": {"id": f"ai_{metric}_{dim}", "title": f"{title_metric} by {title_dim}",
                       "metric": metric, "dimension": dim, "chart": chart},
            "spoken": f"Done \u2014 here's {title_metric.lower()} by {title_dim}."}


def _local(question, agg):
    q = question.lower().strip()
    has = lambda *ws: any(w in q for w in ws)

    # ---- LINE performance (score or count) ----
    if has("line", "wav", "which line", "by line"):
        sc = agg["by_line_score"]
        cnt = dict(agg["by_line_cnt"])
        if sc:
            worst = max(sc.items(), key=lambda x: x[1])
            best = min(sc.items(), key=lambda x: x[1])
            return {"answer": f"By line: {worst[0]} has the highest (worst) average score at {worst[1]}, "
                              f"while {best[0]} is best at {best[1]}. Defect volume leads with "
                              f"{agg['by_line_cnt'][0][0]} ({agg['by_line_cnt'][0][1]} defects).",
                    "chart": _chart_cnt(agg["by_line_cnt"])}

    # ---- CONVERSION comparison ----
    if has("conversion", "side entry", "rear entry", "side-entry", "rear-entry", "entry type"):
        cs = agg["by_conv_score"]
        if cs:
            items = sorted(cs.items(), key=lambda x: x[1])
            line = ", ".join(f"{k}: avg score {v}" for k, v in items)
            return {"answer": f"Conversion comparison \u2014 {line}. Lower score is better. "
                              f"Volume: {agg['by_conv_cnt'][0][0]} has the most defects "
                              f"({agg['by_conv_cnt'][0][1]}).",
                    "chart": _chart_cnt(agg["by_conv_cnt"], "doughnut")}

    # ---- SCORE questions ----
    if has("score", "qtyscore", "target", "fty", "dpu", "quality score"):
        return {"answer": f"Average vehicle score is {agg['avg_score']} across {agg['defects']} defects on "
                          f"{agg['vehicles']} vehicles. By line, "
                          f"{max(agg['by_line_score'].items(), key=lambda x:x[1])[0] if agg['by_line_score'] else 'n/a'} "
                          f"runs highest. Lower scores are better against target.",
                "chart": _chart_cnt(agg["by_line_cnt"]) if agg["by_line_cnt"] else None}

    # ---- FAILURE MODE / pareto ----
    if has("failure mode", "failuremode", "pareto", "top defect", "most common defect", "driver", "root cause"):
        d = agg["by_fmode"]
        if d:
            return {"answer": f"Top failure mode is {d[0][0]} with {d[0][1]} occurrences, followed by "
                              f"{d[1][0] if len(d)>1 else ''} ({d[1][1] if len(d)>1 else 0}). "
                              f"These are the vital few to target.",
                    "chart": _chart_cnt(d)}

    # ---- MODULE by count (QS2.0) ----
    if has("module", "category", "subsystem", "by area") and agg["by_module_cnt"]:
        d = agg["by_module_cnt"]
        return {"answer": f"Defects by module: {d[0][0]} dominates with {d[0][1]} defects, then "
                          f"{d[1][0] if len(d)>1 else ''} ({d[1][1] if len(d)>1 else 0}).",
                "chart": _chart_cnt(d, "doughnut")}

    # ---- MAKE by count (QS2.0) ----
    if has("make", "brand", "manufacturer", "by car", "which car", "by make"):
        d = sorted(agg["cnt_make"].items(), key=lambda x: -x[1])[:8]
        d = [(k, v) for k, v in d if k not in ("Unknown", "Unspecified")]
        if d:
            return {"answer": f"By make, {d[0][0]} has the most defects ({d[0][1]}), then "
                              f"{d[1][0] if len(d)>1 else ''} ({d[1][1] if len(d)>1 else 0}).",
                    "chart": _chart_cnt(d)}

    # ---- COUNTS ----
    if has("how many", "count", "number of", "vehicles", "inspections", "defects", "total"):
        return {"answer": f"The dataset holds {agg['defects']} defects across {agg['inspections']} inspections "
                          f"on {agg['vehicles']} unique vehicles, with an average score of {agg['avg_score']}.",
                "chart": _chart_cnt(agg["by_module_cnt"]) if agg["by_module_cnt"] else None}

    # ---- TREND ----
    if has("trend", "month", "over time", "growth", "when", "recent", "increase", "improving"):
        d = list(agg["by_month"].items())
        if len(d) > 1:
            g = round((d[-1][1] / d[0][1] - 1) * 100) if d[0][1] else 0
            return {"answer": f"Defect volume by month: {d[0][1]} in {d[0][0]} to {d[-1][1]} in {d[-1][0]} "
                              f"\u2014 {'up' if g>=0 else 'down'} {abs(g)}%.",
                    "chart": {"type": "line", "data": [{"label": k, "value": v} for k, v in d]}}

    # ---- STATUS ----
    if has("status", "resolved", "reviewed", "flagged", "follow up", "follow-up", "open", "pending", "workflow"):
        s = agg["by_status"]
        return {"answer": f"Workflow: {s.get('New',0)} New, {s.get('Reviewed',0)} Reviewed, "
                          f"{s.get('Resolved',0)} Resolved. {agg['flagged']} flagged.",
                "chart": _chart_cnt(list(s.items()))}

    # ---- SUMMARY ----
    if has("summary", "overview", "tell me", "insight", "highlight", "key", "how are we"):
        mod = agg["by_module_cnt"]
        return {"answer": f"Across {agg['inspections']} inspections, {agg['defects']} defects on {agg['vehicles']} "
                          f"vehicles, avg score {agg['avg_score']}. {mod[0][0] if mod else 'n/a'} is the top defect "
                          f"module ({mod[0][1] if mod else 0}). Top failure mode: "
                          f"{agg['by_fmode'][0][0] if agg['by_fmode'] else 'n/a'}.",
                "chart": _chart_cnt(mod) if mod else None}

    return {"answer": "I can break defects down by line, module, make, failure mode, or conversion type; "
                      "show average scores and DPU; compare lines; show monthly trends; or report workflow status. "
                      "Try \u201Cwhich line has the worst score\u201D or \u201Ccompare side vs rear entry.\u201D",
            "chart": None}


def _local_OLD_cost(question, agg):
    q = question.lower().strip()
    has = lambda *ws: any(w in q for w in ws)

    # ---- specific make mentioned ----
    for mk in agg["makes"]:
        if mk not in ("Unknown","Unspecified") and mk.lower() in q:
            cost = sum(r.cost for r in agg["rows"] if r.make == mk)
            cnt = agg["cnt_make"][mk]
            veh = len({r.vin for r in agg["rows"] if r.make == mk and r.vin})
            share = round(cost / agg["total"] * 100) if agg["total"] else 0
            pm = defaultdict(float)
            for r in agg["rows"]:
                if r.make == mk:
                    pm[r.part_name] += r.cost
            tp = sorted(pm.items(), key=lambda x: -x[1])[:6]
            return {"answer": f"{mk} accounts for {_fmt(cost)} in reconditioning across {cnt} defects on {veh} vehicles "
                              f"\u2014 about {share}% of total cost. Its biggest cost driver is {_title(tp[0][0])} ({_fmt(tp[0][1])}).",
                    "chart": _chart(tp)}

    # ---- module ----
    if has("module", "atlas", "artemis", "helios", "exterior", "tire", "tyre", "undercarriage", "body"):
        d = dict(agg["by_module"])
        atlas = d.get("Atlas", 0)
        pct = round(atlas / agg["total"] * 100) if agg["total"] else 0
        return {"answer": f"Cost by inspection module. Atlas (exterior body) dominates at {_fmt(atlas)} \u2014 {pct}% of all "
                          f"reconditioning. Artemis covers tires, Helios the undercarriage.", "chart": _chart(agg["by_module"], "doughnut")}

    # ---- make breakdown ----
    if has("make", "brand", "manufacturer", "oem", "by car", "which car"):
        d = agg["by_make"]
        return {"answer": f"Reconditioning cost by make. {d[0][0]} leads at {_fmt(d[0][1])}, followed by "
                          f"{d[1][0]} ({_fmt(d[1][1])}) and {d[2][0]} ({_fmt(d[2][1])}).", "chart": _chart(d)}

    # ---- parts ----
    if has("part", "component", "panel", "fender", "bumper", "door", "roof", "hood", "trunk", "where", "location"):
        d = agg["by_part"]
        return {"answer": f"Top parts by reconditioning cost. {_title(d[0][0])} is highest at {_fmt(d[0][1])}, then "
                          f"{_title(d[1][0])} ({_fmt(d[1][1])}).", "chart": _chart(d)}

    # ---- worst / most expensive / priority ----
    if has("worst", "expensive", "biggest", "severe", "critical", "priorit", "highest", "costly", "focus"):
        ranked = sorted(agg["rows"], key=lambda r: -r.cost)[:6]
        pairs = [(r.part_name, r.cost) for r in ranked]
        return {"answer": f"Highest-cost individual defects. The single most expensive is {_title(ranked[0].part_name)} "
                          f"on a {ranked[0].year} {ranked[0].make} at {_fmt(ranked[0].cost)}.", "chart": _chart(pairs)}

    # ---- trend / time ----
    if has("trend", "month", "time", "over time", "growth", "grew", "when", "recent", "increase"):
        d = list(agg["by_month"].items())
        if len(d) > 1:
            g = round((d[-1][1] / d[0][1] - 1) * 100)
            return {"answer": f"Defect volume by month. It went from {d[0][1]} defects in {d[0][0]} to {d[-1][1]} in "
                              f"{d[-1][0]} \u2014 {'up' if g >= 0 else 'down'} {abs(g)}% as scan coverage expanded.",
                    "chart": {"type": "line", "data": [{"label": k, "value": v} for k, v in d]}}
        return {"answer": f"All {d[0][1]} defects fall in {d[0][0]}.", "chart": None}

    # ---- average ----
    if has("average", "avg", "mean", "typical", "per defect"):
        return {"answer": f"Average reconditioning cost is {_fmt(agg['avg'])} per defect, across {agg['defects']} defects "
                          f"on {agg['vehicles']} vehicles.", "chart": None}

    # ---- per inspection ----
    if has("per inspection", "per vehicle", "per car", "each inspection"):
        return {"answer": f"That's {_fmt(agg['total'] / agg['inspections'])} per inspection on average, "
                          f"across {agg['inspections']} inspections.", "chart": None}

    # ---- totals ----
    if has("total", "sum", "overall", "how much", "spend", "spent", "all together"):
        return {"answer": f"Total reconditioning cost is {_fmt(agg['total'])} across {agg['inspections']} inspections "
                          f"and {agg['defects']} detected defects.", "chart": None}

    # ---- status / workflow ----
    if has("status", "resolved", "reviewed", "flagged", "follow up", "follow-up", "open", "pending", "workflow"):
        s = agg["by_status"]
        return {"answer": f"Workflow status: {s.get('New',0)} New, {s.get('Reviewed',0)} Reviewed, "
                          f"{s.get('Resolved',0)} Resolved. {agg['flagged']} defect(s) flagged for follow-up.",
                "chart": _chart(list(s.items()))}

    # ---- counts ----
    if has("how many", "count", "number of", "vehicles", "inspections", "defects"):
        return {"answer": f"The dataset holds {agg['defects']} defects across {agg['inspections']} inspections "
                          f"on {agg['vehicles']} unique vehicles.", "chart": None}

    # ---- summary / overview ----
    if has("summary", "overview", "tell me", "insight", "highlight", "key"):
        d = agg["by_part"]
        atlas = dict(agg["by_module"]).get("Atlas", 0)
        pct = round(atlas / agg["total"] * 100) if agg["total"] else 0
        return {"answer": f"Across {agg['inspections']} inspections, {agg['defects']} defects total {_fmt(agg['total'])} in "
                          f"reconditioning. Exterior (Atlas) drives {pct}% of cost, {_title(d[0][0])} is the top part "
                          f"({_fmt(d[0][1])}), and {agg['by_make'][0][0]} is the costliest make ({_fmt(agg['by_make'][0][1])}).",
                "chart": _chart(d)}

    return {"answer": "I can break down cost by make, module, or part; rank the most expensive defects; show the monthly "
                      "trend; give totals, averages, or counts; or report workflow status. Try \u201Cwhich make costs the "
                      "most\u201D or \u201Cwhat should we prioritize.\u201D", "chart": None}


def answer_question(question, rows):
    agg = _aggregate(rows)

    if not ANTHROPIC_API_KEY:
        out = _local(question, agg)
        out["mode"] = "local"
        return out

    context = (
        "You are a quality-analytics assistant for the Northwind Quality Network, analyzing vehicle "
        "inspection defect data. Answer concisely (2-4 sentences), conversational, cite specific numbers. "
        "Use ONLY these aggregates. Note: lower quality score is better (it's a defect-severity score vs target). "
        f"{agg['defects']} defects, {agg['inspections']} inspections, {agg['vehicles']} vehicles, "
        f"avg score {agg['avg_score']}. Defects by module: {agg['by_module_cnt']}. By make: {agg['cnt_make']}. "
        f"By line (count): {agg['by_line_cnt']}. Avg score by line: {agg['by_line_score']}. "
        f"By conversion: {agg['by_conv_cnt']}, avg score: {agg['by_conv_score']}. "
        f"Top failure modes: {agg['by_fmode']}. Monthly: {agg['by_month']}. "
        f"Status: {agg['by_status']}, flagged {agg['flagged']}."
    )
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 400,
                  "messages": [{"role": "user", "content": f"{context}\n\nQuestion: {question}"}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = " ".join(b["text"] for b in data["content"] if b["type"] == "text")
        local = _local(question, agg)
        return {"answer": text.strip(), "chart": local["chart"], "mode": "claude"}
    except Exception as e:
        out = _local(question, agg)
        out["mode"] = "local-fallback"
        out["note"] = f"Claude unavailable ({type(e).__name__}); used local analysis."
        return out
