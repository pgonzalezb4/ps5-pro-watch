"""Transition detection + alert cooldown, persisted as JSON."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .models import Result, Stock

STATE_PATH = Path(os.environ.get(
    "PS5WATCH_STATE",
    Path.home() / ".local/state/ps5watch/state.json"))


def load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "history": []}


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = state.get("history", [])[-500:]
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def diff(results: list[Result], state: dict, cooldown_min: int = 45) -> list[Result]:
    """Return results worth an INSTANT alert.

    Fires on OUT/UNKNOWN/BLOCKED -> IN transitions, and re-fires only after the
    cooldown so a long restock doesn't spam you every scan.
    """
    now = dt.datetime.now(dt.timezone.utc)
    seen = state.setdefault("seen", {})
    alerts = []
    for res in results:
        k = res.target.key
        prev = seen.get(k, {})
        prev_status = prev.get("status")
        if res.status is Stock.IN:
            last = prev.get("last_alert")
            stale = True
            if last:
                try:
                    stale = (now - dt.datetime.fromisoformat(last)).total_seconds() > cooldown_min * 60
                except ValueError:
                    pass
            if prev_status != Stock.IN.value or stale:
                alerts.append(res)
                prev["last_alert"] = now.isoformat(timespec="seconds")
        if prev_status != res.status.value:
            state.setdefault("history", []).append({
                "t": now.isoformat(timespec="seconds"), "key": k,
                "from": prev_status, "to": res.status.value,
                "retailer": res.target.retailer, "price": res.price})
        prev.update(status=res.status.value, price=res.price,
                    checked_at=res.checked_at, retailer=res.target.retailer,
                    url=res.target.url, note=res.note)
        seen[k] = prev
    return alerts
