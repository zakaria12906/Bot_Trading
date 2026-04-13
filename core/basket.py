"""Basket — tracks all open positions of a single hedged-grid cycle.

A basket contains TWO sides:
    - RECOVERY side: positions with increasing lots (the 1.5× sequence)
    - HEDGE side: positions with base lot (0.01)

Every grid level produces one RECOVERY position and one HEDGE position.
The basket closes ALL positions at once when net P/L >= target.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from broker.base_broker import BUY, SELL

log = logging.getLogger("hedged_grid.basket")


@dataclass
class LivePosition:
    ticket: int
    direction: int          # BUY or SELL
    volume: float
    entry_price: float
    level: int              # grid level index
    side: str               # "recovery" or "hedge"
    open_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Basket:
    """Manages all positions of one hedged-grid cycle."""

    def __init__(self, symbol: str, magic: int, basket_tp: float):
        self.symbol = symbol
        self.magic = magic
        self.basket_tp = basket_tp

        self.positions: Dict[int, LivePosition] = {}   # ticket → LivePosition
        self.recovery_direction: Optional[int] = None   # which side is scaling up
        self.current_level: int = 0
        self.created_at: datetime = datetime.now(timezone.utc)
        self.closed: bool = False

    @property
    def is_active(self) -> bool:
        return not self.closed and len(self.positions) > 0

    @property
    def trade_count(self) -> int:
        return len(self.positions)

    def add_position(self, pos: LivePosition) -> None:
        self.positions[pos.ticket] = pos
        self.current_level = max(self.current_level, pos.level)

    def remove_position(self, ticket: int) -> Optional[LivePosition]:
        return self.positions.pop(ticket, None)

    def net_profit(self, broker) -> float:
        """Get real-time net P/L from the broker for all positions."""
        total = 0.0
        live = broker.get_positions(self.symbol, self.magic)
        live_map = {p.ticket: p.profit + p.swap for p in live}
        for ticket in self.positions:
            total += live_map.get(ticket, 0.0)
        return total

    def should_close(self, broker) -> bool:
        """True if net P/L of the basket >= target profit."""
        return self.net_profit(broker) >= self.basket_tp

    def all_tickets(self) -> List[int]:
        return list(self.positions.keys())

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    def summary(self, broker) -> dict:
        recovery_lots = sum(
            p.volume for p in self.positions.values() if p.side == "recovery"
        )
        hedge_lots = sum(
            p.volume for p in self.positions.values() if p.side == "hedge"
        )
        return {
            "symbol": self.symbol,
            "level": self.current_level,
            "positions": self.trade_count,
            "recovery_lots": round(recovery_lots, 2),
            "hedge_lots": round(hedge_lots, 2),
            "net_pnl": round(self.net_profit(broker), 2),
            "target": self.basket_tp,
            "age_sec": int(self.age_seconds()),
        }
