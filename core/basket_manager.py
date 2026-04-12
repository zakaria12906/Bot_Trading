"""Basket manager — tracks the lifecycle of one recovery basket.

A basket is a collection of same-direction, fixed-lot positions opened by
the grid.  The basket exits as a unit according to the exit priority hierarchy:

    1. Emergency equity stop      (handled by RiskManager)
    2. Regime-failure liquidation  (handled by RiskManager)
    3. Time-expiry exit
    4. Basket take-profit
    5. Soft scratch exit           (close near breakeven after regime deterioration)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from broker.base_broker import BaseBroker, Position
from core.grid_manager import GridPlan

log = logging.getLogger("grid_bot.basket")

BUY = 0
SELL = 1


@dataclass
class BasketState:
    symbol: str
    magic: int
    direction: int
    plan: GridPlan
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    close_reason: str = ""
    realized_pnl: float = 0.0


class BasketManager:

    def __init__(self, broker: BaseBroker, sym_cfg: dict, magic: int):
        self.broker = broker
        self.cfg = sym_cfg
        self.magic = magic
        self.active: Optional[BasketState] = None

    @property
    def has_active_basket(self) -> bool:
        return self.active is not None and not self.active.closed

    def open_basket(self, symbol: str, direction: int, plan: GridPlan) -> BasketState:
        self.active = BasketState(
            symbol=symbol,
            magic=self.magic,
            direction=direction,
            plan=plan,
        )
        log.info(
            "%s basket opened: dir=%s levels=%d",
            symbol, "BUY" if direction == BUY else "SELL", len(plan.levels),
        )
        return self.active

    # ------------------------------------------------------------------
    # Basket P/L queries
    # ------------------------------------------------------------------
    def basket_positions(self) -> List[Position]:
        if not self.active:
            return []
        return self.broker.get_positions(self.active.symbol, self.magic)

    def basket_net_pnl(self) -> float:
        return sum(p.profit + p.swap for p in self.basket_positions())

    def basket_trade_count(self) -> int:
        return len(self.basket_positions())

    # ------------------------------------------------------------------
    # Exit checks (priorities 3-5)
    # ------------------------------------------------------------------
    def check_take_profit(self) -> bool:
        """Priority 4 — net basket P/L reached target."""
        if not self.active:
            return False
        tp = self.cfg.get("basket_tp_currency", 5.0)
        pnl = self.basket_net_pnl()
        if pnl >= tp:
            log.info(
                "%s basket TP reached: P/L %.2f >= %.2f",
                self.active.symbol, pnl, tp,
            )
            return True
        return False

    def check_time_stop(self) -> bool:
        """Priority 3 — basket has exceeded its maximum lifespan."""
        if not self.active:
            return False
        max_min = self.cfg.get("time_stop_minutes", 480)
        elapsed = (datetime.now(timezone.utc) - self.active.opened_at).total_seconds() / 60
        if elapsed >= max_min:
            log.warning(
                "%s basket time stop: %.0f min >= %d min",
                self.active.symbol, elapsed, max_min,
            )
            return True
        return False

    def check_scratch_exit(self, regime_deteriorated: bool) -> bool:
        """Priority 5 — close near breakeven if regime has deteriorated."""
        if not self.active or not regime_deteriorated:
            return False
        pnl = self.basket_net_pnl()
        # Accept a small loss (≤ 20% of TP target) as a scratch
        tp = self.cfg.get("basket_tp_currency", 5.0)
        threshold = -(tp * 0.20)
        if pnl >= threshold:
            log.info(
                "%s scratch exit: P/L %.2f (regime deteriorated)",
                self.active.symbol, pnl,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Close all positions
    # ------------------------------------------------------------------
    def close_basket(self, reason: str) -> float:
        """Close every open position in the basket. Returns realized P/L."""
        if not self.active:
            return 0.0

        positions = self.basket_positions()
        total_pnl = 0.0
        for pos in positions:
            total_pnl += pos.profit + pos.swap
            result = self.broker.close_position(pos.ticket)
            if not result.success:
                log.error(
                    "Failed to close ticket %d: %s", pos.ticket, result.message,
                )

        self.active.closed = True
        self.active.close_reason = reason
        self.active.realized_pnl = total_pnl

        log.info(
            "%s basket closed [%s]: %d trades, P/L %.2f",
            self.active.symbol, reason, len(positions), total_pnl,
        )
        return total_pnl

    def clear(self) -> None:
        """Reset after a basket is fully closed."""
        self.active = None
