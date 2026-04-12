"""Volatility filter — blocks trading when ATR is abnormally elevated."""

from __future__ import annotations

import logging
from typing import List

from broker.base_broker import BaseBroker, Bar
from indicators.atr import atr, atr_series
from utils.helpers import tf_to_mt5

log = logging.getLogger("grid_bot.volatility")


class VolatilityFilter:

    def __init__(self, broker: BaseBroker, sym_cfg: dict):
        self.broker = broker
        self.cfg = sym_cfg

    def is_acceptable(self, symbol: str) -> bool:
        """Return True if current volatility is within the acceptable envelope."""
        tf = tf_to_mt5(self.cfg.get("atr_timeframe", "M15"))
        period = self.cfg.get("atr_period", 14)
        bars = self.broker.get_bars(symbol, tf, period * 6)
        if len(bars) < period * 2:
            log.warning("%s insufficient bars for volatility check", symbol)
            return False

        series = atr_series(bars, period)
        if not series:
            return False

        current = series[-1]
        baseline = sum(series) / len(series)
        if baseline == 0:
            return False

        ratio = current / baseline
        max_ratio = self.cfg.get("max_atr_ratio", 1.8)
        ok = ratio <= max_ratio
        if not ok:
            log.info(
                "%s volatility blocked: ATR ratio %.2f > %.2f",
                symbol, ratio, max_ratio,
            )
        return ok

    def current_atr(self, symbol: str) -> float:
        """Return the current ATR value for grid-step calculation."""
        tf = tf_to_mt5(self.cfg.get("atr_timeframe", "M15"))
        period = self.cfg.get("atr_period", 14)
        bars = self.broker.get_bars(symbol, tf, period * 3)
        return atr(bars, period)
