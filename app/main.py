"""Proxy between Open WebUI's "External" web search / web loader engines and the DeepSeek tools API."""

import asyncio
import logging
import os
import re
import weakref
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.upstream import UpstreamError, call, config, error, logger, timeout

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="search-transformer")

FETCH_CONCURRENCY = 5
MAX_FETCH_URLS = 100

# A backslash before any ASCII punctuation character (CommonMark's escapable set).
_MARKDOWN_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

# DeepSeek rate-limits per account, so the cap is shared by all requests (one semaphore per event loop,
# as asyncio primitives can't cross loops; uvicorn runs a single loop).
_fetch_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _fetch_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _fetch_semaphores.get(loop)
    if semaphore is None:
        semaphore = _fetch_semaphores[loop] = asyncio.Semaphore(FETCH_CONCURRENCY)
    return semaphore


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


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


def parse_page(payload: Any) -> tuple[str, str]:
    """Upstream `{"title", "content"}` as `(title, content)`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError("upstream response has no 'content' string")
    return _str_or_empty(payload.get("title")), payload["content"]


def unescape_markdown(text: str) -> str:
    return _MARKDOWN_ESCAPE.sub(r"\1", text)


def _log_url(url: str) -> str:
    """The URL for logs: without query string or fragment (they can carry tokens), at most 200 chars."""
    return url.split("?", 1)[0].split("#", 1)[0][:200]


def _unescape_enabled() -> bool:
    return os.getenv("FETCH_UNESCAPE_MARKDOWN", "true").strip().lower() not in {"false", "0", "no", "off"}


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
        return error(400, "request body must be a JSON object")

    cfg = config()
    if cfg is None:
        return error(502, "upstream search API is not configured")

    async with httpx.AsyncClient(timeout=timeout("SEARCH_TIMEOUT")) as client:
        try:
            return await call(client, cfg, "search", body, lambda payload: transform(payload, body.get("count")))
        except UpstreamError as exc:
            return exc.response


@app.post("/fetch", response_model=None)
async def fetch(request: Request) -> list[dict[str, Any]] | Response:
    # Inbound Authorization header is intentionally ignored.
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
        urls = body.get("urls")
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("'urls' is not a list of strings")
    except Exception as exc:
        logger.warning("bad inbound body: %s: %s", type(exc).__name__, exc)
        return error(400, "request body must be a JSON object with a 'urls' list of strings")
    if len(urls) > MAX_FETCH_URLS:
        logger.warning("bad inbound body: %d urls, more than %d", len(urls), MAX_FETCH_URLS)
        return error(400, f"at most {MAX_FETCH_URLS} urls per request")

    if not urls:
        return []
    cfg = config()
    if cfg is None:
        return error(502, "upstream fetch API is not configured")

    semaphore = _fetch_semaphore()

    async def fetch_one(url: str) -> tuple[str, str] | UpstreamError:
        async with semaphore:
            try:
                return await call(
                    client, cfg, "fetch", {"url": url}, parse_page, context=f"fetch {_log_url(url)!r}: "
                )
            except UpstreamError as exc:
                return exc

    # One upstream call per URL: the fetch API takes a single URL.
    async with httpx.AsyncClient(timeout=timeout("FETCH_TIMEOUT")) as client:
        outcomes = await asyncio.gather(*(fetch_one(url) for url in urls))

    unescape = _unescape_enabled()
    docs = [
        {
            "page_content": unescape_markdown(outcome[1]) if unescape else outcome[1],
            "metadata": {"source": url, "title": outcome[0]},
        }
        for url, outcome in zip(urls, outcomes)
        if not isinstance(outcome, UpstreamError)
    ]
    if not docs:
        # Every URL failed: report the first failure, as /search would.
        return outcomes[0].response
    return docs
