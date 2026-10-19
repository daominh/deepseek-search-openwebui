import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import _timeout, app

UPSTREAM = "https://upstream.test/search"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SEARCH_API_URL", UPSTREAM)
    monkeypatch.setenv("API_KEY", "secret-key")


@pytest.fixture
def client():
    return TestClient(app)


def results(*items):
    return {"results": list(items)}


def item(n):
    return {"title": f"T{n}", "url": f"https://e.com/{n}", "snippet": f"S{n}"}


@respx.mock
def test_happy_path_forwards_body_and_headers(client):
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results(item(1), item(2))))
    body = {"query": "open webui", "count": 5, "extra": True}
    r = client.post(
        "/search", json=body, headers={"Authorization": "Bearer caller", "X-OpenWebUI-User-Id": "u"}
    )
    assert r.status_code == 200
    assert r.json() == [
        {"link": "https://e.com/1", "title": "T1", "snippet": "S1"},
        {"link": "https://e.com/2", "title": "T2", "snippet": "S2"},
    ]
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer secret-key"
    assert req.headers["content-type"] == "application/json"
    assert "x-openwebui-user-id" not in req.headers
    assert json.loads(req.content) == body


@respx.mock
def test_count_truncation(client):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results(*[item(i) for i in range(5)])))
    r = client.post("/search", json={"query": "q", "count": 2})
    assert [x["link"] for x in r.json()] == ["https://e.com/0", "https://e.com/1"]


@pytest.mark.parametrize("count", [0, -1, None, "3", True])
@respx.mock
def test_count_not_positive_int_keeps_all(client, count):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results(*[item(i) for i in range(4)])))
    r = client.post("/search", json={"query": "q", "count": count})
    assert len(r.json()) == 4


@respx.mock
def test_null_and_missing_fields(client):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(
            200, json=results({"title": None, "url": "https://a"}, {"url": "https://b", "snippet": None})
        )
    )
    r = client.post("/search", json={"query": "q", "count": 5})
    assert r.json() == [
        {"link": "https://a", "title": "", "snippet": ""},
        {"link": "https://b", "title": "", "snippet": ""},
    ]


@respx.mock
def test_missing_url_skipped(client):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(200, json=results({"title": "x"}, {"title": "y", "url": None}, item(1)))
    )
    r = client.post("/search", json={"query": "q", "count": 5})
    assert r.json() == [{"link": "https://e.com/1", "title": "T1", "snippet": "S1"}]


@respx.mock
def test_non_2xx(client, caplog):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(500, text="boom"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []
    assert "500" in caplog.text
    assert "secret-key" not in caplog.text


@respx.mock
def test_timeout(client):
    respx.post(UPSTREAM).mock(side_effect=httpx.ReadTimeout("timed out"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []


@respx.mock
def test_network_error(client):
    respx.post(UPSTREAM).mock(side_effect=httpx.ConnectError("refused"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []


@respx.mock
def test_bad_json(client):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, text="not json"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize("payload", [{}, {"results": None}, {"results": "x"}, []])
@respx.mock
def test_missing_or_invalid_results(client, payload):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=payload))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []


@respx.mock
def test_non_dict_items_skipped(client):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(200, json=results(1, "x", None, ["a"], 2.5, True, item(1)))
    )
    r = client.post("/search", json={"query": "q", "count": 10})
    assert r.json() == [{"link": "https://e.com/1", "title": "T1", "snippet": "S1"}]


@pytest.mark.parametrize("url", [123, ["https://a"], {"u": 1}, True, ""])
@respx.mock
def test_non_str_url_skipped(client, url):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results({"title": "x", "url": url})))
    r = client.post("/search", json={"query": "q", "count": 5})
    assert r.json() == []


@pytest.mark.parametrize("value", [123, ["a"], {"k": "v"}, True, 1.5])
@respx.mock
def test_non_str_title_snippet_become_empty(client, value):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(200, json=results({"title": value, "url": "https://a", "snippet": value}))
    )
    r = client.post("/search", json={"query": "q", "count": 5})
    assert r.json() == [{"link": "https://a", "title": "", "snippet": ""}]


def test_malformed_upstream_url(client, monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_API_URL", "http://[not a url")
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []
    assert "upstream request failed" in caplog.text


@respx.mock
def test_non_ascii_api_key(client, monkeypatch, caplog):
    monkeypatch.setenv("API_KEY", "clé-secrète-ü")
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results(item(1))))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []
    assert "UnicodeEncodeError" in caplog.text
    assert "clé-secrète-ü" not in caplog.text


@respx.mock
def test_deeply_nested_upstream_json(client, caplog):
    depth = 100_000
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, content=b"[" * depth + b"]" * depth))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200 and r.json() == []
    assert "RecursionError" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 30.0), ("12.5", 12.5), ("0", 30.0), ("-5", 30.0), ("nan", 30.0), ("inf", 30.0), ("abc", 30.0)],
)
def test_timeout_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SEARCH_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("SEARCH_TIMEOUT", value)
    assert _timeout() == expected


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]"])
@respx.mock
def test_bad_inbound_body(client, raw):
    route = respx.post(UPSTREAM)
    r = client.post("/search", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json() == []
    assert not route.called


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
