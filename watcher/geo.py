"""Where is this scan coming from?

US retailers geo-block or show different availability to a Canadian IP (and
vice versa). A scan run behind a CA VPN will under-report US stock, so we
detect the exit country and stamp it on the report instead of quietly lying.
"""
from __future__ import annotations

ENDPOINTS = [
    ("https://ipinfo.io/json", "country"),
    ("https://ipapi.co/json/", "country_code"),
]


async def exit_country(fetcher) -> tuple[str, str]:
    """Return (country_code, ip)."""
    for url, field in ENDPOINTS:
        try:
            r = await fetcher.get(url, headers={"Accept": "application/json"}, retries=0)
            d = r.json()
            cc = (d.get(field) or "").upper()
            if cc:
                return cc, d.get("ip", "?")
        except Exception:
            continue
    return "??", "?"


def warn(cc: str, targets) -> str:
    """Human-readable caveat when the exit IP can't see some targets properly."""
    countries = {t.country for t in targets}
    if cc in ("??",):
        return "⚠️ exit country unknown — geo-sensitive results may be off"
    mismatched = sorted(c for c in countries if c != cc)
    if mismatched:
        return (f"⚠️ scanning from <b>{cc}</b> — "
                f"{'/'.join(mismatched)} retailers may geo-block or show "
                f"different stock. Use a per-country proxy for accuracy.")
    return f"\U0001F30E exit IP: {cc}"
