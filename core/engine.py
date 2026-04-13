"""Hedged Grid Engine — the core decision loop.

Reverse-engineered from live bot screenshots on XAUUSDs (2026-04-13).

HOW IT WORKS
─────────────
1.  Open a BUY 0.01 + SELL 0.01 simultaneously (the initial hedge pair).
2.  Wait for price to move by grid_step in either direction.
3.  When price DROPS by grid_step from the last BUY entry:
      → Open a new BUY with the NEXT lot in the 1.5× sequence (recovery)
      → Open a new SELL 0.01 at the same price (hedge)
4.  When price RISES by grid_step from the last SELL entry:
      → Open a new SELL with the NEXT lot in the 1.5× sequence (recovery)
      → Open a new BUY 0.01 at the same price (hedge)
5.  On every tick, compute the net P/L of ALL open positions.
6.  If net P/L >= basket_tp → close EVERYTHING → profit.
7.  Repeat.

The RECOVERY direction is determined dynamically:
    - Price moves down → the BUY side becomes the recovery side (bigger lots)
    - Price moves up   → the SELL side becomes the recovery side (bigger lots)
    - The opposite side stays at base lot (hedge)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from broker.base_broker import BaseBroker, BUY, SELL
from core.basket import Basket, LivePosition
from core.lot_sequence import build_lot_sequence

log = logging.getLogger("hedged_grid.engine")


class HedgedGridEngine:
    """Manages one symbol's hedged-grid lifecycle."""

    def __init__(self, symbol: str, broker: BaseBroker, cfg: dict, magic: int):
        self.symbol = symbol
        self.broker = broker
        self.magic = magic

        # Configuration
        self.base_lot: float = cfg.get("base_lot", 0.01)
        self.max_levels: int = cfg.get("max_levels", 9)
        self.grid_step: float = cfg.get("grid_step", 5.0)
        self.basket_tp: float = cfg.get("basket_tp", 15.0)
        self.check_interval: float = cfg.get("check_interval_sec", 0.5)
        self.session_start: int = cfg.get("session_start_hour", 0)
        self.session_end: int = cfg.get("session_end_hour", 24)

        # Pre-compute lot sequence (exact match to observed bot)
        custom_mults = cfg.get("lot_multipliers", None)
        self.lot_seq = build_lot_sequence(
            self.base_lot, self.max_levels, custom_mults,
        )
        log.info("%s lot sequence: %s", symbol, self.lot_seq)

        # State
        self.basket: Optional[Basket] = None
        self._last_buy_price: float = 0.0
        self._last_sell_price: float = 0.0
        self._recovery_dir: Optional[int] = None  # BUY or SELL
        self._level_index: int = 0
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Blocking main loop — call from a thread if running multiple symbols."""
        self._running = True
        log.info("%s engine started | step=%.2f | tp=%.2f | levels=%d",
                 self.symbol, self.grid_step, self.basket_tp, self.max_levels)

        while self._running:
            try:
                self._tick()
            except Exception:
                log.exception("%s tick error", self.symbol)
            time.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        if self.basket and self.basket.is_active:
            log.info("%s shutting down — closing basket", self.symbol)
            self._close_basket("BOT_STOP")

    # ── Main tick ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        tick = self.broker.get_tick(self.symbol)
        if tick is None:
            return

        bid, ask = tick["bid"], tick["ask"]

        # Session filter
        now = datetime.now(timezone.utc)
        if not (self.session_start <= now.hour < self.session_end):
            if self.basket and self.basket.is_active:
                pass  # keep managing open basket outside session
            else:
                return

        # ── No basket → open initial hedge pair ──────────────────────────
        if self.basket is None or not self.basket.is_active:
            if self.session_start <= now.hour < self.session_end:
                self._open_initial_pair(bid, ask)
            return

        # ── Basket active → check P/L then check grid levels ─────────────
        net_pnl = self.basket.net_profit(self.broker)

        # EXIT: net P/L >= target → close everything
        if net_pnl >= self.basket_tp:
            self._close_basket("TAKE_PROFIT")
            return

        # CHECK: should we open the next grid level?
        if self._level_index < self.max_levels - 1:
            self._check_next_level(bid, ask)

    # ── Initial pair ──────────────────────────────────────────────────────

    def _open_initial_pair(self, bid: float, ask: float) -> None:
        """Open the first BUY + SELL at base lot."""
        self.basket = Basket(self.symbol, self.magic, self.basket_tp)
        self._level_index = 0
        self._recovery_dir = None

        # BUY at ask
        buy_res = self.broker.open_position(
            self.symbol, BUY, self.base_lot,
            magic=self.magic, comment="HG_L0_BUY",
        )
        if not buy_res.success:
            log.warning("%s initial BUY failed: %s", self.symbol, buy_res.message)
            self.basket = None
            return

        buy_price = buy_res.price if buy_res.price > 0 else ask
        self.basket.add_position(LivePosition(
            ticket=buy_res.ticket, direction=BUY, volume=self.base_lot,
            entry_price=buy_price, level=0, side="initial",
        ))
        self._last_buy_price = buy_price

        # SELL at bid
        sell_res = self.broker.open_position(
            self.symbol, SELL, self.base_lot,
            magic=self.magic, comment="HG_L0_SELL",
        )
        if not sell_res.success:
            log.warning("%s initial SELL failed: %s — closing BUY",
                        self.symbol, sell_res.message)
            self.broker.close_position(buy_res.ticket)
            self.basket = None
            return

        sell_price = sell_res.price if sell_res.price > 0 else bid
        self.basket.add_position(LivePosition(
            ticket=sell_res.ticket, direction=SELL, volume=self.base_lot,
            entry_price=sell_price, level=0, side="initial",
        ))
        self._last_sell_price = sell_price

        log.info("%s CYCLE OPENED | BUY %.2f @ %.2f | SELL %.2f @ %.2f",
                 self.symbol, self.base_lot, buy_price,
                 self.base_lot, sell_price)

    # ── Grid level check ──────────────────────────────────────────────────

    def _check_next_level(self, bid: float, ask: float) -> None:
        """Determine if price moved enough to trigger the next grid level.

        Once recovery direction is locked, only the recovery-side trigger
        can add new levels. This prevents wasting levels on the wrong side
        and matches the observed bot behavior (one side always has big lots).
        """
        next_idx = self._level_index + 1
        step = self.grid_step

        buy_trigger = self._last_buy_price - step
        sell_trigger = self._last_sell_price + step

        if self._recovery_dir is None:
            # First level after initial pair — either side can trigger
            if bid <= buy_trigger and ask >= sell_trigger:
                if (self._last_buy_price - bid) >= (ask - self._last_sell_price):
                    self._add_level_buy_recovery(next_idx, bid, ask)
                else:
                    self._add_level_sell_recovery(next_idx, bid, ask)
            elif bid <= buy_trigger:
                self._add_level_buy_recovery(next_idx, bid, ask)
            elif ask >= sell_trigger:
                self._add_level_sell_recovery(next_idx, bid, ask)

        elif self._recovery_dir == BUY:
            # BUY is recovery → only add when price drops further
            if bid <= buy_trigger:
                self._add_level_buy_recovery(next_idx, bid, ask)

        elif self._recovery_dir == SELL:
            # SELL is recovery → only add when price rises further
            if ask >= sell_trigger:
                self._add_level_sell_recovery(next_idx, bid, ask)

    def _add_level_buy_recovery(self, idx: int, bid: float, ask: float) -> None:
        """Price dropped → add BUY (recovery, bigger lot) + SELL (hedge, base lot)."""
        if self._recovery_dir is None:
            self._recovery_dir = BUY
            self.basket.recovery_direction = BUY

        recovery_lot = self.lot_seq[idx] if self._recovery_dir == BUY else self.base_lot
        hedge_lot = self.base_lot

        # Recovery BUY
        buy_res = self.broker.open_position(
            self.symbol, BUY, recovery_lot,
            magic=self.magic, comment=f"HG_L{idx}_BUY",
        )
        if not buy_res.success:
            log.warning("%s level %d BUY failed: %s", self.symbol, idx, buy_res.message)
            return

        bp = buy_res.price if buy_res.price > 0 else ask
        self.basket.add_position(LivePosition(
            ticket=buy_res.ticket, direction=BUY, volume=recovery_lot,
            entry_price=bp, level=idx,
            side="recovery" if self._recovery_dir == BUY else "hedge",
        ))
        self._last_buy_price = bp

        # Hedge SELL
        sell_res = self.broker.open_position(
            self.symbol, SELL, hedge_lot,
            magic=self.magic, comment=f"HG_L{idx}_SELL",
        )
        if sell_res.success:
            sp = sell_res.price if sell_res.price > 0 else bid
            self.basket.add_position(LivePosition(
                ticket=sell_res.ticket, direction=SELL, volume=hedge_lot,
                entry_price=sp, level=idx,
                side="hedge" if self._recovery_dir == BUY else "recovery",
            ))
            self._last_sell_price = sp

        self._level_index = idx
        log.info("%s LEVEL %d (BUY recovery) | BUY %.2f @ %.2f | SELL %.2f @ %.2f",
                 self.symbol, idx, recovery_lot, bp, hedge_lot,
                 sell_res.price if sell_res.success else 0)

    def _add_level_sell_recovery(self, idx: int, bid: float, ask: float) -> None:
        """Price rose → add SELL (recovery, bigger lot) + BUY (hedge, base lot)."""
        if self._recovery_dir is None:
            self._recovery_dir = SELL
            self.basket.recovery_direction = SELL

        recovery_lot = self.lot_seq[idx] if self._recovery_dir == SELL else self.base_lot
        hedge_lot = self.base_lot

        # Recovery SELL
        sell_res = self.broker.open_position(
            self.symbol, SELL, recovery_lot,
            magic=self.magic, comment=f"HG_L{idx}_SELL",
        )
        if not sell_res.success:
            log.warning("%s level %d SELL failed: %s", self.symbol, idx, sell_res.message)
            return

        sp = sell_res.price if sell_res.price > 0 else bid
        self.basket.add_position(LivePosition(
            ticket=sell_res.ticket, direction=SELL, volume=recovery_lot,
            entry_price=sp, level=idx,
            side="recovery" if self._recovery_dir == SELL else "hedge",
        ))
        self._last_sell_price = sp

        # Hedge BUY
        buy_res = self.broker.open_position(
            self.symbol, BUY, hedge_lot,
            magic=self.magic, comment=f"HG_L{idx}_BUY",
        )
        if buy_res.success:
            bp = buy_res.price if buy_res.price > 0 else ask
            self.basket.add_position(LivePosition(
                ticket=buy_res.ticket, direction=BUY, volume=hedge_lot,
                entry_price=bp, level=idx,
                side="hedge" if self._recovery_dir == SELL else "recovery",
            ))
            self._last_buy_price = bp

        self._level_index = idx
        log.info("%s LEVEL %d (SELL recovery) | SELL %.2f @ %.2f | BUY %.2f @ %.2f",
                 self.symbol, idx, recovery_lot, sp, hedge_lot,
                 buy_res.price if buy_res.success else 0)

    # ── Close basket ──────────────────────────────────────────────────────

    def _close_basket(self, reason: str) -> float:
        """Close all positions in the basket. Returns net P/L."""
        if self.basket is None:
            return 0.0

        net = self.basket.net_profit(self.broker)
        tickets = self.basket.all_tickets()

        closed = 0
        for ticket in tickets:
            res = self.broker.close_position(ticket)
            if res.success:
                closed += 1
            else:
                log.warning("%s failed to close ticket %d: %s",
                            self.symbol, ticket, res.message)

        log.info("%s BASKET CLOSED [%s] | %d/%d positions | net P/L=%.2f | level=%d",
                 self.symbol, reason, closed, len(tickets), net,
                 self._level_index)

        self.basket.closed = True
        self.basket = None
        self._recovery_dir = None
        self._level_index = 0

        return net

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        if self.basket and self.basket.is_active:
            return self.basket.summary(self.broker)
        return {"symbol": self.symbol, "status": "idle"}
