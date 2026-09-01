"""Playwright tier: for retailers behind Cloudflare / Akamai / PerimeterX."""
from __future__ import annotations

import asyncio
import random

_PW = None
_BROWSER = None
_CTX = None
_LOCK = asyncio.Lock()
_SEM: asyncio.Semaphore | None = None

STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['en-CA','en','fr-CA']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
window.chrome={runtime:{},app:{isInstalled:false}};
const q=window.navigator.permissions.query;
window.navigator.permissions.query=(p)=>p.name==='notifications'
  ? Promise.resolve({state:Notification.permission}) : q(p);
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
"""

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


async def _ensure(headless: bool = True, concurrency: int = 3):
    """Start one browser + one shared context (cookies persist across targets)."""
    global _PW, _BROWSER, _CTX, _SEM
    async with _LOCK:
        if _CTX is not None:
            return _CTX
        from playwright.async_api import async_playwright
        _SEM = asyncio.Semaphore(concurrency)
        _PW = await async_playwright().start()
        _BROWSER = await _PW.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        _CTX = await _BROWSER.new_context(
            user_agent=UA,
            viewport={"width": 1512, "height": 900},
            locale="en-CA",
            timezone_id="America/Toronto",
            device_scale_factor=2,
            extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
        )
        await _CTX.add_init_script(STEALTH)
        return _CTX


async def fetch(url: str, *, wait_selector: str | None = None,
                wait_ms: int = 2500, headless: bool = True,
                concurrency: int = 3) -> tuple[int, str]:
    """Return (status, html) after JS has run."""
    ctx = await _ensure(headless, concurrency)
    async with _SEM:
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else 0
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=12000)
                except Exception:
                    pass
            # let Cloudflare interstitials resolve + lazy content paint
            await page.wait_for_timeout(wait_ms + random.randint(0, 900))
            try:
                await page.mouse.move(random.randint(200, 900), random.randint(200, 600))
                await page.evaluate("window.scrollTo(0, 700)")
                await page.wait_for_timeout(600)
            except Exception:
                pass
            html = await page.content()
            return status, html
        finally:
            await page.close()


async def shutdown():
    global _PW, _BROWSER, _CTX
    for obj, meth in ((_CTX, "close"), (_BROWSER, "close"), (_PW, "stop")):
        if obj is not None:
            try:
                await getattr(obj, meth)()
            except Exception:
                pass
    _PW = _BROWSER = _CTX = None
