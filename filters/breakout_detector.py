"""Breakout / trend-failure detector.

Combines multiple signals to decide whether recent price action looks like
a genuine directional expansion rather than a tradable retracement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from broker.base_broker import Bar, BaseBroker
from indicators.atr import atr
from indicators.adx import adx
from indicators.moving_average import sma, distance_from_ma
from indicators.candle_analysis import consecutive_impulse_candles
from utils.helpers import tf_to_mt5

log = logging.getLogger("grid_bot.breakout")


@dataclass
class BreakoutSignals:
    impulse_count: int = 0
    adx_value: float = 0.0
    distance_from_mean: float = 0.0
    retest_failed: bool = False
    time_under_water: bool = False
    score: int = 0          # how many independent signals are firing


class BreakoutDetector:

    def __init__(self, broker: BaseBroker, sym_cfg: dict, risk_cfg: dict):
        self.broker = broker
        self.sym_cfg = sym_cfg
        self.risk_cfg = risk_cfg
        self._minutes_under_water: float = 0.0

    def set_time_under_water(self, minutes: float) -> None:
        """Injected by the engine each tick so the detector can score it."""
        self._minutes_under_water = minutes

    def evaluate(self, symbol: str) -> BreakoutSignals:
        tf = tf_to_mt5(self.sym_cfg.get("entry_tf", "M15"))
        atr_period = self.sym_cfg.get("atr_period", 14)
        adx_period = self.sym_cfg.get("adx_period", 14)
        short_ma = self.sym_cfg.get("short_ma_period", 20)

        bars = self.broker.get_bars(symbol, tf, max(atr_period, adx_period, short_ma) * 4)
        if len(bars) < atr_period * 2:
            return BreakoutSignals()

        atr_val = atr(bars, atr_period)
        adx_val = adx(bars, adx_period)

        impulse_count = consecutive_impulse_candles(
            bars,
            atr_val,
            self.risk_cfg.get("breakout_range_mult", 1.8),
        )

        dist = distance_from_ma(bars, short_ma, atr_val)

        # Retest failure: price pulled back toward the short MA but the last
        # bar closed back in the breakout direction without reclaiming it.
        ma_val = sma(bars, short_ma)
        retest_failed = False
        if len(bars) >= 3 and ma_val > 0 and atr_val > 0:
            prev_close = bars[-2].close
            curr_close = bars[-1].close
            approached = abs(prev_close - ma_val) < atr_val * 0.5
            rejected = abs(curr_close - ma_val) > atr_val * 0.8
            retest_failed = approached and rejected

        # Time-under-water: basket stuck in loss beyond half the time stop
        time_stop = self.sym_cfg.get("time_stop_minutes", 480)
        tuw = self._minutes_under_water > time_stop * 0.5

        score = 0
        if impulse_count >= self.risk_cfg.get("breakout_candle_count", 3):
            score += 1
        if adx_val >= self.sym_cfg.get("adx_ceiling", 30):
            score += 1
        if abs(dist) > self.sym_cfg.get("distance_from_mean_max", 2.5):
            score += 1
        if retest_failed:
            score += 1
        if tuw:
            score += 1

        sig = BreakoutSignals(
            impulse_count=impulse_count,
            adx_value=adx_val,
            distance_from_mean=dist,
            retest_failed=retest_failed,
            time_under_water=tuw,
            score=score,
        )

        if score >= 2:
            log.warning(
                "%s breakout detected (score=%d): impulse=%d ADX=%.1f dist=%.2f retest=%s",
                symbol, score, impulse_count, adx_val, dist, retest_failed,
            )
        return sig
