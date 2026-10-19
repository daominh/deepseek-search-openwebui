"""Proxy between Open WebUI's "External" web search engine and a DeepSeek search API."""

import logging
import math
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("search_transformer")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="search-transformer")

DEFAULT_TIMEOUT = 30.0


def _timeout() -> float:
    try:
        value = float(os.getenv("SEARCH_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT
    return value


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _scrub(text: str, secret: str) -> str:
    """Remove the key from text, including its escaped forms (as in str or bytes reprs)."""
    if not secret:
        return text
    forms = {secret, repr(secret)[1:-1]}
    try:
        forms.add(repr(secret.encode())[2:-1])
    except UnicodeEncodeError:
        pass
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def _describe(exc: BaseException, secret: str) -> str:
    """Exception type and message, safe to log: never includes the API key."""
    if isinstance(exc, (UnicodeError, httpx.LocalProtocolError)):
        # These messages quote the offending header value, i.e. the key.
        return type(exc).__name__
    return f"{type(exc).__name__}: {_scrub(str(exc), secret)}"


def transform(payload: Any, count: Any) -> list[dict[str, str]]:
    """Reshape upstream `{"results": [...]}` into Open WebUI's `[{link, title, snippet}]`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("upstream response has no 'results' list")
    out: list[dict[str, str]] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        out.append(
            {
                "link": url,
                "title": _str_or_empty(item.get("title")),
                "snippet": _str_or_empty(item.get("snippet")),
            }
        )
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        out = out[:count]
    return out


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/search", response_model=None)
async def search(request: Request) -> list[dict[str, str]] | Response:
    # Inbound Authorization / X-OpenWebUI-* headers are intentionally ignored.
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
    except Exception as exc:
        logger.warning("bad inbound body: %s: %s", type(exc).__name__, exc)
        return _error(400, "request body must be a JSON object")

    url = os.getenv("SEARCH_API_URL", "")
    api_key = os.getenv("API_KEY", "").strip()
    if not url:
        logger.warning("SEARCH_API_URL is not set")
        return _error(502, "upstream search API is not configured")

    # Never log the request headers or raw exception messages: they can carry the API key.
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        logger.warning("upstream request timed out: %s", _describe(exc, api_key))
        return _error(504, "upstream search API timed out")
    except Exception as exc:
        logger.warning("upstream request failed: %s", _describe(exc, api_key))
        return _error(502, f"upstream search API request failed ({type(exc).__name__})")

    if not resp.is_success:
        # Pass the upstream error through: same status, body bytes and Content-Type only.
        logger.warning("upstream returned HTTP %s: %.500s", resp.status_code, _scrub(resp.text, api_key))
        out = Response(content=resp.content, status_code=resp.status_code)
        # Raw bytes: a decoded value may not survive re-encoding (e.g. non-latin-1 parameters).
        content_type = next((v for k, v in resp.headers.raw if k.lower() == b"content-type"), None)
        if content_type is not None:
            out.raw_headers.append((b"content-type", content_type))
        return out

    try:
        return transform(resp.json(), body.get("count"))
    except Exception as exc:
        logger.warning(
            "invalid upstream response: %s; body: %.500s", _describe(exc, api_key), _scrub(resp.text, api_key)
        )
        return _error(502, "upstream search API returned an invalid response")
