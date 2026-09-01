"""Core data types shared by every adapter."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from enum import Enum


class Stock(str, Enum):
    IN = "IN_STOCK"
    OUT = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"      # page fetched but availability not parseable
    ABSENT = "NOT_LISTED"    # retailer simply doesn't carry/list a PS5 Pro
    BLOCKED = "BLOCKED"      # bot wall / captcha / 403
    ERROR = "ERROR"          # network or parse blew up

    @property
    def emoji(self) -> str:
        return {
            "IN_STOCK": "\U0001F7E2",
            "OUT_OF_STOCK": "\U000026AA",
            "UNKNOWN": "\U0001F7E1",
            "NOT_LISTED": "\U000026AB",
            "BLOCKED": "\U0001F6AB",
            "ERROR": "\U0001F534",
        }[self.value]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Target:
    """One product at one retailer."""
    retailer: str
    country: str                 # "CA" | "US"
    adapter: str
    url: str
    sku: str = ""
    title: str = "PS5 Pro"
    api: str = ""                # optional URL template for generic_json
    paths: dict = field(default_factory=dict)
    in_words: list = field(default_factory=list)
    out_words: list = field(default_factory=list)
    enabled: bool = True
    needs_browser: bool = False

    @property
    def key(self) -> str:
        return f"{self.country}:{self.retailer}:{self.sku or self.url}"


@dataclass
class Result:
    target: Target
    status: Stock
    price: float | None = None
    currency: str | None = None
    note: str = ""
    checked_at: str = field(default_factory=_now)
    elapsed_ms: int = 0

    @property
    def is_actionable(self) -> bool:
        return self.status is Stock.IN

    def to_json(self) -> dict:
        d = asdict(self)
        d["target"] = self.target.key
        d["retailer"] = self.target.retailer
        d["country"] = self.target.country
        d["url"] = self.target.url
        d["status"] = self.status.value
        return d
