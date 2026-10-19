import importlib
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The server reads its configuration at import time, so set it up first.
DATA_DIR = tempfile.mkdtemp(prefix="pocket-caldav-test-")
API_KEY = secrets.token_urlsafe(32)
DAV_PASSWORD = secrets.token_urlsafe(12)
os.environ.update(
    {
        "DATA_DIR": DATA_DIR,
        "CALDAV_API_KEY": API_KEY,
        "CALDAV_USERS": f"tester:{DAV_PASSWORD}",
        "HOST": "127.0.0.1",
        "DEFAULT_TIMEZONE": "Europe/Berlin",
    }
)
for name in ("PUBLIC_BASE_URL", "ALLOWED_HOSTS", "ATTACHMENT_TTL_DAYS", "CLEANUP_INTERVAL_SECONDS"):
    os.environ.pop(name, None)


@pytest.fixture(scope="session")
def server():
    return importlib.import_module("server")


@pytest.fixture(scope="session")
def client(server):
    from fastapi.testclient import TestClient

    with TestClient(server.app, base_url="http://localhost") as test_client:
        test_client.headers["X-API-Key"] = API_KEY
        yield test_client


@pytest.fixture
def calendar(client, request):
    """A fresh calendar owned by the DAV test user."""
    name = "t-" + secrets.token_hex(4)
    response = client.post("/api/calendars", json={"owner": "tester", "calendar": name, "display_name": request.node.name[:40]})
    assert response.status_code == 200
    return "tester", name


@pytest.fixture
def dav_auth():
    return ("tester", DAV_PASSWORD)
