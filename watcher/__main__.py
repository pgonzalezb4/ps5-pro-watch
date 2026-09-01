"""ps5-pro-watch CLI:  scan | digest | discover | test | find-chat-id"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

import yaml

from . import adapters, browser, extract, fx, geo, llm, notify, state, stealth, tlsfetch
from .httpclient import Fetcher
from .models import Result, Stock, Target

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> tuple[dict, list[Target]]:
    cfg_path = Path(path or os.environ.get("PS5WATCH_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(cfg_path.read_text())
    settings = raw.get("settings", {})
    settings["bestbuy_api_key"] = os.environ.get("BESTBUY_API_KEY", "")
    # Env overrides so CI can retune the browser tier without editing config.
    # Under Xvfb the browser is headed on a virtual display, so the off-screen
    # window trick is unnecessary (and negative coords confuse some WMs).
    for env, key in (("PS5WATCH_STEALTH_HEADLESS", "stealth_headless"),
                     ("PS5WATCH_STEALTH_OFFSCREEN", "stealth_offscreen")):
        if os.environ.get(env) is not None:
            settings[key] = os.environ[env].lower() in ("1", "true", "yes")
    for env, key in (("PS5WATCH_STEALTH_CONCURRENCY", "stealth_concurrency"),
                     ("PS5WATCH_BROWSER_CONCURRENCY", "browser_concurrency")):
        if os.environ.get(env):
            settings[key] = int(os.environ[env])
    targets = [Target(**t) for t in raw.get("targets", []) if t.get("enabled", True)]
    return settings, targets


def load_env():
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # Strip trailing "  # comment" -- only when preceded by
                # whitespace, so a value containing '#' survives intact.
                v = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
                if v:
                    os.environ.setdefault(k.strip(), v)


async def scan_all(cfg: dict, targets: list[Target], verbose: bool = True) -> tuple[list[Result], float]:
    t0 = time.monotonic()
    http_sem = asyncio.Semaphore(cfg.get("http_concurrency", 8))
    results: list[Result] = []

    async with Fetcher(min_gap=cfg.get("min_gap", 1.2)) as f:
        rate = await fx.usd_to_cad(f)
        cc, ip = await geo.exit_country(f)
        cfg["_geo"] = geo.warn(cc, targets)
        if verbose:
            print(f"  [exit IP {ip} / {cc}]\n")

        async def one(t: Target):
            async with http_sem:
                res = await adapters.run_target(t, f, cfg)
            # optional self-healing pass
            if res.status is Stock.UNKNOWN and os.environ.get("PS5WATCH_LLM_FALLBACK") == "1":
                try:
                    _s, _h = await browser.fetch(t.url, headless=cfg.get("headless", True))
                    st, price, why = await llm.classify(extract.visible_text(_h), f)
                    if st is not Stock.UNKNOWN:
                        res.status, res.note = st, why
                        res.price = res.price or price
                except Exception:
                    pass
            if verbose:
                p = f" ${res.price:,.2f}" if res.price else ""
                print(f"  {res.status.emoji} {res.target.country} {res.target.retailer:<32} "
                      f"{res.status.value:<13}{p:<12} {res.note[:44]} ({res.elapsed_ms}ms)",
                      flush=True)
            return res

        results = list(await asyncio.gather(*(one(t) for t in targets)))
    await browser.shutdown()
    await stealth.shutdown()
    await tlsfetch.close()
    return results, rate if isinstance(rate, float) else 1.37


async def cmd_scan(args):
    cfg, targets = load_config(args.config)
    if args.only:
        needle = args.only.lower()
        targets = [t for t in targets if needle in t.retailer.lower() or needle in t.country.lower()]
    print(f"Scanning {len(targets)} retailers...\n")
    t0 = time.monotonic()
    results, rate = await scan_all(cfg, targets, verbose=not args.quiet)
    elapsed = time.monotonic() - t0

    st = state.load()
    alerts = state.diff(results, st, cooldown_min=args.cooldown)
    state.save(st)

    async with Fetcher() as f:
        if alerts:
            print(f"\n\U0001F6A8 {len(alerts)} IN STOCK -> alerting")
            await notify.send(f, notify.format_alert(alerts, rate), preview=True)
        elif args.digest:
            await notify.send(f, notify.format_digest(results, rate, elapsed, cfg.get("_geo","")), silent=True)
        else:
            print(f"\nNothing new in stock. ({elapsed:.0f}s)")
    _summary(results, elapsed)
    return 0


async def cmd_digest(args):
    cfg, targets = load_config(args.config)
    print(f"Daily digest over {len(targets)} retailers...\n")
    t0 = time.monotonic()
    results, rate = await scan_all(cfg, targets, verbose=not args.quiet)
    elapsed = time.monotonic() - t0
    st = state.load()
    state.diff(results, st, cooldown_min=args.cooldown)
    state.save(st)
    async with Fetcher() as f:
        await notify.send(f, notify.format_digest(results, rate, elapsed, cfg.get("_geo","")),
                          silent=not any(r.status is Stock.IN for r in results))
    _summary(results, elapsed)
    return 0


def _summary(results, elapsed):
    from collections import Counter
    c = Counter(r.status.value for r in results)
    print(f"\n--- {len(results)} targets in {elapsed:.0f}s ---")
    for k, v in c.most_common():
        print(f"  {k:<14} {v}")


async def cmd_discover(args):
    """Find real PS5 Pro SKUs/URLs at retailers that expose a search API."""
    cfg, _ = load_config(args.config)
    inc = cfg.get("product_match", ["ps5 pro"])
    exc = cfg.get("exclude_match", [])
    async with Fetcher() as f:
        print("Best Buy Canada search API:")
        r = await f.get("https://www.bestbuy.ca/api/v2/json/search"
                        "?query=playstation%205%20pro%20console&page=1&pageSize=40&lang=en-CA",
                        headers={"Accept": "application/json"})
        for p in r.json().get("products", []):
            name = (p.get("name") or "").lower()
            if any(i in name for i in inc) and not any(x in name for x in exc):
                print(f"  sku={p['sku']}  ${p.get('salePrice')}  {p.get('name')[:60]}")
                print(f"    url: https://www.bestbuy.ca/en-ca/product/{p['sku']}")
    return 0


async def cmd_test(args):
    """Verify Telegram wiring end to end."""
    async with Fetcher() as f:
        rate = await fx.usd_to_cad(f)
        ok = await notify.send(
            f, "\U00002705 <b>ps5-pro-watch</b> is wired up.\n"
               f"<i>USD→CAD {rate:.4f}</i>\nYou'll get a \U0001F6A8 alert the moment "
               "a PS5 Pro goes buyable.")
    print("Telegram OK" if ok else "Telegram NOT configured (see .env)")
    return 0 if ok else 1


async def cmd_find_chat_id(args):
    async with Fetcher() as f:
        cid = await notify.resolve_chat_id(f)
    if cid:
        print(f"TELEGRAM_CHAT_ID={cid}\n\nAdd that line to {ROOT/'.env'}")
        return 0
    print("No chat found. Open Telegram, send your bot any message, then re-run.")
    return 1


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="ps5-pro-watch")
    ap.add_argument("command", choices=["scan", "digest", "discover", "test", "find-chat-id"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--only", default=None, help="filter retailers by substring or country")
    ap.add_argument("--cooldown", type=int, default=45, help="minutes between repeat alerts")
    ap.add_argument("--digest", action="store_true", help="also send digest when nothing new")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    fn = {"scan": cmd_scan, "digest": cmd_digest, "discover": cmd_discover,
          "test": cmd_test, "find-chat-id": cmd_find_chat_id}[args.command]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
