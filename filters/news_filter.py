"""News filter — blocks new baskets around high-impact economic events.

Uses the free Forex Factory JSON calendar by default.  A background scheduler
refreshes the calendar periodically.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

log = logging.getLogger("grid_bot.news")

# Maps currency → list of symbols affected
_CURRENCY_SYMBOLS: Dict[str, List[str]] = {
    "USD": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US30", "US100", "USDCHF", "USDCAD"],
    "EUR": ["EURUSD", "EURGBP", "EURJPY", "EURCHF"],
    "GBP": ["GBPUSD", "EURGBP", "GBPJPY"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY"],
    "AUD": ["AUDUSD", "AUDJPY"],
    "NZD": ["NZDUSD"],
    "CAD": ["USDCAD"],
    "CHF": ["USDCHF", "EURCHF"],
}


class NewsEvent:
    def __init__(self, title: str, currency: str, impact: str, dt: datetime):
        self.title = title
        self.currency = currency
        self.impact = impact
        self.dt = dt

    def __repr__(self) -> str:
        return f"<News {self.dt:%H:%M} {self.currency} {self.impact} {self.title}>"


class NewsFilter:

    def __init__(self, news_cfg: dict):
        self.enabled = news_cfg.get("enabled", True)
        self.url = news_cfg.get(
            "calendar_url",
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        )
        self.impact_levels = set(news_cfg.get("impact_levels", ["High"]))
        self.refresh_sec = news_cfg.get("refresh_interval_sec", 3600)
        self._events: List[NewsEvent] = []
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """Fetch the latest calendar.  Safe to call from a scheduler thread."""
        if not self.enabled:
            return
        try:
            resp = requests.get(self.url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("News calendar fetch failed: %s", exc)
            return

        events: List[NewsEvent] = []
        for item in data:
            impact = item.get("impact", "")
            if impact not in self.impact_levels:
                continue
            try:
                dt = datetime.strptime(item["date"], "%Y-%m-%dT%H:%M:%S%z")
            except (KeyError, ValueError):
                continue
            events.append(NewsEvent(
                title=item.get("title", ""),
                currency=item.get("country", ""),
                impact=impact,
                dt=dt,
            ))

        with self._lock:
            self._events = events
        log.info("News calendar refreshed: %d high-impact events loaded", len(events))

    def is_blocked(self, symbol: str, lockout_minutes: int, now: Optional[datetime] = None) -> bool:
        """Return True if a high-impact event is within *lockout_minutes* of *now*
        and it affects the given symbol."""
        if not self.enabled:
            return False
        now = now or datetime.now(timezone.utc)
        window = timedelta(minutes=lockout_minutes)

        with self._lock:
            for ev in self._events:
                affected = _CURRENCY_SYMBOLS.get(ev.currency, [])
                if symbol not in affected:
                    continue
                delta = ev.dt - now
                if -window <= delta <= window:
                    log.info(
                        "%s blocked by news: %s (%s) in %s",
                        symbol, ev.title, ev.currency, delta,
                    )
                    return True
        return False
