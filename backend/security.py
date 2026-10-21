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
  ALLOWED_ORIGINS    comma-separated origins  (default: none, same-origin only)
  RATE_LIMIT_PER_MIN requests/min per IP      (default: 120)
  REQUIRE_AUTH       "false" disables auth     (default: true)
  TRUSTED_PROXY_COUNT reverse proxies in front (default: 1; 0 ignores X-Forwarded-For)
  LOGIN_MAX_FAILURES failed logins per IP before lockout (default: 5)
  LOGIN_LOCKOUT_SECONDS lockout window         (default: 900)
  APP_ENV            "production" refuses to start on the default password or secret
"""
import os
import time
import hmac
import json
import base64
import hashlib
import logging
import secrets
from collections import defaultdict, deque

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
audit = logging.getLogger("audit")

# ---- config ----
DEFAULT_PASSWORD = "changeme-please"
DEFAULT_SECRET = "dev-secret-change-me"
APP_ENV = os.getenv("APP_ENV", "development").lower()
USERNAME = os.getenv("APP_USERNAME", "admin")
PASSWORD = os.getenv("APP_PASSWORD", DEFAULT_PASSWORD)
SECRET_KEY = os.getenv("SECRET_KEY", DEFAULT_SECRET).encode()
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))  # 8 hours
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() != "false"
# Same-origin by default: the API serves its own frontend. Set ALLOWED_ORIGINS to allow others.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "1"))
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900"))


def check_config(env: str, password: str, secret: bytes) -> list[str]:
    """Return the insecure defaults in use. In production they are fatal, elsewhere a warning."""
    problems = []
    if password == DEFAULT_PASSWORD:
        problems.append("APP_PASSWORD is the published default")
    if secret == DEFAULT_SECRET.encode():
        problems.append("SECRET_KEY is the published default, so anyone can forge session tokens")
    if problems and env == "production":
        raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    return problems


for _msg in check_config(APP_ENV, PASSWORD, SECRET_KEY):
    audit.warning("INSECURE DEFAULT: %s (fine for a local demo, set it before real use)", _msg)


# ---- password hashing (constant-time compare) ----
_PBKDF2_ROUNDS = 120_000
_SALT = secrets.token_bytes(16)      # per-process salt; the plaintext is never compared directly


def _hash(pw: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), _SALT, _PBKDF2_ROUNDS)


_PW_HASH = _hash(PASSWORD)


def verify_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest((username or "").encode(), USERNAME.encode())
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
    if not isinstance(payload, dict):
        return False
    exp = payload.get("exp", 0)
    return isinstance(exp, (int, float)) and exp >= int(time.time())


# ---- client address behind reverse proxies ----
def client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """Address used for rate limiting and lockout.

    The left side of X-Forwarded-For is whatever the client chose to send, so it is never trusted.
    With N trusted proxies the Nth entry from the right is the address the outermost trusted proxy
    saw. N=0 ignores the header entirely (direct connections).
    """
    n = TRUSTED_PROXY_COUNT
    if n > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= n:
            return parts[-n]
    return peer or "?"


# ---- rate limiting (in-memory, per IP, sliding window) ----
_hits = defaultdict(deque)
_last_sweep = 0.0


def rate_limited(ip: str) -> bool:
    global _last_sweep
    now = time.time()
    if now - _last_sweep > 60:               # drop idle IPs so the table cannot grow forever
        _last_sweep = now
        for k in [k for k, w in _hits.items() if not w or w[-1] < now - 60]:
            del _hits[k]
    window = _hits[ip]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return True
    window.append(now)
    return False


# ---- login lockout (in-memory, per IP) ----
_failures = defaultdict(deque)


def login_locked(ip: str) -> bool:
    now = time.time()
    w = _failures.get(ip)
    if not w:
        return False
    while w and w[0] < now - LOGIN_LOCKOUT_SECONDS:
        w.popleft()
    if not w:
        del _failures[ip]
        return False
    return len(w) >= LOGIN_MAX_FAILURES


def record_login_failure(ip: str) -> None:
    _failures[ip].append(time.time())


def clear_login_failures(ip: str) -> None:
    _failures.pop(ip, None)


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
