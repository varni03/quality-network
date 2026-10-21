import pytest
from sqlalchemy import event

import security


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_api_requires_login(client):
    assert client.get("/api/defects").status_code == 401
    assert client.get("/api/analytics").status_code == 401
    assert client.get("/api/defects", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_login_and_authenticated_access(client, auth):
    r = client.get("/api/defects?limit=5", headers=auth)
    assert r.status_code == 200 and 0 < len(r.json()) <= 5


def test_forged_token_signed_with_the_old_default_secret_is_rejected(client):
    import base64, hashlib, hmac, json, time
    raw = base64.urlsafe_b64encode(json.dumps({"u": "admin", "exp": int(time.time()) + 999}).encode()).decode()
    sig = hmac.new(b"dev-secret-change-me", raw.encode(), hashlib.sha256).hexdigest()
    assert client.get("/api/defects", headers={"Authorization": f"Bearer {raw}.{sig}"}).status_code == 401


def test_security_headers_present(client):
    h = client.get("/health").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "SAMEORIGIN" and h["referrer-policy"] == "no-referrer"


def test_session_cookie_is_httponly(client):
    r = client.post("/api/login", json={"username": "admin", "password": "demo-test-password"})
    assert "httponly" in r.headers["set-cookie"].lower()


def test_wrong_password_is_401(client):
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_lockout_after_repeated_failures_blocks_even_the_right_password(client, monkeypatch):
    monkeypatch.setattr(security, "LOGIN_MAX_FAILURES", 3)
    hdr = {"x-forwarded-for": "9.9.9.9"}
    for _ in range(3):
        assert client.post("/api/login", json={"username": "admin", "password": "x"},
                           headers=hdr).status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": "demo-test-password"}, headers=hdr)
    assert r.status_code == 429
    other = client.post("/api/login", json={"username": "admin", "password": "demo-test-password"},
                        headers={"x-forwarded-for": "8.8.8.8"})
    assert other.status_code == 200            # other addresses are unaffected


def test_spoofed_forwarded_for_prefix_does_not_dodge_lockout(client, monkeypatch):
    monkeypatch.setattr(security, "LOGIN_MAX_FAILURES", 3)
    for i in range(3):
        client.post("/api/login", json={"username": "admin", "password": "x"},
                    headers={"x-forwarded-for": f"10.0.0.{i}, 7.7.7.7"})   # same real address
    r = client.post("/api/login", json={"username": "admin", "password": "demo-test-password"},
                    headers={"x-forwarded-for": "10.9.9.9, 7.7.7.7"})
    assert r.status_code == 429


def test_successful_login_resets_the_failure_count(client, monkeypatch):
    monkeypatch.setattr(security, "LOGIN_MAX_FAILURES", 3)
    hdr = {"x-forwarded-for": "5.5.5.5"}
    for _ in range(2):
        client.post("/api/login", json={"username": "admin", "password": "x"}, headers=hdr)
    assert client.post("/api/login", json={"username": "admin", "password": "demo-test-password"},
                       headers=hdr).status_code == 200
    for _ in range(2):
        client.post("/api/login", json={"username": "admin", "password": "x"}, headers=hdr)
    assert client.post("/api/login", json={"username": "admin", "password": "demo-test-password"},
                       headers=hdr).status_code == 200


def test_request_rate_limit_returns_429(client, auth, monkeypatch):
    monkeypatch.setattr(security, "RATE_LIMIT", 5)
    codes = [client.get("/health", headers={"x-forwarded-for": "4.4.4.4"}).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}


def test_invalid_defect_status_is_rejected_and_valid_one_is_saved(client, auth):
    d = client.get("/api/defects?limit=1", headers=auth).json()[0]
    assert client.patch(f"/api/defects/{d['id']}", json={"status": "Bogus"}, headers=auth).status_code == 400
    ok = client.patch(f"/api/defects/{d['id']}", json={"status": "Reviewed"}, headers=auth)
    assert ok.status_code == 200 and ok.json()["status"] == "Reviewed"


def test_missing_defect_is_404(client, auth):
    assert client.get("/api/defects/99999999", headers=auth).status_code == 404


def test_notes_round_trip_and_validation(client, auth):
    d = client.get("/api/defects?limit=1", headers=auth).json()[0]
    assert client.post(f"/api/defects/{d['id']}/notes", json={"body": ""}, headers=auth).status_code == 422
    n = client.post(f"/api/defects/{d['id']}/notes", json={"body": "checked", "author": "QA"}, headers=auth)
    assert n.status_code == 200
    listed = client.get(f"/api/defects/{d['id']}", headers=auth).json()
    assert any(x["body"] == "checked" for x in listed["notes"])
    assert client.delete(f"/api/notes/{n.json()['id']}", headers=auth).status_code == 200


def test_listing_defects_does_not_issue_one_query_per_row(client, auth):
    """Regression test: notes used to be lazy-loaded, so a 200-row page cost 201 queries."""
    from database import engine
    for d in client.get("/api/defects?limit=20", headers=auth).json():
        client.post(f"/api/defects/{d['id']}/notes", json={"body": "n"}, headers=auth)
    seen = []
    def count(conn, cursor, statement, *a):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)
    event.listen(engine, "before_cursor_execute", count)
    try:
        r = client.get("/api/defects?limit=20", headers=auth)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200 and len(r.json()) == 20
    assert len(seen) <= 3, f"{len(seen)} SELECTs for 20 rows"


def test_db_pool_covers_the_worker_thread_pool():
    from database import engine
    assert engine.pool.size() + engine.pool._max_overflow >= 40


def test_analytics_and_quality_app_endpoints_respond(client, auth):
    for path in ("/api/analytics", "/api/quality-app/pulse", "/api/vehicles", "/api/priority"):
        assert client.get(path, headers=auth).status_code == 200, path
