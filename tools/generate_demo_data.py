"""Generate the fully synthetic demo data this project ships with.

Nothing here comes from a real company: VINs are random and start with DEMO,
descriptions come from templates, plants/lines/people are invented.

    python tools/generate_demo_data.py

Rewrites backend/seed_data.json, backend/data/quality_seed.json and
backend/data/pdi_seed.json (deterministic: same output every run).
"""
import json
import os
import random
import re
from datetime import date, timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
rnd = random.Random(2026)
END = date(2026, 9, 22)
ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"      # real VINs never use I, O or Q
MAKES = {"TOYOTA": ["Sienna", "Highlander"], "CHRYSLER": ["Pacifica", "Voyager"],
         "HONDA": ["Odyssey", "Pilot"], "FORD": ["Transit", "Explorer"],
         "CHEVROLET": ["Express", "Traverse"], "DODGE": ["Grand Caravan"]}


def fake_vin(used):
    while True:
        v = "DEMO" + "".join(rnd.choice(ALPHABET) for _ in range(13))
        if v not in used:
            used.add(v)
            return v


def days_ago(n):
    return (END - timedelta(days=n)).isoformat()


# ── UVeye-style scans ───────────────────────────────────────────────
PARTS = ["FenderRearRight", "FenderRearLeft", "FenderFrontRight", "FenderFrontLeft", "Roof", "Trunk",
         "DoorFrontLeft", "DoorFrontRight", "DoorRearLeft", "DoorRearRight", "BumperFront", "BumperRear",
         "Hood", "RightFrontTire", "LeftFrontTire", "RightRearTire", "LeftRearTire", "LicensePlateRear",
         "Antenna", "MirrorCoverRight", "Sunroof", "Undercarriage", "RoofRackRight"]
WEIGHTS = [60, 53, 51, 42, 48, 35, 26, 20, 32, 25, 22, 18, 16, 14, 13, 12, 7, 14, 6, 3, 1, 1, 1]
TEXT = {
    "minor": ["The {p} has light surface scratches that are cosmetic only.",
              "Small paint chip on the {p}; no metal exposed.",
              "Minor scuffing on the {p}, cosmetic and not affecting function."],
    "moderate": ["The {p} has a medium dent with some paint damage that needs repair.",
                 "Moderate scratches through the clear coat on the {p}.",
                 "The {p} shows a visible dent and paint cracking around the edge."],
    "severe": ["Large dent with deep paint damage on the {p}; panel repair required.",
               "Significant crease and cracked paint on the {p}, needs replacement.",
               "Severe damage on the {p} with exposed metal and corrosion risk."],
}
COST = {"minor": (60, 140), "moderate": (180, 320), "severe": (380, 640)}


def words(part):
    return " ".join(re.findall(r"[A-Z][a-z]+", part)).lower()


def uveye():
    used, rows = set(), []
    vins = [(fake_vin(used),) + rnd.choice(list(MAKES.items())) for _ in range(90)]
    for vin, make, models in vins:
        model, year = rnd.choice(models), str(rnd.choice([2024, 2025, 2026]))
        for _ in range(rnd.choice([1, 1, 1, 2, 3])):
            insp = "-".join("".join(rnd.choice("0123456789abcdef") for _ in range(n)) for n in (8, 4, 4, 4, 12))
            when = days_ago(rnd.randint(0, 60))
            chunk = []
            for _ in range(rnd.randint(2, 7)):
                part = rnd.choices(PARTS, WEIGHTS)[0]
                sev = rnd.choices(["minor", "moderate", "severe"], [5, 4, 1])[0]
                lo, hi = COST[sev]
                chunk.append({"inspection": insp, "vin": vin, "make": make, "model": model, "year": year,
                              "date": when, "part": part, "module": rnd.choices(["Atlas", "Artemis"], [9, 1])[0],
                              "desc": rnd.choice(TEXT[sev]).format(p=words(part)),
                              "cost": float(rnd.randrange(lo, hi, 5))})
            total = sum(c["cost"] for c in chunk)
            for c in chunk:
                c["total"] = total
            rows += chunk
    return rows


# ── Quality-system defects ──────────────────────────────────────────
MODULES = ["Electrical", "Ramp", "Door", "Paint", "Structure", "Floor"]
MODES = ["Missing Weld", "Poor Fit", "Loose Fastener", "Scratch", "Misalignment", "Bad Weld", "Wire Exposed",
         "Gap Too Large", "Light Powder Coat", "Rubs", "Sealant Missing", "Bracket Bent", "Rattle",
         "Paint Run", "Torque Out of Spec"]
LINES = ["QUAL", "ENGR", "WELD", "LN1", "LN2", "LN3", "LN4", "LN5"]
LINE_W = [410, 397, 386, 189, 175, 167, 166, 162]


def quality():
    used, rows = set(), []
    pool = [fake_vin(used) for _ in range(1470)]
    for i in range(2052):
        vin = pool[i] if i < len(pool) else rnd.choice(pool)
        mode = rnd.choice(MODES)
        rows.append({"inspection_id": f"QS26-{rnd.randint(10000, 99999)}", "vin": vin,
                     "make": rnd.choice(list(MAKES)), "line": rnd.choices(LINES, LINE_W)[0],
                     "inspected_at": days_ago(rnd.randint(0, 84)), "failure_mode": mode, "part_name": mode,
                     "module": rnd.choice(MODULES), "conversion": rnd.choice(["Side Entry", "Rear Entry"]),
                     "score": rnd.randint(1, 12), "plant": "P1"})
    return rows


# ── Pre-delivery inspection ─────────────────────────────────────────
PDI = [("BRAKES", "TESTDRIVE", "Rear rotors show surface rust that is audible when braking."),
       ("TIRES", "BODYEXTERIOR", "Both rear tires are worn close to the minimum tread depth."),
       ("LIGHTS", "ELECTRICAL", "Driver-side marker light is intermittent."),
       ("WIPERS", "BODYEXTERIOR", "Rear wiper streaks and needs replacement."),
       ("SEATBELT", "INTERIOR", "Second-row seat belt retractor is slow to return."),
       ("DOOR", "BODYEXTERIOR", "Sliding door drags at the bottom track."),
       ("HVAC", "TESTDRIVE", "Rear blower is noisy on the highest setting.")]


def pdi():
    used, rows = set(), []
    pool = [fake_vin(used) for _ in range(29)]
    for _ in range(52):
        part, mod, desc = rnd.choice(PDI)
        rows.append({"inspection_id": f"PDI26-{rnd.randint(10000, 99999)}", "vin": rnd.choice(pool),
                     "make": rnd.choice(list(MAKES)), "inspected_at": days_ago(rnd.randint(0, 60)),
                     "part_name": part, "module": mod, "description": desc, "plant": "PLANT-A",
                     "conversion": rnd.choice(["V1", "V2", "V3", "V4"]), "failure_mode": part})
    return rows


def dump(rows, rel):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f)
    print(f"{rel}: {len(rows)} rows")


if __name__ == "__main__":
    dump(uveye(), "seed_data.json")
    dump(quality(), "data/quality_seed.json")
    dump(pdi(), "data/pdi_seed.json")
