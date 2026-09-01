"""Stealth browser tier (patchright).

Vanilla Playwright leaks the CDP `Runtime.enable` call and a set of patched
JS properties that Cloudflare/Akamai fingerprint directly. Patchright is a
drop-in Playwright fork with those leaks removed. It must run with a
*persistent* context and no custom UA/viewport overrides -- setting those
re-introduces the very fingerprints it strips.

Measured: gets through Walmart CA, Walmart US, GameStop US and Adorama, which
both httpx and vanilla Playwright fail.
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

_CTX = None
_LOCK = asyncio.Lock()
_SEM: asyncio.Semaphore | None = None
_PW = None

PROFILE_DIR = Path.home() / ".cache/ps5watch/chrome-profile"


# Off-screen window placement.
#
# Headless is itself a detection signal -- measured on this target list,
# headless patchright fails every hard site (Walmart CA 456, Walmart US wall,
# GameStop 403, eBay 403) while the same code headed passes all four.
# macOS does not clamp negative window coordinates, so parking the window far
# off-screen keeps full headed stealth with zero visual disruption.
OFFSCREEN_ARGS = [
    "--window-position=-3000,-3000",
    "--window-size=1512,900",
    # An off-screen window counts as "occluded", and Chrome throttles timers in
    # occluded windows -- which would stall the very JS challenges we're here to
    # let run. These keep it executing at full speed while invisible.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]


async def _ensure(headless: bool = False, concurrency: int = 2,
                  offscreen: bool = True):
    global _CTX, _PW, _SEM
    async with _LOCK:
        if _CTX is not None:
            return _CTX
        from patchright.async_api import async_playwright
        _SEM = asyncio.Semaphore(concurrency)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _PW = await async_playwright().start()
        args = list(OFFSCREEN_ARGS) if (offscreen and not headless) else []
        # NOTE: no user_agent / viewport / init-script overrides on purpose.
        _CTX = await _PW.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chromium",
            headless=headless,
            no_viewport=True,
            locale="en-CA",
            timezone_id="America/Toronto",
            args=args,
        )
        return _CTX


async def fetch(url: str, *, wait_ms: int = 6000, headless: bool = False,
                concurrency: int = 2, scroll: bool = True,
                offscreen: bool = True) -> tuple[int, str]:
    ctx = await _ensure(headless, concurrency, offscreen)
    async with _SEM:
        page = await ctx.new_page()
        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # page.bring_to_front() is deliberately never called: it would yank
            # the off-screen window onto the active desktop and steal focus.
            await page.wait_for_timeout(wait_ms + random.randint(0, 1200))
            if scroll:
                try:
                    await page.evaluate("window.scrollTo(0,1200)")
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
            return (r.status if r else 0), await page.content()
        finally:
            await page.close()


async def shutdown():
    global _CTX, _PW
    for obj, meth in ((_CTX, "close"), (_PW, "stop")):
        if obj is not None:
            try:
                await getattr(obj, meth)()
            except Exception:
                pass
    _CTX = _PW = None
