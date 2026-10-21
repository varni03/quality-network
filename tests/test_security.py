import base64
import hashlib
import hmac
import json
import time

import pytest

import security


def _sign(payload) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{raw}.{hmac.new(security.SECRET_KEY, raw.encode(), hashlib.sha256).hexdigest()}"


# ---- tokens ----
def test_token_round_trip():
    assert security.verify_token(security.issue_token("admin"))


def test_tampered_payload_is_rejected():
    raw, sig = security.issue_token("admin").rsplit(".", 1)
    forged = base64.urlsafe_b64encode(json.dumps({"u": "root", "exp": 9999999999}).encode()).decode()
    assert not security.verify_token(f"{forged}.{sig}")


def test_tampered_signature_is_rejected():
    raw, sig = security.issue_token("admin").rsplit(".", 1)
    assert not security.verify_token(f"{raw}.{'0' * len(sig)}")


def test_expired_token_is_rejected():
    assert not security.verify_token(_sign({"u": "admin", "exp": int(time.time()) - 1}))


@pytest.mark.parametrize("payload", [[1, 2], 5, "text", None, {"u": "a", "exp": "never"}, {"u": "a"}])
def test_validly_signed_but_malformed_payload_is_rejected(payload):
    assert not security.verify_token(_sign(payload))


@pytest.mark.parametrize("token", ["", "abc", "a.b", ".", "..", None])
def test_garbage_tokens_are_rejected(token):
    assert not security.verify_token(token)


# ---- passwords ----
def test_credentials():
    assert security.verify_credentials("admin", "demo-test-password")
    assert not security.verify_credentials("admin", "wrong")
    assert not security.verify_credentials("root", "demo-test-password")
    assert not security.verify_credentials("", "")


def test_stored_hash_is_salted_pbkdf2_not_plain_sha256():
    legacy = hashlib.sha256(("demo-test-password" + "uveye-salt").encode()).hexdigest().encode()
    assert security._PW_HASH != legacy and len(security._PW_HASH) == 32


# ---- production guard ----
def test_production_refuses_published_defaults():
    with pytest.raises(RuntimeError, match="published default"):
        security.check_config("production", security.DEFAULT_PASSWORD, b"x" * 32)
    with pytest.raises(RuntimeError, match="forge"):
        security.check_config("production", "good-password", security.DEFAULT_SECRET.encode())


def test_production_accepts_real_secrets_and_development_only_warns():
    assert security.check_config("production", "good-password", b"x" * 32) == []
    assert len(security.check_config("development", security.DEFAULT_PASSWORD,
                                     security.DEFAULT_SECRET.encode())) == 2


# ---- client address ----
def test_client_ip_ignores_client_supplied_left_side(monkeypatch):
    monkeypatch.setattr(security, "TRUSTED_PROXY_COUNT", 1)
    assert security.client_ip("6.6.6.6, 1.2.3.4", "10.0.0.1") == "1.2.3.4"
    assert security.client_ip("1.2.3.4", "10.0.0.1") == "1.2.3.4"


def test_client_ip_two_proxies(monkeypatch):
    monkeypatch.setattr(security, "TRUSTED_PROXY_COUNT", 2)
    assert security.client_ip("6.6.6.6, 1.2.3.4, 10.9.9.9", "10.0.0.1") == "1.2.3.4"


def test_client_ip_without_proxy_ignores_header(monkeypatch):
    monkeypatch.setattr(security, "TRUSTED_PROXY_COUNT", 0)
    assert security.client_ip("6.6.6.6", "10.0.0.1") == "10.0.0.1"


def test_client_ip_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(security, "TRUSTED_PROXY_COUNT", 2)
    assert security.client_ip("1.2.3.4", "10.0.0.1") == "10.0.0.1"
    assert security.client_ip(None, None) == "?"


# ---- rate limit ----
def test_rate_limit_blocks_after_limit_and_is_per_ip(monkeypatch):
    monkeypatch.setattr(security, "RATE_LIMIT", 3)
    assert [security.rate_limited("a") for _ in range(4)] == [False, False, False, True]
    assert security.rate_limited("b") is False


def test_rate_limit_window_slides(monkeypatch):
    monkeypatch.setattr(security, "RATE_LIMIT", 2)
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    security.rate_limited("a"); security.rate_limited("a")
    assert security.rate_limited("a") is True
    now[0] += 61
    assert security.rate_limited("a") is False


def test_idle_ips_are_evicted(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    monkeypatch.setattr(security, "_last_sweep", 0.0)
    for i in range(50):
        security.rate_limited(f"ip{i}")
    assert len(security._hits) == 50
    now[0] += 120
    security.rate_limited("fresh")
    assert set(security._hits) == {"fresh"}


# ---- login lockout ----
def test_lockout_after_max_failures_then_expires(monkeypatch):
    monkeypatch.setattr(security, "LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr(security, "LOGIN_LOCKOUT_SECONDS", 60)
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    for _ in range(2):
        security.record_login_failure("x")
    assert not security.login_locked("x")
    security.record_login_failure("x")
    assert security.login_locked("x") and not security.login_locked("y")
    now[0] += 61
    assert not security.login_locked("x")


def test_success_clears_failures():
    for _ in range(4):
        security.record_login_failure("x")
    security.clear_login_failures("x")
    assert not security.login_locked("x")


# ---- masking ----
def test_masking():
    assert security.mask_vin("1HGCM82633A004352", False) == "••••4352"
    assert security.mask_vin("1HGCM82633A004352", True) == "1HGCM82633A004352"
    assert security.mask_vin("", True) == ""
    assert security.mask_name("Jane Doe", False).startswith("J.")
    assert security.mask_name("Jane Doe", True) == "Jane Doe"


def test_cors_is_same_origin_by_default():
    assert security.ALLOWED_ORIGINS == []
