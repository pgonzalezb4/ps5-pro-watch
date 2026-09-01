"""TLS-impersonating fetcher (curl_cffi).

Many retailer 403s are *TLS/JA3 fingerprint* rejections, not JS challenges:
httpx/requests announce a cipher+extension order no real browser produces, so
the server rejects the connection before reading a single header. curl_cffi
binds curl-impersonate, replaying Chrome's exact TLS stack and HTTP/2 SETTINGS.

Measured on this project's target list: 7 retailers that returned 403 to httpx
return 200 here (London Drugs, Micro Center, Antonline, Target, Sam's Club,
Costco US, Best Buy US) at ~1/15th the cost of a browser page load.
"""
from __future__ import annotations

import asyncio
import random

from curl_cffi.requests import AsyncSession

# rotate across recent Chrome/Safari fingerprints
PROFILES = ["chrome131", "chrome124", "chrome123", "safari17_0"]

# Hosts with broken/incomplete cert chains that curl rejects but browsers accept.
_VERIFY: dict[str, bool] = {"www.vgp.ca": False, "www.thesource.ca": False}

_sess: AsyncSession | None = None
_lock = asyncio.Lock()
_last: dict[str, float] = {}


async def _session() -> AsyncSession:
    global _sess
    async with _lock:
        if _sess is None:
            _sess = AsyncSession(timeout=25)
        return _sess


async def get(url: str, *, referer: str | None = None, retries: int = 1,
              profile: str | None = None) -> tuple[int, str]:
    """Return (status, body). Raises only on total transport failure."""
    s = await _session()
    headers = {"Accept-Language": "en-CA,en;q=0.9",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Upgrade-Insecure-Requests": "1"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(retries + 1):
        try:
            r = await s.get(url, impersonate=profile or random.choice(PROFILES),
                            headers=headers, allow_redirects=True,
                            verify=_VERIFY.get(url.split("/")[2], True))
            if r.status_code in (429, 503) and i < retries:
                await asyncio.sleep(1.5 * (i + 1))
                continue
            return r.status_code, r.text
        except Exception as e:
            last = e
            if i < retries:
                await asyncio.sleep(1.0)
                continue
            raise last
    return 0, ""


async def warm(origin: str) -> None:
    """Hit the homepage first so we carry normal cookies into the search page."""
    try:
        await get(origin)
        await asyncio.sleep(random.uniform(0.6, 1.4))
    except Exception:
        pass


async def close():
    global _sess
    if _sess is not None:
        try:
            await _sess.close()
        except Exception:
            pass
        _sess = None
