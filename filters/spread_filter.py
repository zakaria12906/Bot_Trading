"""Spread filter — blocks trading when bid-ask spread is abnormally wide."""

from __future__ import annotations

import logging
from broker.base_broker import BaseBroker

log = logging.getLogger("grid_bot.spread")


class SpreadFilter:

    def __init__(self, broker: BaseBroker, sym_cfg: dict, risk_cfg: dict):
        self.broker = broker
        self.sym_cfg = sym_cfg
        self.risk_cfg = risk_cfg
        self._baseline_spread: float | None = None

    def update_baseline(self, symbol: str) -> None:
        """Snapshot the current spread as the session baseline (call once at
        session open when conditions are normal)."""
        info = self.broker.get_symbol_info(symbol)
        if info:
            self._baseline_spread = float(info.spread)

    def is_acceptable(self, symbol: str) -> bool:
        """Return True if the spread is below the hard max for this symbol."""
        info = self.broker.get_symbol_info(symbol)
        if info is None:
            return False
        max_spread = self.sym_cfg.get("max_spread_points", 30)
        ok = info.spread <= max_spread
        if not ok:
            log.info(
                "%s spread %d > max %d → blocked", symbol, info.spread, max_spread,
            )
        return ok

    def is_defensive(self, symbol: str) -> bool:
        """Return True if spread has widened enough to trigger defensive mode."""
        if self._baseline_spread is None or self._baseline_spread == 0:
            return False
        info = self.broker.get_symbol_info(symbol)
        if info is None:
            return True
        mult = self.risk_cfg.get("spread_defensive_mult", 2.0)
        return info.spread > self._baseline_spread * mult

    def is_shutdown(self, symbol: str) -> bool:
        """Return True if spread explosion warrants emergency shutdown."""
        if self._baseline_spread is None or self._baseline_spread == 0:
            return False
        info = self.broker.get_symbol_info(symbol)
        if info is None:
            return True
        mult = self.risk_cfg.get("spread_shutdown_mult", 4.0)
        return info.spread > self._baseline_spread * mult
