# search-transformer

A small HTTP proxy that lets Open WebUI use the DeepSeek tools API for web search and page loading:

- `POST /search` for the **External** web search engine. It forwards the body unchanged to
  `$DEEPSEEK_API_URL/tools/search` (with `Authorization: Bearer $DEEPSEEK_API_KEY`) and reshapes the
  `{"results": [{title, url, snippet}]}` response into Open WebUI's `[{link, title, snippet}]` format.
- `POST /fetch` for the **External** web loader. It takes `{"urls": [...]}`, calls
  `$DEEPSEEK_API_URL/tools/fetch` with `{"url": ...}` once per URL (at most 5 at a time), and returns
  `[{"page_content": <markdown>, "metadata": {"source": <input url>, "title": <title>}}]` in input order.
  `metadata.source` is always the input URL string, unchanged.

The inbound `Authorization` header is ignored on both endpoints.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEEPSEEK_API_URL` | (required) | Base URL without the tool suffix, e.g. `https://api.deepseek.com/v1`; a trailing `/` is ignored |
| `DEEPSEEK_API_KEY` | (required) | Sent upstream as `Authorization: Bearer ...`; surrounding whitespace is stripped |
| `SEARCH_TIMEOUT` | `30` | Upstream timeout for `/search`, in seconds |
| `FETCH_TIMEOUT` | `30` | Upstream timeout for each `/fetch` URL, in seconds |
| `FETCH_UNESCAPE_MARKDOWN` | `true` | Turn `\#`, `\-`, `\\` etc. back into `#`, `-`, `\` in fetched `page_content` (DeepSeek escapes all ASCII punctuation); `false`, `0`, `no` or `off` disables it |
| `LOG_LEVEL` | `INFO` | Python log level |

A timeout that is not a positive finite number falls back to 30.

## Errors

| Situation | Response |
| --- | --- |
| Inbound body is not valid JSON or not a JSON object (for `/fetch`: or `urls` is not a list of strings) | `400` `{"error": "..."}`; upstream is not called |
| Upstream returns non-2xx | Passed through as is: same status code, body bytes and `Content-Type` (no other upstream headers) |
| Upstream times out | `504` `{"error": "..."}` |
| No upstream response (network error, malformed or unset `DEEPSEEK_API_URL`, non-ASCII `DEEPSEEK_API_KEY`, any other error) | `502` `{"error": "..."}` |
| Upstream 2xx body is not valid JSON or not an object, or has no `results` list (`/search`) or no `content` string (`/fetch`) | `502` `{"error": "..."}` |

`/fetch` with an empty `urls` list returns `200 []`. When only some URLs fail, `/fetch` leaves them out and
returns `200` with the rest; when every URL fails, it returns the first URL's error (in input order) as in the
table above, e.g. DeepSeek's `404` for a page it could not fetch.

Error messages and logs never contain the API key or request headers. Each failure logs one warning.

## Run

```sh
cp .env.example .env   # fill in DEEPSEEK_API_KEY
docker compose up -d --build
curl -s localhost:8000/health   # only after exposing a port, see below
```

By default no host port is published: the service is reachable only on its Docker network, at
`http://search-transformer:8000`.

### Local overrides

Create a `docker-compose.override.yml` (picked up automatically by compose, gitignored) when you need more.

Expose on the host (Open WebUI then uses `http://<host>:8000/search` and `http://<host>:8000/fetch`):

```yaml
services:
  search-transformer:
    ports: ["8000:8000"]
```

Join Open WebUI's existing Docker network (check the name with `docker network ls`):

```yaml
services:
  search-transformer:
    networks: [default, openwebui]
networks:
  openwebui:
    external: true
    name: open-webui_default
```

For local development: `uv sync`, `uv run pytest`, `uv run uvicorn app.main:app --reload`.

## Configure Open WebUI

Admin Settings → Web Search:

- Web Search Engine: `external`
- External Web Search URL: `http://search-transformer:8000/search` (same Docker network), or `http://<host>:8000/search` with the port exposed
- External Web Search API Key: any value (it is ignored)
- Web Loader Engine: `external` (Open WebUI v0.6.10 or later)
- External Web Loader URL: `http://search-transformer:8000/fetch`, or `http://<host>:8000/fetch`
- External Web Loader API Key: any value (it is ignored)

Or set them through Open WebUI's environment (these only seed the settings: once they are saved in the Admin UI,
the stored values win unless `ENABLE_PERSISTENT_CONFIG=false`):

```sh
ENABLE_WEB_SEARCH=true
WEB_SEARCH_ENGINE=external
EXTERNAL_WEB_SEARCH_URL=http://search-transformer:8000/search
EXTERNAL_WEB_SEARCH_API_KEY=unused
WEB_LOADER_ENGINE=external
EXTERNAL_WEB_LOADER_URL=http://search-transformer:8000/fetch
EXTERNAL_WEB_LOADER_API_KEY=unused
```
