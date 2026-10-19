import pytest
from fastapi.testclient import TestClient

from app import upstream
from app.main import app

BASE = "https://upstream.test/v1"
SEARCH = f"{BASE}/tools/search"
FETCH = f"{BASE}/tools/fetch"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_URL", BASE)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-key")
    for name in ("SEARCH_TIMEOUT", "FETCH_TIMEOUT", "FETCH_UNESCAPE_MARKDOWN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(upstream, "_warned_tool_suffix", set())


@pytest.fixture
def client():
    return TestClient(app)


def assert_error(r, status):
    assert r.status_code == status
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert set(body) == {"error"} and isinstance(body["error"], str) and body["error"]
    assert "secret-key" not in r.text
