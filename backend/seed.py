"""Seed the database from the UVeye export (seed_data.json).

Idempotent: only seeds if the defects table is empty, so restarts don't duplicate.
Run automatically on app startup; can also be run standalone: python seed.py
"""
import json
import os
from database import SessionLocal, engine, Base
import models

SEED_FILE = os.path.join(os.path.dirname(__file__), "seed_data.json")


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(models.Defect).filter(models.Defect.source == "uveye").count() > 0:
            print("UVeye data already seeded; skipping.")
            return
        with open(SEED_FILE) as f:
            rows = json.load(f)
        objs = []
        for r in rows:
            objs.append(models.Defect(
                inspection_id=r.get("inspection", ""),
                vin=r.get("vin", ""),
                make=r.get("make") or "Unspecified",
                model=r.get("model", ""),
                year=str(r.get("year", "")),
                inspected_at=r.get("date", ""),
                part_name=r.get("part", ""),
                module=r.get("module") or "Other",
                description=r.get("desc", ""),
                cost=float(r.get("cost") or 0),
                status="New",
                flagged=False,
                source="uveye",
            ))
        db.bulk_save_objects(objs)
        # --- demo multi-scan vehicle (pre / post / re-verification) ---
        # Lets the scan-timeline feature show real before/after data until
        # real conversion test data lands. Same VIN, three dates.
        demo_vin = "DEMO1CONV2025TEST"
        demo = [
            ("2026-06-01", "Roof", "Atlas", "Multiple dents and scratches on roof panel pre-conversion.", 540),
            ("2026-06-01", "FenderRearLeft", "Atlas", "Large dent with paint damage requiring repair.", 415),
            ("2026-06-01", "DoorFrontRight", "Atlas", "Medium scratch and small dent.", 225),
            ("2026-06-01", "Hood", "Atlas", "Surface scratches penetrating clear coat.", 235),
            ("2026-06-01", "LeftRearTire", "Artemis", "Wheel damage detected, replacement needed.", 300),
            ("2026-06-08", "FenderRearLeft", "Atlas", "Prior dent still present, not yet repaired.", 415),
            ("2026-06-08", "RockerPanelLeft", "Atlas", "New scuff introduced during conversion handling.", 180),
            ("2026-06-15", "RockerPanelLeft", "Atlas", "Minor scuff, cosmetic only, pending final touch-up.", 90),
        ]
        for date, part, module, desc, cost in demo:
            db.add(models.Defect(
                inspection_id=f"demo-{date}", vin=demo_vin, make="CHRYSLER",
                model="Pacifica", year="2025", inspected_at=date, part_name=part,
                module=module, description=desc, cost=float(cost),
                status="New", flagged=False, source="uveye"))
        db.commit()
        print(f"Seeded {len(objs)} defects.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
