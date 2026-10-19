import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import _describe, _timeout, app

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


def assert_error(r, status):
    assert r.status_code == status
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert set(body) == {"error"} and isinstance(body["error"], str) and body["error"]
    assert "secret-key" not in r.text


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


@pytest.mark.parametrize(
    ("status", "content", "content_type"),
    [
        (401, b'{"error":{"message":"Invalid API key"}}', "application/json"),
        (429, b'{"error": "rate limited", "retry_after": 3}', "application/json; charset=utf-8"),
        (500, b"boom: internal error\n", "text/plain"),
    ],
)
@respx.mock
def test_non_2xx_passthrough(client, caplog, status, content, content_type):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(
            status, content=content, headers={"Content-Type": content_type, "X-Upstream-Secret": "x"}
        )
    )
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == status
    assert r.content == content
    assert r.headers["content-type"] == content_type
    assert "x-upstream-secret" not in r.headers
    assert str(status) in caplog.text
    assert "secret-key" not in caplog.text


@respx.mock
def test_non_2xx_without_content_type(client):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(403, content=b"nope"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 403
    assert r.content == b"nope"
    assert "content-type" not in r.headers


@respx.mock
def test_timeout(client, caplog):
    respx.post(UPSTREAM).mock(side_effect=httpx.ReadTimeout("timed out"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 504)
    assert "ReadTimeout" in caplog.text


@respx.mock
def test_network_error(client, caplog):
    respx.post(UPSTREAM).mock(side_effect=httpx.ConnectError("refused"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
    assert "ConnectError" in caplog.text
    assert "secret-key" not in caplog.text


@respx.mock
def test_unexpected_exception(client):
    respx.post(UPSTREAM).mock(side_effect=RuntimeError("kaboom"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)


@respx.mock
def test_exception_message_with_key_is_scrubbed(client, caplog):
    respx.post(UPSTREAM).mock(side_effect=httpx.ConnectError("failed with Bearer secret-key"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
    assert "secret-key" not in caplog.text


def test_missing_upstream_url(client, monkeypatch):
    monkeypatch.delenv("SEARCH_API_URL")
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)


@respx.mock
def test_bad_json(client):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, text="not json"))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)


@pytest.mark.parametrize("payload", [{}, {"results": None}, {"results": "x"}, [], "str", 1, None])
@respx.mock
def test_missing_or_invalid_results(client, payload):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=payload))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)


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
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize("value", [123, ["a"], {"k": "v"}, True, 1.5])
@respx.mock
def test_non_str_title_snippet_become_empty(client, value):
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(200, json=results({"title": value, "url": "https://a", "snippet": value}))
    )
    r = client.post("/search", json={"query": "q", "count": 5})
    assert r.json() == [{"link": "https://a", "title": "", "snippet": ""}]


@pytest.mark.parametrize(
    ("url", "exc_name"),
    [("http://example.com:abc/", "InvalidURL"), ("not-a-url", "UnsupportedProtocol")],
)
def test_malformed_upstream_url(client, monkeypatch, caplog, url, exc_name):
    monkeypatch.setenv("SEARCH_API_URL", url)
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
    assert "upstream request failed" in caplog.text
    assert exc_name in caplog.text


@respx.mock
def test_non_ascii_api_key(client, monkeypatch, caplog):
    key = "clé-secrète-ü"
    monkeypatch.setenv("API_KEY", key)
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=results(item(1))))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
    assert not route.called
    assert "UnicodeEncodeError" in caplog.text
    for text in (r.text, caplog.text):
        assert key not in text
        assert "é" not in text and "\\xe9" not in text


@respx.mock
def test_deeply_nested_upstream_json(client, caplog):
    depth = 100_000
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, content=b"[" * depth + b"]" * depth))
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
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


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]", b'"q"', b"null", b""])
@respx.mock
def test_bad_inbound_body(client, raw):
    route = respx.post(UPSTREAM)
    r = client.post("/search", content=raw, headers={"Content-Type": "application/json"})
    assert_error(r, 400)
    assert not route.called


@pytest.fixture
def real_upstream(monkeypatch):
    """A local HTTP server, so requests go through real header serialization (h11)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.dumps(results(item(1))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SEARCH_API_URL", f"http://127.0.0.1:{server.server_port}/search")
    yield seen
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("key", ["secret-key\r", "secret-key\r\n", " secret-key\t", "secret-key \n"])
def test_api_key_surrounding_whitespace_stripped(client, monkeypatch, caplog, real_upstream, key):
    monkeypatch.setenv("API_KEY", key)
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 200
    assert r.json() == [{"link": "https://e.com/1", "title": "T1", "snippet": "S1"}]
    assert real_upstream == ["Bearer secret-key"]
    assert "secret-key" not in caplog.text


@pytest.mark.parametrize("key", ["secret\rkey", "secret\nkey", "secret\r\nkey"])
def test_api_key_with_control_chars(client, monkeypatch, caplog, real_upstream, key):
    monkeypatch.setenv("API_KEY", key)
    r = client.post("/search", json={"query": "q", "count": 3})
    assert_error(r, 502)
    assert real_upstream == []
    assert "LocalProtocolError" in caplog.text
    for text in (r.text, caplog.text):
        assert "secret" not in text and "key" not in text.replace("API key", "")


@pytest.mark.parametrize(
    "message",
    [
        "boom secret\rkey boom",
        "boom 'secret\\rkey' boom",
        "boom b'secret\\rkey' boom",
    ],
)
def test_describe_scrubs_raw_and_escaped_key(message):
    out = _describe(httpx.ConnectError(message), "secret\rkey")
    assert out.startswith("ConnectError: boom ")
    assert "secret" not in out


@respx.mock
def test_non_2xx_log_scrubbed_body_unchanged(client, caplog):
    content = b'{"error": "bad key secret-key"}'
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(401, content=content, headers={"Content-Type": "application/json"})
    )
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 401 and r.content == content
    assert "401" in caplog.text
    assert "secret-key" not in caplog.text


@respx.mock
def test_non_2xx_non_latin1_content_type():
    raw_ct = "text/plain; x=€".encode()
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(400, content=b"bad", headers=[(b"Content-Type", raw_ct)])
    )

    # Starlette's TestClient can't represent non-ASCII response headers; ASGITransport keeps raw bytes.
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as c:
            return await c.post("/search", json={"query": "q", "count": 3})

    r = asyncio.run(call())
    assert r.status_code == 400 and r.content == b"bad"
    assert [v for k, v in r.headers.raw if k.lower() == b"content-type"] == [raw_ct]


@respx.mock
def test_non_2xx_gzip_body_decompressed(client):
    text = b"upstream exploded " * 50
    respx.post(UPSTREAM).mock(
        return_value=httpx.Response(
            503,
            content=gzip.compress(text),
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
        )
    )
    r = client.post("/search", json={"query": "q", "count": 3})
    assert r.status_code == 503
    assert r.content == text
    assert r.headers["content-length"] == str(len(text))
    assert "content-encoding" not in r.headers


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
