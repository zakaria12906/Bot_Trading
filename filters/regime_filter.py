"""Regime filter — classifies the market into CALM, CAUTION, or HOSTILE.

A grid should only deploy in CALM.  CAUTION triggers defensive mode.
HOSTILE triggers shutdown.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List

from broker.base_broker import Bar, BaseBroker
from indicators.atr import atr, atr_series
from indicators.adx import adx
from indicators.moving_average import distance_from_ma
from indicators.candle_analysis import bars_show_overlap
from utils.helpers import tf_to_mt5

log = logging.getLogger("grid_bot.regime")


class Regime(Enum):
    CALM = "calm"
    CAUTION = "caution"
    HOSTILE = "hostile"


class RegimeFilter:

    def __init__(self, broker: BaseBroker, sym_cfg: dict):
        self.broker = broker
        self.cfg = sym_cfg

    def evaluate(self, symbol: str) -> Regime:
        """Run all sub-checks and return the worst regime among them."""
        regimes = [
            self._check_volatility(symbol),
            self._check_trend_strength(symbol),
            self._check_structure(symbol),
            self._check_distance(symbol),
        ]
        if Regime.HOSTILE in regimes:
            return Regime.HOSTILE
        if Regime.CAUTION in regimes:
            return Regime.CAUTION
        return Regime.CALM

    # ---- sub-checks ----

    def _check_volatility(self, symbol: str) -> Regime:
        """Current ATR vs baseline ATR ratio."""
        tf = tf_to_mt5(self.cfg.get("atr_timeframe", "M15"))
        period = self.cfg.get("atr_period", 14)
        bars = self.broker.get_bars(symbol, tf, period * 6)
        if len(bars) < period * 3:
            return Regime.CAUTION

        series = atr_series(bars, period)
        if not series:
            return Regime.CAUTION

        current = series[-1]
        baseline = sum(series) / len(series)
        if baseline == 0:
            return Regime.CAUTION

        ratio = current / baseline
        shutdown_ratio = self.cfg.get("shutdown_atr_ratio", 2.2)
        defensive_ratio = self.cfg.get("max_atr_ratio", 1.8)

        if ratio >= shutdown_ratio:
            log.warning("%s ATR ratio %.2f → HOSTILE", symbol, ratio)
            return Regime.HOSTILE
        if ratio >= defensive_ratio:
            log.info("%s ATR ratio %.2f → CAUTION", symbol, ratio)
            return Regime.CAUTION
        return Regime.CALM

    def _check_trend_strength(self, symbol: str) -> Regime:
        """ADX-based trend strength."""
        tf = tf_to_mt5(self.cfg.get("entry_tf", "M15"))
        period = self.cfg.get("adx_period", 14)
        bars = self.broker.get_bars(symbol, tf, period * 4)

        adx_val = adx(bars, period)
        shutdown_adx = self.cfg.get("shutdown_adx", 40)
        ceiling = self.cfg.get("adx_ceiling", 30)

        if adx_val >= shutdown_adx:
            log.warning("%s ADX %.1f → HOSTILE", symbol, adx_val)
            return Regime.HOSTILE
        if adx_val >= ceiling:
            log.info("%s ADX %.1f → CAUTION", symbol, adx_val)
            return Regime.CAUTION
        return Regime.CALM

    def _check_structure(self, symbol: str) -> Regime:
        """Require candle overlap (range-bound behaviour)."""
        tf = tf_to_mt5(self.cfg.get("entry_tf", "M15"))
        bars = self.broker.get_bars(symbol, tf, 10)
        if not bars_show_overlap(bars):
            log.info("%s no candle overlap → CAUTION", symbol)
            return Regime.CAUTION
        return Regime.CALM

    def _check_distance(self, symbol: str) -> Regime:
        """Price stretched too far from higher-TF MA."""
        htf = tf_to_mt5(self.cfg.get("higher_tf", "H1"))
        period = self.cfg.get("higher_ma_period", 200)
        atf = tf_to_mt5(self.cfg.get("atr_timeframe", "M15"))
        atr_period = self.cfg.get("atr_period", 14)

        bars_htf = self.broker.get_bars(symbol, htf, period + 10)
        bars_atr = self.broker.get_bars(symbol, atf, atr_period * 3)

        atr_val = atr(bars_atr, atr_period) if bars_atr else 0.0
        dist = distance_from_ma(bars_htf, period, atr_val) if bars_htf else 0.0
        threshold = self.cfg.get("distance_from_mean_max", 2.5)

        if abs(dist) > threshold * 1.5:
            log.warning("%s distance %.2f ATR → HOSTILE", symbol, dist)
            return Regime.HOSTILE
        if abs(dist) > threshold:
            log.info("%s distance %.2f ATR → CAUTION", symbol, dist)
            return Regime.CAUTION
        return Regime.CALM
