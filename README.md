# search-transformer

A small HTTP proxy that lets Open WebUI's **External** web search engine use a DeepSeek search API.
It forwards `POST /search` bodies unchanged to `SEARCH_API_URL` (with `Authorization: Bearer $API_KEY`)
and reshapes the `{"results": [{title, url, snippet}]}` response into Open WebUI's
`[{link, title, snippet}]` format.

## Errors

| Situation | Response |
| --- | --- |
| Inbound body is not valid JSON or not a JSON object | `400` `{"error": "..."}`; upstream is not called |
| Upstream returns non-2xx | Passed through as is: same status code, body bytes and `Content-Type` (no other upstream headers) |
| Upstream times out | `504` `{"error": "..."}` |
| No upstream response (network error, malformed or unset `SEARCH_API_URL`, non-ASCII `API_KEY`, any other error) | `502` `{"error": "..."}` |
| Upstream 2xx body is not valid JSON, not an object, or has no `results` list | `502` `{"error": "..."}` |

Error messages and logs never contain the API key or request headers. Each failure logs one warning.

## Run

```sh
cp .env.example .env   # fill in SEARCH_API_URL and API_KEY
docker compose up -d --build
curl -s localhost:8000/health   # only after exposing a port, see below
```

By default no host port is published: the service is reachable only on its Docker network, at `http://search-transformer:8000/search`.

### Local overrides

Create a `docker-compose.override.yml` (picked up automatically by compose, gitignored) when you need more.

Expose on the host (Open WebUI then uses `http://<host>:8000/search`):

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

`SEARCH_TIMEOUT` (seconds, default 30) sets the upstream timeout.

For local development: `uv sync`, `uv run pytest`, `uv run uvicorn app.main:app --reload`.

## Configure Open WebUI

Admin Settings → Web Search:

- Web Search Engine: `external`
- External Web Search URL: `http://search-transformer:8000/search` (same Docker network), or `http://<host>:8000/search` with the port exposed
- External Web Search API Key: any value (it is ignored)
