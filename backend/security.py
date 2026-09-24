"""Security guardrails for the UVeye Quality Intelligence API.

This module layers several protections on top of the app. None of these make
the app enterprise-grade on their own, but together they close the most glaring
holes for an externally hosted prototype:

  1. Authentication      - login required; signed session tokens (no more open access)
  2. Role-based reveal   - only authenticated users may see unmasked VINs
  3. API-level masking   - raw API responses mask VINs too, so masking can't be
                           bypassed by calling the API directly
  4. Rate limiting       - per-IP request cap to deter scraping / abuse
  5. Security headers     - standard hardening headers on every response
  6. Audit logging       - records logins and sensitive data access
  7. Locked CORS         - configurable allowed origins instead of wildcard

Configuration (environment variables):
  APP_USERNAME       login username           (default: admin)
  APP_PASSWORD       login password           (default: changeme-please)
  SECRET_KEY         token signing secret     (default: dev-secret-change-me)
  ALLOWED_ORIGINS    comma-separated origins  (default: * for local dev)
  RATE_LIMIT_PER_MIN requests/min per IP      (default: 120)
  REQUIRE_AUTH       "false" disables auth     (default: true)
"""
import os
import time
import hmac
import json
import base64
import hashlib
import logging
from collections import defaultdict, deque

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
audit = logging.getLogger("audit")

# ---- config ----
USERNAME = os.getenv("APP_USERNAME", "admin")
PASSWORD = os.getenv("APP_PASSWORD", "changeme-please")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me").encode()
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))  # 8 hours
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() != "false"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]


# ---- password hashing (constant-time compare) ----
def _hash(pw: str) -> str:
    return hashlib.sha256((pw + "uveye-salt").encode()).hexdigest()


_PW_HASH = _hash(PASSWORD)


def verify_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username or "", USERNAME)
    pass_ok = hmac.compare_digest(_hash(password or ""), _PW_HASH)
    return user_ok and pass_ok


# ---- signed session tokens (stateless, HMAC-signed) ----
def issue_token(username: str) -> str:
    payload = {"u": username, "exp": int(time.time()) + TOKEN_TTL}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(SECRET_KEY, raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY, raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return False
    return payload.get("exp", 0) >= int(time.time())


# ---- rate limiting (in-memory, per IP, sliding window) ----
_hits = defaultdict(deque)


def rate_limited(ip: str) -> bool:
    now = time.time()
    window = _hits[ip]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return True
    window.append(now)
    return False


# ---- VIN masking for API responses ----
def mask_vin(vin: str, authorized: bool) -> str:
    if not vin:
        return ""
    if authorized:
        return vin
    return "\u2022\u2022\u2022\u2022" + vin[-4:]


def mask_name(name: str, authorized: bool) -> str:
    if not name:
        return ""
    if authorized:
        return name
    parts = name.strip().split()
    return parts[0][0] + ". \u2022\u2022\u2022\u2022\u2022" if parts else ""


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
}
