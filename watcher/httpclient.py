"""Shared HTTP client: browser-ish headers, per-host throttling, retries."""
from __future__ import annotations

import asyncio
import random
import time

import httpx

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="139", "Not(A:Brand";v="24", "Google Chrome";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

BLOCK_MARKERS = (
    "access denied", "captcha", "are you a human", "unusual traffic",
    "px-captcha", "/_Incapsula_", "request unsuccessful", "bot detection",
    "verify you are a human", "cf-browser-verification",
)


class Fetcher:
    """One httpx client + a token bucket per hostname so we stay polite."""

    def __init__(self, timeout: float = 20.0, min_gap: float = 1.5, proxy: str | None = None):
        self.min_gap = min_gap
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            http2=False,
            proxy=proxy,
            headers=BASE_HEADERS,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def _throttle(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = time.monotonic() - self._last.get(host, 0.0)
            wait = self.min_gap + random.uniform(0, 0.8) - gap
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def get(self, url: str, *, headers: dict | None = None, retries: int = 2,
                  referer: str | None = None) -> httpx.Response:
        host = httpx.URL(url).host or url
        h = {"User-Agent": random.choice(UAS)}
        if referer:
            h["Referer"] = referer
        if headers:
            h.update(headers)
        last_exc = None
        for attempt in range(retries + 1):
            await self._throttle(host)
            try:
                r = await self.client.get(url, headers=h)
                if r.status_code in (429, 503) and attempt < retries:
                    await asyncio.sleep(2 ** attempt + random.random() * 2)
                    continue
                return r
            except (httpx.TransportError, httpx.HTTPError) as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # pragma: no cover


def looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403, 429):
        return True
    if resp.status_code >= 500:
        return False
    body = resp.text[:6000].lower()
    return any(m in body for m in BLOCK_MARKERS)
