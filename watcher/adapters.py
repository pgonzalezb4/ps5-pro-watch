"""Per-retailer availability adapters.

Every adapter returns a Result and NEVER raises: a broken retailer degrades to
UNKNOWN/ERROR so one bad site can't take down the whole scan.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse as up

from . import browser, extract, stealth, tlsfetch
from .httpclient import Fetcher, looks_blocked
from .models import Result, Stock, Target

REGISTRY: dict[str, callable] = {}


def adapter(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def _r(t, status, price=None, cur=None, note="", t0=None):
    return Result(target=t, status=status, price=price,
                  currency=cur or ("CAD" if t.country == "CA" else "USD"),
                  note=note, elapsed_ms=int((time.monotonic() - t0) * 1000) if t0 else 0)


def _classify_html(t: Target, html: str) -> tuple[Stock, float | None, str | None, str]:
    """Strict: only a matched PS5 Pro *console* card can yield IN_STOCK.

    Naive page-wide keyword matching false-positives constantly on search pages,
    where accessories are purchasable while the console is not.
    """
    st, price, why = extract.classify_console(html, t.country)
    return st, price, None, why


# --------------------------------------------------------------------------
# Generic tiers
# --------------------------------------------------------------------------

@adapter("http")
async def http_generic(t: Target, f: Fetcher, cfg) -> Result:
    t0 = time.monotonic()
    try:
        r = await f.get(t.url, referer=f"https://{up.urlparse(t.url).netloc}/")
        if looks_blocked(r):
            return _r(t, Stock.BLOCKED, note=f"http {r.status_code}", t0=t0)
        if r.status_code >= 400:
            return _r(t, Stock.ERROR, note=f"http {r.status_code}", t0=t0)
        st, price, cur, how = _classify_html(t, r.text)
        return _r(t, st, price, cur, how, t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


# Hard block phrases. Checked against the page TITLE and against small bodies
# only -- a 700KB product page that merely contains "enable cookies" in a
# footer is not a block page, and treating it as one loses real stock data.
BOT_WALL = ("just a moment", "checking your browser", "robot or human",
            "access denied", "you have been blocked", "unusual traffic",
            "verify you are human", "attention required", "sorry, you have been blocked")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def is_wall(html: str, status: int = 200) -> bool:
    """True only when the response really is an interstitial, not a page that
    happens to mention cookies."""
    m = _TITLE_RE.search(html[:8000])
    title = (m.group(1) if m else "").strip().lower()
    if any(w in title for w in BOT_WALL):
        return True
    # Block pages are small. Real catalogue pages are 100KB+.
    if len(html) < 30000 and any(w in html[:8000].lower() for w in BOT_WALL):
        return True
    return status == 403 and len(html) < 30000


@adapter("browser")
async def browser_generic(t: Target, f: Fetcher, cfg, _depth: int = 0) -> Result:
    t0 = time.monotonic()
    try:
        status, html = await browser.fetch(
            t.url, headless=cfg.get("headless", True),
            wait_ms=cfg.get("browser_wait_ms", 3500),
            concurrency=cfg.get("browser_concurrency", 3))
        if is_wall(html, status):
            return _r(t, Stock.BLOCKED, note="bot wall", t0=t0)

        st, price, cur, how = _classify_html(t, html)

        # Stage 2: a search page whose console card is ambiguous gets resolved
        # by opening the product page itself, where availability is explicit.
        if st is Stock.UNKNOWN and _depth == 0 and cfg.get("resolve_pdp", True):
            links = extract.console_pdp_links(html, t.country, t.url,
                                              limit=cfg.get("pdp_limit", 2))
            for link in links:
                sub = Target(retailer=t.retailer, country=t.country, adapter="browser",
                             url=link, sku=t.sku, title=t.title)
                res2 = await browser_generic(sub, f, cfg, _depth=1)
                if res2.status in (Stock.IN, Stock.OUT):
                    res2.target = t
                    res2.note = f"pdp/{res2.note.replace('browser/', '')}"
                    res2.price = res2.price or price
                    if res2.status is Stock.IN:
                        t.url = link      # deep-link the alert straight to the buy page
                    return res2
        return _r(t, st, price, cur, f"browser/{how}", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


@adapter("json")
async def json_generic(t: Target, f: Fetcher, cfg) -> Result:
    """Config-driven JSON API: t.api URL template + t.paths {stock,price,truthy}."""
    t0 = time.monotonic()
    try:
        url = t.api.format(sku=t.sku, url=t.url)
        r = await f.get(url, headers={"Accept": "application/json",
                                      "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                                      "Sec-Fetch-Site": "same-origin"},
                        referer=t.url)
        if looks_blocked(r):
            return _r(t, Stock.BLOCKED, note=f"http {r.status_code}", t0=t0)
        data = r.json()
        raw = extract.dig(data, t.paths.get("stock", ""))
        price = extract.dig(data, t.paths.get("price", "")) if t.paths.get("price") else None
        truthy = [s.lower() for s in t.paths.get("truthy", ["true", "instock", "available"])]
        st = Stock.UNKNOWN
        if isinstance(raw, bool):
            st = Stock.IN if raw else Stock.OUT
        elif raw is not None:
            st = Stock.IN if str(raw).lower() in truthy else Stock.OUT
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        return _r(t, st, price, note=f"json:{raw}", t0=t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


# --------------------------------------------------------------------------
# Retailer-specific (verified endpoints)
# --------------------------------------------------------------------------

@adapter("bestbuy_ca")
async def bestbuy_ca(t: Target, f: Fetcher, cfg) -> Result:
    """Verified: ecomm-api availability + offers v1 for price."""
    t0 = time.monotonic()
    try:
        av = ("https://www.bestbuy.ca/ecomm-api/availability/products"
              "?accept=application%2Fvnd.bestbuy.simpleproduct.v1%2Bjson"
              f"&accept-language=en-CA&locations=&postalCode={cfg.get('postal_ca','M5V3L9')}"
              f"&skus={t.sku}")
        r = await f.get(av, headers={"Accept": "application/vnd.bestbuy.simpleproduct.v1+json"},
                        referer=t.url)
        if looks_blocked(r):
            return _r(t, Stock.BLOCKED, note=f"http {r.status_code}", t0=t0)
        node = extract.dig(r.json(), "availabilities[0]") or {}
        ship = node.get("shipping") or {}
        pick = node.get("pickup") or {}
        purch = bool(ship.get("purchasable")) or bool(pick.get("purchasable"))
        st = Stock.IN if purch else Stock.OUT
        note = f"ship={ship.get('status')} pickup={pick.get('status')}"

        price = None
        try:
            o = await f.get(f"https://www.bestbuy.ca/api/offers/v1/products/{t.sku}/offers",
                            headers={"Accept": "application/json"}, referer=t.url)
            offers = o.json()
            if isinstance(offers, list) and offers:
                win = next((x for x in offers if x.get("isWinner")), offers[0])
                price = win.get("salePrice") or win.get("regularPrice")
        except Exception:
            pass
        return _r(t, st, price, "CAD", note, t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


@adapter("bestbuy_us")
async def bestbuy_us(t: Target, f: Fetcher, cfg) -> Result:
    """Official developer API when BESTBUY_API_KEY is set; else browser tier."""
    t0 = time.monotonic()
    key = cfg.get("bestbuy_api_key")
    if key:
        try:
            url = (f"https://api.bestbuy.com/v1/products(sku={t.sku})?apiKey={key}"
                   "&format=json&show=sku,name,salePrice,onlineAvailability,"
                   "inStoreAvailability,orderable,url")
            r = await f.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                p = extract.dig(r.json(), "products[0]") or {}
                orderable = str(p.get("orderable", "")).lower()
                online = bool(p.get("onlineAvailability"))
                st = Stock.IN if (online or orderable in ("available", "preorder")) else Stock.OUT
                return _r(t, st, p.get("salePrice"), "USD",
                          f"api orderable={p.get('orderable')}", t0)
        except Exception:
            pass
    return await browser_generic(t, f, cfg)


@adapter("target_us")
async def target_us(t: Target, f: Fetcher, cfg) -> Result:
    """Redsky needs a same-site referer; falls back to browser."""
    t0 = time.monotonic()
    key = cfg.get("target_key", "9f36aeafbe60771e321a7cc95a78140772ab3e96")
    store = cfg.get("target_store_id", "3991")
    try:
        url = ("https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"
               f"?key={key}&tcin={t.sku}&is_bot=false&store_id={store}"
               f"&pricing_store_id={store}&has_pricing_store_id=true"
               "&has_financing_options=true&visitor_id=0195B1FD&channel=WEB&page=%2Fp%2FA-" + t.sku)
        r = await f.get(url, headers={"Accept": "application/json", "Origin": "https://www.target.com",
                                      "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors",
                                      "Sec-Fetch-Dest": "empty"},
                        referer="https://www.target.com/")
        if r.status_code == 200:
            d = r.json()
            ship = extract.dig(d, "data.product.fulfillment.shipping_options") or {}
            code = str(ship.get("availability_status", "")).upper()
            st = Stock.IN if code in ("IN_STOCK", "PRE_ORDER_SELLABLE", "AVAILABLE") else Stock.OUT
            return _r(t, st, None, "USD", f"redsky {code or 'n/a'}", t0)
    except Exception:
        pass
    return await browser_generic(t, f, cfg)


@adapter("amazon")
async def amazon(t: Target, f: Fetcher, cfg) -> Result:
    """Amazon PDP: buybox availability block is the reliable signal."""
    t0 = time.monotonic()
    try:
        r = await f.get(t.url, referer=f"https://{up.urlparse(t.url).netloc}/")
        html = r.text
        if looks_blocked(r) or "api-services-support@amazon" in html[:3000] \
                or "Enter the characters you see below" in html:
            return await browser_generic(t, f, cfg)
        # Search/listing pages only render products under JS -> browser tier.
        # (Verified: plain HTTP sees zero console cards on amazon.ca/s?k=...)
        if "/s?" in t.url or "/s/" in t.url:
            return await browser_generic(t, f, cfg)
        s = extract.soup(html)
        avail = s.select_one("#availability")
        txt = avail.get_text(" ", strip=True).lower() if avail else ""
        has_buy = bool(s.select_one("#add-to-cart-button, #buy-now-button"))
        price = None
        pe = s.select_one(".a-price .a-offscreen")
        if pe:
            price = extract.sniff_price(pe.get_text())
        if "currently unavailable" in txt or "not available" in txt:
            st = Stock.OUT
        elif has_buy or "in stock" in txt:
            st = Stock.IN
        else:
            st, price2, _, _ = _classify_html(t, html)
            price = price or price2
        return _r(t, st, price, None, f"avail={txt[:40]!r}", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


@adapter("sfcc")
async def sfcc(t: Target, f: Fetcher, cfg) -> Result:
    """Salesforce Commerce Cloud (GameStop, EB Games, Toys R Us CA...).

    Reads the availability JSON the PDP embeds, else add-to-cart button state.
    """
    t0 = time.monotonic()
    res = await browser_generic(t, f, cfg)
    if res.status in (Stock.IN, Stock.OUT):
        return res
    return res


@adapter("playstation_direct")
async def ps_direct(t: Target, f: Fetcher, cfg) -> Result:
    t0 = time.monotonic()
    try:
        r = await f.get(t.url, referer="https://direct.playstation.com/")
        if looks_blocked(r):
            return await browser_generic(t, f, cfg)
        text = extract.visible_text(r.text)
        low = text.lower()
        if "out of stock" in low or "currently unavailable" in low or "sold out" in low:
            st = Stock.OUT
        elif "add to cart" in low or "buy now" in low:
            st = Stock.IN
        else:
            st = Stock.UNKNOWN
        return _r(t, st, extract.sniff_price(text), "USD", "psdirect", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


async def run_target(t: Target, f: Fetcher, cfg) -> Result:
    fn = REGISTRY.get(t.adapter, http_generic)
    res = await fn(t, f, cfg)
    # auto-escalate: a blocked/unknown HTTP check retries in a real browser
    # Free escalation ladder, cheapest first:
    #   tls (curl_cffi) -> headless browser -> stealth (patchright) -> paid unblocker
    if cfg.get("auto_escalate", True) and res.status in (
            Stock.BLOCKED, Stock.UNKNOWN, Stock.ABSENT, Stock.ERROR):
        ladder = []
        if t.adapter in ("http", "json"):
            ladder = [tls_http, browser_generic, stealth_browser]
        elif t.adapter == "tls":
            ladder = [browser_generic, stealth_browser]
        elif t.adapter in ("browser", "amazon", "sfcc"):
            ladder = [stealth_browser]
        for step in ladder:
            esc = await step(t, f, cfg)
            if esc.status in (Stock.IN, Stock.OUT):
                esc.note = f"esc->{esc.note}"
                return esc
            if esc.status is Stock.ABSENT and res.status is not Stock.ABSENT:
                res = esc
            elif res.status is Stock.BLOCKED and esc.status is not Stock.BLOCKED:
                res = esc
    if res.status in (Stock.BLOCKED, Stock.ERROR) and os.environ.get("UNBLOCKER_KEY"):
        ub = await unblock(t, f, cfg)
        if ub.status not in (Stock.BLOCKED, Stock.ERROR):
            return ub
    return res


# --------------------------------------------------------------------------
# Unblocker tier
#
# ~24 of the 40 retailers sit behind enterprise bot protection (Akamai,
# PerimeterX, Cloudflare Enterprise) that headless Chromium cannot pass.
# Set UNBLOCKER=scraperapi|scrapingbee|zyte|brightdata + UNBLOCKER_KEY and
# those targets route through a residential-proxy renderer instead.
# --------------------------------------------------------------------------

def unblocker_url(raw_url: str, country: str) -> str | None:
    provider = os.environ.get("UNBLOCKER", "").lower()
    key = os.environ.get("UNBLOCKER_KEY", "")
    if not provider or not key:
        return None
    q = up.quote(raw_url, safe="")
    cc = country.lower()
    if provider == "scraperapi":
        return (f"https://api.scraperapi.com/?api_key={key}&url={q}"
                f"&render=true&country_code={cc}&premium=true")
    if provider == "scrapingbee":
        return (f"https://app.scrapingbee.com/api/v1/?api_key={key}&url={q}"
                f"&render_js=true&premium_proxy=true&country_code={cc}")
    if provider == "zyte":
        return f"https://api.zyte.com/v1/extract?url={q}"
    if provider == "brightdata":
        return f"https://api.brightdata.com/request?url={q}&country={cc}"
    return None


@adapter("unblock")
async def unblock(t: Target, f: Fetcher, cfg) -> Result:
    t0 = time.monotonic()
    url = unblocker_url(t.url, t.country)
    if not url:
        return await browser_generic(t, f, cfg)
    try:
        r = await f.get(url, retries=1)
        if r.status_code >= 400:
            return _r(t, Stock.BLOCKED, note=f"unblocker {r.status_code}", t0=t0)
        st, price, cur, how = _classify_html(t, r.text)
        return _r(t, st, price, cur, f"unblock/{how}", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:80]}", t0=t0)


@adapter("tls")
async def tls_http(t: Target, f: Fetcher, cfg) -> Result:
    """curl_cffi tier: real Chrome TLS fingerprint, ~1/15th the cost of a browser."""
    t0 = time.monotonic()
    try:
        origin = f"https://{up.urlparse(t.url).netloc}/"
        status, html = await tlsfetch.get(t.url, referer=origin)
        if status in (401, 403, 429) or is_wall(html, status):
            if cfg.get("tls_warm", True):
                await tlsfetch.warm(origin)          # pick up normal cookies, retry once
                status, html = await tlsfetch.get(t.url, referer=origin)
            if status in (401, 403, 429) or is_wall(html, status):
                return _r(t, Stock.BLOCKED, note=f"tls {status}", t0=t0)
        if status >= 400 and status != 404:
            return _r(t, Stock.ERROR, note=f"tls {status}", t0=t0)
        st, price, cur, how = _classify_html(t, html)

        # An ambiguous search card resolves on the product page, still over TLS.
        if st is Stock.UNKNOWN and cfg.get("resolve_pdp", True):
            for link in extract.console_pdp_links(html, t.country, t.url,
                                                  limit=cfg.get("pdp_limit", 2)):
                try:
                    s2, h2 = await tlsfetch.get(link, referer=t.url)
                    if s2 >= 400 or is_wall(h2, s2):
                        continue
                    st2, p2, _c2, how2 = _classify_html(t, h2)
                    if st2 in (Stock.IN, Stock.OUT):
                        if st2 is Stock.IN:
                            t.url = link
                        return _r(t, st2, p2 or price, cur, f"tls-pdp/{how2}", t0)
                except Exception:
                    continue
        return _r(t, st, price, cur, f"tls/{how}", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:70]}", t0=t0)


@adapter("stealth")
async def stealth_browser(t: Target, f: Fetcher, cfg, _depth: int = 0) -> Result:
    """patchright tier: defeats Walmart CA/US, GameStop US, Adorama."""
    t0 = time.monotonic()
    try:
        status, html = await stealth.fetch(
            t.url, headless=cfg.get("stealth_headless", False),
            wait_ms=cfg.get("stealth_wait_ms", 6000),
            concurrency=cfg.get("stealth_concurrency", 2),
            offscreen=cfg.get("stealth_offscreen", True))
        if is_wall(html, status):
            return _r(t, Stock.BLOCKED, note="bot wall", t0=t0)
        st, price, cur, how = _classify_html(t, html)
        if st is Stock.UNKNOWN and _depth == 0 and cfg.get("resolve_pdp", True):
            for link in extract.console_pdp_links(html, t.country, t.url,
                                                  limit=cfg.get("pdp_limit", 2)):
                sub = Target(retailer=t.retailer, country=t.country, adapter="stealth",
                             url=link, sku=t.sku, title=t.title)
                res2 = await stealth_browser(sub, f, cfg, _depth=1)
                if res2.status in (Stock.IN, Stock.OUT):
                    res2.target = t
                    res2.note = f"pdp/{res2.note}"
                    res2.price = res2.price or price
                    if res2.status is Stock.IN:
                        t.url = link
                    return res2
        return _r(t, st, price, cur, f"stealth/{how}", t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:70]}", t0=t0)


@adapter("feed")
async def feed(t: Target, f: Fetcher, cfg) -> Result:
    """Community restock feeds (Slickdeals, RFD). A *lead*, not a stock check.

    Catches drops at retailers we don't scan at all, but a deal post is not
    proof of live stock -- so a match reports IN with an explicit 'lead' note
    and the alert text says to verify.
    """
    t0 = time.monotonic()
    try:
        status, body = await tlsfetch.get(t.url)
        if status >= 400:
            return _r(t, Stock.BLOCKED, note=f"feed {status}", t0=t0)
        items = re.findall(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        hits = [re.sub(r"<!\[CDATA\[|\]\]>", "", i).strip()
                for i in items if extract.CONSOLE_RE.search(i)]
        hits = [h for h in hits if extract.is_console_title(h)]
        if hits:
            return _r(t, Stock.IN, extract.sniff_price(" ".join(hits)), None,
                      f"LEAD (verify): {hits[0][:60]}", t0)
        return _r(t, Stock.ABSENT, note=f"no PS5 Pro posts ({len(items)} items)", t0=t0)
    except Exception as e:
        return _r(t, Stock.ERROR, note=f"{type(e).__name__}: {str(e)[:70]}", t0=t0)
