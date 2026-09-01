"""USD -> CAD so US listings are comparable to Canadian ones."""
from __future__ import annotations

FALLBACK = 1.37
_ENDPOINTS = [
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CAD",
    "https://open.er-api.com/v6/latest/USD",
]


async def usd_to_cad(fetcher) -> float:
    for url in _ENDPOINTS:
        try:
            r = await fetcher.get(url, headers={"Accept": "application/json"}, retries=1)
            d = r.json()
            rate = (d.get("rates") or {}).get("CAD")
            if rate:
                return float(rate)
        except Exception:
            continue
    return FALLBACK


def landed_cad(usd_price: float, rate: float, duty_pct: float = 0.0,
               tax_pct: float = 13.0) -> float:
    """Rough landed cost: FX + optional duty + provincial tax."""
    return round(usd_price * rate * (1 + duty_pct / 100) * (1 + tax_pct / 100), 2)
