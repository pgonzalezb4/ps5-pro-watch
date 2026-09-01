"""OPTIONAL last-resort classifier.

Only invoked when the deterministic extractors return UNKNOWN and
PS5WATCH_LLM_FALLBACK=1. Never used to confirm stock on its own -- it just
recovers from markup changes so the scanner self-heals between maintenance.
"""
from __future__ import annotations

import json
import os

from .models import Stock

MODEL = os.environ.get("PS5WATCH_LLM_MODEL", "claude-haiku-4-5-20251001")

PROMPT = """You are reading text scraped from a retailer product/search page.
Decide whether a *PlayStation 5 Pro console* (not an accessory, cover, stand,
controller, or refurbished unit) is CURRENTLY PURCHASABLE on this page.

Reply with ONLY compact JSON: {"status":"IN_STOCK|OUT_OF_STOCK|UNKNOWN","price":<number or null>,"why":"<8 words max>"}

Rules:
- "Add to cart"/"Buy now" active => IN_STOCK
- "Sold out"/"Out of stock"/"Notify me"/"Coming soon" => OUT_OF_STOCK
- Only accessories present, or no PS5 Pro console at all => OUT_OF_STOCK
- Genuinely cannot tell => UNKNOWN. Never guess IN_STOCK.

PAGE TEXT:
"""


async def classify(text: str, fetcher) -> tuple[Stock, float | None, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or os.environ.get("PS5WATCH_LLM_FALLBACK") != "1":
        return Stock.UNKNOWN, None, "llm-disabled"
    try:
        r = await fetcher.client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 200,
                  "messages": [{"role": "user", "content": PROMPT + text[:12000]}]},
            timeout=30,
        )
        body = r.json()
        raw = "".join(b.get("text", "") for b in body.get("content", []))
        start, end = raw.find("{"), raw.rfind("}")
        d = json.loads(raw[start:end + 1])
        st = {"IN_STOCK": Stock.IN, "OUT_OF_STOCK": Stock.OUT}.get(d.get("status"), Stock.UNKNOWN)
        return st, d.get("price"), f"llm:{d.get('why','')[:40]}"
    except Exception as e:
        return Stock.UNKNOWN, None, f"llm-error:{type(e).__name__}"
