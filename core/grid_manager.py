"""Grid construction — computes adaptive step sizes and grid levels.

The grid step is ATR-normalized, never falls below a configured floor, and
widens when volatility expands mid-cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from filters.volatility_filter import VolatilityFilter

log = logging.getLogger("grid_bot.grid")


@dataclass
class GridLevel:
    """Represents one planned or filled level in the grid."""
    index: int
    target_price: float
    filled: bool = False
    ticket: int = 0


@dataclass
class GridPlan:
    direction: int          # 0 = BUY grid (expecting price to retrace up), 1 = SELL grid
    lot_size: float
    step_points: float      # step in price terms (not raw points)
    levels: List[GridLevel] = field(default_factory=list)
    anchor_price: float = 0.0


class GridManager:

    def __init__(self, sym_cfg: dict, vol_filter: VolatilityFilter):
        self.cfg = sym_cfg
        self.vol = vol_filter

    def compute_step(self, symbol: str, point: float) -> float:
        """Return the adaptive grid step in *price* terms.

        step = max(min_step_points * point, ATR * step_multiplier)
        """
        atr_val = self.vol.current_atr(symbol)
        multiplier = self.cfg.get("grid_step_multiplier", 1.5)
        min_step = self.cfg.get("min_step_points", 100) * point

        step = max(min_step, atr_val * multiplier)
        log.debug(
            "%s grid step: ATR=%.5f mult=%.1f min=%.5f → step=%.5f",
            symbol, atr_val, multiplier, min_step, step,
        )
        return step

    def build_plan(
        self,
        symbol: str,
        direction: int,
        anchor_price: float,
        point: float,
    ) -> GridPlan:
        """Create a fresh grid plan starting from *anchor_price*."""
        step = self.compute_step(symbol, point)
        lot = self.cfg.get("lot_size", 0.01)
        max_trades = self.cfg.get("max_trades", 6)

        levels: List[GridLevel] = []
        # Level 0 is the initial entry (already filled by the caller)
        for i in range(max_trades):
            if direction == 0:  # BUY grid → levels below anchor
                target = anchor_price - step * i
            else:               # SELL grid → levels above anchor
                target = anchor_price + step * i
            levels.append(GridLevel(index=i, target_price=target))

        plan = GridPlan(
            direction=direction,
            lot_size=lot,
            step_points=step,
            levels=levels,
            anchor_price=anchor_price,
        )
        log.info(
            "%s grid plan: dir=%s lot=%.2f step=%.5f levels=%d anchor=%.5f",
            symbol,
            "BUY" if direction == 0 else "SELL",
            lot, step, len(levels), anchor_price,
        )
        return plan

    def next_unfilled_level(self, plan: GridPlan) -> GridLevel | None:
        for lv in plan.levels:
            if not lv.filled:
                return lv
        return None

    def recalculate_unfilled_levels(
        self, plan: GridPlan, symbol: str, point: float,
    ) -> None:
        """Re-space unfilled levels using the current ATR.

        Called before each add-on check so the grid widens when volatility
        expands mid-cycle (blueprint requirement).
        """
        new_step = self.compute_step(symbol, point)
        if new_step <= plan.step_points:
            return  # only widen, never tighten

        last_filled_price = plan.anchor_price
        for lv in plan.levels:
            if lv.filled:
                last_filled_price = lv.target_price
            else:
                break

        idx_offset = 1
        for lv in plan.levels:
            if lv.filled:
                continue
            if plan.direction == 0:
                lv.target_price = last_filled_price - new_step * idx_offset
            else:
                lv.target_price = last_filled_price + new_step * idx_offset
            idx_offset += 1

        plan.step_points = new_step
        log.info(
            "%s grid widened mid-cycle: new step=%.5f", symbol, new_step,
        )

    def should_fill_next(
        self, plan: GridPlan, current_price: float,
    ) -> GridLevel | None:
        """Return the next level if price has reached it, else None."""
        nxt = self.next_unfilled_level(plan)
        if nxt is None:
            return None

        if plan.direction == 0:  # BUY — price must drop to target
            if current_price <= nxt.target_price:
                return nxt
        else:                    # SELL — price must rise to target
            if current_price >= nxt.target_price:
                return nxt
        return None
