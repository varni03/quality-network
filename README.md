# Quality Network (portfolio demo)

A full-stack manufacturing quality platform: vehicle inspection analytics, defect
workflow, and a Quality App workspace for the plant floor. **All data in this repo is
synthetic** (random `DEMO...` VINs, template-generated descriptions, invented lines and
people). No real company data, credentials or internal system names are included.

**Stack:** FastAPI + SQLAlchemy/SQLite backend, single-file vanilla JS frontend
(Chart.js), HMAC-signed session tokens, rate limiting, audit log, PDF export.

## What's in it
- **Analytics workspaces** (UVeye scans, quality system, pre-delivery inspection): KPIs,
  anomaly detection, priority ranking, scan timeline, AI co-pilot with a local analytics
  engine (optional Claude mode).
- **Quality App workspace:** Today (floor overview), Inspection with body map, Check-in,
  Vehicle 360, Repair queue, My queue (department-first worklist with aging), Holds,
  Sign-Off (bulk), Trends, 510 Report, Admin (users and departments), Audit trail.
- **Security:** login, role-aware directory accounts (PBKDF2), signed tokens, VIN masking,
  rate limiting, locked CORS, security headers.

## Run locally
```
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```
Open http://127.0.0.1:8000. Default demo login: `admin` / `changeme-please`
(override with `APP_USERNAME` / `APP_PASSWORD` in `backend/.env`; see `.env.example`).

## Demo data
- `python tools/generate_demo_data.py` regenerates the seed files deterministically.
- The Quality App tabs read from a local SQLite database built at startup by
  `backend/local_sf.py`. It mirrors a data-warehouse schema, translates the warehouse SQL,
  shifts dates so the newest record is "today", and generates holds, sign-offs,
  locations and check-ins.
- The app was originally built against a warehouse (Snowflake). `LOCAL_DATA=0` plus
  `SNOWFLAKE_*` settings re-enables that path if you have your own.
- `DEMO_WRITES = true` in `frontend/index.html` keeps edits in the browser tab only.

See `DEPLOY.md` for hosting options (includes a Dockerfile).
