"""Telegram delivery."""
from __future__ import annotations

import html as _html
import os

from .extract import price_flag
from .fx import landed_cad
from .models import Result, Stock

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000


def _esc(s) -> str:
    return _html.escape(str(s), quote=False)


async def send(fetcher, text: str, *, silent: bool = False,
               preview: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing:\n")
        print(text)
        return False
    ok = True
    for chunk in _chunks(text):
        try:
            r = await fetcher.client.post(
                API.format(token=token, method="sendMessage"),
                json={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": not preview,
                      "disable_notification": silent},
                timeout=25)
            if r.status_code != 200:
                print(f"[notify] telegram {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[notify] {type(e).__name__}: {e}")
            ok = False
    return ok


def _chunks(text: str):
    lines, buf, n = text.split("\n"), [], 0
    for ln in lines:
        if n + len(ln) + 1 > LIMIT and buf:
            yield "\n".join(buf)
            buf, n = [], 0
        buf.append(ln)
        n += len(ln) + 1
    if buf:
        yield "\n".join(buf)


def _price(res: Result, rate: float) -> str:
    if res.price is None:
        return ""
    cur = res.currency or ("CAD" if res.target.country == "CA" else "USD")
    s = f"{res.price:,.2f} {cur}"
    if cur == "USD":
        s += f"  (~${landed_cad(res.price, rate, tax_pct=13):,.0f} CAD landed)"
    return s + price_flag(res.price, res.target.country)


def format_alert(alerts: list[Result], rate: float) -> str:
    out = ["\U0001F6A8 <b>PS5 PRO IN STOCK</b> \U0001F6A8", ""]
    for r in alerts:
        out.append(f"{r.status.emoji} <b>{_esc(r.target.retailer)}</b> [{r.target.country}]")
        p = _price(r, rate)
        if p:
            out.append(f"   \U0001F4B0 {_esc(p)}")
        out.append(f"   \U0001F517 <a href=\"{_esc(r.target.url)}\">Buy now</a>")
        out.append("")
    out.append("<i>Go. Now. These last minutes.</i>")
    return "\n".join(out)


def format_digest(results: list[Result], rate: float, elapsed: float,
                  geo_note: str = "") -> str:
    order = {Stock.IN: 0, Stock.OUT: 1, Stock.UNKNOWN: 2, Stock.ABSENT: 3,
             Stock.BLOCKED: 4, Stock.ERROR: 5}
    rs = sorted(results, key=lambda r: (order[r.status], r.target.country, r.target.retailer))
    n_in = sum(1 for r in rs if r.status is Stock.IN)
    head = "\U0001F7E2 FOUND" if n_in else "\U000026AA none in stock"
    out = [f"<b>PS5 Pro daily scan</b> — {head}",
           f"<i>{len(rs)} retailers · {elapsed:.0f}s · USD→CAD {rate:.4f}</i>"]
    if geo_note:
        out.append(f"<i>{geo_note}</i>")
    out.append("")

    for country in ("CA", "US"):
        sub = [r for r in rs if r.target.country == country]
        if not sub:
            continue
        flag = "\U0001F1E8\U0001F1E6 Canada" if country == "CA" else "\U0001F1FA\U0001F1F8 United States"
        out.append(f"<b>{flag}</b>")
        for r in sub:
            line = f"{r.status.emoji} {_esc(r.target.retailer)}"
            p = _price(r, rate)
            if p:
                line += f" — {_esc(p)}"
            if r.status is Stock.IN:
                line = f"<a href=\"{_esc(r.target.url)}\">{line}</a>"
            elif r.status in (Stock.BLOCKED, Stock.ERROR, Stock.UNKNOWN) and r.note:
                line += f" <i>({_esc(r.note[:38])})</i>"
            out.append(line)
        out.append("")

    cheapest = [r for r in rs if r.status is Stock.IN and r.price]
    if cheapest:
        best = min(cheapest, key=lambda r: r.price * (rate if r.currency == "USD" else 1))
        out.append(f"\U0001F3C6 Cheapest in stock: <b>{_esc(best.target.retailer)}</b> "
                   f"— {_esc(_price(best, rate))}")
    out.append("\U0001F7E2 in · \U000026AA out · \U0001F7E1 unknown · "
               "\U000026AB not listed · \U0001F6AB blocked · \U0001F534 error")
    return "\n".join(out)


async def resolve_chat_id(fetcher) -> str | None:
    """Helper: message your bot once, then run `--find-chat-id`."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    r = await fetcher.client.get(API.format(token=token, method="getUpdates"), timeout=20)
    for u in reversed(r.json().get("result", [])):
        msg = u.get("message") or u.get("channel_post") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid:
            return str(cid)
    return None
