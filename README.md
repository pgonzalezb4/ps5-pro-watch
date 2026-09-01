# ps5-pro-watch

Scans **40 retailers** across Canada and the US for a buyable PlayStation 5 Pro
console and pushes results to Telegram.

## Does it need an LLM?

**No — and deliberately so.** Stock checking is deterministic: hit an endpoint,
read a boolean. An LLM in the hot path would be slower, cost money on every
scan, and can hallucinate stock that isn't there. All 40 retailers are handled
by structured extraction:

1. **Official / internal JSON APIs** — exact, instant, no scraping.
   Best Buy CA (`ecomm-api/availability`) and Best Buy US (developer API).
2. **schema.org JSON-LD** — most retailers publish `offers.availability`.
3. **DOM product-card matching** — locate the card that *names a PS5 Pro
   console*, then read availability from inside that card only.
4. **PDP resolution** — an ambiguous search card gets resolved by opening the
   product page, where availability is explicit.

The LLM is wired in as an **optional fallback only** (`PS5WATCH_LLM_FALLBACK=1`):
when every deterministic parser returns `UNKNOWN`, Claude Haiku re-reads the page
text so the scanner self-heals after a site redesign. It can never *raise* an
alert on its own judgment of a page the parsers understood.

## Why not "daily"?

PS5 Pro restocks sell out in **2–10 minutes**. A once-a-day scan reports
"sold out" 365 days a year. So this runs two ways:

- `scan` — every 10 min, alerts **only on an OUT → IN transition** (45-min
  cooldown so one restock doesn't spam you).
- `digest` — once a day at 09:00, the full 40-retailer table.

## Setup

Multiple people can be notified. Each gets their own bot token (so anyone can
revoke theirs without breaking the others): blank suffix for the first, then
`_2`, `_3`, up to 10. A send failure for one person never blocks the others.

```bash
make install
cp .env.example .env        # paste TELEGRAM_BOT_TOKEN (+ _2 for a second person)
# EACH person must open their bot in Telegram and press Start first --
# a bot cannot open a conversation, so there's no chat id until they do.
make chat-id                # prints a TELEGRAM_CHAT_ID line per bot
make test                   # confirms delivery
make scan                   # live run
make schedule               # launchd: scan/10min + digest daily
```

## Bypassing bot walls — for free

Three free tiers, tried cheapest-first. Every `http` target auto-escalates
down this ladder before anything paid is considered.

| Tier | Tool | Cost | Beats |
|---|---|---|---|
| 1. TLS impersonation | `curl_cffi` | free, ~1s | JA3/TLS fingerprint 403s |
| 2. Headless browser | `playwright` | free, ~8s | plain JS rendering |
| 3. Stealth browser | `patchright` | free, ~30s | Cloudflare/Akamai JS challenges |
| 4. Unblocker | ScraperAPI etc. | ~$30/mo | the last Cloudflare Enterprise holdouts |

**Why `curl_cffi` matters most.** Most of these 403s were never JS challenges —
they were *TLS fingerprint* rejections. httpx/requests announce a cipher and
extension order no real browser produces, so the server drops you before reading
a header. `curl_cffi` binds curl-impersonate and replays Chrome's exact TLS stack
and HTTP/2 SETTINGS frame. Measured here: **9 retailers flipped 403 → 200**
(London Drugs, Micro Center, Antonline, Target, Sam's Club, Costco US, Best Buy
US, Kohl's, BJ's) at ~1/15th the cost of a browser page load.

**Why `patchright`, not `playwright`.** Vanilla Playwright leaks the CDP
`Runtime.enable` call plus patched JS properties that Cloudflare fingerprints
directly. Patchright is a drop-in fork with those leaks removed. It must run
with a **persistent context and no custom UA/viewport** — setting those
re-introduces the fingerprints it strips. Measured: gets through **Walmart CA,
Walmart US, GameStop US, Adorama, eBay**, which both httpx and vanilla
Playwright fail.

### Headless does not work — run headed but off-screen instead

Headless is itself a detection signal. Measured on the four hardest targets:

| | Walmart CA | Walmart US | GameStop US | eBay |
|---|---|---|---|---|
| `headless=True` | 456 ✗ | bot wall ✗ | 403 ✗ | 403 ✗ |
| headed, off-screen | 701KB ✓ | 345KB ✓ | 585KB ✓ | 2.2MB ✓ |

macOS does not clamp negative window coordinates, so the browser is parked at
`--window-position=-3000,-3000`: **fully headed and stealthy, never visible and
never focus-stealing.** This is the default (`stealth_offscreen: true`).

Off-screen windows count as *occluded*, and Chrome throttles timers in occluded
windows — which would stall the JS challenges we're there to let run. So
`--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`,
`--disable-background-timer-throttling` and `--disable-features=CalculateNativeWinOcclusion`
are set too, and `page.bring_to_front()` is never called.

Verify with `make stealth-check`. If you ever want it fully off your machine,
the included GitHub Actions workflow runs the same scan in the cloud.

### Results: 40 → 55 retailers, 12 blocked → 5

| | before | after |
|---|---|---|
| retailers scanned | 40 | **55** |
| blocked | 12 | **5** |
| returning real stock+price | 13 | **23** |

Newly readable, with live prices: Walmart US `$779.99`, GameStop US `$899.99`,
Costco CA `$1,000`, Canada Computers `$1,099.99`, eBay CA/US (29/36 listings),
Vuugo `$1,739`, Simply Computing `$1,799`.

Still hard-blocked (5, all Cloudflare Enterprise): Staples CA, EB Games CA,
Mike's Computer Shop, Real Canadian Superstore, GameStop CA. These need the
paid unblocker tier — `UNBLOCKER=scraperapi` + `UNBLOCKER_KEY`.

## Free ways to add more retailers

1. **Free official APIs** — `BESTBUY_API_KEY` (developer.bestbuy.com) gives exact
   US stock *and* in-store availability by postal code, no scraping at all.
   Best Buy CA's `ecomm-api` is already used and needs no key.
2. **Community restock feeds** (`adapter: feed`) — Slickdeals and RedFlagDeals
   RSS catch drops at stores this scanner doesn't cover. Reported as
   `LEAD (verify)`, never as confirmed stock, since a deal post isn't live stock.
3. **Just add a URL** — most retailers need no custom code:

```yaml
  - retailer: Some Store
    country: CA
    adapter: tls          # start here; it escalates automatically
    url: https://example.com/search?q=playstation+5+pro
```

### Geo matters
US retailers show different availability to a Canadian IP. The report stamps the
exit country and warns when it can't see a country's retailers properly. For
true dual-country accuracy, run two instances behind country-matched proxies.

## Statuses

🟢 in stock · ⚪ out of stock · 🟡 unknown (parser couldn't tell) ·
⚫ not listed (retailer doesn't carry it) · 🚫 blocked · 🔴 error

Prices above 1.35× MSRP are flagged **⚠️ marketplace/scalper** — Newegg and
Amazon third-party listings are frequently 1.8× and are not real restocks.
US prices are converted to CAD with live FX plus 13% tax as a landed estimate.

## Adding a retailer

Append to `config.yaml`:

```yaml
  - retailer: Some Store
    country: CA
    adapter: http        # http | browser | unblock | json | amazon | sfcc
    url: https://example.com/search?q=playstation+5+pro
```

`http` targets that come back blocked auto-escalate to the browser tier, then to
the unblocker. `make discover` prints live Best Buy CA SKUs.
