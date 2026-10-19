"""Shared plumbing for calls to the DeepSeek tools API: config, auth, key scrubbing and error mapping."""

import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("search_transformer")

DEFAULT_TIMEOUT = 30.0

T = TypeVar("T")


def timeout(name: str) -> float:
    """Seconds from env var `name`; missing, unparsable, non-finite or non-positive values give the default."""
    try:
        value = float(os.getenv(name, DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT
    return value


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str

    def url(self, tool: str) -> str:
        return f"{self.base_url}/tools/{tool}"


def config() -> Config | None:
    """Upstream base URL and API key from the environment, or None when the base URL is unset."""
    base_url = os.getenv("DEEPSEEK_API_URL", "").rstrip("/")
    if not base_url:
        logger.warning("DEEPSEEK_API_URL is not set")
        return None
    return Config(base_url=base_url, api_key=os.getenv("DEEPSEEK_API_KEY", "").strip())


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def scrub(text: str, secret: str) -> str:
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


def describe(exc: BaseException, secret: str) -> str:
    """Exception type and message, safe to log: never includes the API key."""
    if isinstance(exc, (UnicodeError, httpx.LocalProtocolError)):
        # These messages quote the offending header value, i.e. the key.
        return type(exc).__name__
    return f"{type(exc).__name__}: {scrub(str(exc), secret)}"


def passthrough(resp: httpx.Response) -> Response:
    """The upstream response as is: same status, body bytes and Content-Type only."""
    out = Response(content=resp.content, status_code=resp.status_code)
    # Raw bytes: a decoded value may not survive re-encoding (e.g. non-latin-1 parameters).
    content_type = next((v for k, v in resp.headers.raw if k.lower() == b"content-type"), None)
    if content_type is not None:
        out.raw_headers.append((b"content-type", content_type))
    return out


class UpstreamError(Exception):
    """A failed upstream call, carrying the response to send back to Open WebUI."""

    def __init__(self, response: Response):
        super().__init__(response.status_code)
        self.response = response


async def call(
    client: httpx.AsyncClient,
    cfg: Config,
    tool: str,
    body: Any,
    parse: Callable[[Any], T],
    context: str = "",
) -> T:
    """POST `body` to the `tool` endpoint and return `parse(json)`.

    Every failure logs one warning (prefixed with `context`) and raises UpstreamError:
    non-2xx is passed through, a timeout gives 504, any other request failure or an
    unparsable 2xx body gives 502.
    """
    key = cfg.api_key
    prefix = scrub(context, key)
    # Never log the request headers or raw exception messages: they can carry the API key.
    try:
        resp = await client.post(
            cfg.url(tool),
            json=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    except httpx.TimeoutException as exc:
        logger.warning("%supstream request timed out: %s", prefix, describe(exc, key))
        raise UpstreamError(error(504, f"upstream {tool} API timed out")) from None
    except Exception as exc:
        logger.warning("%supstream request failed: %s", prefix, describe(exc, key))
        raise UpstreamError(
            error(502, f"upstream {tool} API request failed ({type(exc).__name__})")
        ) from None

    if not resp.is_success:
        logger.warning("%supstream returned HTTP %s: %.500s", prefix, resp.status_code, scrub(resp.text, key))
        raise UpstreamError(passthrough(resp))

    try:
        return parse(resp.json())
    except Exception as exc:
        logger.warning(
            "%sinvalid upstream response: %s; body: %.500s", prefix, describe(exc, key), scrub(resp.text, key)
        )
        raise UpstreamError(error(502, f"upstream {tool} API returned an invalid response")) from None
