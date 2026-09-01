"""Deterministic availability extraction. No LLM involved."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .models import Stock

# schema.org availability values, normalised
_IN_SCHEMA = {"instock", "instoreonly", "limitedavailability", "onlineonly", "presale"}
_OUT_SCHEMA = {"outofstock", "soldout", "discontinued", "backorder", "outofservice"}

DEFAULT_OUT_WORDS = [
    "out of stock", "sold out", "currently unavailable", "temporarily unavailable",
    "no longer available", "rupture de stock", "épuisé", "notify me when available",
    "coming soon", "unavailable online", "not available", "email me when available",
    "out-of-stock", "sold-out", "back order", "backordered",
]
DEFAULT_IN_WORDS = [
    "add to cart", "add to bag", "buy now", "in stock", "available online",
    "ajouter au panier", "en stock", "pick up today", "ship it", "add to basket",
    "available for shipping", "checkout now",
]


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def visible_text(html: str, limit: int = 20000) -> str:
    s = soup(html)
    for tag in s(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()
    txt = re.sub(r"\s+", " ", s.get_text(" ", strip=True))
    return txt[:limit]


def dig(obj, path: str, default=None):
    """dig(d, 'a.b[0].c') -> value or default."""
    cur = obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        try:
            if part.startswith("["):
                cur = cur[int(part[1:-1])]
            elif isinstance(cur, list):
                cur = cur[int(part)]
            else:
                cur = cur[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return cur


def iter_jsonld(html: str):
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def _norm_schema(val) -> str:
    if not isinstance(val, str):
        return ""
    return re.sub(r"[^a-z]", "", val.rsplit("/", 1)[-1].lower())


def from_jsonld(html: str) -> tuple[Stock, float | None, str | None]:
    """Read schema.org Product/Offer blocks. Widely supported, very reliable."""
    price = cur = None
    verdict = Stock.UNKNOWN
    for node in iter_jsonld(html):
        offers = node.get("offers") if isinstance(node, dict) else None
        candidates = []
        if isinstance(offers, dict):
            candidates = [offers] + (offers.get("offers") or [] if isinstance(offers.get("offers"), list) else [])
        elif isinstance(offers, list):
            candidates = [o for o in offers if isinstance(o, dict)]
        elif isinstance(node, dict) and "availability" in node:
            candidates = [node]
        for off in candidates:
            avail = _norm_schema(off.get("availability") or off.get("itemAvailability"))
            if price is None and off.get("price") not in (None, ""):
                try:
                    price = float(str(off["price"]).replace(",", "").replace("$", ""))
                    cur = off.get("priceCurrency") or cur
                except ValueError:
                    pass
            if avail in _IN_SCHEMA:
                return Stock.IN, price, cur
            if avail in _OUT_SCHEMA:
                verdict = Stock.OUT
    return verdict, price, cur


def from_keywords(text: str, in_words=None, out_words=None) -> Stock:
    """Last-resort text heuristic. OUT wins ties: false negatives beat false alarms."""
    t = text.lower()
    outs = [w.lower() for w in (out_words or DEFAULT_OUT_WORDS)]
    ins = [w.lower() for w in (in_words or DEFAULT_IN_WORDS)]
    hit_out = any(w in t for w in outs)
    hit_in = any(w in t for w in ins)
    if hit_out:
        return Stock.OUT
    if hit_in:
        return Stock.IN
    return Stock.UNKNOWN


PRICE_RE = re.compile(r"\$\s?([0-9]{3,4}(?:[.,][0-9]{2})?)")


def sniff_price(text: str) -> float | None:
    best = None
    for m in PRICE_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # PS5 Pro lives in this band; ignore accessory/warranty prices
        if 600 <= v <= 1400 and (best is None or v < best):
            best = v
    return best


# ==========================================================================
# Console-specific matching.
#
# The naive "does the page say 'add to cart'" test produces false positives on
# search pages, where accessories and other consoles are also purchasable.
# We instead locate product *cards* that name a PS5 Pro console, then read
# availability and price from inside that card only.
# ==========================================================================

CONSOLE_RE = re.compile(r"(playstation\s*5\s*pro|ps5\s*pro)", re.I)

# NOTE: these use \w* suffixes, not a trailing \b. "\brefurbish\b" does NOT
# match "Refurbished" -- the boundary fails between 'h' and 'e' -- which let a
# $1,499 refurbished unit through as a genuine in-stock alert. Same trap for
# plurals ("covers", "stands").
ACCESSORY_RE = re.compile(
    r"\b(cover\w*|stand\w*|fan\w*|bracket\w*|disc drive|controller\w*|charger\w*|"
    r"skin\w*|case\w*|faceplate\w*|cooling|mount\w*|warranty|protection|headset\w*|"
    r"dualsense|cable\w*|holder\w*|dock\w*|grip\w*|plate\w*|sticker\w*|decal\w*|"
    r"carry\w*|bag\w*|remote\w*|camera\w*|charging|station\w*|earbud\w*|"
    r"adapter\w*|hub\w*|screen protector|sleeve\w*|strap\w*)\b", re.I)

REFURB_RE = re.compile(
    r"\b(refurb\w*|renew\w*|pre-?owned|preowned|used|open[- ]box|recondition\w*|"
    r"certified pre|second[- ]hand|as[- ]is|for parts|damaged|scratch)\b", re.I)

PRICE_BAND = {"CA": (850.0, 1900.0), "US": (600.0, 1500.0)}

_CARD_PRICE = re.compile(r"\$\s?([0-9][0-9,]{2,6}(?:\.[0-9]{2})?)")


def is_console_title(title: str) -> bool:
    """True only for an actual PS5 Pro console listing."""
    if not title or not CONSOLE_RE.search(title):
        return False
    if ACCESSORY_RE.search(title) or REFURB_RE.search(title):
        return False
    return True


def _prices_in(text: str, band: tuple[float, float]) -> list[float]:
    out = []
    for m in _CARD_PRICE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if band[0] <= v <= band[1]:
            out.append(v)
    return out


def console_offers_jsonld(html: str, country: str) -> list[dict]:
    """PS5 Pro console offers declared in schema.org markup."""
    band = PRICE_BAND.get(country, PRICE_BAND["US"])
    found = []
    for node in iter_jsonld(html):
        name = node.get("name") or ""
        if not isinstance(name, str) or not is_console_title(name):
            continue
        offers = node.get("offers")
        cands = []
        if isinstance(offers, dict):
            cands = [offers]
            if isinstance(offers.get("offers"), list):
                cands += [o for o in offers["offers"] if isinstance(o, dict)]
        elif isinstance(offers, list):
            cands = [o for o in offers if isinstance(o, dict)]
        for off in cands:
            avail = _norm_schema(off.get("availability") or off.get("itemAvailability"))
            price = None
            try:
                price = float(str(off.get("price", "")).replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                pass
            if price is not None and not (band[0] <= price <= band[1]):
                continue          # accessory priced offer attached to a console name
            if avail in _IN_SCHEMA:
                found.append({"title": name, "price": price, "status": Stock.IN, "src": "jsonld"})
            elif avail in _OUT_SCHEMA:
                found.append({"title": name, "price": price, "status": Stock.OUT, "src": "jsonld"})
    return found


def console_offers_dom(html: str, country: str, max_cards: int = 40) -> list[dict]:
    """Walk up from each PS5-Pro-console text node to its product card."""
    band = PRICE_BAND.get(country, PRICE_BAND["US"])
    s = soup(html)
    for tag in s(["script", "style", "noscript", "svg"]):
        tag.decompose()

    seen_cards, found = set(), []
    nodes = [el for el in s.find_all(["a", "h1", "h2", "h3", "h4", "span", "div", "p"])
             if el.string and CONSOLE_RE.search(el.string)]
    for el in nodes[:200]:
        title = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not is_console_title(title):
            continue
        card, hops = el, 0
        while card.parent is not None and hops < 6:
            card = card.parent
            hops += 1
            ctext = card.get_text(" ", strip=True)
            if len(ctext) > 120 and _prices_in(ctext, band):
                break
        cid = id(card)
        if cid in seen_cards:
            continue
        seen_cards.add(cid)

        ctext = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        if len(ctext) > 4000:            # climbed into the whole page: unreliable
            continue
        prices = _prices_in(ctext, band)
        low = ctext.lower()
        out_hit = any(w in low for w in DEFAULT_OUT_WORDS)
        in_hit = any(w in low for w in DEFAULT_IN_WORDS)
        disabled = bool(card.select_one("button[disabled], [aria-disabled='true'], "
                                        ".disabled, .out-of-stock, .sold-out"))
        if out_hit or disabled:
            st = Stock.OUT
        elif in_hit and prices:
            st = Stock.IN
        else:
            st = Stock.UNKNOWN
        href = None
        a = card.select_one("a[href]") if card.name != "a" else card
        if a is not None and a.has_attr("href"):
            href = a["href"]
        found.append({"title": title[:90], "price": prices[0] if prices else None,
                      "status": st, "src": "dom", "url": href})
        if len(found) >= max_cards:
            break
    return found


def classify_console(html: str, country: str) -> tuple[Stock, float | None, str]:
    """Authoritative verdict for a page. IN only with a matched console card."""
    offers = console_offers_jsonld(html, country)
    src = "jsonld"
    if not offers:
        offers = console_offers_dom(html, country)
        src = "dom"
    if not offers:
        # Distinguish "page never rendered" from "retailer genuinely has none".
        body = visible_text(html, 40000)
        if len(body) < 1500:
            return Stock.UNKNOWN, None, f"page not rendered ({len(body)}b)"
        if CONSOLE_RE.search(body):
            return Stock.UNKNOWN, None, "console named, no parseable card"
        return Stock.ABSENT, None, "no PS5 Pro console listed"

    ins = [o for o in offers if o["status"] is Stock.IN]
    if ins:
        priced = [o for o in ins if o["price"]]
        best = min(priced, key=lambda o: o["price"]) if priced else ins[0]
        return Stock.IN, best["price"], f"{src}: {best['title'][:48]}"
    outs = [o for o in offers if o["status"] is Stock.OUT]
    if outs:
        p = next((o["price"] for o in outs if o["price"]), None)
        return Stock.OUT, p, f"{src}: {len(offers)} card(s) unavailable"
    p = next((o["price"] for o in offers if o["price"]), None)
    return Stock.UNKNOWN, p, f"{src}: ambiguous card"


def console_pdp_links(html: str, country: str, base: str, limit: int = 3) -> list[str]:
    """Absolute URLs of PS5 Pro console cards on a search/listing page."""
    import urllib.parse as _up
    urls, seen = [], set()
    for o in console_offers_dom(html, country):
        u = o.get("url")
        if not u or u.startswith(("javascript:", "#")):
            continue
        full = _up.urljoin(base, u)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
        if len(urls) >= limit:
            break
    return urls


# MSRP reference for flagging marketplace/scalper pricing
MSRP = {"CA": 949.99, "US": 699.99}


def price_flag(price: float | None, country: str) -> str:
    if not price:
        return ""
    msrp = MSRP.get(country, MSRP["US"])
    if price > msrp * 1.35:
        return f" \u26a0\ufe0f {price/msrp:.1f}x MSRP (marketplace/scalper?)"
    return ""
