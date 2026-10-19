"""Proxy between Open WebUI's "External" web search engine and a DeepSeek search API."""

import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request

logger = logging.getLogger("search_transformer")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="search-transformer")


def _timeout() -> float:
    try:
        return float(os.getenv("SEARCH_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def transform(payload: Any, count: Any) -> list[dict[str, str]]:
    """Reshape upstream `{"results": [...]}` into Open WebUI's `[{link, title, snippet}]`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("upstream response has no 'results' list")
    out: list[dict[str, str]] = []
    for item in payload["results"]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        out.append(
            {
                "link": str(item["url"]),
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
        )
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        out = out[:count]
    return out


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/search")
async def search(request: Request) -> list[dict[str, str]]:
    # Inbound Authorization / X-OpenWebUI-* headers are intentionally ignored.
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
    except Exception as exc:
        logger.warning("bad inbound body: %s", exc)
        return []

    url = os.getenv("SEARCH_API_URL", "")
    api_key = os.getenv("API_KEY", "")
    if not url:
        logger.warning("SEARCH_API_URL is not set")
        return []

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("upstream request failed: %s: %s", type(exc).__name__, exc)
        return []

    if not resp.is_success:
        logger.warning("upstream returned HTTP %s: %.500s", resp.status_code, resp.text)
        return []

    try:
        return transform(resp.json(), body.get("count"))
    except ValueError as exc:  # includes JSONDecodeError
        logger.warning("invalid upstream response (%s): %.500s", exc, resp.text)
        return []
