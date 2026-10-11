"""Async Freshdesk REST v2 client: auth, retries, rate limiting and pagination.

Only GET requests are implemented. The connector is read-only by design, which also makes
every retry safe (GETs are idempotent).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx

from . import __version__
from .config import Settings
from .errors import (
    AuthError,
    ConnectorError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
)
from .rate_limit import RateLimitState, Sleep, TokenBucket

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class Page:
    items: list[dict[str, Any]]
    page: int
    next_page: int | None
    total: int | None = None


class FreshdeskClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = anyio.sleep,
    ):
        self.settings = settings
        self._sleep = sleep
        self.rate_state = RateLimitState()
        self._bucket = TokenBucket(settings.rate_limit_per_min, sleep=sleep)
        # Freshdesk API-key auth is HTTP Basic with the key as username and any password.
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            auth=httpx.BasicAuth(settings.api_key, "X"),
            headers={
                "Accept": "application/json",
                "User-Agent": f"freshdesk-connector/{__version__}",
            },
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core request

    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            await self._bucket.acquire(self.rate_state)
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt < self.settings.max_retries:
                    await self._backoff(attempt, reason=f"network error: {exc!r}")
                    attempt += 1
                    continue
                raise UpstreamError(f"Could not reach Freshdesk: {exc}") from exc

            self.rate_state.update_from_headers(resp.headers)

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                self.rate_state.last_retry_after = retry_after
                self.rate_state.throttled_requests += 1
                wait = retry_after if retry_after is not None else 2 ** attempt
                if attempt < self.settings.max_retries and wait <= self.settings.max_retry_wait_seconds:
                    log.warning("Freshdesk rate limit hit; waiting %.1fs (attempt %d)", wait, attempt + 1)
                    await self._sleep(wait)
                    attempt += 1
                    continue
                raise RateLimitedError(
                    "Freshdesk's API rate limit for this account is exhausted. "
                    f"Try again in about {int(wait)} seconds.",
                    retry_after=wait,
                )

            if resp.status_code in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                await self._backoff(attempt, reason=f"HTTP {resp.status_code}")
                attempt += 1
                continue

            if resp.is_success:
                return resp
            raise _error_for(resp)

    async def _backoff(self, attempt: int, *, reason: str) -> None:
        # Exponential backoff with full jitter: 0..1s, 0..2s, 0..4s ...
        delay = random.uniform(0, 2 ** attempt)
        log.warning("Retrying Freshdesk request after %s in %.2fs", reason, delay)
        await self._sleep(delay)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.get(path, params)).json()

    async def get_page(self, path: str, params: dict[str, Any], page: int) -> Page:
        """List endpoints paginate with ?page=N and announce the next page via a Link header."""
        resp = await self.get(path, {**params, "page": page})
        return Page(items=resp.json(), page=page, next_page=_next_page_from_link(resp))

    async def search(self, entity: str, query: str, page: int) -> Page:
        """/search/<entity>?query="..." returns 30 results/page and at most 10 pages."""
        data = await self.get_json(f"/search/{entity}", {"query": f'"{query}"', "page": page})
        total = data.get("total", 0)
        has_more = page * 30 < total and page < 10
        return Page(items=data.get("results", []), page=page, next_page=page + 1 if has_more else None, total=total)


# ---------------------------------------------------------------------- helpers


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; Freshdesk sends seconds, so we don't parse dates


def _next_page_from_link(resp: httpx.Response) -> int | None:
    nxt = resp.links.get("next", {}).get("url")
    if not nxt:
        return None
    pages = parse_qs(urlparse(nxt).query).get("page")
    try:
        return int(pages[0]) if pages else None
    except ValueError:
        return None


def _error_for(resp: httpx.Response) -> ConnectorError:
    status = resp.status_code
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            if body.get("errors"):
                detail = "; ".join(
                    f"{e.get('field', '')}: {e.get('message', '')}".strip(": ") for e in body["errors"]
                )
            else:
                detail = body.get("message") or body.get("description") or ""
    except ValueError:
        pass

    if status == 401:
        return AuthError("Freshdesk rejected the API key. It may be wrong, revoked or rotated.", status=status)
    if status == 403:
        return PermissionDeniedError(
            "The Freshdesk agent that owns this API key does not have permission for this resource."
            + (f" ({detail})" if detail else ""),
            status=status,
        )
    if status == 404:
        return NotFoundError("Not found in Freshdesk." + (f" ({detail})" if detail else ""), status=status)
    if status == 400:
        return ValidationError(f"Freshdesk rejected the request: {detail or 'invalid parameters'}", status=status)
    if status >= 500:
        return UpstreamError(f"Freshdesk is having problems (HTTP {status}). Try again shortly.", status=status)
    return ConnectorError(f"Unexpected Freshdesk response HTTP {status}: {detail}", status=status)
