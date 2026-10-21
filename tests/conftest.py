"""Test setup. The app reads its configuration at import time, so the environment is fixed first."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="qn-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["LOCAL_DB_PATH"] = f"{_TMP}/local.db"
os.environ["RATE_LIMIT_PER_MIN"] = "100000"          # tests that need a limit set their own
os.environ["TRUSTED_PROXY_COUNT"] = "1"
os.environ["APP_PASSWORD"] = "demo-test-password"
os.environ["SECRET_KEY"] = "test-signing-secret"
os.environ.pop("APP_ENV", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_security_state():
    import security
    security._hits.clear()
    security._failures.clear()
    yield
    security._hits.clear()
    security._failures.clear()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def auth(client):
    r = client.post("/api/login", json={"username": "admin", "password": "demo-test-password"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}
