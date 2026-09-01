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


def recipients() -> list[tuple[str, str, str]]:
    """Every configured (token, chat_id, label).

    Supports any number of people: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID for the
    first, then _2, _3, ... for the rest. Separate tokens mean each person can
    run their own bot and revoke it independently.
    """
    out = []
    for suffix in [""] + [f"_{i}" for i in range(2, 11)]:
        tok = (os.environ.get(f"TELEGRAM_BOT_TOKEN{suffix}") or "").strip()
        chat = (os.environ.get(f"TELEGRAM_CHAT_ID{suffix}") or "").strip()
        if tok and chat and "Example" not in tok:
            label = (os.environ.get(f"TELEGRAM_LABEL{suffix}") or "").strip() \
                or f"bot{tok.split(':')[0]}"
            out.append((tok, chat, label))
    # Loud warning for a half-configured recipient: this exact case (token set
    # in CI but chat id not mapped through) silently dropped a person's alerts.
    for suffix in [""] + [f"_{i}" for i in range(2, 11)]:
        tok = (os.environ.get(f"TELEGRAM_BOT_TOKEN{suffix}") or "").strip()
        chat = (os.environ.get(f"TELEGRAM_CHAT_ID{suffix}") or "").strip()
        if bool(tok) != bool(chat):
            missing = "TELEGRAM_CHAT_ID" if tok else "TELEGRAM_BOT_TOKEN"
            print(f"[notify] WARNING: recipient{suffix or ' 1'} is half-configured "
                  f"-- {missing}{suffix} is missing, so they will NOT be notified")
    return out


async def send(fetcher, text: str, *, silent: bool = False,
               preview: bool = False) -> bool:
    """Fan out to every recipient. One person's failure never blocks another's."""
    people = recipients()
    if not people:
        print("[notify] no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID pairs set; printing:\n")
        print(text)
        return False
    all_ok = True
    for token, chat, label in people:
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
                    print(f"[notify] {label}: telegram {r.status_code}: {r.text[:160]}")
                    ok = False
            except Exception as e:
                print(f"[notify] {label}: {type(e).__name__}: {e}")
                ok = False
        print(f"[notify] {label} -> {'sent' if ok else 'FAILED'}")
        all_ok = all_ok and ok
    return all_ok


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


def format_digest_compact(results: list[Result], rate: float) -> str:
    """The nothing-to-report digest: 3 lines, not a 55-row table.

    A daily 'still sold out' message is only worth sending if it can be read at
    a glance, so this keeps just the headline and the best price per country.
    """
    tracked = [r for r in results if r.status in (Stock.IN, Stock.OUT)]
    best = []
    for cc, sym in (("CA", "🇨🇦"), ("US", "🇺🇸")):
        priced = [r for r in results if r.target.country == cc and r.price
                  and r.status is Stock.OUT]
        if priced:
            b = min(priced, key=lambda r: r.price)
            best.append(f"{sym} {_esc(b.target.retailer)} ${b.price:,.0f}")
    line2 = " · ".join(best) if best else "no prices readable this run"
    return ("\u26aa <b>PS5 Pro \u2014 still sold out</b>\n"
            f"{line2}\n"
            f"<i>{len(results)} retailers checked, {len(tracked)} readable \u00b7 "
            f"you'll get a \U0001F6A8 the moment one goes buyable</i>")


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
    """Print the chat id for every configured bot. Each person must message
    their bot first -- Telegram forbids a bot from opening a conversation."""
    found = None
    for suffix in [""] + [f"_{i}" for i in range(2, 11)]:
        token = (os.environ.get(f"TELEGRAM_BOT_TOKEN{suffix}") or "").strip()
        if not token or "Example" in token:
            continue
        tag = token.split(":")[0]
        try:
            me = await fetcher.client.get(API.format(token=token, method="getMe"), timeout=20)
            uname = (me.json().get("result") or {}).get("username", "?")
            r = await fetcher.client.get(API.format(token=token, method="getUpdates"), timeout=20)
            seen = {}
            for u in r.json().get("result", []):
                msg = u.get("message") or u.get("channel_post") or {}
                ch = msg.get("chat") or {}
                if ch.get("id"):
                    seen[ch["id"]] = ch.get("username") or ch.get("first_name") or "?"
            if seen:
                for cid, who in seen.items():
                    print(f"  @{uname} (bot {tag}) -> TELEGRAM_CHAT_ID{suffix}={cid}   [{who}]")
                    found = found or str(cid)
            else:
                print(f"  @{uname} (bot {tag}) -> no messages yet; "
                      f"open the bot in Telegram and press Start")
        except Exception as e:
            print(f"  bot {tag}: {type(e).__name__}: {e}")
    return found
