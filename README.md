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
- **Security:** login, role-aware directory accounts (PBKDF2), HMAC-signed tokens, VIN masking,
  per-IP rate limiting, failed-login lockout, same-origin CORS by default, security headers, and a
  production guard that refuses to start on the published default password or signing secret.

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

## Tests
```
pip install -r requirements-dev.txt
pytest --cov=security --cov=database
```
48 tests cover token signing and tampering, password checks, the production guard, client-address
handling behind proxies, rate limiting, login lockout, the auth gate, and the defects and notes API.
Two regressions are pinned by tests: listing defects used to issue one query per row, and the
database pool (5 + 10 connections) was smaller than FastAPI's 40 worker threads.

**Measured effect** (300 requests at concurrency 100 on `/api/defects?limit=20`, one uvicorn worker,
2 vCPU, local SQLite): before, 107 succeeded, 30 returned HTTP 500 (`QueuePool limit reached`) and
133 timed out, with p50 21.6 s. After, all 300 succeeded at 131.6 req/s with p50 588 ms and p95
1.76 s. `/api/analytics` and `/api/vehicles` were not affected by the change.
