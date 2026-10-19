# search-transformer

A small HTTP proxy that lets Open WebUI's **External** web search engine use a DeepSeek search API.
It forwards `POST /search` bodies unchanged to `SEARCH_API_URL` (with `Authorization: Bearer $API_KEY`)
and reshapes the `{"results": [{title, url, snippet}]}` response into Open WebUI's
`[{link, title, snippet}]` format. Any failure returns `[]`.

## Run

```sh
cp .env.example .env   # fill in SEARCH_API_URL and API_KEY
docker compose up -d --build
curl -s localhost:8000/health
```

Set `PORT=<port>` to publish on a different host port. `SEARCH_TIMEOUT` (seconds, default 30) sets the upstream timeout.

For local development: `uv sync`, `uv run pytest`, `uv run uvicorn app.main:app --reload`.

## Configure Open WebUI

Admin Settings → Web Search:

- Web Search Engine: `external`
- External Web Search URL: `http://<host>:8000/search`
  (or `http://search-transformer:8000/search` if Open WebUI shares a docker network with this service)
- External Web Search API Key: any value (it is ignored)
