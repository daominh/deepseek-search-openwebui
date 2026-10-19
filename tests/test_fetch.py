import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import respx

from app.main import unescape_markdown
from app.upstream import timeout
from tests.conftest import FETCH as UPSTREAM
from tests.conftest import assert_error


def page(n):
    return {"title": f"T{n}", "content": f"C{n}"}


def doc(url, n):
    return {"page_content": f"C{n}", "metadata": {"source": url, "title": f"T{n}"}}


def by_url(responses):
    """respx side effect answering each upstream call by the `url` in its JSON body."""

    def handler(request):
        outcome = responses[json.loads(request.content)["url"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler


@respx.mock
def test_happy_path(client):
    route = respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://a.test/": httpx.Response(200, json=page(1)),
                "https://b.test/": httpx.Response(200, json=page(2)),
            }
        )
    )
    r = client.post(
        "/fetch",
        json={"urls": ["https://a.test/", "https://b.test/"]},
        headers={"Authorization": "Bearer caller"},
    )
    assert r.status_code == 200
    assert r.json() == [doc("https://a.test/", 1), doc("https://b.test/", 2)]
    assert route.call_count == 2
    for call in route.calls:
        assert call.request.headers["authorization"] == "Bearer secret-key"
        assert call.request.headers["content-type"] == "application/json"
        assert set(json.loads(call.request.content)) == {"url"}


@respx.mock
def test_source_is_input_url_byte_for_byte(client):
    urls = [
        "https://Example.COM:443/a/../b?q=a%2fb&x=%C3%A9#frag",
        "https://例え.jp/パス?q=é",
        "http://e.test/path with space/",
        "https://e.test",
        "https://e.test/?b=2&a=1",
    ]
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=page(1)))
    r = client.post("/fetch", json={"urls": urls})
    assert r.status_code == 200
    assert [d["metadata"]["source"] for d in r.json()] == urls
    assert sorted(json.loads(c.request.content)["url"] for c in route.calls) == sorted(urls)


@respx.mock
def test_empty_and_missing_fields(client):
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://a.test/": httpx.Response(200, json={"title": "T", "content": ""}),
                "https://b.test/": httpx.Response(200, json={"content": "C"}),
                "https://c.test/": httpx.Response(200, json={"title": None, "content": "C"}),
                "https://d.test/": httpx.Response(200, json={"title": 5, "content": "C", "extra": 1}),
            }
        )
    )
    urls = ["https://a.test/", "https://b.test/", "https://c.test/", "https://d.test/"]
    r = client.post("/fetch", json={"urls": urls})
    assert r.json() == [
        {"page_content": "", "metadata": {"source": urls[0], "title": "T"}},
        {"page_content": "C", "metadata": {"source": urls[1], "title": ""}},
        {"page_content": "C", "metadata": {"source": urls[2], "title": ""}},
        {"page_content": "C", "metadata": {"source": urls[3], "title": ""}},
    ]


@respx.mock
def test_duplicate_urls_fetched_each(client):
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=page(1)))
    r = client.post("/fetch", json={"urls": ["https://a.test/", "https://a.test/"]})
    assert r.json() == [doc("https://a.test/", 1), doc("https://a.test/", 1)]
    assert route.call_count == 2


@respx.mock
def test_empty_list(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_URL")
    route = respx.post(UPSTREAM)
    r = client.post("/fetch", json={"urls": []})
    assert r.status_code == 200 and r.json() == []
    assert not route.called


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"",
        b"null",
        b'"https://a.test/"',
        b'["https://a.test/"]',
        b"{}",
        b'{"urls": null}',
        b'{"urls": "https://a.test/"}',
        b'{"urls": {"0": "https://a.test/"}}',
        b'{"urls": ["https://a.test/", 1]}',
        b'{"urls": [null]}',
        b'{"urls": [["https://a.test/"]]}',
        b'{"url": "https://a.test/"}',
    ],
)
@respx.mock
def test_bad_inbound_body(client, raw):
    route = respx.post(UPSTREAM)
    r = client.post("/fetch", content=raw, headers={"Content-Type": "application/json"})
    assert_error(r, 400)
    assert not route.called


def test_missing_upstream_url(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_URL")
    r = client.post("/fetch", json={"urls": ["https://a.test/"]})
    assert_error(r, 502)


@pytest.mark.parametrize("base", ["https://upstream.test/v1/", "https://upstream.test/v1//"])
@respx.mock
def test_base_url_trailing_slash(client, monkeypatch, base):
    monkeypatch.setenv("DEEPSEEK_API_URL", base)
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=page(1)))
    r = client.post("/fetch", json={"urls": ["https://a.test/"]})
    assert r.json() == [doc("https://a.test/", 1)]
    assert route.called


@respx.mock
def test_partial_failure_omitted(client, caplog):
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://ok1.test/": httpx.Response(200, json=page(1)),
                "https://404.test/": httpx.Response(404, json={"error": {"message": "couldn't fetch"}}),
                "https://timeout.test/": httpx.ReadTimeout("timed out"),
                "https://ok2.test/": httpx.Response(200, json=page(2)),
                "https://conn.test/": httpx.ConnectError("refused"),
                "https://bad.test/": httpx.Response(200, json=["not", "an", "object"]),
                "https://ok3.test/": httpx.Response(200, json=page(3)),
            }
        )
    )
    urls = [
        "https://ok1.test/",
        "https://404.test/",
        "https://timeout.test/",
        "https://ok2.test/",
        "https://conn.test/",
        "https://bad.test/",
        "https://ok3.test/",
    ]
    r = client.post("/fetch", json={"urls": urls})
    assert r.status_code == 200
    assert r.json() == [doc("https://ok1.test/", 1), doc("https://ok2.test/", 2), doc("https://ok3.test/", 3)]
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 4
    for url, marker in [
        ("https://404.test/", "HTTP 404"),
        ("https://timeout.test/", "ReadTimeout"),
        ("https://conn.test/", "ConnectError"),
        ("https://bad.test/", "invalid upstream response"),
    ]:
        assert any(url in w and marker in w for w in warnings), (url, warnings)


@pytest.mark.parametrize(
    "payload",
    [None, [], "str", 1, {}, {"title": "T"}, {"content": None}, {"content": 1}, {"content": ["a"]}],
)
@respx.mock
def test_bad_2xx_body_is_a_failure(client, payload):
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://bad.test/": httpx.Response(200, json=payload),
                "https://ok.test/": httpx.Response(200, json=page(1)),
            }
        )
    )
    r = client.post("/fetch", json={"urls": ["https://bad.test/", "https://ok.test/"]})
    assert r.json() == [doc("https://ok.test/", 1)]
    r = client.post("/fetch", json={"urls": ["https://bad.test/"]})
    assert_error(r, 502)


@respx.mock
def test_invalid_json_2xx_all_failed(client):
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, text="not json"))
    r = client.post("/fetch", json={"urls": ["https://a.test/"]})
    assert_error(r, 502)


@respx.mock
def test_all_failed_first_error_passed_through(client):
    content = b'{"error":{"message":"Failed to fetch URL","type":"invalid_request_error","param":null,"code":null}}'
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://slow-fail.test/": httpx.Response(
                    404, content=content, headers={"Content-Type": "application/json", "X-Other": "x"}
                ),
                "https://second.test/": httpx.Response(500, text="boom"),
                "https://third.test/": httpx.ReadTimeout("timed out"),
            }
        )
    )
    r = client.post("/fetch", json={"urls": ["https://slow-fail.test/", "https://second.test/", "https://third.test/"]})
    assert r.status_code == 404
    assert r.content == content
    assert r.headers["content-type"] == "application/json"
    assert "x-other" not in r.headers


@pytest.mark.parametrize(
    ("first", "status"),
    [
        (httpx.ReadTimeout("timed out"), 504),
        (httpx.ConnectTimeout("timed out"), 504),
        (httpx.ConnectError("refused"), 502),
        (RuntimeError("kaboom"), 502),
        (httpx.Response(200, json={"title": "no content"}), 502),
    ],
)
@respx.mock
def test_all_failed_first_error_mapping(client, first, status):
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {"https://first.test/": first, "https://second.test/": httpx.Response(429, text="slow down")}
        )
    )
    r = client.post("/fetch", json={"urls": ["https://first.test/", "https://second.test/"]})
    assert_error(r, status)


@respx.mock
def test_key_absent_from_logs_and_error_bodies(client, caplog):
    respx.post(UPSTREAM).mock(
        side_effect=by_url(
            {
                "https://a.test/": httpx.ConnectError("failed with Bearer secret-key"),
                "https://b.test/": httpx.Response(401, json={"error": {"message": "bad key secret-key"}}),
                "https://c.test/secret-key": httpx.Response(200, text="oops secret-key"),
            }
        )
    )
    r = client.post("/fetch", json={"urls": ["https://a.test/", "https://b.test/", "https://c.test/secret-key"]})
    assert_error(r, 502)
    assert len([rec for rec in caplog.records if rec.levelname == "WARNING"]) == 3
    assert "secret-key" not in caplog.text


@respx.mock
def test_non_ascii_api_key(client, monkeypatch, caplog):
    key = "clé-secrète-ü"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    route = respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=page(1)))
    r = client.post("/fetch", json={"urls": ["https://a.test/", "https://b.test/"]})
    assert_error(r, 502)
    assert not route.called
    assert "UnicodeEncodeError" in caplog.text
    for text in (r.text, caplog.text):
        assert key not in text
        assert "é" not in text and "\\xe9" not in text


def test_unescape_markdown():
    assert unescape_markdown(r"\# Open WebUI is self\-hosted\!") == "# Open WebUI is self-hosted!"
    assert unescape_markdown("\\\\") == "\\"
    assert unescape_markdown("\\\\\\#") == "\\#"
    assert unescape_markdown(r"C:\path\to\file") == r"C:\path\to\file"
    punctuation = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    assert unescape_markdown("".join("\\" + c for c in punctuation)) == punctuation
    assert unescape_markdown("\\a\\1\\ \\\n\\é\\") == "\\a\\1\\ \\\n\\é\\"


ESCAPED = "\\# Title\n\nself\\-hosted \\[link\\]\\(x\\) a\\\\b"
UNESCAPED = "# Title\n\nself-hosted [link](x) a\\b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, UNESCAPED),
        ("true", UNESCAPED),
        ("1", UNESCAPED),
        ("yes", UNESCAPED),
        ("on", UNESCAPED),
        ("", UNESCAPED),
        ("false", ESCAPED),
        ("False", ESCAPED),
        (" OFF ", ESCAPED),
        ("0", ESCAPED),
        ("no", ESCAPED),
    ],
)
@respx.mock
def test_unescape_env(client, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("FETCH_UNESCAPE_MARKDOWN", value)
    respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json={"title": "a\\-b", "content": ESCAPED}))
    r = client.post("/fetch", json={"urls": ["https://a\\-b.test/"]})
    assert r.json() == [{"page_content": expected, "metadata": {"source": "https://a\\-b.test/", "title": "a\\-b"}}]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 30.0), ("12.5", 12.5), ("0", 30.0), ("-5", 30.0), ("nan", 30.0), ("inf", 30.0), ("abc", 30.0)],
)
def test_fetch_timeout_env(monkeypatch, value, expected):
    monkeypatch.setenv("SEARCH_TIMEOUT", "7")
    if value is not None:
        monkeypatch.setenv("FETCH_TIMEOUT", value)
    assert timeout("FETCH_TIMEOUT") == expected


@pytest.fixture
def fake_deepseek(monkeypatch):
    """A local threaded fetch API: answers each URL after `delays[url]` seconds and records concurrency."""
    state = {"delays": {}, "in_flight": 0, "max_in_flight": 0, "paths": set(), "auth": set()}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            url = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["url"]
            with lock:
                state["paths"].add(self.path)
                state["auth"].add(self.headers.get("Authorization"))
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            time.sleep(state["delays"].get(url, 0.05))
            with lock:
                state["in_flight"] -= 1
            payload = json.dumps({"title": url, "content": f"content of {url}"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DEEPSEEK_API_URL", f"http://127.0.0.1:{server.server_port}/v1/")
    yield state
    server.shutdown()
    server.server_close()


def test_order_preserved_and_concurrency_capped(client, fake_deepseek):
    urls = [f"https://e.test/{i}" for i in range(20)]
    # Earlier URLs answer later, so completion order is roughly the reverse of input order.
    fake_deepseek["delays"] = {url: 0.02 * (len(urls) - i) for i, url in enumerate(urls)}
    r = client.post("/fetch", json={"urls": urls}, headers={"Authorization": "Bearer caller"})
    assert r.status_code == 200
    assert r.json() == [
        {"page_content": f"content of {url}", "metadata": {"source": url, "title": url}} for url in urls
    ]
    assert fake_deepseek["max_in_flight"] == 5
    assert fake_deepseek["paths"] == {"/v1/tools/fetch"}
    assert fake_deepseek["auth"] == {"Bearer secret-key"}


def test_fetch_timeout_applies(client, monkeypatch, fake_deepseek):
    monkeypatch.setenv("FETCH_TIMEOUT", "0.2")
    fake_deepseek["delays"] = {"https://slow.test/": 1.0, "https://fast.test/": 0.0}
    r = client.post("/fetch", json={"urls": ["https://slow.test/", "https://fast.test/"]})
    assert r.json() == [
        {"page_content": "content of https://fast.test/", "metadata": {"source": "https://fast.test/", "title": "https://fast.test/"}}
    ]
    r = client.post("/fetch", json={"urls": ["https://slow.test/"]})
    assert_error(r, 504)
